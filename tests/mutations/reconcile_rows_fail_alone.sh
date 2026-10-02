#!/bin/zsh
# Mutation battery for #1492: one row of the reconciler's sweep fails alone. `reconcile_ambiguous`
# owns its transactions. It reads the sweep in one short transaction and runs each row in one of its
# own, so a row that raises rolls back alone, and the rows behind it land. No savepoint is carried
# across rows (#1441's subtransaction bound), whatever `reconcile_limit` is. After the beat, the
# first failure is re-raised, so the job still fails and retries (#1438's alarm). A caller that
# passes its own session (the unit seam) gets a savepoint per row instead.
#
# Each behaviour has one named mutation, and it must make its test FAIL ("killed"). It must PASS on
# the clean tree first; otherwise the verdict is BASELINE RED. A selector that selects nothing is NO
# TEST SELECTED, never a kill. Files are restored from the COMMITTED tree after each mutation, so
# commit first, and run the battery in its own worktree (`STORYDUMP_ROOT`). The gates need a
# PostgreSQL. The DB_* fields are read from the environment, and default to the Docker server that
# `AGENTS.md` › Testing starts. `STORYDUMP_PY` points at another venv's python.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=${STORYDUMP_PY:-/Users/chris/Projects/storydump/.venv/bin/python}
cd "$ROOT" || exit 2
: ${DB_HOST:=localhost} ${DB_PORT:=65433} ${DB_USER:=test_user} ${DB_PASSWORD:=test_password}
: ${DB_NAME:=storyline_ai} ${TEST_DB_NAME:=storyline_test}
export DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME TEST_DB_NAME
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
GATE_RUN="env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH REQUIRE_TEST_DATABASE=1 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^check "' "$0")
verdict() {
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}
mutate() {  # file old new [old2 new2]: each old must match exactly once when its turn comes
  OLD="$2" NEW="$3" OLD2="${4-}" NEW2="${5-}" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
pairs = [(os.environ["OLD"], os.environ["NEW"])]
if os.environ["OLD2"]:
    pairs.append((os.environ["OLD2"], os.environ["NEW2"]))
for old, new in pairs:
    if s.count(old) != 1:
        print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
    s = s.replace(old, new, 1)
p.write_text(s)
PY
}
check() {  # name file old new test-selector [old2 new2]
  local name=$1 file=$2 old=$3 new=$4 sel=$5 old2=${6-} new2=${7-}
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new" "$old2" "$new2"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

W=src/services/target/work_loop.py
U=tests/src/services/target/test_work_loop.py
L3=tests/scripts/test_l3_permit_rail.py

# A failed row is that row's failure alone: the rows behind it run, and those that resolve commit.
check "a failed row stops the beat (unit)" $W '            except Exception as exc:  # noqa: BLE001 — re-raised after the beat' '            except ZeroDivisionError as exc:  # noqa: BLE001 — re-raised after the beat' "$U -k 'first_failure_is_raised'"
check "a failed row stops the beat (gate)" $W '            except Exception as exc:  # noqa: BLE001 — re-raised after the beat' '            except ZeroDivisionError as exc:  # noqa: BLE001 — re-raised after the beat' "$L3 -k 'missed_flip_fails_only_its_own_row'"
# The bound: no transaction carries a second row, and no row nests a savepoint in its own.
check "the rows share the sweep's transaction" $W '                    short(session, job) as row_session,' '                    nullcontext(reader) as row_session,' "$U -k 'no_transaction_carries_more_than_one_row'"
check "a row nests a savepoint in its own transaction" $W '                    nullcontext() if session is None else session.begin_nested(),' '                    row_session.begin_nested(),' "$U -k 'no_transaction_carries_more_than_one_row'"
check "the unit seam loses its per-row savepoint" $W '                    nullcontext() if session is None else session.begin_nested(),' '                    nullcontext(),' "$U -k 'savepoint_per_row'"
# The alarm: after the beat the FIRST failure is re-raised, so the job fails (#1438).
check "the beat swallows its failures" $W '            raise failures[0]' '            pass' "$L3 -k 'missed_flip_fails_the_reconcile_beat'"
check "the last failure is the one raised" $W '            raise failures[0]' '            raise failures[-1]' "$U -k 'first_failure_is_raised'"
# The mark: the loop hands the executor no session, so its rows are transactions, not savepoints.
check "the executor runs in the job's transaction" $W $'    @own_transactions\n    async def reconcile_ambiguous(session, job):' '    async def reconcile_ambiguous(session, job):' "$U -k 'provider_facing_kinds_are_exactly_the_marked_ones'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
