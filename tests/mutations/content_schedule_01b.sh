#!/bin/zsh
# Mutation battery for the content schedule's phase 1b, the reaper's cancel leg (migration 087; #1235,
# plan PR #1414): 087 redefines fn_reaper_sweep on 086's body, so every leg the sweep runs is in one
# statement. Each check makes ONE change — a leg removed, or one of the cancel leg's guards, its order
# or its recheck undone — and its named test must FAIL ("killed") — and must PASS on the clean tree
# first, or the verdict is BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a
# kill. Files are restored from the COMMITTED tree after each (and on an interrupt), so commit first,
# and run it in its own worktree (`STORYDUMP_ROOT`). The gates need a PostgreSQL: the DB_* fields are
# read from the environment, defaulting to the Docker server `AGENTS.md` › Testing starts;
# `STORYDUMP_PY` points at another venv's python.
#
# The §30 mutations edit `07` §30, not the 087 file: every gate here replays the ADVERTISED stream, so
# §30 is the SQL they run. Most legs read the same in §22, §29 and §30, so the edit is made inside
# §30's fence only (`edit_s30`), and §30's manifest entry is re-hashed so the stream still builds. The
# one 087 mutation is caught by the prefix check that holds the file to the stream.
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
EXPECTED=$(grep -cE '^check(_s30)? "' "$0")

DOC=documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md
MANIFEST=scripts/advertised_ddl_manifest.json
MIGRATION=scripts/migrations/087_reaper_cancels_flagged_waiting_intents.sql
PIPELINE=src/services/target/publish_pipeline.py
L=tests/scripts/test_l5_pipeline_gate.py
LEGS="$L::TestEveryLegRunsInTheFinalBody"
CANCEL="$L::TestTheReapersCancelLeg"
# An interrupted check must not leave a mutant behind with a manifest that agrees with it.
trap 'cd "$ROOT" && git checkout HEAD -- "$DOC" "$MANIFEST" "$MIGRATION" "$PIPELINE"' EXIT INT TERM

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
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout HEAD -- "$file"; return; fi
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout HEAD -- "$file"
}

# One edit inside §30's fence, then §30's manifest entry re-hashed, in one pass. A leg runs from the
# fence line holding its first words through the first line after it holding `n := n + c`, where every
# leg ends. `drop` cuts the leg holding *arg*; `sub` replaces the one fence line equal to *arg* with
# *new*; `after` moves the leg holding *arg* to just after the leg holding *new*.
edit_s30() {  # drop <leg> | sub <line> <new> | after <leg> <anchor leg>
  MODE="$1" ARG="$2" NEW="${3:-}" $PY - "$DOC" "$MANIFEST" <<'PY'
import json, os, sys, pathlib
from scripts.advertised_ddl import extract_blocks
doc, manifest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
lines = doc.read_text().split("\n")
(label,) = [i for i, l in enumerate(lines) if l.startswith("-- [§30 ")]
mode, arg, new = os.environ["MODE"], os.environ["ARG"], os.environ["NEW"]

def fence_end():
    return next(i for i in range(label, len(lines)) if lines[i] == "```")

def one(pred):
    hits = [i for i in range(label, fence_end()) if pred(lines[i])]
    if len(hits) != 1:
        print(f"MUTATION NOT APPLIED ({len(hits)} matches in §30)"); sys.exit(3)
    return hits[0]

def leg(frag):
    start = one(lambda l: frag in l)
    return start, next(i for i in range(start, fence_end()) if "n := n + c" in lines[i])

if mode == "sub":
    lines[one(lambda l: l == arg)] = new
else:
    start, end = leg(arg)
    cut = lines[start : end + 1]
    del lines[start : end + 1]
    if mode == "after":
        _, anchor_end = leg(new)
        lines[anchor_end + 1 : anchor_end + 1] = cut
doc.write_text("\n".join(lines))
(block,) = [b for b in extract_blocks(str(doc)) if b.sql.startswith("-- [§30 ")]
m = json.loads(manifest.read_text())
(entry,) = [e for e in m["blocks"] if e["label"].startswith("§30 ")]
entry["sha256"] = block.sha256
manifest.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
PY
}
check_s30() {  # name mode arg new test-selector — one §30 edit, re-hashed
  local name=$1 mode=$2 arg=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! edit_s30 "$mode" "$arg" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout HEAD -- "$DOC" "$MANIFEST"; return; fi
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout HEAD -- "$DOC" "$MANIFEST"
}

