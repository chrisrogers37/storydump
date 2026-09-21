#!/bin/zsh
# Mutation battery for the worker's doors (082, #751 part 2): each behaviour has one named mutation that
# must make its test FAIL ("killed") — and must PASS on the clean tree first, or the verdict is
# BASELINE RED, not a kill; a selector that selects nothing is NO TEST SELECTED, never a kill. Files
# are restored from the COMMITTED tree after each, so commit first. Needs the Docker test Postgres
# (the DB recipe): the killers replay the advertised stream and run the sweeps as svc_worker.
# `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
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
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

GATE=tests/scripts/test_worker_login_gate.py
LANE="tests/scripts/test_lineage_lane.py -k one_run_applies_the_whole_corpus"

# The reads through the doors: each read emptied blinds BOTH logins — the owner control kills, the worker's assertion with it.
check "the sender sweep stops minting (the owner control is what kills)" src/services/target/work_loop.py 'text("SELECT fn_sender_sweep(:prefix, :attempts, :deadline, :age, :lim)"),' 'text("SELECT (SELECT count(*) FROM channel_bindings b WHERE b.state = :prefix)"),' "$GATE -k sender_sweep_mints"
check "the prompt sweep reads no due story (the owner control is what kills)" src/services/target/prompts.py '"  FROM fn_prompts_due(:lim)"' '"  FROM fn_prompts_due(:lim) WHERE false"' "$GATE -k prompt_sweep_prompts"
check "the settled-card sweep selects no card (the owner control is what kills)" src/services/target/prompts.py '"  FROM fn_settled_cards(CAST(:terminal AS text[]), :lim)"' '"  FROM fn_settled_cards(CAST(:terminal AS text[]), :lim) WHERE false"' "$GATE -k settled_card_sweep"
check "the stranded alert selects nothing (the owner control is what kills)" src/services/target/media_sync.py '"  FROM fn_stranded_sources(:age, :lim)"' '"  FROM fn_stranded_sources(:age, :lim) WHERE false"' "$GATE -k stranded_alert_finds"
# The writes under the workspace's tenant: each claim forgotten is a write the policy refuses (or a read that sees nothing).
check "the prompt sweep forgets to claim the workspace before its writes" src/services/target/prompts.py '        ws = str(row["workspace_id"])
        await claims.claim(ws)
        if ws not in bindings_by_workspace:' '        ws = str(row["workspace_id"])
        if ws not in bindings_by_workspace:' "$GATE -k prompt_sweep_prompts"
check "the prompt sweep's advance phase forgets to claim the workspace" src/services/target/prompts.py '        await claims.claim(str(row["workspace_id"]))
        # A refusal is a Postgres check_violation, which aborts the' '        # A refusal is a Postgres check_violation, which aborts the' "$GATE -k prompt_sweep_prompts"
check "the settled-card sweep forgets to claim the workspace before its writes" src/services/target/prompts.py '    for row in rows:
        await claims.claim(str(row["workspace_id"]))
' '    for row in rows:
' "$GATE -k settled_card_sweep"
check "the stranded alert forgets to claim the workspace before its stamp" src/services/target/media_sync.py '        await claims.claim(workspace_id)
        stamped = (' '        stamped = (' "$GATE -k stranded_alert_finds"
check "the reconciler's poll forgets to claim the workspace before its read" src/worker.py '            await unit_of_work.apply_gucs(
                session, tenant_id=str(workspace_id), actor_kind="system"
            )
            row = (' '            row = (' "$GATE -k reconciler_poll_reaches"
# The caller's scope: a prompt sweep that keeps the last workspace it prompted fences the tenant job that ran it.
check "the prompt sweep keeps the last workspace it prompted" src/services/target/prompts.py '            pass  # raced by the fast path — the state is already right
    await claims.release()
    return counts' '            pass  # raced by the fast path — the state is already right
    return counts' "$GATE -k hands_the_callers_scope_back"
# The ladder's count: read before the claim, a tenant-less session counts every step as the first.
check "the reconciler counts the ladder before claiming the row's workspace" src/services/target/work_loop.py '            await unit_of_work.apply_gucs(
                session,
                tenant_id=str(op["workspace_id"]),
                actor_kind="system",
            )
            await reconciler.reconcile_intent(
                session,
                intent_id=op["intent_id"],
                workspace_id=op["workspace_id"],
                poll=deps.poll,
                checks=await reconciler.checks_so_far(
                    session, intent_id=op["intent_id"]
                ),
            )' '            climbed = await reconciler.checks_so_far(
                session, intent_id=op["intent_id"]
            )
            await unit_of_work.apply_gucs(
                session,
                tenant_id=str(op["workspace_id"]),
                actor_kind="system",
            )
            await reconciler.reconcile_intent(
                session,
                intent_id=op["intent_id"],
                workspace_id=op["workspace_id"],
                poll=deps.poll,
                checks=climbed,
            )' "$GATE -k climbs_the_ladder"
# The advance phase's refusal rides a savepoint: without it one raced row aborts the sweep's transaction.
check "the advance phase's refusal has no savepoint" src/services/target/prompts.py '            async with session.begin_nested():
                await intent_ledger.transition(
                    session, str(row["id"]), "awaiting_approval"
                )
' '            await intent_ledger.transition(
                session, str(row["id"]), "awaiting_approval"
            )
' "tests/src/services/target/test_prompts.py -k refused_transition_rolls_back"
# The due door's select list is _CARD_SELECT verbatim: the pin reads the door's body from the file.
check "the due door drops a card column" scripts/migrations/082_worker_doors.sql '         m.file_name, m.media_kind, m.mime_type,' '         m.file_name, m.media_kind,' "tests/src/services/target/test_prompts.py -k carries_the_card_select"
# The file's own adoption probes: the runner refuses a file that does not leave what it claims.
check "a policy svc_maintenance needs is not created" scripts/migrations/082_worker_doors.sql 'CREATE POLICY p_maint_media ON media_items FOR SELECT TO svc_maintenance USING (true);' '-- (no policy)' "$LANE"
check "the worker loses EXECUTE on the sender door" scripts/migrations/082_worker_doors.sql 'GRANT EXECUTE ON FUNCTION fn_sender_sweep(p_prefix text, p_attempts int, p_deadline numeric, p_age numeric, p_limit int) TO svc_worker;' '-- (no grant)' "$LANE"
check "the settled-card door is not handed to svc_maintenance" scripts/migrations/082_worker_doors.sql 'ALTER FUNCTION fn_settled_cards(p_terminal text[], p_limit int) OWNER TO svc_maintenance;' '-- (owner not set)' "$LANE"
check "the CREATE bracket is left open on svc_maintenance" scripts/migrations/082_worker_doors.sql 'REVOKE CREATE ON SCHEMA public FROM svc_maintenance;' '-- (bracket left open)' "$LANE"
# The binding predicate has one owner: the door's body (the file the pin reads) must carry it verbatim.
check "the door's binding predicate drifts from bindings.push_binding_where" scripts/migrations/082_worker_doors.sql "     WHERE b.state = 'active' AND b.channel LIKE 'telegram%'" "     WHERE b.state = 'active'" "tests/src/services/target/test_work_loop.py -k four_push_statements_read_the_one_predicate"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
