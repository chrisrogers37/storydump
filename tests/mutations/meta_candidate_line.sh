#!/bin/zsh
# Mutation battery for the Meta callback's candidate line (#739): each behaviour has one named mutation
# that must make its test FAIL ("killed") — and must PASS on the clean tree first, or the verdict is
# BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a kill. Files are restored
# from the COMMITTED tree after each, so commit first. No database: the tests build the app without one.
# `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL DB_PORT=65432 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
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

T="tests/src/api/test_meta_callbacks.py -k TheLineNamesWhichSecretVerified"

check "the line is never logged" src/api/routes/meta.py '    logger.info(
        "meta callback verified by %s (candidate %d of %d)",
        meta_callbacks.app_secret_names()[position],
        position + 1,
        len(secrets),
    )
' '' "$T"
check "the position is counted from zero" src/api/routes/meta.py '        position + 1,
        len(secrets),' '        position,
        len(secrets),' "$T"
check "the names and the secrets disagree on their order" src/services/target/meta_callbacks.py '_CANDIDATE_SETTINGS = ("INSTAGRAM_APP_SECRET", "FACEBOOK_APP_SECRET")' '_CANDIDATE_SETTINGS = ("FACEBOOK_APP_SECRET", "INSTAGRAM_APP_SECRET")' "$T"
check "the line names the value, not the setting" src/api/routes/meta.py '        meta_callbacks.app_secret_names()[position],' '        secret,' "$T"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
