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
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE PYTHONDONTWRITEBYTECODE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
GATE="env PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH PYTHONDONTWRITEBYTECODE=1 DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
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
REACH=tests/scripts/test_target_reachability.py
INV=tests/scripts/legacy_inventory.py
WGATE=tests/src/test_worker_impl_gate.py
LANE=tests/scripts/test_lineage_lane.py

# --- the predicate (the standing guard) ---------------------------------------------------
check "the AST scan walks into function bodies" $GONE '            for node in ast.walk(tree):
                names = (' '            for node in tree.body:
                names = (' "$UNIT" "$GONE -k a_planted_importer_is_found"
check "a from-import of a deleted submodule counts" $GONE '            for alias in node.names:
                yield f"{node.module}.{alias.name}"' '            for alias in node.names:
                pass' "$UNIT" "$GONE -k a_from_import_of_a_deleted_submodule_is_found"
check "the prefix arm catches a deeper import" $GONE '    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)' '    return any(name == p for p in FORBIDDEN_PREFIXES)' "$UNIT" "$GONE -k a_deeper_import_under_a_deleted_package_is_found"
check "bare src.models is forbidden" $GONE '    if name == "src.models" or (' '    if False or (' "$UNIT" "$GONE -k a_planted_importer_is_found"
check "the forbidden set names every deleted module" $GONE '    "src.services.core",
    "src.services.integrations",' '    "src.services.integrations",' "$UNIT" "$GONE -k the_forbidden_set_names_every_deleted_module"

# --- the ratchet, the closure, the entrypoint ----------------------------------------------
check "the ratchet's core segment must read empty" $BASE '  "core_telegram_modules": [],' '  "core_telegram_modules": [
    "src/services/core/x.py"
  ],' "$UNIT" "$GONE -k the_fc2_ratchet_reads_the_target_tier_only"

m_closure() {  # a two-file mutation: a stub of a deleted package, imported by the entrypoint
  local name="a legacy module in the deployed closure is refused"
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  mkdir -p src/services/core && echo '"""mutant: a stub of the deleted package."""' > src/services/core/__init__.py
  printf '\nimport src.services.core  # mutant\n' >> $MAIN
  rm -rf src/__pycache__
  eval "$UNIT $GONE -k the_deployed_entrypoints_pull_no_legacy_module" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && rm -rf src/services/core && git checkout -- $MAIN
}
m_closure

check "the entrypoint refuses a garbage WORKER_IMPL before the root runs" $MAIN '    impl = resolve_worker_impl(os.environ)' '    impl = WORKER_IMPL_TARGET' "$UNIT" "$WGATE -k garbage_refuses"
check "the entrypoint runs the target root" $MAIN '    target_worker.main()


if __name__' '    return


if __name__' "$UNIT" "$WGATE -k unset_runs_the_target_root"
check "the models package exports nothing" $MODELS 'exported here, on purpose — `tests/src/test_legacy_tier_gone.py` pins it.
"""
' 'exported here, on purpose — `tests/src/test_legacy_tier_gone.py` pins it.
"""

from src.models.target import TargetBase  # mutant
' "$UNIT" "$GONE -k the_models_package_exports_nothing"
check "a dependency only the legacy tier used stays gone" requirements.txt 'click==8.3.3
' 'click==8.3.3
Pillow==12.3.0
' "$UNIT" "$GONE -k a_dependency_only_the_legacy_tier_used_is_gone"
check "the reachability specimen reaches no target module" $REACH '    SPECIMEN = "scripts.migration_runner"' '    SPECIMEN = "src.worker"' "$UNIT" "$REACH -k a_module_that_imports_no_target_reports_none_after_one_that_does"

# --- the lane's inventory literal (a DB gate) ------------------------------------------------
check "a name dropped from the lineage inventory is a missing snapshot" $INV '    "users",
' '' "$GATE" "$LANE -k legacy_holds_the_inventory"
