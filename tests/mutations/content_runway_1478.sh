#!/bin/zsh
# Mutation battery for #1478: days of content left per account, and the one notice when it runs
# low. The count is the planner's own pool (`category_mix.pool`): the eligibility rule, the
# folders the draw can land on, the account's cadence. The notice fires once per crossing below
# a week, re-arms at eight days, and rides the empty library's path. The Overview shows the
# figure. Each behaviour has one named mutation that must make its test FAIL ("killed") — and
# must PASS on the clean tree first, or the verdict is BASELINE RED; a selector that selects
# nothing is NO TEST SELECTED, never a kill. Files are restored from the COMMITTED tree after
# each, so commit first, and run it in its own worktree (`STORYDUMP_ROOT`). The gates need a
# PostgreSQL: the DB_* fields are read from the environment, defaulting to the Docker server
# `AGENTS.md` › Testing starts; `STORYDUMP_PY` points at another venv's python. The VITEST
# checks need `landing/node_modules`.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=${STORYDUMP_PY:-/Users/chris/Projects/storydump/.venv/bin/python}
cd "$ROOT" || exit 2
: ${DB_HOST:=localhost} ${DB_PORT:=65433} ${DB_USER:=test_user} ${DB_PASSWORD:=test_password}
: ${DB_NAME:=storyline_ai} ${TEST_DB_NAME:=storyline_test}
export DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME TEST_DB_NAME
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
PYTEST="env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH REQUIRE_TEST_DATABASE=1 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
VITEST="npx vitest run --maxWorkers=1"
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^check "' "$0")
run() {  # runner selector -> /tmp/claude/mut.log, returns the run's exit code
  if [ "$1" = VITEST ]; then
    (cd "$ROOT/landing" && eval "$VITEST $2") > /tmp/claude/mut.log 2>&1
  else
    eval "$PYTEST $2" > /tmp/claude/mut.log 2>&1
  fi
}
selected_nothing() {
  grep -qE '/ 0 selected|no tests ran|No test files found' /tmp/claude/mut.log
}
summary() {
  grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)|^ +Tests +' /tmp/claude/mut.log | tail -1
}
verdict() {
  local name=$1 rc=$2
  if selected_nothing; then echo "NO TEST SELECTED (bad): $name  [$(summary)]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$(summary)]"
  elif ! grep -qE '^=+ .*[0-9]+ failed|^ +Tests +[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$(summary)]"
  else echo "killed: $name  [$(summary)]"; fi
}
mutate() {  # file old new: old must match exactly once
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old, new = os.environ["OLD"], os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}
# Each runner and selector's clean-tree verdict, measured once: every check
# restores its file from the committed tree (and verifies the restore), so the
# clean tree cannot change within a run.
typeset -A BASE BASE_SUMMARY
baseline() {  # runner selector -> 0 green, 1 red, 2 selected nothing
  local key="$1 $2"
  if (( ! ${+BASE[$key]} )); then
    run "$1" "$2"; local rc=$?
    if selected_nothing; then BASE[$key]=2; elif [ $rc -ne 0 ]; then BASE[$key]=1; else BASE[$key]=0; fi
    BASE_SUMMARY[$key]="$(summary)"
  fi
  return ${BASE[$key]}
}
check() {  # name runner file old new test-selector
  local name=$1 runner=$2 file=$3 old=$4 new=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  baseline "$runner" "$sel"; local base=$?
  if [ $base -eq 2 ]; then echo "NO TEST SELECTED (bad): $name  [${BASE_SUMMARY[$runner $sel]}]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [${BASE_SUMMARY[$runner $sel]}]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  run "$runner" "$sel"; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
  if ! git diff --quiet -- "$file"; then echo "RESTORE FAILED: $file — stopping, every later verdict would be wrong"; exit 4; fi
}

C=src/services/target/content_runway.py
K=src/services/target/category_mix.py
S=src/services/target/scheduler.py
V=src/api/routes/v1.py
G=tests/scripts/test_customer_notice_gate.py
U=tests/src/services/target/test_content_runway.py
R=tests/src/api/test_v1_routes.py
WL=tests/src/services/target/test_work_loop.py
LIB=landing/src/lib/runway.ts
CARD=landing/src/components/dashboard/runway-card.tsx
PAGE='landing/src/app/(dashboard)/dashboard/page.tsx'

