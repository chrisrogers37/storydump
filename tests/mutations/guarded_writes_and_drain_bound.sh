#!/bin/zsh
# Mutation battery for two fixes in one PR. #1438: four state-guarded `post_intents` writes outside
# the ledger now check that they landed, and raise on a miss as the checkpoints beside them do (the
# reconciler's verdict flip and its park, the ambiguous park after a lost publish answer, and the
# pipeline's `container_ready` checkpoint). #1441: the offboarding drain cancels at most 64 intents
# (writing savepoints) per transaction, drains the rest on the next run at once, and records a
# refused cancel in its own transaction. Each behaviour has one named mutation that must make its test FAIL
# ("killed") — and must PASS on the clean tree first, or the verdict is BASELINE RED; a selector
# that selects nothing is NO TEST SELECTED, never a kill. Files are restored from the COMMITTED
# tree after each, so commit first, and run it in its own worktree (`STORYDUMP_ROOT`). The gates
# need a PostgreSQL: the DB_* fields are read from the environment, defaulting to the Docker server
# `AGENTS.md` › Testing starts; `STORYDUMP_PY` points at another venv's python. The review of #1488
# added targets (a) to (e) at the end. A mutation that needs two edits, such as a move or a wrap,
# passes a second old/new pair, applied after the first.
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

R=src/services/target/reconciler.py
V=src/services/target/provider_ops.py
F=src/services/target/publish_pipeline.py
O=src/services/target/offboarding.py
L3=tests/scripts/test_l3_permit_rail.py
L5=tests/scripts/test_l5_pipeline_gate.py
G=tests/scripts/test_offboard_gate.py

# #1438: a state-guarded write that matched no row raises; the caller does not go on. Each mutation
# puts back what main did on a miss: carry on, or (the park) return silently.
check "the verdict's flip goes unchecked" $R '    if flipped is None:' '    if flipped is None and False:' "$L3 -k 'verdict_whose_intent_already_left or empty_tenant'"
check "the park goes unchecked" $R '    if moved is None:' $'    if moved is None:\n        return\n    if moved is None and False:' "$L3 -k 'park_whose_intent_already_left_raises'"
check "the ambiguous park goes unchecked" $V '    if moved is None:' '    if moved is None and False:' "$L3 -k 'already_left_publishing_raises'"
check "the ready checkpoint goes unchecked" $F '        if ready is None:' '        if ready is None and False:' "$L5 -k 'ready_checkpoint_that_matches_no_row'"
# #1441: at most 64 cancels (writing savepoints) per run, refusals not counted; a run the bound stopped
# drains again at once, before legs 2-3 and while publishing drains; any other run waits; a refusal is
# recorded in its own transaction.
check "the drain writes past the subtransaction cache" $O '        if cancelled == MAX_WRITING_SAVEPOINTS:' '        if False:' "$G -k 'cancels_at_most_64'"
check "refusals use up the write bound" $O '        if cancelled == MAX_WRITING_SAVEPOINTS:' '        if cancelled + len(refusals) == MAX_WRITING_SAVEPOINTS:' "$G -k 'refusals_do_not_use_up_the_bound'"
check "a full batch waits for the window" $O '    again = drained["more"] and drained["cancelled"] == MAX_WRITING_SAVEPOINTS' '    again = False' "$G -k 'cancels_at_most_64'"
check "a run the bound did not stop re-runs at once" $O '    again = drained["more"] and drained["cancelled"] == MAX_WRITING_SAVEPOINTS' '    again = drained["more"]' "$G -k 'bound_did_not_stop'"
check "a window of refusals crawls in back-to-back runs" $O '    again = drained["more"] and drained["cancelled"] == MAX_WRITING_SAVEPOINTS' '    again = drained["more"] and drained["cancelled"]' "$G -k 'bound_did_not_stop'"
check "while publishing drains, a full batch waits for the recheck" $O $'        if again:\n            run_at_sql, params = "now()", {}' $'        if False:\n            run_at_sql, params = "now()", {}' "$G -k 'while_publishing_drains'"
check "a refusal leaves only a log line" $O '    if refusals:' '    if False:' "$G -k 'refused_cancel_does_not_abort'"
check "the refusal's record rides the job's transaction" $O '        await _audit(own_tx, workspace_id, "offboard_cancel_refused", *refusals)' '        [await audit.record(session, workspace_id=workspace_id, entity_kind="workspace", entity_id_sql="CAST(:ws AS uuid)", from_state="offboarding", to_state="offboarding", detail={"v": 1, "event": "offboard_cancel_refused", **d}, actor_sql=audit.ACTOR_SYSTEM_CHANNEL) for d in refusals]' "$G -k 'refusals_record_survives'"

