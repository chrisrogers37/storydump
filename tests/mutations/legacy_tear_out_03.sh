#!/bin/zsh
# Mutation battery for the legacy tear-out, phase 03 (the 3f snapshot migration and the ratchet's file rule):
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
# Counted and printed at the end: a script that dies half-way ends with NO last line (phase 02's
# battery once skipped 18 of 34 on a quoting slip), and a full run says so in numbers.
RAN=0
EXPECTED=$(grep -cE '^check "' "$0")

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
  RAN=$((RAN + 1))
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
M078=scripts/migrations/078_legacy_snapshots_pre_cutover.sql
RUNNER=scripts/migration_runner.py
RATCHET=scripts/advertised_ddl.py
INV=tests/scripts/legacy_inventory.py
LANE=tests/scripts/test_lineage_lane.py
TSNAP=tests/scripts/test_legacy_snapshots.py
TADV=tests/scripts/test_advertised_ddl.py
TRUN=tests/scripts/test_migration_runner.py
TINV=tests/scripts/test_legacy_inventory.py

# --- the file rule and the runner's markers -------------------------------------------------
M078=scripts/migrations/078_legacy_snapshots_pre_cutover.sql
RUNNER=scripts/migration_runner.py
RATCHET=scripts/advertised_ddl.py
INV=tests/scripts/legacy_inventory.py
LANE=tests/scripts/test_lineage_lane.py
TSNAP=tests/scripts/test_legacy_snapshots.py
TADV=tests/scripts/test_advertised_ddl.py
TRUN=tests/scripts/test_migration_runner.py
TINV=tests/scripts/test_legacy_inventory.py

