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
# the Queue read, the name rule, the API's refusal body and token gate, the CLI, the prompt sweeps'
# re-read of a due story, and what a card and the burst view take for a move. A rule that is a
# statement's shape (a predicate, a bound parameter, a row lock) is judged by the unit tests, which
# assert it; where a unit test pins what a gate already pins, the gate's check stands for both, and
# a check of its own marks what only the unit can see. A lock set's mutation is judged by a test
# that names its kinds, never by one parametrized over the mutated set (that test's case would vanish
# with the mutant). The port's tenant predicates (`workspace_id = :ws`) are judged at the SQL seam:
# the gates run as `svc_ingress`, whose policies hide another tenant's row anyway, so at the gate
# those mutants would be equivalent.
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
PE=tests/src/services/target/test_planning_executors.py
OV=tests/scripts/test_ops_views_gate.py
VT=tests/src/services/target/test_vocabulary.py
# An interrupted check must not leave a mutant behind: every file a check mutates is restored from
# the committed tree. Not under DRY=1, which mutates nothing: there the restore would only discard
# uncommitted work.
[ -n "${DRY:-}" ] || trap 'cd "$ROOT" && git checkout HEAD -- "$EX" "$VOC" "$WS" src/services/target/commands.py src/services/target/identity.py src/api/app.py src/api/principal.py src/api/routes/v1.py src/services/target/prompts.py src/services/target/intent_ledger.py storydump_cli/commands/writes.py storydump_cli/commands/reads.py storydump_cli/output.py storydump_cli/client.py src/services/target/ops_views.py' EXIT INT TERM

# schedule_item: the account, the item, the lock and item rule (F7), the database's duplicate.
check "a removed destination is found" $VOC 'LIVE_ACCOUNT_STATES: tuple[str, ...] = ("active", "reauth_required")' 'LIVE_ACCOUNT_STATES: tuple[str, ...] = ("active", "reauth_required", "disabled", "moved")' "$S -k 'only_a_live_account'"
check "an account awaiting reconnection is not found" $VOC 'LIVE_ACCOUNT_STATES: tuple[str, ...] = ("active", "reauth_required")' 'LIVE_ACCOUNT_STATES: tuple[str, ...] = ("active",)' "$S -k 'only_a_live_account'"
check "a missing account is not named" $EX '"not_found", f"account {account_id}", facts={"missing": "account"}' '"not_found", f"account {account_id}", facts={}' "$S -k 'only_a_live_account'"
check "an item that cannot post schedules" $EX "    blockers = [] if item[\"state\"] == \"available\" else [f\"item_{item['state']}\"]" "    blockers = []" "$S -k 'item_that_cannot_post'"
check "a reject lock no longer blocks" $VOC 'BLOCKING_LOCKS: tuple[str, ...] = ("reject", "unsupported", "hold", "seasonal")' 'BLOCKING_LOCKS: tuple[str, ...] = ("unsupported", "hold", "seasonal")' "$S -k 'blocker_and_a_warning'"
check "a skip lock is not in the way" $VOC 'WARNING_LOCKS: tuple[str, ...] = ("skip", "recent")' 'WARNING_LOCKS: tuple[str, ...] = ("recent",)' "$S -k 'blocker_and_a_warning'"
check "the override gets past a blocker" $EX "    if blockers or (warnings and not override):" "    if (blockers or warnings) and not override:" "$S -k 'blocking_lock_blocks_even or item_that_cannot_post'"
check "a warning needs no override" $EX "    if blockers or (warnings and not override):" "    if blockers:" "$S -k 'warning_lock_holds'"
check "an expired lock counts" $EX '"                AND (l.expires_at IS NULL OR l.expires_at > now())"' '"                AND true"' "$S -k 'expired_lock_do_not_count'"
check "another account's recent post counts" $EX '"                AND (l.ig_account_id IS NULL OR l.ig_account_id = :acct)"' '"                AND (l.ig_account_id IS NULL OR l.ig_account_id <> :acct OR l.ig_account_id = :acct)"' "$S -k 'expired_lock_do_not_count'"
check "the override is not audited" $EX '        detail["override"] = warnings' '        pass' "$S -k 'warning_lock_holds'"
check "the refusal says an override helps a blocker" $EX '"overridable": not blockers' '"overridable": True' "$S -k 'blocking_lock_blocks_even'"
check "the duplicate is a 500" $EX '        " ON CONFLICT (workspace_id, media_item_id, ig_account_id)"
        f" WHERE {intent_ledger.NOT_TERMINAL} DO NOTHING RETURNING id, workspace_id",' '        " RETURNING id, workspace_id",' "$S -k 'same_item_waiting'"