# The review of #1488: (a) re-running at once needs intents left, not 64 cancels alone; (b) the bound's
# value; (c) `more`'s arithmetic, pinned by the drain's unit tests; (d) the refusal record is written
# before anything can raise, one row per refusal, and committed; (e) the reconcile sweep does not
# swallow a missed flip.
U=tests/src/services/target/test_offboarding.py
W=src/services/target/work_loop.py
check "(a) 64 cancels with nothing left re-run at once" $O '    again = drained["more"] and drained["cancelled"] == MAX_WRITING_SAVEPOINTS' '    again = drained["cancelled"] == MAX_WRITING_SAVEPOINTS' "$G -k 'exactly_64_live'"
check "(b) the bound is 65" $O 'MAX_WRITING_SAVEPOINTS = 64' 'MAX_WRITING_SAVEPOINTS = 65' "$U -k 'TestTheDrainBound'"
check "(b) the bound is 63" $O 'MAX_WRITING_SAVEPOINTS = 64' 'MAX_WRITING_SAVEPOINTS = 63' "$U -k 'TestTheDrainBound'"
check "(c) a read that fills its limit is not more" $O '"more": processed < len(rows) or len(rows) == limit,' '"more": processed < len(rows),' "$U -k 'TestTheDrainBound'"
check "(c) every run is more" $O '"more": processed < len(rows) or len(rows) == limit,' '"more": processed <= len(rows) or len(rows) == limit,' "$U -k 'TestTheDrainBound'"
check "(c) a refused intent is not processed" $O $'        processed += 1\n' '' "$U -k 'TestTheDrainBound'" $'            cancelled += 1\n' $'            cancelled += 1\n            processed += 1\n'
check "(d) the refusal record waits until after the timeout's raise" $O $'    if refusals:\n        await _audit(own_tx, workspace_id, "offboard_cancel_refused", *refusals)\n' '' "$G -k 'refusals_record_survives'" $'    if again:\n        # Legs 2-3 wait for the drain to finish' $'    if refusals:\n        await _audit(own_tx, workspace_id, "offboard_cancel_refused", *refusals)\n    if again:\n        # Legs 2-3 wait for the drain to finish'
check "(d) only the first refusal is recorded" $O '"offboard_cancel_refused", *refusals)' '"offboard_cancel_refused", *refusals[:1])' "$G -k 'refusals_do_not_use_up_the_bound'"
check "(d) the refusal record is never committed" $O $'                actor_sql=audit.ACTOR_SYSTEM_CHANNEL,\n            )\n        await session.commit()' $'                actor_sql=audit.ACTOR_SYSTEM_CHANNEL,\n            )' "$G -k 'refusals_record_survives or refusals_do_not_use_up_the_bound'"
check "(e) the reconcile sweep swallows a missed flip" $W $'            await reconciler.reconcile_intent(\n' $'            try:\n              await reconciler.reconcile_intent(\n' "$L3 -k 'missed_flip_fails_the_reconcile_beat'" $'            )\n\n        ladder_skipped = unreachable = 0\n' $'            )\n            except Exception:\n                pass\n\n        ladder_skipped = unreachable = 0\n'

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
