#!/bin/zsh
# Mutation battery for the legacy tear-out, phase 04 (the runner's gated door, 079, 080, the gate and the
# runbook's pins): each behaviour the phase pins has one named mutation that must make its test FAIL
# ("killed"); the file is restored from the COMMITTED tree after each, so commit first. Run from the repo
# root with the sandbox off (the gates need the Docker test PostgreSQL on 65433). `ONLY=<regex>` runs a
# subset; `STORYDUMP_ROOT` points the battery at a worktree. The recipe sets no variable of either tier.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
GATE="env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH PYTHONDONTWRITEBYTECODE=1 DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude
# Counted and printed at the end: a script that dies half-way ends with NO last line, and a full run
# says so in numbers (phase 02's battery once skipped 18 of 34 on a quoting slip).
RAN=0
EXPECTED=$(grep -cE '^check "' "$0")

verdict() {  # name rc — reads /tmp/claude/mut.log
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}

mutate() {  # file old new — exit 3 unless `old` occurs exactly once
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}

check() {  # name file old new runner test-selector
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

RUNNER=scripts/migration_runner.py
RATCHET=scripts/advertised_ddl.py
M079=scripts/migrations/079_drop_legacy_schema.sql
M080=scripts/migrations/080_window_stand_down.sql
TRUN=tests/scripts/test_migration_runner.py
TCLOSE=tests/scripts/test_window_close.py
TADV=tests/scripts/test_advertised_ddl.py
TLANE=tests/scripts/test_lineage_lane.py
TDOCS=tests/test_agent_docs.py

# --- the runner's gated door (the synthetic corpus; DB-backed like every runner test) --------------
check "apply applies a manual file instead of owing it" $RUNNER '        report.owed = [m for m in pending if m.manual]
        pending = [m for m in pending if not m.manual]' '        report.owed = []' "$GATE" "$TRUN -k owes_the_manual_file_and_applies_the_ordinary_ones_above_it"
check "the below-head rule sees a manual file again (the deploy wedges after the next ordinary file)" $RUNNER '        pending = [m for m in migrations if m.version not in ledger]
        report.owed = [m for m in pending if m.manual]
        pending = [m for m in pending if not m.manual]
        for migration in pending:' '        pending = [m for m in migrations if m.version not in ledger]
        for migration in pending:
            if migration.version < applied_head and not migration.reapply_safe:
                raise MigrationRunnerError("below the head")
        report.owed = [m for m in pending if m.manual]
        pending = [m for m in pending if not m.manual]
        for migration in pending:' "$GATE" "$TRUN -k owes_the_manual_file_and_applies_the_ordinary_ones_above_it"
check "--manual refuses nothing: an ordinary file applies by name" $RUNNER '    if not migration.manual:
        raise MigrationRunnerError(' '    if False:
        raise MigrationRunnerError(' "$GATE" "$TRUN -k apply_manual_refuses_a_file_without_the_directive"
check "--manual applies an already-recorded version again" $RUNNER '        if version in ledger:
            _checksum, row_status = ledger[version]
            raise MigrationRunnerError(' '        if version in ledger and False:
            _checksum, row_status = ledger[version]
            raise MigrationRunnerError(' "$GATE" "$TRUN -k apply_manual_refuses_a_version_already_recorded"
check "--manual applies below the head no more (the operator door meets the deploy's rule)" $RUNNER '        try:
            _apply_one(conn, migration)
        except MigrationRunnerError:
            raise
        except Exception as exc:
            raise MigrationRunnerError(
                f"migration {migration.label} failed: {exc}"
            ) from exc
        report.applied.append(migration)
    finally:
        conn.close()
    return report


@dataclass
class AdoptReport:' '        if version < max(ledger, default=0):
            raise MigrationRunnerError("below the head")
        try:
            _apply_one(conn, migration)
        except MigrationRunnerError:
            raise
        except Exception as exc:
            raise MigrationRunnerError(
                f"migration {migration.label} failed: {exc}"
            ) from exc
        report.applied.append(migration)
    finally:
        conn.close()
    return report


@dataclass
class AdoptReport:' "$GATE" "$TRUN -k apply_manual_applies_it_below_the_head"
check "status lists a manual file as pending, not owed" $RUNNER '        report.pending = [m for m in unrecorded if not m.manual]
        report.owed = [m for m in unrecorded if m.manual]' '        report.pending = list(unrecorded)
        report.owed = []' "$GATE" "$TRUN -k status_lists_it_as_owed_not_pending"
check "the CLI prints no owed line" $RUNNER '            for migration in report.owed:
                print(f"owed (manual) {migration.label}")' '            pass' "$GATE" "$TRUN -k cli_reports_owed_files_and_exits_zero"
check "a manual marker with an argument is read as the marker" $RUNNER '        elif rest:
            raise MigrationRunnerError(' '        elif rest and word != "manual":
            raise MigrationRunnerError(' "$UNIT" "$TRUN -k a_manual_marker_refuses_an_argument"
check "the marker is not read off the file" $RUNNER '        flags["manual"],
        tuple(postconditions),' '        False,
        tuple(postconditions),' "$UNIT" "$TRUN -k manual_is_read_off_the_file"

# --- the ratchet's file rule ---------------------------------------------------------------------
check "a manual file enters the F.2 prefix" $RATCHET '        if m.version > move.version and not m.unadvertised and not m.manual' '        if m.version > move.version and not m.unadvertised' "$UNIT" "$TADV -k a_manual_file_above_the_move_is_not_in_the_lineage_either"

# --- the two files, pinned without a database --------------------------------------------------
check "079 loses its manual directive (the deploy would drop legacy)" $M079 '-- runner:manual
-- runner:unadvertised
-- runner:postcondition SELECT NOT EXISTS' '-- runner:unadvertised
-- runner:postcondition SELECT NOT EXISTS' "$UNIT" "$TCLOSE -k 'it_is_gated_unadvertised_and_wrapped and 79'"
check "079 drops a table from its inventory" $M079 "    'posting_queue', 'schema_version', 'service_runs', 'user_chat_memberships'," "    'posting_queue', 'service_runs', 'user_chat_memberships'," "$UNIT" "$TCLOSE -k 079_names_every_table"
check "079 names another date than 078's" $M079 "t || '_pre_cutover_20260917')) IS NULL THEN" "t || '_pre_cutover_20260918')) IS NULL THEN" "$UNIT" "$TCLOSE -k 079_names_every_table"
check "080 loses its manual directive" $M080 '-- runner:manual
-- runner:unadvertised
-- runner:postcondition SELECT to_regclass' '-- runner:unadvertised
-- runner:postcondition SELECT to_regclass' "$UNIT" "$TCLOSE -k 'it_is_gated_unadvertised_and_wrapped and 80'"
check "080 revokes the memberships as printed (F8 (a) undone)" $M080 'DROP SCHEMA IF EXISTS window_ddl CASCADE;' 'DROP SCHEMA IF EXISTS window_ddl CASCADE;
REVOKE svc_claim, svc_clock, svc_maintenance, svc_membership FROM svc_migration;' "$UNIT" "$TCLOSE -k 080_carries_the_identity_guard_and_no_membership_revoke"

# --- the gates, as they run ------------------------------------------------------------------------
check "079's precondition lets a missing snapshot through" $M079 "    IF to_regclass(format('archive.%I', t || '_pre_cutover_20260917')) IS NULL THEN
      RAISE EXCEPTION '3g refused: no snapshot for legacy.%', t;
    END IF;" "" "$GATE" "$TCLOSE -k 079_refuses_when_a_snapshot_is_missing"
check "079's precondition ignores a count that no longer matches" $M079 "    IF src <> snap THEN" "    IF src <> snap AND false THEN" "$GATE" "$TCLOSE -k 079_refuses_when_a_snapshot_no_longer_matches"
check "080's guard passes on a present legacy schema" $M080 "  IF to_regclass('public.jobs') IS NULL
     OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'legacy') THEN" "  IF to_regclass('public.jobs') IS NULL THEN" "$GATE" "$TCLOSE -k 080_refuses_while_legacy_is_present"
check "080 keeps the door schema" $M080 'DROP SCHEMA IF EXISTS window_ddl CASCADE;' '-- (the door stays)' "$GATE" "$TCLOSE -k 080_closes_the_window_and_the_gate_answers_as_printed"
check "080 leaves CREATE ON DATABASE with svc_migration" $M080 "  EXECUTE format('REVOKE CREATE ON DATABASE %I FROM svc_migration', current_database());" "  PERFORM 1;" "$GATE" "$TCLOSE -k 080_closes_the_window_and_the_gate_answers_as_printed"
check "080 revokes the owner's membership of svc_migration (the door chain breaks)" $M080 'DROP SCHEMA IF EXISTS window_ddl CASCADE;' 'DROP SCHEMA IF EXISTS window_ddl CASCADE;
DO $$ BEGIN EXECUTE format('"'"'REVOKE svc_migration FROM %I'"'"', current_user); END $$;' "$GATE" "$TCLOSE -k door_file_statement_still_lands_after_the_stand_down"
check "the lane's world applies the gated pair" $RUNNER '        report.owed = [m for m in pending if m.manual]
        pending = [m for m in pending if not m.manual]' '        report.owed = []' "$GATE" "$TLANE -k one_run_applies_the_whole_corpus"
check "the lane's list drops the gated pair" $TLANE '            "079_drop_legacy_schema.sql",
            "080_window_stand_down.sql",
        ], (' '        ], (' "$UNIT" "$TLANE -k the_target_lineage_is_the_increments"

# --- the never-run lists ----------------------------------------------------------------------------
check "the gated door leaves the never-run list" AGENTS.md 'python -m scripts.migration_runner apply --manual <version>   # Applies a gated file: 079 DROPS the legacy schema — the owner runs the window (F7)
' '' "$UNIT" "$TDOCS -k 'never_run_list_is_not_empty or two_never_run_lists_are_identical'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
