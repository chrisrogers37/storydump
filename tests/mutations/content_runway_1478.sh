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
check() {  # name runner file old new test-selector [old2 new2]
  local name=$1 runner=$2 file=$3 old=$4 new=$5 sel=$6 old2=${7-} new2=${8-}
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  run "$runner" "$sel"; local base=$?
  if selected_nothing; then echo "NO TEST SELECTED (bad): $name  [$(summary)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(summary)]"; return; fi
  if ! mutate "$file" "$old" "$new" "$old2" "$new2"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  run "$runner" "$sel"; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

C=src/services/target/content_runway.py
K=src/services/target/category_mix.py
S=src/services/target/scheduler.py
V=src/api/routes/v1.py
G=tests/scripts/test_customer_notice_gate.py
U=tests/src/services/target/test_content_runway.py
R=tests/src/api/test_v1_routes.py
CARD=landing/src/components/dashboard/runway-card.tsx
PAGE='landing/src/app/(dashboard)/dashboard/page.tsx'

# The count is the planner's pool: eligible files in the folders the draw can land on.
check "a file live for this account still counts" PYTEST $K '                     AND p.ig_account_id = :acct"' '                     AND p.ig_account_id = :acct AND false"' "$G -k 'count_is_the_planners_pool'"
check "this account's own recent lock holds nothing" PYTEST $K '(l.ig_account_id IS NULL OR l.ig_account_id = :acct)' '(l.ig_account_id IS NULL)' "$G -k 'count_is_the_planners_pool'"
check "an expired lock still holds its file" PYTEST $K '(l.expires_at IS NULL OR l.expires_at > now())' '(true)' "$G -k 'count_is_the_planners_pool'"
check "an Off folder's files count" PYTEST $K '        return sum(self.counts.get(sid, 0) for sid, _ in self.drawable)' '        return sum(self.counts.values())' "$U -k 'TestThePool'"
check "the mint is not taken off the count" PYTEST $S '        eligible=drawn.eligible - 1,' '        eligible=drawn.eligible,' "$G -k 'crosses_a_week'"
# The cadence: the account's own posts per day, else the workspace's.
check "the notice divides by the workspace's cadence first" PYTEST $C $'        "SELECT a.handle,"\n        "       COALESCE(a.posts_per_day, w.posts_per_day) AS posts_per_day,"' $'        "SELECT a.handle,"\n        "       COALESCE(w.posts_per_day, a.posts_per_day) AS posts_per_day,"' "$G -k 'crosses_a_week'"
check "the read divides by the workspace's cadence first" PYTEST $C $'        "SELECT a.id, a.handle, a.display_name, a.state,"\n        "       COALESCE(a.posts_per_day, w.posts_per_day) AS posts_per_day,"' $'        "SELECT a.id, a.handle, a.display_name, a.state,"\n        "       COALESCE(w.posts_per_day, a.posts_per_day) AS posts_per_day,"' "$G -k 'count_is_the_planners_pool'"
# Not posting: no days left, and never low.
check "an account with no slot cursor is posting" PYTEST $C '    "(a.state = '"'"'active'"'"' AND a.next_slot_at IS NOT NULL"' '    "(a.state = '"'"'active'"'"'"' "$G -k 'does_not_post_for'"
check "a paused workspace is posting" PYTEST $C '    " AND w.state = '"'"'active'"'"' AND NOT w.is_paused)"' '    " AND w.state = '"'"'active'"'"')"' "$G -k 'does_not_post_for'"
check "an account not posting is given days left" PYTEST $C '                "days_left": days_left(eligible, posts_per_day) if posting else None,' '                "days_left": days_left(eligible, posts_per_day),' "$G -k 'does_not_post_for'"
check "an account not posting is marked low" PYTEST $C '                "low": posting and is_below(eligible, posts_per_day, below_days),' '                "low": is_below(eligible, posts_per_day, below_days),' "$G -k 'does_not_post_for'"
# Once per crossing, re-armed at eight days, through the empty library's path.
check "exactly a week left is below it" PYTEST $C '    return eligible < days * posts_per_day' '    return eligible <= days * posts_per_day' "$G -k 'crosses_a_week'"
check "every mint below the line is told" PYTEST $C '        return None if latched else NOTICE_EVENT' '        return NOTICE_EVENT' "$G -k 'crosses_a_week'"
check "the notice writes no latch" PYTEST $C $'    detail.update(below_days=below_days, told=len(bindings))\n    await _latch(session, workspace_id, ig_account_id, detail)\n' $'    detail.update(below_days=below_days, told=len(bindings))\n' "$G -k 'crosses_a_week'"
check "the latch never re-arms" PYTEST $C '    if latched and not is_below(eligible, posts_per_day, rearm_days):' '    if False:' "$G -k 'refills_rearms'"
check "the latch re-arms at the warning level" PYTEST $C '    if latched and not is_below(eligible, posts_per_day, rearm_days):' '    if latched and not is_below(eligible, posts_per_day, below_days):' "$G -k 'hovering_at_the_line'"
check "the latch reads its oldest row" PYTEST $C '        "         ORDER BY e.id DESC LIMIT 1) AS latch"' '        "         ORDER BY e.id LIMIT 1) AS latch"' "$G -k 'refills_rearms'"
check "an undeliverable notice reads as delivered" PYTEST $C '        return outbox.UNDELIVERABLE' '        return 0' "$G -k 'undeliverable_once_per_crossing'"
check "the latch is read without the account's lock" PYTEST $C '        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),' '        text("SELECT 1"),' "$U -k 'under_the_accounts_own_lock'"
# The Overview's figure: whole days, counted once on the server, at the notice's own level.
check "a part-day is rounded up into a promise" PYTEST $C '    return eligible // posts_per_day' '    return round(eligible / posts_per_day)' "$U -k 'TestDaysLeft'"
check "the card marks low at a level of its own" PYTEST $V '            below_days=WorkerConfig().low_runway_days,' '            below_days=WorkerConfig().low_runway_days + 1,' "$R -k 'runway'"
check "the low mark is on the wrong accounts" VITEST $CARD '                    row.low ? "font-medium text-amber-700" : "text-muted-foreground"' '                    row.low ? "text-muted-foreground" : "font-medium text-amber-700"' "src/components/dashboard/runway-card.test.tsx"
check "a failed runway read does not stop the page" VITEST $PAGE $'    !statsResult.ok ||\n    !runwayResult.ok ||\n' $'    !statsResult.ok ||\n' "'src/app/(dashboard)/dashboard/overview-runway.test.tsx'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
