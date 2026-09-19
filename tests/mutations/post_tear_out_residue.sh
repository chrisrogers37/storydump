#!/bin/zsh
# Mutation battery for the post-tear-out residue PR: each behaviour it changes has one named mutation
# that must make its test FAIL ("killed") — and must PASS on the clean tree first, or the verdict is
# BASELINE RED, not a kill; files are restored from the COMMITTED tree after each, so commit first.
# Needs no database. `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^(check|checkv) "' "$0")
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
  # BASELINE first: a test that is red on the clean tree cannot kill anything, and a battery that
  # counted it as a kill once (the residue's own hint test, structural lens finding 2) is why.
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

checkv() {  # name file old new vitest-file test-name-pattern — the landing's tests, same discipline
  local name=$1 file=$2 old=$3 new=$4 vfile=$5 pat=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  [ -e landing/node_modules ] || { echo "SKIPPED (no landing/node_modules): $name"; return; }
  (cd landing && npx vitest run "$vfile" -t "$pat") > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE 'No test files found|no tests' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^ +Tests ' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  (cd landing && npx vitest run "$vfile" -t "$pat") > /tmp/claude/mut.log 2>&1; local rc=$?
  local summary; summary=$(grep -E '^ +Tests ' /tmp/claude/mut.log | tail -1 | sed 's/^ *//')
  if [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^ +Tests .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
  cd "$ROOT" && git checkout -- "$file"
}

check "the health renderer reads pool keys the API never emits again" storydump_cli/output.py 'POOL_FACTS = ("size", "checked_out", "checked_out_peak")' 'POOL_FACTS = ("size", "in_use", "peak")' "tests/storydump_cli/test_env.py -k pools_real_keys"
check "the renderer spells the old keys inline, bypassing POOL_FACTS" storydump_cli/output.py '                _facts(pool, *POOL_FACTS),' '                _facts(pool, "size", "in_use", "peak"),' "tests/storydump_cli/test_env.py -k pools_real_keys"
check "the CLI's not_connected hint sends the user to the Integrations tab again" storydump_cli/main.py '"connect the Instagram account under Settings › Accounts"' '"connect the Instagram account under Settings › Integrations"' "tests/storydump_cli/test_writes.py -k not_connected_refusal_points_at_the_accounts_tab"
check "the Facebook Graph host returns to the egress allow-list (FC-4)" src/services/target/egress.py '        "graph.instagram.com",
        "api.instagram.com",' '        "graph.instagram.com",
        "graph.facebook.com",
        "api.instagram.com",' "tests/src/services/target/test_egress_hosts.py -k no_facebook_host"
check "the Instagram Graph host leaves the allow-list" src/services/target/egress.py '        "graph.instagram.com",
        "api.instagram.com",' '        "api.instagram.com",' "tests/src/services/target/test_egress_hosts.py -k instagram_graph_host_is_allowed"
checkv "the web's not_connected copy sends the user to the Integrations tab again" landing/src/lib/intents.ts 'Connect it in Settings › Accounts, or post by hand.' 'Connect it in Settings › Integrations, or post by hand.' src/lib/intents.test.ts "sends a not_connected refusal to the Accounts tab"
check "an unread constant returns to defaults.py" src/config/defaults.py 'DEFAULT_SKIP_TTL_DAYS = 45' 'DEFAULT_SKIP_TTL_DAYS = 45
DEFAULT_POSTS_PER_DAY = 3' "tests/src/config/test_defaults.py -k every_declared_default_is_read"
check "the defaults pin stops finding readers (an empty search tree passes vacuously)" tests/src/config/test_defaults.py 'for p in (ROOT / "src").rglob("*.py")' 'for p in (ROOT / "src" / "nowhere").rglob("*.py")' "tests/src/config/test_defaults.py -k checker_sees_a_known_reader"
check "the landing example drops the variable the landing reads first" landing/.env.local.example 'TARGET_API_URL=http://localhost:8000
' '' "tests/test_landing_env_example.py -k every_variable_the_landing_reads_is_in_the_example"
check "the landing reads a variable the example does not name" landing/src/lib/target-api.ts 'process.env.TARGET_API_URL || process.env.BACKEND_URL' 'process.env.TARGET_API_URL || process.env.GOOGLE_CLIENT_ID || process.env.BACKEND_URL' "tests/test_landing_env_example.py -k every_variable_the_landing_reads_is_in_the_example"
check "the landing example names a variable nothing reads" landing/.env.local.example 'BACKEND_URL=' 'BACKEND_URL=
JWT_SECRET=' "tests/test_landing_env_example.py -k every_example_line_is_read"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
