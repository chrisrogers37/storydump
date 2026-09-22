#!/bin/zsh
# Mutation battery for make init-db's runner URL (scripts/app_db_url.py; the tear-out's owner queue):
# each behaviour has one named mutation that must make its test FAIL ("killed") — and must PASS on the
# clean tree first, or the verdict is BASELINE RED; a selector that selects nothing is NO TEST SELECTED,
# never a kill. Files are restored from the COMMITTED tree after each, so commit first. No database.
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

T=tests/scripts/test_app_db_url.py

check "the password is pasted raw" scripts/app_db_url.py 'auth += ":" + quote(password, safe="")' 'auth += ":" + password' "$T -k password_with_reserved"
check "a slash in the password is left unencoded" scripts/app_db_url.py 'auth += ":" + quote(password, safe="")' 'auth += ":" + quote(password)' "$T -k password_with_reserved"
check "the user is pasted raw" scripts/app_db_url.py 'auth = quote(user, safe="")' 'auth = user' "$T -k user_with_reserved"
check "an empty password still writes its colon" scripts/app_db_url.py '    if password:
        auth += ":" + quote(password, safe="")' '    auth += ":" + quote(password, safe="")' "$T -k no_password"
check "the default host drifts from the Makefile's" scripts/app_db_url.py 'host=env.get("DB_HOST", "localhost"),' 'host=env.get("DB_HOST", "127.0.0.1"),' "$T -k makefiles_defaults"
check "init-db pastes the fields into the URL again" Makefile '@DATABASE_URL="$$(python -m scripts.app_db_url)" python -m scripts.migration_runner apply' '@DATABASE_URL="postgresql://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)" python -m scripts.migration_runner apply' "$T -k 'takes_its_url or pastes_no_field'"
check "the defaults are not exported without a .env" Makefile 'export DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD' '# (no named export)' "$T -k reach_the_helper"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
