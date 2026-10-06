#!/bin/zsh
# Mutation battery for #1508: the reconciler asks the provider with no transaction open. The egress
# floor refuses a provider call made inside a transaction, so a ladder row of `reconcile_ambiguous`
# asks the provider (`reconciler.observe`) before its transaction opens, then claims the row's
# workspace, reads how far its ladder has climbed and records the answer in that one transaction.
# `reconcile_intent` takes what was observed, and never calls a provider.
#
# Each behaviour has one named mutation, and it must make its test FAIL ("killed"). It must PASS on
# the clean tree first; otherwise the verdict is BASELINE RED. A selector that selects nothing is NO
# TEST SELECTED, never a kill. Files are restored from the COMMITTED tree after each mutation, so
# commit first, and run the battery in its own worktree (`STORYDUMP_ROOT`). The gates need a
# PostgreSQL. The DB_* fields are read from the environment, and default to the Docker server that
# `AGENTS.md` › Testing starts. `STORYDUMP_PY` points at another venv's python.
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
check() {  # name file old new test-selector [old2 new2]
  local name=$1 file=$2 old=$3 new=$4 sel=$5 old2=${6-} new2=${7-}
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$file" "$old" "$new" "$old2" "$new2"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$GATE_RUN $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

W=src/services/target/work_loop.py
R=src/services/target/reconciler.py
U=tests/src/services/target/test_work_loop.py
L3=tests/scripts/test_l3_permit_rail.py

# The ask, and where it must not be: inside the row's transaction.
ASK=$'            status_code = await reconciler.observe(\n                deps.poll, intent_id=op["intent_id"], workspace_id=op["workspace_id"]\n            )\n'
INSIDE=$'                status_code = await reconciler.observe(\n                    deps.poll, intent_id=op["intent_id"], workspace_id=op["workspace_id"]\n                )\n'
CLAIM=$'                await unit_of_work.apply_gucs(\n                    session,\n                    tenant_id=str(op["workspace_id"]),\n                    actor_kind="system",\n                )\n'
check "the provider is asked inside the row's transaction (unit)" $W "$ASK" '' "$U -k 'provider_is_asked_with_no_transaction_open'" "$CLAIM" "$INSIDE$CLAIM"
check "the provider is asked inside the row's transaction (gate)" $W "$ASK" '' "$L3 -k 'real_poll_reaches_the_provider'" "$CLAIM" "$INSIDE$CLAIM"
# The verdict: claimed for the row's workspace, and made from what was observed.
check "the row forgets to claim its workspace" $W "$CLAIM" '' "$L3 -k 'real_poll_reaches_the_provider'"
check "the verdict ignores what was observed" $W $'                    status_code=status_code,\n' $'                    status_code=None,\n' "$U -k 'provider_is_asked_with_no_transaction_open'"
# The stories listing still reaches the evidence trail, now passed in rather than fetched.
check "the stories listing is dropped from the trail" $R $'    if stories is not None:\n' $'    if False:\n' "$L3 -k 'evidence_capture_parks_review_required_WITH_the_trail'"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
