#!/bin/zsh
# Mutation battery for the legacy tear-out, phase 01 (the deletion of the legacy tier, #1216):
# each behaviour the phase pins has one named mutation that must make its test FAIL ("killed");
# the file is restored from the COMMITTED tree after each, so commit first. Run from the repo root
# with the sandbox off (units resolve DNS; the lane needs the Docker test PostgreSQL on 65433).
# `ONLY=<regex>` runs a subset; `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
GATE="env PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH PYTHONDONTWRITEBYTECODE=1 DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude

verdict() {  # name rc — reads /tmp/claude/mut.log
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  # A selector that matches no test exits non-zero too — that is not a kill. Nor is a kill by a
  # collection or fixture error a test's verdict; both are flagged for a human to read.
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}

check() {  # name file old new runner test-selector
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  OLD="$old" NEW="$new" $PY - "$file" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  # Two mutations of the SAME size written within the same second leave the interpreter a
  # bytecode cache it considers valid (mtime + size match) — purge it.
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

GONE=tests/src/test_legacy_tier_gone.py
MAIN=src/main.py
MODELS=src/models/__init__.py
BASE=scripts/telegram_ratchet_baseline.json
INV=tests/scripts/legacy_inventory.py
TINV=tests/scripts/test_legacy_inventory.py
WGATE=tests/src/test_worker_entrypoint.py
LANE=tests/scripts/test_lineage_lane.py
SCONF=tests/scripts/conftest.py
L3=tests/scripts/test_l3_permit_rail.py

# --- the predicate (the standing guard) ---------------------------------------------------
check "the AST scan walks into function bodies" $GONE '    for node in ast.walk(tree):
        if isinstance(node, ast.Import):' '    for node in tree.body:
        if isinstance(node, ast.Import):' "$UNIT" "$GONE -k every_forbidden_prefix_planted_at_function_depth_is_found"
check "a from-import of a deleted submodule counts" $GONE '            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}" if base else alias.name' '            for alias in node.names:
                pass' "$UNIT" "$GONE -k a_from_import_of_a_deleted_submodule_is_found"
check "the prefix arm catches a deeper import" $GONE '    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)' '    return any(name == p for p in FORBIDDEN_PREFIXES)' "$UNIT" "$GONE -k a_deeper_import_under_a_deleted_package_is_found"
check "a relative import is resolved against its package" $GONE '                anchor = package[: len(package) - (node.level - 1)]' '                anchor = []' "$UNIT" "$GONE -k a_relative_import_is_resolved_against_its_package"
check "a literal module name handed to the import machinery counts" $GONE '    if target not in ("import_module", "__import__"):
        return' '    if True:
        return' "$UNIT" "$GONE -k a_literal_module_name_handed_to_the_import_machinery_is_found"
check "the forbidden set names every deleted module" $GONE '    "src.services.core",
    "src.services.integrations",' '    "src.services.integrations",' "$UNIT" "$GONE -k the_forbidden_set_names_every_deleted_module"
check "the entrypoints are read from the deploy files" $GONE '    found.update(_CONSOLE.findall((root / "setup.py").read_text()))' '    pass' "$UNIT" "$GONE -k the_entrypoints_are_read_from_where_a_deploy_reads_them"

# --- the ratchet, the closure, the entrypoint ----------------------------------------------
check "the ratchet's core segment must read empty" $BASE '  "core_telegram_modules": [],' '  "core_telegram_modules": [
    "src/services/core/x.py"
  ],' "$UNIT" "$GONE -k the_fc2_ratchet_reads_the_target_tier_only"
check "a Telegram-named module outside the target tier is a stray" $BASE '    "src/channels/telegram_transport.py",' '    "src/legacy/telegram_transport.py",' "$UNIT" "$GONE -k the_fc2_ratchet_reads_the_target_tier_only"

m_stub() {  # a two-file mutation: a stub of a deleted package, imported by the entrypoint
  local name=$1 sel=$2
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  mkdir -p src/services/core && echo '"""mutant: a stub of the deleted package."""' > src/services/core/__init__.py
  printf '\nimport src.services.core  # mutant\n' >> $MAIN
  rm -rf src/__pycache__
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && rm -rf src/services/core && git checkout -- $MAIN
}
m_stub "a legacy module in the deployed closure is refused" "$GONE -k the_deployed_entrypoints_pull_no_legacy_module"
m_stub "an importer of a deleted module anywhere in the tree is refused" "$GONE -k nothing_in_the_tree_imports_a_deleted_module"
m_stub "a re-created legacy package is refused" "$GONE -k the_legacy_package_is_gone"

# (The refusal of a garbage worker switch was pinned here until the tear-out's phase 02 retired
# the switch, its contract module and the read; `legacy_tear_out_02.sh` pins that nothing reads it.)
check "the entrypoint runs the target root" $MAIN '    target_worker.main()


if __name__' '    return


if __name__' "$UNIT" "$WGATE -k main_runs_the_target_root"
check "the models package exports nothing" $MODELS 'exported here, on purpose — `tests/src/test_legacy_tier_gone.py` pins it.
"""
' 'exported here, on purpose — `tests/src/test_legacy_tier_gone.py` pins it.
"""

from src.models.target import TargetBase  # mutant
' "$UNIT" "$GONE -k the_models_package_exports_nothing"
check "a dependency only the legacy tier used stays gone, whatever its spelling" requirements.txt 'click==8.3.3
' 'click==8.3.3
pillow==12.3.0
' "$UNIT" "$GONE -k a_dependency_only_the_legacy_tier_used_is_gone"

# --- the inventory literal (a unit pin and a DB gate) ---------------------------------------
check "the sixteenth table is in the inventory (F4)" $INV '    "posting_history_dedup_archive",
    "posting_queue",' '    "posting_queue",' "$UNIT" "$TINV"
check "a name dropped from the lineage inventory is a missing snapshot" $INV '    "users",
' '' "$GATE" "$LANE -k legacy_holds_the_inventory"
check "the hand-made table is kept out of the lineage subset" $INV 'HAND_MADE = ("posting_history_dedup_archive",)' 'HAND_MADE = ()' "$GATE" "$LANE -k legacy_holds_the_inventory"

# --- the implicit dependency made explicit (a DB gate) --------------------------------------
check "psycopg2 adapts a uuid parameter because the gates register the adapter" $SCONF 'psycopg2.extensions.register_adapter(uuid.UUID, UUID_adapter)' 'pass' "$GATE" "$L3 -k container_create_resumes_by_bumping_generation"