check "the duplicate names no story" $EX '            if existing
            else {},' '            if False
            else {},' "$S -k 'same_item_waiting or cadence_story_waiting'"
check "the story in the way may be one that ended (gate)" $EX '            f"   AND ig_account_id = :acct AND {intent_ledger.NOT_TERMINAL}",' '            "   AND ig_account_id = :acct",' "$S -k 'live_one_never_an_ended_one'"
check "a story being cancelled is not said" $EX '                    "cancel_requested": existing["cancel_requested"],' '' "$S -k 'being_cancelled_still_holds'"
check "a story being cancelled is not said (unit)" $EX '                    "cancel_requested": existing["cancel_requested"],' '' "$PE -k 'duplicate_is_the_databases'"
check "the story in the way may be one that ended" $EX '            f"   AND ig_account_id = :acct AND {intent_ledger.NOT_TERMINAL}",' '            "   AND ig_account_id = :acct",' "$PE -k 'duplicate_is_the_databases'"
check "the scheduler is not recorded" $EX "        \"         'planned', :by)\"" "        \"         'planned', NULL)\"" "$S -k 'born_planned_by_the_person'"
check "a planned story is born cadence" $EX "        \"         'planned', :by)\"" "        \"         'cadence', :by)\"" "$S -k 'born_planned_by_the_person'"
check "no bound chat goes unsaid" $EX '        warnings=[] if bound else [vocabulary.NO_PUSH_BINDING],' '        warnings=[],' "$S -k 'no_bound_chat'"
check "a member cannot schedule" src/services/target/commands.py '    "schedule_item": "member",' '    "schedule_item": "admin",' "$S -k 'member_schedules'"
check "the account is read in any workspace" $EX '        " WHERE a.id = :acct AND a.workspace_id = :ws"' '        " WHERE a.id = :acct"' "$PE -k 'inserted_under_the_person'"
check "the item is read in any workspace" $EX '        "  FROM media_items m WHERE m.id = :media AND m.workspace_id = :ws",' '        "  FROM media_items m WHERE m.id = :media",' "$PE -k 'inserted_under_the_person'"
check "the story in the way is read in any workspace" $EX '            " WHERE workspace_id = :ws AND media_item_id = :media"' '            " WHERE media_item_id = :media"' "$PE -k 'duplicate_is_the_databases'"
check "a removal slips between the account read and the story" $EX '        " FOR SHARE OF a",' '        "",' "$PE -k 'inserted_under_the_person'"
check "a new story is born unaudited" $EX '    await _audit_intent(
        session, born, from_state=None, to_state="scheduled", detail=detail
    )' '    pass' "$S -k 'the_audit_row_names_the_person'"

# The wall time: Postgres's reading, a skipped time refused, the first occurrence, the window.
check "a skipped wall time has an instant" $EX '    "SELECT min(v.at) FILTER (WHERE v.at AT TIME ZONE p.z = p.l) AS at,"' '    "SELECT min(v.at) FILTER (WHERE true) AS at,"' "$S -k 'clocks_skip or skipped_wall_time'"
check "an ambiguous time is its second occurrence" $EX '    "SELECT min(v.at) FILTER (WHERE v.at AT TIME ZONE p.z = p.l) AS at,"' '    "SELECT max(v.at) FILTER (WHERE v.at AT TIME ZONE p.z = p.l) AS at,"' "$S -k 'ambiguous'"
check "a skipped time does not say so" $EX '            facts={"at_rule": "skipped"},' '            facts={},' "$S -k 'skipped_wall_time'"
check "the past is accepted" $EX '    if at <= found["now"]:' '    if False and at <= found["now"]:' "$S -k 'past_and_now'"
check "the horizon is open" $EX '    if at > found["horizon"]:' '    if False and at > found["horizon"]:' "$S -k 'past_and_now'"
check "an offset is dropped, not refused" $EX '_LOCAL_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?")' '_LOCAL_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?.*")' "$S -k 'offset_is_refused'"
check "another script's digits are the shape" $EX '_LOCAL_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?")' '_LOCAL_AT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")' "$PE -k 'anything_else_names_the_rule'"

