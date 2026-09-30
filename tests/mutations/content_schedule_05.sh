#!/bin/zsh
# Mutation battery for the content schedule's phase 5, a planned story made and moved through the
# command port and the CLI (#1413, plan PR #1414; F3, F4, F7 and F11): each behaviour has one named
# mutation that must make its test FAIL ("killed") — and must PASS on the clean tree first, or the
# verdict is BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a kill. Files
# are restored from the COMMITTED tree after each, so commit first, and run it in its own worktree
# (`STORYDUMP_ROOT`). The gates need a PostgreSQL: the DB_* fields are read from the environment,
# defaulting to the Docker server `AGENTS.md` › Testing starts; `STORYDUMP_PY` points at another
# venv's python.
#
# Phase 5 has no DDL, so every mutation here is Python: the executors, the vocabulary's F7 split,
# the Queue read, the name rule, the API's refusal body and token gate, and the CLI. A lock set's
# mutation is judged by a test that names its kinds, never by one parametrized over the mutated set
# (that test's case would vanish with the mutant). The account read's own `workspace_id` predicate
# is not mutated: the gates run as `svc_ingress`, whose policies hide another tenant's row anyway, so
# that mutant is equivalent here — `test_ops_views_gate.py`'s bypass arm is where predicates are
# proven without the policies.
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

EX=src/services/target/command_executors.py
VOC=src/services/target/vocabulary.py
WS=src/services/target/workspaces.py
S=tests/scripts/test_schedule_verbs_gate.py
CG=tests/scripts/test_cli_planning_gate.py
CU=tests/storydump_cli/test_planning.py
# An interrupted check must not leave a mutant behind: every file a check mutates is restored from
# the committed tree. Not under DRY=1, which mutates nothing: there the restore would only discard
# uncommitted work.
[ -n "${DRY:-}" ] || trap 'cd "$ROOT" && git checkout HEAD -- "$EX" "$VOC" "$WS" src/services/target/commands.py src/services/target/identity.py src/api/app.py src/api/principal.py src/api/routes/v1.py storydump_cli/commands/writes.py storydump_cli/commands/reads.py' EXIT INT TERM

# schedule_item: the account, the item, the lock and item rule (F7), the database's duplicate.
check "a removed destination is found" $EX "        \"   AND a.state IN ('active', 'reauth_required')\"," "        \"   AND a.state IN ('active', 'reauth_required', 'disabled', 'moved')\"," "$S -k 'only_a_live_account'"
check "an account awaiting reconnection is not found" $EX "        \"   AND a.state IN ('active', 'reauth_required')\"," "        \"   AND a.state IN ('active')\"," "$S -k 'only_a_live_account'"
check "an item that cannot post schedules" $EX "    blockers = [] if item[\"state\"] == \"available\" else [f\"item_{item['state']}\"]" "    blockers = []" "$S -k 'item_that_cannot_post'"
check "a reject lock no longer blocks" $VOC 'BLOCKING_LOCKS: tuple[str, ...] = ("reject", "unsupported", "hold", "seasonal")' 'BLOCKING_LOCKS: tuple[str, ...] = ("unsupported", "hold", "seasonal")' "$S -k 'blocker_and_a_warning'"
check "a skip lock is not in the way" $VOC 'WARNING_LOCKS: tuple[str, ...] = ("skip", "recent")' 'WARNING_LOCKS: tuple[str, ...] = ("recent",)' "$S -k 'blocker_and_a_warning'"
check "the override gets past a blocker" $EX "    if blockers or (warnings and not override):" "    if (blockers or warnings) and not override:" "$S -k 'blocking_lock_blocks_even or item_that_cannot_post'"
check "a warning needs no override" $EX "    if blockers or (warnings and not override):" "    if blockers:" "$S -k 'warning_lock_holds'"
check "an expired lock counts" $EX '"                AND (l.expires_at IS NULL OR l.expires_at > now())"' '"                AND true"' "$S -k 'expired_lock_do_not_count'"
check "another account's recent post counts" $EX '"                AND (l.ig_account_id IS NULL OR l.ig_account_id = :acct)"' '"                AND (l.ig_account_id IS NULL OR l.ig_account_id <> :acct OR l.ig_account_id = :acct)"' "$S -k 'expired_lock_do_not_count'"
check "the override is not audited" $EX '        detail["override"] = warnings' '        pass' "$S -k 'warning_lock_holds'"
check "the refusal says an override helps a blocker" $EX '"overridable": not blockers' '"overridable": True' "$S -k 'blocking_lock_blocks_even'"
check "the duplicate is a 500" $EX '        if constraint_violated(exc, "uq_intent_live_subject"):' '        if constraint_violated(exc, "uq_intent_slot"):' "$S -k 'same_item_waiting'"
check "the scheduler is not recorded" $EX "            \"         'planned', :by)\"" "            \"         'planned', NULL)\"" "$S -k 'born_planned_by_the_person'"
check "a planned story is born cadence" $EX "            \"         'planned', :by)\"" "            \"         'cadence', :by)\"" "$S -k 'born_planned_by_the_person'"
check "no bound chat goes unsaid" $EX '        warnings=[] if bound else [vocabulary.NO_PUSH_BINDING],' '        warnings=[],' "$S -k 'no_bound_chat'"
check "a member cannot schedule" src/services/target/commands.py '    "schedule_item": "member",' '    "schedule_item": "admin",' "$S -k 'member_schedules'"