# The count is the planner's pool: eligible files in the folders the draw can land on.
check "a file live for this account still counts" PYTEST $K '                     AND p.ig_account_id = :acct"' '                     AND p.ig_account_id = :acct AND false"' "$G -k 'count_is_the_planners_pool'"
check "this account's own recent lock holds nothing" PYTEST $K '(l.ig_account_id IS NULL OR l.ig_account_id = :acct)' '(l.ig_account_id IS NULL)' "$G -k 'count_is_the_planners_pool'"
check "an expired lock still holds its file" PYTEST $K '(l.expires_at IS NULL OR l.expires_at > now())' '(true)' "$G -k 'count_is_the_planners_pool'"
check "a terminal intent still holds its file" PYTEST $K '    f"                    AND p.state NOT IN ({_TERMINAL_STATES_SQL}))"' '    "                    AND true)"' "$G -k 'terminal_intent_releases'"
check "an Off folder's files count" PYTEST $K '        if row["ratio"] is None or row["ratio"] > 0' '        if row["ratio"] is None or row["ratio"] >= 0' "$G -k 'count_is_the_planners_pool'"
check "a folder with no mix of its own is not counted" PYTEST $K '        if row["ratio"] is None or row["ratio"] > 0' '        if row["ratio"] is not None and row["ratio"] > 0' "$U -k 'TestThePool'"
check "the mint is not taken off the count" PYTEST $K '        return self.eligible - 1' '        return self.eligible' "$G -k 'crosses_a_week'"
# The cadence: the account's own posts per day, else the workspace's, for the card and the notice alike.
check "the workspace's cadence comes first" PYTEST $C '_POSTS_PER_DAY_SQL = "COALESCE(a.posts_per_day, w.posts_per_day)"' '_POSTS_PER_DAY_SQL = "COALESCE(w.posts_per_day, a.posts_per_day)"' "$G -k 'crosses_a_week or count_is_the_planners_pool'"
# Not posting: no days left, and never low.
check "an account with no slot cursor is posting" PYTEST $C '    "(a.state = '"'"'active'"'"' AND a.next_slot_at IS NOT NULL"' '    "(a.state = '"'"'active'"'"'"' "$G -k 'does_not_post_for'"
check "a paused workspace is posting" PYTEST $C '    " AND w.state = '"'"'active'"'"' AND NOT w.is_paused)"' '    " AND w.state = '"'"'active'"'"')"' "$G -k 'does_not_post_for'"
check "an inactive account is posting" PYTEST $C '    "(a.state = '"'"'active'"'"' AND a.next_slot_at IS NOT NULL"' '    "(a.next_slot_at IS NOT NULL"' "$U -k 'TestThePostingPredicate'"
check "another workspace's runway is read" PYTEST $C '        " WHERE a.workspace_id = :ws"' '        " WHERE true"' "$G -k 'another_workspaces'"
check "an account not posting is given days left" PYTEST $C '        days = days_left(eligible, posts_per_day) if posting else None' '        days = days_left(eligible, posts_per_day)' "$G -k 'does_not_post_for'"
check "an account not posting is marked low" PYTEST $C '    return days is not None and days < below_days' '    return days is None or days < below_days' "$G -k 'does_not_post_for'"
# Once per crossing, re-armed a day above the warning level, through the empty library's path.
check "exactly a week left is below it" PYTEST $C '    return days is not None and days < below_days' '    return days is not None and days <= below_days' "$U -k 'TestTheRunway'"
check "every mint below the line is told" PYTEST $C '        return None if latched else NOTICE_EVENT' '        return NOTICE_EVENT' "$G -k 'crosses_a_week'"
check "the notice writes no latch" PYTEST $C $'    detail.update(below_days=below_days, told=len(bindings))\n    await _latch(session, workspace_id, ig_account_id, detail)\n' $'    detail.update(below_days=below_days, told=len(bindings))\n' "$G -k 'crosses_a_week'"
check "the latch never re-arms" PYTEST $C '    if latched and days >= below_days + REARM_MARGIN_DAYS:' '    if False:' "$G -k 'refills_rearms'"
check "the latch re-arms at the warning level" PYTEST $C '    if latched and days >= below_days + REARM_MARGIN_DAYS:' '    if latched and days >= below_days:' "$G -k 'hovering_at_the_line'"
check "the re-arm does not move with the level" PYTEST $C '    if latched and days >= below_days + REARM_MARGIN_DAYS:' '    if latched and days >= 8:' "$U -k 'margin_over_the_warning_level'"
check "the latch reads its oldest row" PYTEST $C '        "         ORDER BY e.id DESC LIMIT 1) AS latch"' '        "         ORDER BY e.id LIMIT 1) AS latch"' "$G -k 'refills_rearms'"
check "the latch reads any audit row of the account" PYTEST $C $'        "           AND e.detail->>\'event\' IN (:told, :rearmed)"' $'        "           AND (e.detail->>\'event\' IN (:told, :rearmed) OR true)"' "$G -k 'edit_to_the_account_is_not_the_latch'"
check "a notice that fails costs the mint" PYTEST $S '    except Exception:  # noqa: BLE001 — logged; the mint stands' '    except ZeroDivisionError:  # noqa: BLE001 — logged; the mint stands' "$WL -k 'never_costs_the_mint'"
check "a minted slot reports an undeliverable notice" PYTEST $S '    return SlotOutcome(intent_id=str(row[0]))' '    return SlotOutcome(intent_id=str(row[0]), notice=outbox.UNDELIVERABLE)' "$WL -k 'nobody_receives'"
check "the undeliverable attempt writes no latch" PYTEST $C $'    detail.update(below_days=below_days, told=len(bindings))\n    await _latch(session, workspace_id, ig_account_id, detail)\n' $'    detail.update(below_days=below_days, told=len(bindings))\n' "$G -k 'no_binding_still_mints_and_latches'"
check "a second writer does not wait for the first" PYTEST $C '        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),' '        text("SELECT 1"),' "$G -k 'second_writer_waits'"
# The Overview's figure: whole days, counted once on the server, at the notice's own level.
check "a part-day is rounded up into a promise" PYTEST $C '    return eligible // posts_per_day' '    return round(eligible / posts_per_day)' "$U -k 'TestDaysLeft'"
check "no content left reads as less than a day" PYTEST $C '    if not eligible:' '    if False:' "$U -k 'no_content_is_not'"
check "the card marks low at a level of its own" PYTEST $V '            below_days=WorkerConfig().low_runway_days,' '            below_days=WorkerConfig().low_runway_days + 1,' "$R -k 'runway'"
check "the low mark is on the wrong accounts" VITEST $CARD '                    {row.low && (' '                    {!row.low && (' "src/components/dashboard/runway-card.test.tsx"
check "an empty library reads as less than a day" VITEST $LIB '    const empty = account.posting && account.eligible === 0;' '    const empty = account.posting && account.eligible < 0;' "src/lib/runway.test.ts"
check "a failed runway read does not stop the page" VITEST $PAGE $'    !statsResult.ok ||\n    !runwayResult.ok ||\n' $'    !statsResult.ok ||\n' "'src/app/(dashboard)/dashboard/overview-conditions.test.tsx'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