# reschedule_item: the row read under its lock decides; the move is audited. cancel: audited on every
# story. The sweeps serve or miss a planned story only at the time they read.
check "a served story moves" $EX '    if intent["origin"] != "planned" or intent["state"] != "scheduled":' '    if intent["origin"] != "planned":' "$S -k 'served_story_no_longer_moves'"
check "a cadence story moves" $EX '    if intent["origin"] != "planned" or intent["state"] != "scheduled":' '    if intent["state"] != "scheduled":' "$S -k 'cadence_story_does_not_move'"
check "a story being cancelled moves" $EX '    _refuse_if_cancelling(intent)
    local_at = _local_at(command)' '    local_at = _local_at(command)' "$S -k 'being_cancelled'"
check "the time is judged before the story" $EX '    intent = await _intent_row(session, command)
    if intent["origin"] != "planned" or intent["state"] != "scheduled":' '    intent = await _intent_row(session, command)
    _local_at(command)
    if intent["origin"] != "planned" or intent["state"] != "scheduled":' "$S -k 'cadence_story_does_not_move'"
check "the time is judged before the story (unit)" $EX '    intent = await _intent_row(session, command)
    if intent["origin"] != "planned" or intent["state"] != "scheduled":' '    intent = await _intent_row(session, command)
    _local_at(command)
    if intent["origin"] != "planned" or intent["state"] != "scheduled":' "$PE -k 'judged_before_its_new_time'"
check "a malformed story id is a 500" $EX '    intent_id = _id_arg(command, "intent_id")' '    intent_id = _arg(command, "intent_id")' "$S -k 'malformed_or_foreign_story_id or any_spelling'"
check "the move is not audited" $EX '            "event": "rescheduled",' '            "event": "moved",' "$S -k 'moves_in_place'"
check "a reschedule moves nothing" $EX '            "UPDATE post_intents SET schedule_slot_at = :at"' '            "UPDATE post_intents SET schedule_slot_at = schedule_slot_at"' "$S -k 'time_moves_in_place'"
check "cancel's flag names no tenant" $EX '            "UPDATE post_intents SET cancel_requested = true"
            " WHERE id = :id AND workspace_id = :ws"' '            "UPDATE post_intents SET cancel_requested = true"
            " WHERE id = :id"' "$PE -k 'flag_is_bound_to_the_tenant'"
