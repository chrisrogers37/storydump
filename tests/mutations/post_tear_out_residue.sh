#!/bin/zsh
# Mutation battery for the post-tear-out residue PR: each behaviour it changes has one named mutation
# that must make its test FAIL ("killed"); files are restored from the COMMITTED tree after each, so
# commit first. Needs no database. `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
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
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

check "the health renderer reads pool keys the API never emits again" storydump_cli/output.py 'POOL_FACTS = ("size", "checked_out", "checked_out_peak")' 'POOL_FACTS = ("size", "in_use", "peak")' "tests/storydump_cli/test_env.py -k pools_real_keys"
check "the renderer stops reading the named tuple (a second spelling of the keys)" storydump_cli/output.py '                _facts(pool, *POOL_FACTS),' '                _facts(pool, "size", "in_use", "peak"),' "tests/storydump_cli/test_env.py -k pools_real_keys"
check "the not_connected hint sends the user to the read-only tab again" storydump_cli/main.py '"connect the Instagram account under Settings › Accounts"' '"connect the Instagram account under Settings › Integrations"' "tests/storydump_cli/test_writes.py -k unknown_source_says_source_not_story"
check "the Facebook Graph host returns to the egress allow-list (FC-4)" src/services/target/egress.py '        "graph.instagram.com",
        "api.instagram.com",' '        "graph.instagram.com",
        "graph.facebook.com",
        "api.instagram.com",' "tests/src/services/target/test_egress_hosts.py -k no_facebook_host"
check "the Instagram Graph host leaves the allow-list" src/services/target/egress.py '        "graph.instagram.com",
        "api.instagram.com",' '        "api.instagram.com",' "tests/src/services/target/test_egress_hosts.py -k instagram_graph_host_is_allowed"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