# Every leg, removed in turn from the final body. 076's six:
check_s30 "the lease leg is gone" drop "  UPDATE jobs SET state = 'ready', locked_by = NULL" "" "'$LEGS::test_the_lease_leg_re_readies_an_expired_lease'"
check_s30 "the slot leg is gone" drop "  UPDATE post_intents SET state = 'expired'" "" "'$LEGS::test_the_slot_leg_expires_a_story_past_its_slot'"
check_s30 "the approval leg is gone" drop "  UPDATE post_intents i SET state = 'expired'" "" "'$LEGS::test_the_approval_leg_expires_a_card_past_its_ttl'"
check_s30 "the lock leg is gone" drop "  DELETE FROM post_locks" "" "'$LEGS::test_the_lock_leg_deletes_an_expired_lock'"
check_s30 "the invitation leg is gone" drop "  UPDATE workspace_invitations SET state = 'expired'" "" "'$LEGS::test_the_invitation_leg_expires_a_pending_invitation'"
check_s30 "the onboarding leg is gone" drop "  DELETE FROM onboarding_sessions" "" "'$LEGS::test_the_onboarding_leg_deletes_an_expired_session'"
# 086's deadline leg (#1429), still the sweep's last:
check_s30 "the deadline leg is gone" drop "  WITH ended AS (" "" "'$LEGS::test_the_deadline_leg_ends_a_ready_job_past_its_deadline'"
# 087's cancel leg (#1235):
check_s30 "the cancel leg is gone" drop "  UPDATE post_intents SET state = 'cancelled'" "" "'$LEGS::test_the_cancel_leg_ends_a_flagged_waiting_story'"
# ...and each thing it must hold to. Its guards: the flag, the debit, all four waiting states.
check_s30 "the cancel leg ignores the flag" sub "                 WHERE cancel_requested AND cap_consumed_on IS NULL" "                 WHERE cap_consumed_on IS NULL" "'$CANCEL::test_an_unflagged_waiting_row_is_left_alone'"
check_s30 "the cancel leg takes a debited story" sub "                 WHERE cancel_requested AND cap_consumed_on IS NULL" "                 WHERE cancel_requested" "'$CANCEL::test_a_debited_story_is_left_for_the_path_that_refunds_it'"
check_s30 "the cancel leg forgets approved" sub "                   AND state IN ('scheduled','prompt_pending','awaiting_approval','approved')" "                   AND state IN ('scheduled','prompt_pending','awaiting_approval')" "'$CANCEL::test_a_flagged_row_in_each_waiting_state_ends_cancelled'"
# Its budget and its place ahead of the expiry legs:
check_s30 "the cancel leg ignores the budget" sub "                 LIMIT rem)" "                 )" "'$CANCEL::test_the_leg_draws_on_the_sweeps_one_budget'"
check_s30 "the cancel leg runs after the expiry legs" after "  UPDATE post_intents SET state = 'cancelled'" "  UPDATE post_intents i SET state = 'expired'" "'$CANCEL::test_a_flag_outranks_an_expiry_in_the_same_sweep'"
# The two cancellers of one story, in each order: the sweep rechecks a row the job changed under it,
# and the job takes a row the sweep already ended as its work done.
check_s30 "the cancel leg forgets a changed row" sub "     AND state IN ('scheduled','prompt_pending','awaiting_approval','approved');  -- rechecked on a changed row" "     ;" "'$CANCEL::test_a_story_the_job_cancels_under_the_sweep_does_not_fail_it'"
check "the job fails a story the sweep already cancelled" "$PIPELINE" '        if state != "cancelled":' '        if True:' "'$CANCEL::test_a_sweep_between_the_jobs_load_and_its_cancel_is_benign'"
# The file is held to the stream: a cancel leg dropped from 087 alone is caught by the prefix check.
check "the 087 file drifts from §30" "$MIGRATION" "                 WHERE cancel_requested AND cap_consumed_on IS NULL" "                 WHERE cancel_requested" "tests/scripts/test_advertised_ddl.py -k 'wired_prefix_holds_against_the_real_stream'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