check "cancel is not audited" $EX "    # nothing for it: the request is recorded here, naming the person.
    await _audit_intent(" "    # nothing for it: the request is recorded here, naming the person.
    if False:
      await _audit_intent(" "$S -k 'every_cancel_leaves'"

# The sweeps serve or miss a planned story only at the time they read; a card's "who last moved it"
# skips the rows that moved nothing.
PR=src/services/target/prompts.py
TP=tests/src/services/target/test_prompts.py
check "the serve sweep serves a story changed under it" $PR '        if not await _still_due(session, row):' '        if False:' "$S -k 'serve_sweep_leaves or another_sweep_served'"
check "the serve re-read takes no lock" $PR '        " WHERE id = :id AND workspace_id = :ws FOR UPDATE",' '        " WHERE id = :id AND workspace_id = :ws",' "$S -k 'serve_sweep_leaves_a_story_moved'"
check "the serve re-read takes no lock (unit)" $PR '        " WHERE id = :id AND workspace_id = :ws FOR UPDATE",' '        " WHERE id = :id AND workspace_id = :ws",' "$TP -k 'as_the_door_read_it_is_served'"
check "the serve re-read names no tenant" $PR '        " WHERE id = :id AND workspace_id = :ws FOR UPDATE",' '        " WHERE id = :id FOR UPDATE",' "$TP -k 'as_the_door_read_it_is_served'"
check "the serve re-read ignores the time" $PR '        and found["schedule_slot_at"] == row["schedule_slot_at"]' '        and True' "$S -k 'serve_sweep_leaves_a_story_moved'"
check "the serve re-read ignores the time (unit)" $PR '        and found["schedule_slot_at"] == row["schedule_slot_at"]' '        and True' "$TP -k 'changed_since_the_door_read_it and moved'"
check "the serve re-read ignores the cancel flag" $PR '        and not found["cancel_requested"]' '        and True' "$S -k 'serve_sweep_leaves_a_story_flagged'"
check "the serve re-read ignores the cancel flag (unit)" $PR '        and not found["cancel_requested"]' '        and True' "$TP -k 'changed_since_the_door_read_it and flagged'"
check "the serve re-read ignores the state" $PR '        and found["state"] == row["state"]' '        and True' "$S -k 'another_sweep_served'"
check "the serve re-read ignores the state (unit)" $PR '        and found["state"] == row["state"]' '        and True' "$TP -k 'changed_since_the_door_read_it and served_by_another_sweep'"
check "the serve sweep serves nothing" $PR '    return (
        found is not None' '    return False and (
        found is not None' "$S -k 'serve_sweep'"
check "the serve sweep serves nothing (unit)" $PR '    return (
        found is not None' '    return False and (
        found is not None' "$TP -k 'as_the_door_read_it_is_served'"
check "a story left to its next reading goes unsaid" $PR '            logger.info("prompt sweep: intent %s changed under the sweep", row["id"])' '            pass' "$TP -k 'changed_since_the_door_read_it and moved'"
check "the miss sweep misses a moved story (unit)" $PR '                            "   AND schedule_slot_at = :slot RETURNING id"' '                            "   RETURNING id"' "$TP -k 'each_miss_is_ended'"
check "the miss sweep misses a moved story" $PR '                            "   AND schedule_slot_at = :slot RETURNING id"' '                            "   RETURNING id"' "$S -k 'miss_sweep_leaves'"
check "the sweeps take tied rows in the order they came" $PR '    return (str(row["workspace_id"]), row["schedule_slot_at"], str(row["id"]))' '    return (str(row["workspace_id"]), row["schedule_slot_at"])' "$TP -k 'tied_rows_by_id'"
check "a cancel request is who last moved it" src/services/target/intent_ledger.py "                    f\"       AND {audit.moved('e')}\"" '                    ""' "$S -k 'who_only_asked_for_a_cancel'"
check "a cancel request is a tap" src/services/target/ops_views.py "    f\"   AND {audit.moved('a')}\"," '    "",' "$OV -k 'only_this_workspaces_rows'"
check "a cancel request is a tap (unit)" src/services/target/ops_views.py "    f\"   AND {audit.moved('a')}\"," '    "",' "tests/src/services/target/test_ops_views.py -k 'tap_is_a_move'"

# The Queue read: the origin filter, the zone, the miss reason, the order, the scheduler's name.
check "the origin filter is ignored" $WS '        where += " AND i.origin = :origin"' '        where += ""' "$S -k 'origin_filter'"
check "the zone is always the workspace's" $WS '" COALESCE(a.tz, w.tz) AS tz,"' '" w.tz AS tz,"' "$S -k 'zone_its_times'"
check "every error reads as a miss reason" $WS "f\" CASE WHEN i.last_error->>'class' = '{vocabulary.PLANNED_MISSED}'\"" "f\" CASE WHEN i.last_error IS NOT NULL\"" "$S -k 'missed_planned_story_says_why'"
check "newest first is ignored" $WS '    order = "DESC" if newest_first else "ASC"' '    order = "ASC"' "$S -k 'newest_first'"
check "the tie-break runs one way only" $WS '        f" ORDER BY i.schedule_slot_at {order}, i.id {order} LIMIT :lim",' '        f" ORDER BY i.schedule_slot_at {order}, i.id ASC LIMIT :lim",' "$S -k 'one_instant_orders'"
check "the tie-break runs one way only (unit)" $WS '        f" ORDER BY i.schedule_slot_at {order}, i.id {order} LIMIT :lim",' '        f" ORDER BY i.schedule_slot_at {order}, i.id ASC LIMIT :lim",' "$PE -k 'origin_the_states_and_the_order'"
check "a cadence story names a scheduler" $WS '" CASE WHEN i.scheduled_by_user_id IS NOT NULL"' '" CASE WHEN true"' "$S -k 'origin_filter_and_the_schedulers_name'"
check "an empty name is a name" src/services/target/identity.py "\"   AND ui.display_name IS NOT NULL AND ui.display_name <> ''\"" "\"   AND ui.display_name IS NOT NULL\"" "$S -k 'no_name_is_a_teammate'"
check "the Telegram name is not preferred" src/services/target/identity.py "\" ORDER BY (ui.provider = 'telegram') DESC, ui.created_at LIMIT 1),\"" "\" ORDER BY (ui.provider = 'telegram') ASC, ui.created_at LIMIT 1),\"" "$S -k 'telegram_name_comes_first'"

# The API: the refusal body's facts, a token's read of the Queue.
check "a refusal's facts never reach the browser" src/api/app.py '    if exc.facts:
        body["facts"] = exc.facts' '    if False:
        body["facts"] = exc.facts' "tests/src/api/test_v1_routes.py -k 'facts_ride_its_body'"
check "every refusal grows a facts key" src/api/app.py '    if exc.facts:
        body["facts"] = exc.facts' '    if True:
        body["facts"] = exc.facts' "tests/src/api/test_v1_routes.py -k 'each_port_refusal_maps'"
check "a service identity reads another workspace" src/api/principal.py '        require_own_workspace(principal, workspace_id)' '        pass' "tests/src/api/test_ops_routes.py -k 'service_identity_reads_its_own'"
check "a token cannot read the Queue" src/api/routes/v1.py '    principal: Principal = Depends(current_principal),
    state: Optional[str] = Query(None),' '    principal: Principal = Depends(require_session),
    state: Optional[str] = Query(None),' "$CG -k 'read_but_cannot_plan'"
check "a token cannot read the Queue (the route gate)" src/api/routes/v1.py '    principal: Principal = Depends(current_principal),
    state: Optional[str] = Query(None),' '    principal: Principal = Depends(require_session),
    state: Optional[str] = Query(None),' "$OV -k 'admit_tokens'"

# The CLI: the handle, the keys, the refusal's words, the read's filter.
check "a handle takes a removed account" storydump_cli/commands/writes.py '        and ("state" not in row or row["state"] in LIVE_ACCOUNT_STATES)' '        and True' "$CU -k 'one_live_account'"
check "a handle on an older API finds no account" storydump_cli/commands/writes.py '        and ("state" not in row or row["state"] in LIVE_ACCOUNT_STATES)' '        and row.get("state") in LIVE_ACCOUNT_STATES' "$CU -k 'older_than_the_cli'"
check "the account view drops the account's state" src/services/target/ops_views.py '    "SELECT a.workspace_id, a.id, a.handle, a.state,"' '    "SELECT a.workspace_id, a.id, a.handle,"' "$OV -k 'only_this_workspaces_rows'"
check "a blank account is looked up" storydump_cli/commands/writes.py '    if not account.strip():' '    if False:' "$CU -k 'blank_account'"
check "the story in the way is not named" storydump_cli/commands/writes.py '        if isinstance(existing, dict) and existing.get("intent_id"):' '        if False:' "$CU -k 'names_the_story_in_the_way'"
check "a cadence story in the way is called the person's" storydump_cli/commands/writes.py '            if existing.get("origin") == "planned":' '            if True:' "$CU -k 'names_the_story_in_the_way'"
check "a handle naming two live accounts takes the first" storydump_cli/commands/writes.py '    if len(live) > 1:' '    if False:' "$CU -k 'two_live_accounts'"
check "a not_found names nothing" storydump_cli/commands/writes.py '    if exc.reason == "not_found" and exc.facts.get("missing") in MISSING_SENTENCES:' '    if False:' "$CU -k 'names_what_is_missing'"
check "a missing item points at the account view" storydump_cli/commands/writes.py "    \"item\": \"check the item's id: storydump story <story> shows a story's item as media\"," '    "item": "storydump account <handle> shows an account",' "$CU -k 'names_what_is_missing'"
check "a refused write exits with the verb's own code" storydump_cli/commands/writes.py '                code=exit_code_for(exc.status, exc.reason),' '                code=EXIT_USAGE,' "$CU -k 'held_back'"
check "a schedule replays its first answer" storydump_cli/commands/writes.py '        key_for=_fresh_key("schedule"),' '        key_for=_story_key("schedule_item", item),' "$CU -k 'fresh_key or new_attempt'"
check "a reschedule replays its first answer" storydump_cli/commands/writes.py '        key_for=_fresh_key("reschedule"),' '        key_for=_story_key("reschedule_item", story),' "$CU -k 'reschedule_sends'"
check "a held-back item offers no override" storydump_cli/commands/writes.py '        if exc.facts.get("overridable") is True:' '        if False:' "$CU -k 'held_back'"
check "the override is sent unasked" storydump_cli/commands/writes.py '    if override_locks:' '    if True:' "$CU -k 'override_locks_is_sent_only_when_asked'"
check "a refused time says only that it was refused" storydump_cli/commands/writes.py '    if exc.reason == "invalid_args" and rule in AT_RULE_SENTENCES:' '    if False:' "$CU -k 'which_rule'"
check "planned lists cadence stories too" storydump_cli/commands/reads.py '                origin="planned",' '                origin=None,' "$CU -k 'queue_read_filtered'"
check "planned reads a history oldest first" storydump_cli/client.py '        if newest_first:
            params["order"] = "desc"' '        if False:
            params["order"] = "desc"' "$CU -k 'newest_first_when_asked'"
check "the due time is UTC" storydump_cli/output.py '    ("due", _planned_due),' '    ("due", "schedule_slot_at"),' "$CU -k 'queue_read_filtered'"
check "a full page reads as the whole list" storydump_cli/output.py '        if isinstance(limit, int) and len(_dicts(entry.get("rows"))) >= limit:' '        if False:' "$CU -k 'full_page'"
check "a full page at the most one read returns says to raise it" storydump_cli/output.py '                if limit < LIST_LIMIT_MAX' '                if True' "$CU -k 'most_one_read_returns'"
check "a zone that cannot be read breaks the list" storydump_cli/output.py '    except (ValueError, OSError, ZoneInfoNotFoundError):' '    except (ValueError, ZoneInfoNotFoundError):' "$CU -k 'zone_cannot_be_read'"
check "a missed story does not say why" storydump_cli/output.py '    if row.get("miss_reason"):' '    if False:' "$CU -k 'missed_at_its_time_says_why'"
check "seconds are dropped" storydump_cli/output.py '    clock = "%H:%M:%S" if local.second else "%H:%M"' '    clock = "%H:%M"' "$CU -k 'seconds_shows_them'"
check "a time with no zone is read in the terminal's own" storydump_cli/output.py '        if instant.tzinfo is None:
            return at' '        if False:
            return at' "$CU -k 'names_no_instant'"
check "an account's state is not shown" storydump_cli/output.py "        console.print(f\"    state     {_cell(row.get('state'))}\")" '        pass' "tests/storydump_cli/test_reads.py -k 'account_by_handle_json_and_human'"
check "a story being cancelled reads as one that waits" storydump_cli/commands/writes.py '            if existing.get("cancel_requested"):' '            if False:' "$CU -k 'being_cancelled_says_to_wait'"
check "a cancel that landed reads as still landing" storydump_cli/output.py '    if row.get("cancel_requested") and state not in TERMINAL_STATES:' '    if row.get("cancel_requested"):' "$CU -k 'cancel_that_landed'"
check "the page size is dropped" storydump_cli/commands/reads.py '    if isinstance(data.get("limit"), int):' '    if False:' "$CU -k 'full_page or bounds_the_page'"

# The vocabulary: every fact a refusal carries keeps its words (the CLI's tests are parametrized over
# these tables, so their own cases would vanish with the key).
check "a time rule loses its words" $VOC "    \"skipped\": \"that time does not happen in the account's zone: the clocks skip it\"," "" "$VT -k 'planned_story_is_refused_with'"
check "an item state loses its words" $VOC '    "item_removed": "it was removed from the library",' '' "$VT -k 'planned_story_is_refused_with'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
