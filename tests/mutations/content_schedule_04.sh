#!/bin/zsh
# Mutation battery for the content schedule's phase 4, the daily-cap exemption (#1413, plan PR #1414;
# the owner's F8 (a)): a planned story neither spends the account's daily cap nor waits on it. Each
# behaviour has one named mutation that must make its test FAIL ("killed") — and must PASS on the
# clean tree first, or the verdict is BASELINE RED; a selector that selects nothing is NO TEST
# SELECTED, never a kill. Files are restored from the COMMITTED tree after each, so commit first, and
# run it in its own worktree (`STORYDUMP_ROOT`). The gates need a PostgreSQL: the DB_* fields are read
# from the environment, defaulting to the Docker server `AGENTS.md` › Testing starts; `STORYDUMP_PY`
# points at another venv's python.
#
# No DDL here: every mutation edits the Python that carries the SQL. The cadence checks are about
# what must NOT change — a cadence story still spends the cap and still waits on a spent day, and a
# cadence story posted by hand still counts, through the command port and through the tap — because
# an exemption that leaked into the cadence would post over the cap, the direction the cap exists to
# prevent.
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
mutate() {
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}
check() {  # name file old new test-selector
  local name=$1 file=$2 old=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

P=src/services/target/publish_cap.py
X=src/services/target/command_executors.py
G=tests/scripts/test_publish_cap_gate.py
L=tests/scripts/test_l5_pipeline_gate.py
E=tests/scripts/test_command_executors_gate.py
U=tests/src/services/target/test_publish_cap.py
T=tests/scripts/test_w4_tap_gate.py

# The flip: a planned story takes its day without a debit.
check "the flip debits a planned story" $P '"     AND me.spends_cap"' '"     AND true"' "$G -k 'the_cadence_still_spends_its_whole_cap_after_one'"
check "a planned story waits on a spent day" $P '"          OR NOT (SELECT spends_cap FROM me))"' '"          )"' "$G -k 'on_a_spent_day_it_flips_at_once'"
check "a planned story flips without its day" $P '"                                    CAST(:local_date AS date))"' '"                                    NULL)"' "$G -k 'on_a_spent_day_it_flips_at_once'"
# With no debit guarding it, the flip's own WHERE is all that stands between a planned story's cancel
# and its post.
check "the flip forgets a planned story's cancel" $P '"     AND NOT p.cancel_requested"' '"     AND true"' "$G -k 'cancel_still_wins_over_its_flip'"
# The cadence is unchanged: the exemption never reaches a cadence story, and the rule is not inverted.
check "the exemption reaches a cadence story" $P '"          OR NOT (SELECT spends_cap FROM me))"' '"          OR true)"' "$G -k 'flip_defers_at_the_cap'"
check "the rule is inverted" $P "_SPENDS_CAP_SQL = \"origin = 'cadence'\"" "_SPENDS_CAP_SQL = \"origin <> 'cadence'\"" "$G -k 'first_flip_proceeds_and_debits_once'"
# The refunds: one decrement, and a story that took nothing gets nothing back. The pipeline's own
# check is the only evidence its failure test pins anything: on phase 2's code that test passes,
# because the debit and the refund cancel out.
check "a refund returns a planned story's day" $P 'WHERE id = :intent AND " + _SPENDS_CAP_SQL + "))"' 'WHERE id = :intent))"' "$G -k 'refund_of_it_leaves_the_cadence_debits_alone'"
check "a failure after the flip returns a planned story's day" $P 'WHERE id = :intent AND " + _SPENDS_CAP_SQL + "))"' 'WHERE id = :intent))"' "$L -k 'failure_after_its_flip_leaves_the_count_unchanged'"
# Posted myself, through the command port and through the tap: its debit follows its flip and asks
# the same rule, and the day is stamped either way.
check "Posted myself debits a planned story" $P '"   WHERE flip.spends_cap"' '"   WHERE true"' "$E -k 'planned_story_posted_by_hand'"
check "Posted myself through the tap debits a planned story" $P '"   WHERE flip.spends_cap"' '"   WHERE true"' "$T -k 'planned_story_debits_nothing_and_stamps_its_day'"
check "Posted myself stops counting the cadence" $P '"   WHERE flip.spends_cap"' '"   WHERE false"' "$E -k 'first_post_debits_the_day'"
check "Posted myself through the tap stops counting the cadence" $P '"   WHERE flip.spends_cap"' '"   WHERE false"' "$T -k 'cadence_story_debits_once'"
check "Posted myself stamps no day" $P '"         cap_consumed_on = (now() AT TIME ZONE fn_safe_tz(:tz))::date"' '"         cap_consumed_on = NULL"' "$E -k 'planned_story_posted_by_hand'"
# The debit reads the flip's own row, so a flip that matched nothing debits nothing even when it
# commits (the port rolls a refusal back, which would hide it).
check "the debit is independent of the flip" $P '"  SELECT :ws, :acct, flip.cap_consumed_on, 1, :cap FROM flip"' '"  SELECT :ws, :acct, (now() AT TIME ZONE fn_safe_tz(:tz))::date, 1, :cap FROM (SELECT " + _SPENDS_CAP_SQL + " AS spends_cap FROM post_intents WHERE id = :intent) flip"' "$E -k 'flip_that_matches_no_row_commits_no_debit'"
# Only the cap ledger writes a bucket, so no writer can miss the rule: a write anywhere else fails.
check "a second module writes a bucket" $X '_RESOLVED_BY_SOMEONE_ELSE = "the review was resolved by someone else first"' '_RESOLVED_BY_SOMEONE_ELSE = "UPDATE daily_post_counts SET count = 0"' "$U -k 'only_the_cap_ledger_writes_a_bucket'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
