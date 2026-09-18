#!/bin/zsh
# Mutation battery for the legacy tear-out, phase 02 (the settings, the worker switch and the config surfaces):
# each behaviour the phase pins has one named mutation that must make its test FAIL ("killed");
# the file is restored from the COMMITTED tree after each, so commit first. Run from the repo root
# with the sandbox off (the entrypoint and instrument pins spawn interpreters). Every mutation is a
# UNIT: the phase touches no database. `ONLY=<regex>` runs a subset; `STORYDUMP_ROOT` points the
# battery at a worktree. NOTE the recipe: no Telegram variable is set — that absence is phase 02's
# claim, and a recipe that set one would hide its own regression.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude

verdict() {  # name rc — reads /tmp/claude/mut.log
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  # A selector that matches no test exits non-zero too — that is not a kill. Nor is a kill by a
  # collection or fixture error a test's verdict; both are flagged for a human to read.
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
}

check() {  # name file old new runner test-selector
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  OLD="$old" NEW="$new" $PY - "$file" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

collect() {  # like check, but the expected kill is a refusal to import the mutated module at collection
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  OLD="$old" NEW="$new" $PY - "$file" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  # pytest prints neither a verdict nor a `===` summary when the module refuses to import: the log
  # ends at the SettingsError traceback, and that (with no test outcome at all) is the kill.
  if [ $rc -ne 0 ] && grep -q "settings failed to load" /tmp/claude/mut.log && ! grep -qE '^=+ .*(passed|failed)' /tmp/claude/mut.log; then
    echo "killed (at collection: the settings module refuses to import): $name"
  else verdict "$name" $rc; fi
  cd "$ROOT" && git checkout -- "$file"
}

plant() {  # name new-file content runner test-selector — a file that must NOT exist, re-created
  local name=$1 file=$2 content=$3 runner=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  if [ -e "$file" ]; then echo "MUTATION NOT APPLIED (exists): $name"; return; fi
  print -r -- "$content" > "$file"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  rm -f "$file"
}

SETTINGS=src/config/settings.py
MAIN=src/main.py
WORKER=src/worker.py
UOW=src/services/target/unit_of_work.py
REACH=scripts/target_reachability.py
ENVX=.env.example
MAKEFILE=Makefile
CI=.github/workflows/ci.yml
B01=tests/mutations/legacy_tear_out_01.sh
GUARD=tests/src/test_legacy_settings_gone.py
ENTRY=tests/src/test_worker_entrypoint.py
ECHO=tests/src/config/test_settings_never_echo_values.py
TREACH=tests/scripts/test_target_reachability.py

# --- the fields ------------------------------------------------------------------------------
# A required field back in `Settings` kills at COLLECTION — `settings = Settings()` at module scope
# refuses to import with no variable set, which is the phase's claim seen from the other side; the
# verdict names it so (pytest prints no `===` summary for an interrupted collection).
collect "a required legacy field cannot come back" $SETTINGS '    # Security: the Fernet key(s) the stored credentials are encrypted with.' '    TELEGRAM_BOT_TOKEN: str
    # Security: the Fernet key(s) the stored credentials are encrypted with.' "$UNIT" "$GUARD -k 'no_field_is_required or retired_field_is_gone'"
check "a field nothing reads cannot come back" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    MEDIA_DIR: str = "/tmp/media"' "$UNIT" "$GUARD -k 'retired_field_is_gone or surviving_field_has_a_reader'"
check "a NEW field nothing reads is refused too (the rule, not the list)" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    SOMETHING_NOBODY_READS: int = 1' "$UNIT" "$GUARD -k surviving_field_has_a_reader"
collect "settings construct with no legacy variable" $SETTINGS '    ENCRYPTION_KEY: Optional[str] = None  # Fernet key for encrypting tokens in DB' '    ENCRYPTION_KEY: Optional[str] = None  # Fernet key for encrypting tokens in DB
    ADMIN_TELEGRAM_CHAT_ID: int' "$UNIT" "$GUARD -k settings_construct_with_no_legacy_variable"

# --- the entrypoint and the switch -------------------------------------------------------------
check "the entrypoint runs the target root" $MAIN '    target_worker.main()' '    return' "$UNIT" "$ENTRY -k main_runs_the_target_root"
check "the root is pulled EAGERLY by the entrypoint" $MAIN 'import src.worker as target_worker


def main():
    target_worker.main()' 'def main():
    import src.worker as target_worker

    target_worker.main()' "$UNIT" "$ENTRY -k pulls_the_root"
check "the entrypoint names no environment switch" $MAIN 'def main():' 'WORKER_SWITCH = "WORKER_IMPL"


def main():' "$UNIT" "$GUARD -k worker_switch_and_its_contract_module_are_gone"
plant "the contract module cannot come back" src/worker_impl.py 'WORKER_IMPL_VAR = "WORKER_IMPL"' "$UNIT" "$GUARD -k worker_switch_and_its_contract_module_are_gone"
check "the worker refuses to boot without its database URL" $WORKER '    if url is None:
        # The settings-built fallback' '    if False:
        # The settings-built fallback' "$UNIT" "$GUARD -k worker_refuses_to_boot_without_its_database_url"
check "the refusal names the variable" $WORKER '            f"FATAL: {unit_of_work.DATABASE_URL_VAR} is unset. The worker runs the"
            " target tier only and has no database to run it against; set"
            f" {unit_of_work.DATABASE_URL_VAR} on this service. Refusing to boot.",' '            "FATAL: the database URL is unset. The worker runs the"
            " target tier only and has no database to run it against; set"
            " it on this service. Refusing to boot.",' "$UNIT" "$GUARD -k worker_refuses_to_boot_without_its_database_url"
check "create_engine takes no settings-built fallback" $UOW '    if not url:
        raise ValueError(' '    if url is None and False:
        raise ValueError(' "$UNIT" "$GUARD -k create_engine_takes_no_settings_built_fallback"

# --- the instrument's label ------------------------------------------------------------------
check "the instrument's JSON carries no gate axis" $REACH '                    "deployed": deployed,' '                    "deployed": deployed,
                    "worker_gate": None,' "$UNIT" "$TREACH -k json_carries_the_movement_and_no_gate_axis"
check "the label names the call site, not a hunt" $REACH '            "  legacy tier'"'"'s deletion (#1216); there is no environment switch "
            "to read\n"' '            "  legacy tier'"'"'s deletion (#1216); find the switch by hand"
            "\n"' "$UNIT" "$TREACH -k moved_axis_names_its_call_site"

# --- the surfaces that set variables ---------------------------------------------------------
check ".env.example may not name a variable nothing reads" $ENVX '# ENCRYPTION_KEYS=' '# ENCRYPTION_KEYS=
DRY_RUN_MODE=false' "$UNIT" "$GUARD -k env_example_names_only_variables_the_tree_reads"
check ".env.example must name the target tier's own variables" $ENVX 'TARGET_DATABASE_URL=postgresql://storydump_user@localhost:5432/storydump' '# (the runtime login)' "$UNIT" "$GUARD -k target_tiers_own_variables_are_documented"
check "the pinned environment set sees a new run-time read" $WORKER 'USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"' 'USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"
BRAND_NEW_ENV = "TARGET_BRAND_NEW_KNOB"' "$UNIT" "$GUARD -k environment_read_outside_settings_is_pinned"
check "CI may not set a dead variable" $CI '          LOG_LEVEL: DEBUG' '          LOG_LEVEL: DEBUG
          TELEGRAM_BOT_TOKEN: test_token' "$UNIT" "$GUARD -k 'no_setter_names_a_dead_variable and ci.yml'"
check "a battery recipe may not set a dead variable" $B01 'PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432' 'PYTHONDONTWRITEBYTECODE=1 TELEGRAM_CHANNEL_ID=1 DB_PORT=65432' "$UNIT" "$GUARD -k batteries_recipes_set_no_dead_variable"
check "the Makefile validates the settings that exist" $MAKEFILE '	@python -c "from src.config.settings import settings" && \' '	@python -c "from src.config.settings import get_settings; get_settings()" && \' "$UNIT" "$GUARD -k makefile_validates_the_settings_that_exist"
check "make install installs the CLI extra" $MAKEFILE "	pip install -e '.[cli]'" "	pip install -e ." "$UNIT" "$GUARD -k makefile_validates_the_settings_that_exist"

# --- the redaction boundary, on its new specimen ----------------------------------------------
check "the boundary still redacts on the new specimen (the value never reaches the error)" $SETTINGS '        except ValidationError as exc:
            error = _redact(exc)' '        except ValidationError as exc:
            error = str(exc)' "$UNIT" "$ECHO -k missing_required_field_does_not_echo_a_sibling_value"
# Without the ValidationError arm the TAIL rung (`except ValueError`) still catches it — pydantic's
# ValidationError is a ValueError — so no value leaks and the leak test cannot see the arm go; what
# goes is the FIELD NAME in the message (the opaque rung names only the class), and that is the
# test that sees it.
check "the boundary still catches the validation exit by name (the tail rung would swallow the field)" $SETTINGS '        except ValidationError as exc:
            error = _redact(exc)
' '' "$UNIT" "$ECHO -k field_names_survive_so_the_error_is_still_actionable"