# The wall time: Postgres's reading, a skipped time refused, the first occurrence, the window.
check "a skipped wall time has an instant" $EX '    "SELECT CASE WHEN (r.c AT TIME ZONE r.z) = r.l"' '    "SELECT CASE WHEN true"' "$S -k 'clocks_skip or skipped_wall_time'"
check "an ambiguous time is its second occurrence" $EX '"            THEN LEAST(r.c, CASE' '"            THEN GREATEST(r.c, CASE' "$S -k 'ambiguous'"
check "the past is accepted" $EX '    if at <= found["now"]:' '    if False and at <= found["now"]:' "$S -k 'past_and_now'"
check "the horizon is open" $EX '    if at > found["horizon"]:' '    if False and at > found["horizon"]:' "$S -k 'past_and_now'"
check "an offset is dropped, not refused" $EX '_LOCAL_AT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")' '_LOCAL_AT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?.*")' "$S -k 'offset_is_refused'"

# reschedule_item: the guarded UPDATE decides; the move is audited. cancel: audited on every story.
check "a served story moves" $EX "\"   AND state = 'scheduled' AND NOT cancel_requested\"" "\"   AND NOT cancel_requested\"" "$S -k 'served_story_no_longer_moves'"
check "a cadence story moves" $EX "\" WHERE id = :id AND workspace_id = :ws AND origin = 'planned'\"" "\" WHERE id = :id AND workspace_id = :ws\"" "$S -k 'cadence_story_does_not_move'"
check "a story being cancelled moves" $EX "\"   AND state = 'scheduled' AND NOT cancel_requested\"" "\"   AND state = 'scheduled'\"" "$S -k 'being_cancelled'"
check "the move is not audited" $EX '            "event": "rescheduled",' '            "event": "moved",' "$S -k 'moves_in_place'"
check "cancel is not audited" $EX "    # nothing for it: the request is recorded here, naming the person.
    await _audit_intent(" "    # nothing for it: the request is recorded here, naming the person.
    if False:
      await _audit_intent(" "$S -k 'every_cancel_leaves'"

# The Queue read: the origin filter, the zone, the miss reason, the order, the scheduler's name.
check "the origin filter is ignored" $WS '        where += " AND i.origin = :origin"' '        where += ""' "$S -k 'origin_filter'"
check "the zone is always the workspace's" $WS '" COALESCE(a.tz, w.tz) AS tz,"' '" w.tz AS tz,"' "$S -k 'zone_its_times'"
check "every error reads as a miss reason" $WS "f\" CASE WHEN i.last_error->>'class' = '{vocabulary.PLANNED_MISSED}'\"" "f\" CASE WHEN i.last_error IS NOT NULL\"" "$S -k 'missed_planned_story_says_why'"
check "newest first is ignored" $WS '    order = "DESC" if newest_first else "ASC"' '    order = "ASC"' "$S -k 'newest_first'"
check "a cadence story names a scheduler" $WS '" CASE WHEN i.scheduled_by_user_id IS NOT NULL"' '" CASE WHEN true"' "$S -k 'origin_filter_and_the_schedulers_name'"
check "the Telegram name is not preferred" src/services/target/identity.py "\" ORDER BY (ui.provider = 'telegram') DESC, ui.created_at LIMIT 1),\"" "\" ORDER BY (ui.provider = 'telegram') ASC, ui.created_at LIMIT 1),\"" "$S -k 'telegram_name_comes_first'"

# The API: the refusal body's facts, a token's read of the Queue.
check "a refusal's facts never reach the browser" src/api/app.py '    return {**exc.facts, **_reason_detail(exc, status)}' '    return _reason_detail(exc, status)' "tests/src/api/test_v1_routes.py -k 'facts_ride_its_body'"
check "a fact overwrites the reason" src/api/app.py '    return {**exc.facts, **_reason_detail(exc, status)}' '    return {**_reason_detail(exc, status), **exc.facts}' "tests/src/api/test_v1_routes.py -k 'facts_ride_its_body'"
check "a service identity reads another workspace" src/api/principal.py '        require_own_workspace(principal, workspace_id)' '        pass' "tests/src/api/test_ops_routes.py -k 'service_identity_reads_its_own'"
check "a token cannot read the Queue" src/api/routes/v1.py '    principal: Principal = Depends(current_principal),
    state: Optional[str] = Query(None),' '    principal: Principal = Depends(require_session),
    state: Optional[str] = Query(None),' "$CG -k 'read_but_cannot_plan'"

# The CLI: the handle, the keys, the refusal's words, the read's filter.
check "a handle is sent as the account id" storydump_cli/commands/writes.py '        return {"ig_account_id": account_id}' '        return {"ig_account_id": key}' "$CU -k 'handle_resolves'"
check "a schedule replays its first answer" storydump_cli/commands/writes.py '        key_for=_fresh_key("schedule"),' '        key_for=_story_key("schedule_item", item),' "$CU -k 'fresh_key or new_attempt'"
check "a reschedule replays its first answer" storydump_cli/commands/writes.py '        key_for=_fresh_key("reschedule"),' '        key_for=_story_key("reschedule_item", story),' "$CU -k 'reschedule_sends'"
check "a held-back item offers no override" storydump_cli/commands/writes.py '        if exc.facts.get("overridable") is True:' '        if False:' "$CU -k 'held_back'"
check "a refused time says only that it was refused" storydump_cli/commands/writes.py '            code=EXIT_REFUSED, reason="invalid_args", detail=exc.detail, fix=AT_FIX' '            code=EXIT_REFUSED, reason="invalid_args", detail="refused", fix=AT_FIX' "$CU -k 'which_rule'"
check "planned lists cadence stories too" storydump_cli/commands/reads.py 'client.intents(ws, origin="planned", states=states, limit=limit), ws' 'client.intents(ws, origin=None, states=states, limit=limit), ws' "$CU -k 'queue_read_filtered'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
