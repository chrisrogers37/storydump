#!/bin/zsh
# Mutation battery for the URLs built from DB_* fields — make init-db's (scripts/app_db_url.py) and the
# harness's three, all through src/config/db_url.py (the tear-out's owner queue; PR #1394):
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
RUNNER='@DATABASE_URL="$$(DB_USER=$(DB_USER) DB_PASSWORD="$(DB_PASSWORD)" DB_HOST=$(DB_HOST) DB_PORT=$(DB_PORT) DB_NAME=$(DB_NAME) python -m scripts.app_db_url)" python -m scripts.migration_runner apply'

# The one encoding rule (src/config/db_url.py) and the init-db helper.
check "the password is pasted raw" src/config/db_url.py '(":" + enc(password) if password else "")' '(":" + password if password else "")' "$T -k 'userinfo_round_trips or every_part_round_trips'"
check "a slash is left unencoded" src/config/db_url.py '    return quote(str(part), safe="")' '    return quote(str(part))' "$T -k userinfo_round_trips"
check "the user is pasted raw" src/config/db_url.py '    return enc(user) + (' '    return user + (' "$T -k userinfo_round_trips"
check "the host is pasted raw" scripts/app_db_url.py '{enc(host)}:' '{host}:' "$T -k ipv6_host"
check "the database name is pasted raw" scripts/app_db_url.py '/{enc(name)}"' '/{name}"' "$T -k every_part_round_trips"
check "the default database drifts from the Makefile's" scripts/app_db_url.py 'DEFAULT_NAME = "storydump"' 'DEFAULT_NAME = "storydump_dev"' "$T -k defaults_are_the_makefiles"
# The harness's three builders, each back to pasting the fields.
check "the suite's test URL pastes the fields again" src/config/settings.py '        auth = userinfo(self.DB_USER, self.DB_PASSWORD)' '        auth = f"{self.DB_USER}:{self.DB_PASSWORD}@"' "$T -k suites_test_database_url"
check "the harness's asyncpg URL pastes the fields again" src/services/target/unit_of_work.py '{userinfo(settings.DB_USER, settings.DB_PASSWORD)}' '{settings.DB_USER}:{settings.DB_PASSWORD}@' "$T -k harness_asyncpg_url"
check "the gates' dsn pastes the fields again" tests/scripts/conftest.py '    auth = userinfo(user, password)' '    auth = f"{user}:{password}@"' "$T -k gates_dsn"
# The Makefile: each field quoted as psql gets it, no pasted URL, no exported defaults — each also run through make.
check "init-db reads the helper as a make variable" Makefile '@DATABASE_URL="$$(DB_USER=' '@DATABASE_URL="$(DB_USER=' "$T -k 'each_field_as_psql or hostile_command_line'"
check "the password skips psql's quoting" Makefile ' DB_PASSWORD="$(DB_PASSWORD)" DB_HOST=' ' DB_HOST=' "$T -k 'each_field_as_psql or connects_with_what_psql_does'"
check "the host is quoted where psql's is not" Makefile ' DB_HOST=$(DB_HOST) DB_PORT=' ' DB_HOST="$(DB_HOST)" DB_PORT=' "$T -k 'each_field_as_psql or connects_with_what_psql_does'"
check "init-db pastes the fields into the URL again" Makefile "$RUNNER" '@DATABASE_URL="postgresql://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)" python -m scripts.migration_runner apply' "$T -k 'pastes_no_field or hostile_command_line'"
check "the Makefile exports its defaults to every target" Makefile 'DB_PASSWORD ?=
' 'DB_PASSWORD ?=
export DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD
' "$T -k 'exports_no_db_field or reaches_another_target'"
# Not mutations — EQUIVALENT under libpq, so no behavioural test can kill them: writing `dev:@` for an empty
# password (parse_dsn reads it as `dev@`), and leaving the port unencoded (a port is digits, which encode to
# themselves).

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