# --- the file rule and the runner's markers -------------------------------------------------
check "an unadvertised file stays out of the F.2 prefix" $RATCHET '        if m.version > move.version and not m.unadvertised' '        if m.version > move.version' "$UNIT" "$TADV -k an_unadvertised_file_above_the_move_is_not_in_the_lineage"
check "the snapshot file says it is not advertised" $M078 '-- runner:unadvertised
' '' "$UNIT" "$TADV -k the_real_corpus_holds_the_snapshot_file_outside_the_lineage"
# A misspelt marker IN THE REAL CORPUS ("-- runner:unadvertized" in 078) is refused before any
# test runs: tests/scripts/conftest.py discovers the corpus at import (LEGACY_LINEAGE_MAX), so
# the whole session dies at collection with the runner's message naming the file. Observed on
# the committed tree (a collection error, not a test's verdict); the behaviour itself is pinned
# by the tmp-corpus mutations below, which are the ones this battery counts.
check "the runner refuses a marker it does not know" $RUNNER '        if word not in _KNOWN_WORDS:
            raise MigrationRunnerError(' '        if False:
            raise MigrationRunnerError(' "$UNIT" "$TRUN -k an_unknown_marker_is_refused_at_discovery"
check "a stray space after the colon still reaches the known set" $RUNNER 'r"^--\s*runner\s*:\s*(\S+)(.*)$"' 'r"^--\s*runner:(\S+)(.*)$"' "$UNIT" "$TRUN -k every_spelling_that_reads_as_a_marker"
check "a bare postcondition marker is refused" $RUNNER '            if not rest:
                raise MigrationRunnerError(' '            if False:
                raise MigrationRunnerError(' "$UNIT" "$TRUN -k a_bare_postcondition_marker_is_refused"
check "unadvertised is read off the file" $RUNNER '        else:
            flags[word] = True' '        else:
            flags[word] = word != "unadvertised"' "$UNIT" "$TRUN -k unadvertised_is_read_off_the_file"

# --- the snapshot file, pinned without a database ------------------------------------------------
check "078 names every table of the inventory" $M078 'CREATE TABLE archive.users_pre_cutover_20260917 AS TABLE legacy.users;
ALTER TABLE archive.users_pre_cutover_20260917 OWNER TO svc_maintenance;
' '' "$UNIT" "$TSNAP -k the_file_is_exactly_the_sixteen_copies"
check "a copy made WITH NO DATA is red without a database (an unseeded table)" $M078 'CREATE TABLE archive.posting_history_pre_cutover_20260917 AS TABLE legacy.posting_history;' 'CREATE TABLE archive.posting_history_pre_cutover_20260917 AS TABLE legacy.posting_history WITH NO DATA;' "$UNIT" "$TSNAP -k the_file_is_exactly_the_sixteen_copies"
check "a stray GRANT on any snapshot is red without a database" $M078 'ALTER TABLE archive.users_pre_cutover_20260917 OWNER TO svc_maintenance;
' 'ALTER TABLE archive.users_pre_cutover_20260917 OWNER TO svc_maintenance;
GRANT SELECT ON archive.users_pre_cutover_20260917 TO svc_ingress;
' "$UNIT" "$TSNAP -k the_file_is_exactly_the_sixteen_copies"
check "078 carries a row-count postcondition per table" $M078 "-- runner:postcondition SELECT (SELECT count(*) FROM archive.users_pre_cutover_20260917) = (SELECT count(*) FROM legacy.users)
" '' "$UNIT" "$TSNAP -k the_postconditions_are_exactly_two_per_table"
check "a row-count postcondition compared to the wrong source is red" $M078 "= (SELECT count(*) FROM legacy.users)" "= (SELECT count(*) FROM legacy.api_tokens)" "$UNIT" "$TSNAP -k the_postconditions_are_exactly_two_per_table"
check "the file is one transaction" $M078 '-- runner:unadvertised
' '-- runner:unadvertised
-- runner:no-transaction
' "$UNIT" "$TSNAP -k seventy_eighth_file"
check "a name dropped from the inventory is a snapshot 078 never takes" $INV '    "users",
' '' "$UNIT" "$TSNAP -k the_file_is_exactly_the_sixteen_copies"
check "the hand-made subset is the by-hand fixture's tables" $INV 'HAND_MADE = ("posting_history_dedup_archive",)' 'HAND_MADE = ()' "$UNIT" "$TINV -k hand_made_subset"
# The lane's pin of the split sees the same code mutation the ratchet's own test kills: mutating
# the lane's derivation instead (`m.unadvertised` → `m.version == 78`) is an EQUIVALENT mutant on
# this corpus, where 078 is the only unadvertised file — it survived, and taught that a test-side
# mutation of a list the corpus makes coincide proves nothing.
check "the lane keeps the two lists apart (the ratchet's filter, seen from the lane)" $RATCHET '        if m.version > move.version and not m.unadvertised' '        if m.version > move.version' "$UNIT" "$LANE -k the_target_lineage_is_the_increments"

# --- the snapshots as they run (DB gates) -----------------------------------------------------------
check "every snapshot is handed to svc_maintenance" $M078 'ALTER TABLE archive.users_pre_cutover_20260917 OWNER TO svc_maintenance;
' '' "$GATE" "$TSNAP -k the_owner_snapshots_every_table"
check "a copy made WITH NO DATA fails its row-count postcondition at runtime (the lineage's ledger)" $M078 'CREATE TABLE archive.schema_version_pre_cutover_20260917 AS TABLE legacy.schema_version;' 'CREATE TABLE archive.schema_version_pre_cutover_20260917 AS TABLE legacy.schema_version WITH NO DATA;' "$GATE" "$TSNAP -k the_owner_snapshots_every_table"
check "a snapshot is readable by nothing but svc_maintenance's members, on every table" $M078 'ALTER TABLE archive.media_items_pre_cutover_20260917 OWNER TO svc_maintenance;
' 'ALTER TABLE archive.media_items_pre_cutover_20260917 OWNER TO svc_maintenance;
GRANT SELECT ON archive.media_items_pre_cutover_20260917 TO svc_ingress;
' "$GATE" "$TSNAP -k the_owner_snapshots_every_table"
check "the lane's world holds the hand-made table production holds" $LANE '    psql_apply(dsn, [SETUP_SQL, BY_HAND_SQL])' '    psql_apply(dsn, [SETUP_SQL])' "$GATE" "$LANE -k legacy_holds_the_inventory"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
