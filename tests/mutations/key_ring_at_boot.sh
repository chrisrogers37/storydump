#!/bin/zsh
# Mutation battery for the credential key ring at boot (PR #1401): both roots refuse to start without a
# working ring, and a ring that cannot load is never recorded against a credential. Each behaviour has one
# named mutation that must make its test FAIL ("killed") — and must PASS on the clean tree first, or the
# verdict is BASELINE RED; a selector that selects nothing is NO TEST SELECTED, never a kill. Files are
# restored from the COMMITTED tree after each, so commit first. No database. `STORYDUMP_ROOT` points the
# battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL DB_PORT=65432 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
LOG=/tmp/claude/mut-key-ring.log
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^check2? "' "$0")
verdict() {
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' $LOG | tail -1)
  if grep -qE '/ 0 selected|no tests ran' $LOG; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' $LOG; then echo "KILLED BY ERROR (check): $name  [$summary]"
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
baseline() {  # selector → 0 when the clean tree passes it
  local name=$1 sel=$2
  eval "$UNIT $sel" > $LOG 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' $LOG; then echo "NO TEST SELECTED (bad): $name  [$(grep -E '^=+ .*(selected|no tests ran)' $LOG | tail -1)]"; return 1; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' $LOG | tail -1)]"; return 1; fi
  return 0
}
check() {  # name file old new test-selector
  local name=$1 file=$2 old=$3 new=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  baseline "$name" "$sel" || return
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > $LOG 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}
check2() {  # name file old1 new1 old2 new2 test-selector — two edits to one file, one behaviour
  local name=$1 file=$2 old1=$3 new1=$4 old2=$5 new2=$6 sel=$7
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  baseline "$name" "$sel" || return
  if ! mutate "$file" "$old1" "$new1" || ! mutate "$file" "$old2" "$new2"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$UNIT $sel" > $LOG 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

R=tests/src/services/target/test_ring_unavailable.py
B=tests/src/test_key_ring_at_boot.py
# A ring built lazily INSIDE the door's `try` — what every door did before this PR, in one line.
LAZY='type("Lazy", (), {"decrypt": staticmethod(lambda c: ring().decrypt(c))})'
LAZY_OS='type("Lazy", (), {"decrypt": staticmethod(lambda c: oauth_states.ring().decrypt(c))})'

# The door: a ring that cannot be built is its own type, and names the variable without echoing a key.
check "the door raises the bare ValueError again" src/services/target/oauth_states.py $'    try:\n        return TokenEncryption()\n    except ValueError as exc:\n        raise RingUnavailable(str(exc)) from exc' '    return TokenEncryption()' "$R -k TestTheDoor"
check "the refusal echoes the malformed key" src/utils/encryption.py '            raise ValueError(f"Invalid ENCRYPTION_KEY format: {e}")' '            raise ValueError(f"Invalid ENCRYPTION_KEY format: {e} ({keys_raw or single_key})")' "$R -k never_echoed"
# The roots: each refuses to start, the worker with the database URL's exit code, the API before any task.
check "the worker boots without a ring" src/worker.py $'        oauth_states.ring()\n' $'        pass\n' "$B -k TestTheWorkerRefusesToBoot"
check "the worker's refusal is not the database URL's exit 2" src/worker.py $'ENCRYPTION_KEY on this service. Refusing to boot.",\n            file=sys.stderr,\n        )\n        raise SystemExit(2)' $'ENCRYPTION_KEY on this service. Refusing to boot.",\n            file=sys.stderr,\n        )\n        raise SystemExit(1)' "$B -k 'TestTheWorkerRefusesToBoot and test_without_a_key'"
check "the API starts without a ring" src/api/app.py $'        _require_key_ring()\n        # The role sample' '        # The role sample' "$B -k TestTheApiRefusesToStart"
check2 "the API starts its tasks before the ring check" src/api/app.py $'        _require_key_ring()\n        # The role sample' '        # The role sample' $'            asyncio.create_task(_sample_webhook_live(app_, env)),\n        ]\n' $'            asyncio.create_task(_sample_webhook_live(app_, env)),\n        ]\n        _require_key_ring()\n' "$B -k startup_fails_before_any_task"
# The doors: each builds the ring before its `try` — back inside it, the ring's failure is a corrupt row.
check "the refresh leg's load builds the ring inside its try" src/services/target/ig_login_oauth.py $'    keys = ring()\n' "    keys = $LAZY"$'\n' "$R -k 'TestTheRefreshLegFlipsNothing and not control'"
check "the publish read builds the ring inside its try" src/services/target/ig_credentials.py $'    keys = ring()\n' "    keys = $LAZY"$'\n' "$R -k token_for_account_raises_it"
check "the Drive read builds the ring inside its try" src/services/target/drive_credentials.py $'        keys = ring()\n' "        keys = $LAZY"$'\n' "$R -k token_for_workspace_is_retryable"
check "the revoke builds the ring inside its try" src/services/target/credential_lifecycle.py $'    keys = oauth_states.ring()\n' "    keys = $LAZY_OS"$'\n' "$R -k revoke_raises_it"
# The classifications: nothing sent and no account blamed; our misconfiguration, not a dead grant.
check "the adapter lets the ring's failure out untyped" src/services/target/instagram_graph.py '        except RingUnavailable as exc:' '        except ZeroDivisionError as exc:' "$R -k adapter_sends_nothing"
check "the adapter blames the account (code 190)" src/services/target/instagram_graph.py $'                code=0,\n                message=f"no call made' $'                code=OAUTH_ERROR_CODE,\n                message=f"no call made' "$R -k adapter_sends_nothing"
check "the Drive read calls the ring's failure a dead grant" src/services/target/drive_credentials.py $'    except RingUnavailable as exc:\n        raise DriveRetryableError(' $'    except RingUnavailable as exc:\n        raise DriveCredentialDead(' "$R -k token_for_workspace_is_retryable"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
