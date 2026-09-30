#!/bin/zsh
# Mutation battery for the content schedule's phase 3, planned stories served on time or missed out
# loud (migration 089; #1413, plan PR #1414): each behaviour has one named mutation that must make its
# test FAIL ("killed") — and must PASS on the clean tree first, or the verdict is BASELINE RED; a
# selector that selects nothing is NO TEST SELECTED, never a kill. Files are restored from the
# COMMITTED tree after each, so commit first, and run it in its own worktree (`STORYDUMP_ROOT`). The
# gates need a PostgreSQL: the DB_* fields are read from the environment, defaulting to the Docker
# server `AGENTS.md` › Testing starts; `STORYDUMP_PY` points at another venv's python.
#
# The behavioural SQL mutations edit `07` §32, not the 089 file: every gate here replays the
# ADVERTISED stream, so §32 is the SQL they run. The one 089 mutation is caught by the prefix check
# that holds the file to the stream, and the model mutation by the lane's parity with `create_all`.
# Every mutant changes behaviour: none rewrites a predicate the table's own CHECKs already decide.
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
EXPECTED=$(grep -cE '^check(_doc)? "' "$0")
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
anchored() {  # file old — DRY=1 reports whether the mutation's anchor matches exactly once
  OLD="$2" $PY -c 'import os,sys,pathlib; n = pathlib.Path(sys.argv[1]).read_text().count(os.environ["OLD"]); print(n); sys.exit(0 if n == 1 else 3)' "$1" > /dev/null
}
check() {  # name file old new test-selector
  local name=$1 file=$2 old=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if [ -n "${DRY:-}" ]; then anchored "$file" "$old" && echo "anchor ok: $name" || echo "ANCHOR NOT UNIQUE (bad): $name"; return; fi
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
MANIFEST=scripts/advertised_ddl_manifest.json
G=tests/scripts/test_planned_serve_gate.py
C=tests/scripts/test_scheduler_clock_gate.py
# An interrupted check must not leave a mutant behind, least of all a §32 edit with a manifest
# re-hashed to agree with it: every file a check mutates is restored from the committed tree. Not
# under DRY=1, which mutates nothing: there the restore would only discard uncommitted work.
[ -n "${DRY:-}" ] || trap 'cd "$ROOT" && git checkout HEAD -- "$DOC" "$MANIFEST" src/services/target/prompts.py src/worker.py src/services/target/provisioning.py src/services/target/work_loop.py scripts/migrations/089_planned_serve_and_misses.sql src/models/target/intent_ledger.py' EXIT INT TERM

# A §32 mutation changes the block's sha256, and the manifest ratchet then refuses to build the
# stream ("1 unclassified, 1 orphaned"): every gate would ERROR in its fixture instead of the named
# test deciding. So `check_doc` re-hashes §32's manifest entry after the edit — the stream builds
# with the mutated SQL — and restores both files after the verdict.
rehash() {
  $PY - "$DOC" "$MANIFEST" <<'PY'
import json, sys
from scripts.advertised_ddl import extract_blocks
doc, path = sys.argv[1], sys.argv[2]
(block,) = [b for b in extract_blocks(doc) if b.sql.startswith("-- [§32 ")]
manifest = json.load(open(path))
(entry,) = [e for e in manifest["blocks"] if e["label"].startswith("§32 ")]
entry["sha256"] = block.sha256
open(path, "w").write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
PY
}
check_doc() {  # name old new test-selector — a mutation of §32, re-hashed
  local name=$1 old=$2 new=$3 sel=$4
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if [ -n "${DRY:-}" ]; then anchored "$DOC" "$old" && echo "anchor ok: $name" || echo "ANCHOR NOT UNIQUE (bad): $name"; return; fi
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$DOC" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$DOC"; return; fi
  rehash
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$DOC" "$MANIFEST"
}

# The serve door (§32's fn_prompts_due): the flag, the window, and F7's servable predicate.
check_doc "the serve door serves a cancelled story" "     AND NOT i.cancel_requested
     AND w.state = 'active' AND NOT w.is_paused" "     AND w.state = 'active' AND NOT w.is_paused" "$G -k 'NeverServedWhileFlagged'"
check_doc "a planned story is served past its window" "          OR (i.schedule_slot_at > now() - p_late" "          OR (true" "$G -k 'past-the-window'"
check_doc "a caller that names no window is served planned stories" "          OR (i.schedule_slot_at > now() - p_late" "          OR (i.schedule_slot_at > now() - COALESCE(p_late, interval '1 day')" "$G -k 'names_no_window'"
check_doc "the one-argument call no longer resolves" "CREATE FUNCTION fn_prompts_due(p_limit int, p_late interval DEFAULT NULL)" "CREATE FUNCTION fn_prompts_due(p_limit int, p_late interval)" "$G -k 'names_no_window'"
check_doc "an unsupported item is served" "              AND m.state = 'available'" "              AND m.state <> 'removed'" "$G -k 'media-unsupported'"
check_doc "a removed account is served" "              AND a.state IN ('active', 'reauth_required')" "              AND a.state IN ('active', 'reauth_required', 'disabled', 'moved')" "$G -k 'account-disabled or account-moved'"
check_doc "an account awaiting reconnection is not served" "              AND a.state IN ('active', 'reauth_required')" "              AND a.state IN ('active')" "$G -k 'an-account-awaiting-reconnection'"
check_doc "a reject lock no longer blocks the serve" "i.media_item_id
                                 AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')" "i.media_item_id
                                 AND l.kind IN ('unsupported', 'hold', 'seasonal')" "$G -k 'reject-lock'"
check_doc "a skip lock blocks the serve" "i.media_item_id
                                 AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')" "i.media_item_id
                                 AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal', 'skip')" "$G -k 'a-skip-lock'"
check_doc "an expired lock blocks the serve" "                                 AND (l.expires_at IS NULL OR l.expires_at > now()))))" "                                 AND true)))" "$G -k 'an-expired-reject-lock'"
check_doc "a paused workspace's story is served" "     AND w.state = 'active' AND NOT w.is_paused
     AND (i.origin = 'cadence'" "     AND w.state = 'active'
     AND (i.origin = 'cadence'" "$G -k 'waits_out_the_pause'"
check_doc "a suspended workspace's story is served" "     AND w.state = 'active' AND NOT w.is_paused
     AND (i.origin = 'cadence'" "     AND NOT w.is_paused
     AND (i.origin = 'cadence'" "$G -k 'every_combination'"
check_doc "the window's edge is served" "          OR (i.schedule_slot_at > now() - p_late" "          OR (i.schedule_slot_at >= now() - p_late" "$G -k 'window_edge'"
# The miss door (§32's fn_planned_misses): the flag, each reason, their precedence, the window.
check_doc "a cancelled story is missed out loud" "             AND i.schedule_slot_at <= now() AND NOT i.cancel_requested) d" "             AND i.schedule_slot_at <= now()) d" "$G -k 'cancelled_planned_story'"
check_doc "a removed item is not a miss" "                   WHEN m.id IS NULL OR m.state = 'removed' THEN 'item_removed'" "                   WHEN m.id IS NULL THEN 'item_removed'" "$G -k 'media-removed'"
check_doc "an unsupported item is not a miss" "                   WHEN m.state = 'unsupported' THEN 'item_unsupported'" "                   WHEN false THEN 'item_unsupported'" "$G -k 'media-unsupported'"
check_doc "a hold lock is not a miss" "                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')" "                                   AND l.kind IN ('reject', 'unsupported', 'seasonal')" "$G -k 'hold-lock'"
check_doc "a removed account is not a miss" "                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')" "                   WHEN a.state IS NULL" "$G -k 'account-disabled'"
check_doc "the account outranks the lock" "                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
                                   AND l.ig_account_id IS NULL
                                   AND l.media_item_id = i.media_item_id
                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                   AND (l.expires_at IS NULL OR l.expires_at > now()))
                     THEN 'item_locked'
                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')
                     THEN 'account_removed'" "                   WHEN a.state IS NULL OR a.state NOT IN ('active', 'reauth_required')
                     THEN 'account_removed'
                   WHEN EXISTS (SELECT 1 FROM post_locks l
                                 WHERE l.workspace_id = i.workspace_id
                                   AND l.ig_account_id IS NULL
                                   AND l.media_item_id = i.media_item_id
                                   AND l.kind IN ('reject', 'unsupported', 'hold', 'seasonal')
                                   AND (l.expires_at IS NULL OR l.expires_at > now()))
                     THEN 'item_locked'" "$G -k 'reasons_come_in_their_precedence'"
check_doc "a paused workspace's miss says late" "                   WHEN w.state <> 'active' OR w.is_paused THEN 'paused'" "                   WHEN false THEN 'paused'" "$G -k 'paused_through_the_window'"
check_doc "a story inside its window is missed during a pause" "                   WHEN i.schedule_slot_at > now() - p_late THEN NULL" "                   WHEN false THEN NULL" "$G -k 'waits_out_the_pause'"
check_doc "the miss door counts an expired lock" "                                   AND (l.expires_at IS NULL OR l.expires_at > now()))" "                                   AND true)" "$G -k 'every_combination'"
check_doc "a suspended workspace's miss says late" "                   WHEN w.state <> 'active' OR w.is_paused THEN 'paused'" "                   WHEN w.is_paused THEN 'paused'" "$G -k 'every_combination'"
check_doc "the window's edge is not missed" "                   WHEN i.schedule_slot_at > now() - p_late THEN NULL" "                   WHEN i.schedule_slot_at >= now() - p_late THEN NULL" "$G -k 'window_edge'"
check_doc "a NULL window misses everything" "LANGUAGE sql STABLE STRICT SECURITY DEFINER" "LANGUAGE sql STABLE SECURITY DEFINER" "$G -k 'null_window_lists_no_miss'"
# The reaper's slot expiry (§32's fn_reaper_sweep) and the slot key's contract.
check_doc "the reaper expires a planned story in silence" "                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()
                   AND origin = 'cadence'" "                 WHERE state IN ('scheduled','prompt_pending') AND schedule_slot_at < now()" "$G -k 'ReaperLeavesPlannedStories'"
check_doc "the reaper's slot expiry skips cadence rows" "                   AND origin = 'cadence'
                 LIMIT rem);" "                   AND origin = 'planned'
                 LIMIT rem);" "$G -k 'slot_expiry_ends_a_cadence_story'"
check_doc "the unconditional slot key survives" "DROP INDEX uq_intent_slot;

ALTER INDEX uq_intent_slot_cadence RENAME TO uq_intent_slot;

CREATE OR REPLACE FUNCTION fn_reaper_sweep" "CREATE OR REPLACE FUNCTION fn_reaper_sweep" "$C -k 'holds_on_the_cadence_key_alone'"
# The worker's legs, in Python.
check "the miss notice reaches nobody" src/services/target/prompts.py "                    surfaces[ws] = await push_bindings(session, ws)" "                    surfaces[ws] = []" "$G -k 'media-removed'"
check "a miss nobody heard is counted heard" src/services/target/prompts.py '            counts["unheard"] += 1' '            pass' "$G -k 'nobody_can_hear'"
check "the card forgets its scheduler" src/services/target/prompts.py "    if planned:
        slot_line += f\"\\n{planned}\"" "    if False:
        slot_line += f\"\\n{planned}\"" "$G -k 'names_who_scheduled_it'"
check "every planned card says served late" src/services/target/prompts.py "    if (now or utcnow()) - slot >= SERVED_LATE_AFTER:" "    if True:" "$G -k 'names_who_scheduled_it'"
check "no planned card ever says served late" src/services/target/prompts.py "    if (now or utcnow()) - slot >= SERVED_LATE_AFTER:" "    if False:" "$G -k 'waits_out_the_pause'"
check "the send-time card forgets its scheduler" src/services/target/prompts.py "    return _card_for(await _with_scheduler(session, row), intent_id=intent_id)" "    return _card_for(row, intent_id=intent_id)" "$G -k 'rendered_again_at_send_time'"
check "the sweeper skips the miss leg" src/worker.py "                    misses = await prompts_mod.sweep_planned_misses(" "                    misses = {\"missed\": 0, \"unheard\": 0} or prompts_mod.sweep_planned_misses(" "tests/src/test_worker.py -k 'PromptSweeperConsumesTheSweep'"
check "the removal tells nobody" src/services/target/provisioning.py "    if unserved:
        await prompts.say_removed_before_served(" "    if False:
        await prompts.say_removed_before_served(" "$G -k 'RemovingADestination'"
check "the removal tells about served stories too" src/services/target/provisioning.py '        if row["origin"] == "planned" and row["state"] == "scheduled"' '        if row["origin"] == "planned"' "$G -k 'RemovingADestination'"
check "the late window is not the owner's hour" src/services/target/work_loop.py "    planned_late_seconds: int = 3600" "    planned_late_seconds: int = 900" "tests/src/test_worker.py -k 'late_window_is_the_hour'"
# The removal's notice reads the row the miss door's notice is written from.
check "the removal's notice forgets the account" src/services/target/prompts.py '    "       m.file_name, a.handle, w.tz"' '    "       m.file_name, NULL AS handle, w.tz"' "$G -k 'RemovingADestination'"
# The file and the model are held to the stream. Parity compares uniqueness SEMANTICS, not index
# names, so renaming the model's index would be an equivalent mutant; these mutate what it compares.
check "the 089 file drifts from §32" scripts/migrations/089_planned_serve_and_misses.sql "LANGUAGE sql STABLE STRICT SECURITY DEFINER" "LANGUAGE sql STABLE SECURITY DEFINER" "tests/scripts/test_advertised_ddl.py -k 'wired_prefix_holds_against_the_real_stream'"
check "the model's slot key covers planned rows" src/models/target/intent_ledger.py "            postgresql_where=text(\"origin = 'cadence'\")," "            postgresql_where=text(\"origin = 'planned'\")," "tests/scripts/test_lineage_lane.py -k 'lane_parity_holds_against_the_target_models'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
