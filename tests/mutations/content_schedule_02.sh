#!/bin/zsh
# Mutation battery for the content schedule's phase 2, the ledger learning 'planned' (migration 086;
# #1413, plan PR #1414): each behaviour has one named mutation that must make its test FAIL ("killed")
# — and must PASS on the clean tree first, or the verdict is BASELINE RED; a selector that selects
# nothing is NO TEST SELECTED, never a kill. Files are restored from the COMMITTED tree after each, so
# commit first, and run it in its own worktree (`STORYDUMP_ROOT`). The gates need a PostgreSQL: the
# DB_* fields are read from the environment, defaulting to the Docker server `AGENTS.md` › Testing
# starts; `STORYDUMP_PY` points at another venv's python.
#
# The behavioural mutations edit `07` §29, not the 086 file: every gate here replays the ADVERTISED
# stream, so §29 is the SQL they run. The one 086 mutation is caught by the prefix check that holds
# the file to the stream, and the model mutation by the lane's parity with `create_all`.
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

DOC=documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md
L=tests/scripts/test_intent_ledger_gate.py
C=tests/scripts/test_scheduler_clock_gate.py

# The person rule (§29's trigger): its scope is the trigger's WHEN, the body checks the actor.
check "the person rule is gone" $DOC "        OR (OLD.origin = 'planned' AND OLD.state = 'awaiting_approval' AND NEW.state = 'approved'))" "        )" "$L -k 'anything_but_a_person_is_refused'"
check "a service identity counts as a person" $DOC "  IF COALESCE(current_setting('app.actor_kind', true), '') <> 'user'" "  IF COALESCE(current_setting('app.actor_kind', true), '') NOT IN ('user','operator')" "$L -k 'anything_but_a_person_is_refused and operator'"
check "a user actor needs no user id" $DOC "     OR NULLIF(current_setting('app.actor_user_id', true), '') IS NULL THEN" "     OR false THEN" "$L -k 'anything_but_a_person_is_refused and user-False'"
check "the rule reaches cadence rows" $DOC "        OR (OLD.origin = 'planned' AND" "        OR (OLD.origin IN ('planned','cadence') AND" "$L -k 'cadence_row_is_untouched_by_the_rule'"
check "the rule blocks the pipeline's step back" $DOC "AND OLD.state = 'awaiting_approval' AND NEW.state = 'approved'))" "AND OLD.state IN ('awaiting_approval','publishing') AND NEW.state = 'approved'))" "$L -k 'step_back_is_untouched'"
check "origin can be rewritten" $DOC "  IF NEW.origin IS DISTINCT FROM OLD.origin THEN" "  IF false THEN" "$L -k 'origin_cannot_be_rewritten or rewrite_that_would_admit'"
# The manual-only CHECK and the cadence-only key.
check "a planned row may be auto" $DOC "  CHECK (origin = 'cadence' OR approval_mode = 'manual');" "  CHECK (true);" "$L -k 'born_manual'"
check "the cadence key is unconditional" $DOC "  WHERE origin = 'cadence';" "  ;" "$C -k 'holds_on_the_cadence_key_alone'"
# plan_slot's spelling, run by the real executor on the cadence key alone.
check "plan_slot drops the predicate" src/services/target/scheduler.py "                \" WHERE origin = 'cadence'\"" "                \"\"" "$C -k 'holds_on_the_cadence_key_alone'"
# The file and the model are held to the stream.
check "the 086 file drifts from §29" scripts/migrations/086_intent_origin_planned.sql "CREATE TRIGGER tg_intent_planned_person BEFORE UPDATE OF state, origin ON post_intents" "CREATE TRIGGER tg_intent_planned_person BEFORE UPDATE OF state ON post_intents" "tests/scripts/test_advertised_ddl.py -k 'matching_prefix_holds_and_is_not_vacuous'"
check "the model forgets the cadence key" src/models/target/intent_ledger.py '            "uq_intent_slot_cadence",' '            "uq_intent_slot_cadence_renamed",' "tests/scripts/test_lineage_lane.py -k 'lane_parity_holds_against_the_target_models'"
# Each approve path is a person only because it stamps one: a path that stopped stamping `user`
# would be refused on a planned story, and its test must say so.
check "the tap stamps the worker's actor" src/services/target/telegram_dispatch.py 'actor_kind="user",' 'actor_kind="system",' "tests/scripts/test_w4_tap_gate.py -k 'planned_story_is_approved_by_the_tapper'"
check "a person principal stamps a service identity's actor" src/api/principal.py '        return "operator" if self.is_service_identity else "user"' '        return "operator"' "tests/scripts/test_web_router_x2_gate.py tests/scripts/test_cli_writes_gate.py -k 'planned_story_is_approved_through'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
