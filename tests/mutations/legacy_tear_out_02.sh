#!/bin/zsh
# Mutation battery for the legacy tear-out, phase 02 (the settings, the worker switch and the config surfaces):
# each behaviour the phase pins has one named mutation that must make its test FAIL ("killed");
# the file is restored from the COMMITTED tree after each, so commit first. Run from the repo root
# with the sandbox off (the entrypoint and instrument pins spawn interpreters). Every mutation is a
# UNIT: the phase touches no database. `ONLY=<regex>` runs a subset; `STORYDUMP_ROOT` points the
# battery at a worktree. NOTE the recipe: no variable of either tier is set — that absence is phase
# 02's claim, and a recipe that set one would hide its own regression.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
# The ONE recipe that re-arms the three legacy variables, on purpose: a mutation that makes one of them
# REQUIRED again stops the settings module importing at all, and then no named test can give a verdict
# (round 1 of the review). With the variable present the module imports, and the test the mutation
# targets is what fails. (`env` takes its `-u` options first, then the assignments.)
REARMED="${UNIT/ DB_PORT=65432/ TELEGRAM_BOT_TOKEN=x TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 DB_PORT=65432}"
mkdir -p /tmp/claude
# Counted and printed at the end: a script that dies half-way (a quoting slip in a mutation's name
# once skipped 18 of 34 silently) ends with NO last line, and a full run says so in numbers.
RAN=0
EXPECTED=$(grep -cE '^(check|collect|plant) "' "$0")

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

mutate() {  # file old new — exit 3 unless `old` occurs exactly once
  OLD="$2" NEW="$3" $PY - "$1" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
}

check() {  # name file old new runner test-selector
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

collect() {  # like check, but the expected kill is the mutated module refusing to IMPORT
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
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
  RAN=$((RAN + 1))
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
ENVX=.env.example
MAKEFILE=Makefile
CI=.github/workflows/ci.yml
DEPLOY=documentation/guides/deployment.md
B01=tests/mutations/legacy_tear_out_01.sh
GUARD=tests/src/test_legacy_settings_gone.py
ENTRY=tests/src/test_worker_entrypoint.py
ECHO=tests/src/config/test_settings_never_echo_values.py

# --- the fields ------------------------------------------------------------------------------
collect "with a required field back, nothing imports with no variable set" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    TELEGRAM_CHANNEL_ID: int' "$UNIT" "$GUARD -k no_field_is_required"
check "a required legacy field cannot come back (the named test's verdict)" $SETTINGS '    # Security: the Fernet key(s) the stored credentials are encrypted with.' '    TELEGRAM_BOT_TOKEN: str
    # Security: the Fernet key(s) the stored credentials are encrypted with.' "$REARMED" "$GUARD -k no_field_is_required"
check "settings construct with no legacy variable (the named test's verdict)" $SETTINGS '    ENCRYPTION_KEY: Optional[str] = None  # Fernet key for encrypting tokens in DB' '    ENCRYPTION_KEY: Optional[str] = None  # Fernet key for encrypting tokens in DB
    ADMIN_TELEGRAM_CHAT_ID: int' "$REARMED" "$GUARD -k settings_construct_with_no_legacy_variable"
check "a retired field cannot come back (the list)" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    MEDIA_DIR: str = "/tmp/media"' "$UNIT" "$GUARD -k retired_field_is_gone"
check "a NEW field nothing reads is refused (the rule, not the list)" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    SOMETHING_NOBODY_READS: int = 1' "$UNIT" "$GUARD -k surviving_field_has_a_reader"
check "a field whose name the tree only MENTIONS is unread (a constant and comments are not readers)" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    POOL_SIZE_SEAM: int = 10' "$UNIT" "$GUARD -k surviving_field_has_a_reader"
check "a field that shares its name with an ENVIRONMENT read is unread" $SETTINGS '    # Logging
    LOG_LEVEL: str = "INFO"' '    # Logging
    LOG_LEVEL: str = "INFO"
    WORKER_LOG_LEVEL: str = "INFO"' "$UNIT" "$GUARD -k surviving_field_has_a_reader"

# --- the entrypoint and the switch -------------------------------------------------------------
check "the entrypoint runs the target root" $MAIN '    target_worker.main()' '    return' "$UNIT" "$ENTRY -k main_runs_the_target_root"
check "the root is pulled EAGERLY by the entrypoint" $MAIN 'import src.worker as target_worker


def main():
    target_worker.main()' 'def main():
    import src.worker as target_worker

    target_worker.main()' "$UNIT" "$ENTRY -k pulls_the_root"
check "the entrypoint imports the root and nothing else (it CANNOT read the environment)" $MAIN 'import src.worker as target_worker' 'import os

import src.worker as target_worker' "$UNIT" "$ENTRY -k imports_the_root_and_nothing_else"
check "the entrypoint names no environment switch" $MAIN 'def main():' 'WORKER_SWITCH = "WORKER_IMPL"


def main():' "$UNIT" "$GUARD -k worker_switch_and_its_contract_module_are_gone"
plant "the contract module cannot come back" src/worker_impl.py 'WORKER_IMPL_VAR = "WORKER_IMPL"' "$UNIT" "$GUARD -k worker_switch_and_its_contract_module_are_gone"
check "the worker refuses to boot without its database URL" $WORKER '    if url is None:
        # No fallback' '    if False:
        # No fallback' "$UNIT" "$GUARD -k worker_refuses_to_boot_without_its_database_url"
check "the refusal names the variable" $WORKER '            f"FATAL: {unit_of_work.DATABASE_URL_VAR} is unset. The worker runs the"
            " target tier only and has no database to run it against; set"
            f" {unit_of_work.DATABASE_URL_VAR} on this service. Refusing to boot.",' '            "FATAL: the database URL is unset. The worker runs the"
            " target tier only and has no database to run it against; set"
            " it on this service. Refusing to boot.",' "$UNIT" "$GUARD -k worker_refuses_to_boot_without_its_database_url"
check "a blank database URL meets the same named refusal" $UOW '    url = (env.get(DATABASE_URL_VAR) or "").strip()' '    url = env.get(DATABASE_URL_VAR) or ""' "$UNIT" "$GUARD -k blank_database_url_is_refused"
check "create_engine takes no settings-built fallback" $UOW '    if not url:
        raise ValueError(' '    if url is None and False:
        raise ValueError(' "$UNIT" "$GUARD -k create_engine_takes_no_settings_built_fallback"

# --- the instrument's label -------------------------------------------------------------------
# Three mutations stood here, over the reachability instrument and its test. Both were retired
# with the legacy tier's measurement instruments (#1216 — the CHANGELOG names them), so the pins
# they exercised no longer exist; a mutation over a deleted file reports MUTATION NOT APPLIED
# rather than a verdict, which is noise a future run would have to re-diagnose.

# --- the surfaces that set variables ---------------------------------------------------------
check ".env.example may not name a variable nothing reads" $ENVX '# ENCRYPTION_KEYS=' '# ENCRYPTION_KEYS=
DRY_RUN_MODE=false' "$UNIT" "$GUARD -k env_example_names_exactly_what_the_tree_reads"
check ".env.example may not omit a variable the tree reads" $ENVX 'TARGET_DATABASE_URL=postgresql://storydump_user@localhost:5432/storydump' '# (the runtime login)' "$UNIT" "$GUARD -k env_example_names_exactly_what_the_tree_reads"
check "the pinned environment set sees a new run-time read" $WORKER 'USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"' 'USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"
BRAND_NEW_ENV = "TARGET_BRAND_NEW_KNOB"' "$UNIT" "$GUARD -k environment_read_outside_settings_is_pinned"
check "the dead list cannot name a variable the tree reads" $WORKER 'USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"' 'USAGE_PRECHECK_ENV = "TARGET_USAGE_PRECHECK_ENABLED"
DRY_RUN_ENV = "DRY_RUN_MODE"' "$UNIT" "$GUARD -k dead_list_names_nothing_the_tree_reads"
check "CI may not set a dead variable" $CI '          LOG_LEVEL: DEBUG' '          LOG_LEVEL: DEBUG
          TELEGRAM_BOT_TOKEN: test_token' "$UNIT" "$GUARD -k 'no_setter_names_a_dead_variable and ci.yml'"
# Re-pointed 2026-09-21 (#1325 audit, doc 12): the guide's env block was reworded by #1332
# ("# OAuth and the web front end" became "# The API only"), so this anchor matched nothing and
# reported MUTATION NOT APPLIED forever instead of a verdict. Same mutation, current text.
check "a guide may not prescribe a dead safety step" $DEPLOY 'LOG_LEVEL=INFO

# The API only' 'LOG_LEVEL=INFO
DRY_RUN_MODE=true

# The API only' "$UNIT" "$GUARD -k 'no_setter_names_a_dead_variable and deployment.md'"
check "a battery recipe may not set a dead variable" $B01 'PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432' 'PYTHONDONTWRITEBYTECODE=1 TELEGRAM_CHANNEL_ID=1 DB_PORT=65432' "$UNIT" "$GUARD -k batteries_recipes_set_no_dead_variable"

# --- the Makefile ------------------------------------------------------------------------------
check "validate-env loads the settings that exist" $MAKEFILE '@python -c "from src.config.settings import settings" && \' '@python -c "from src.config.settings import get_settings; get_settings()" && \' "$UNIT" "$GUARD -k makefile_validates_the_settings_that_exist_and_can_fail"
check "validate-env can fail" $MAKEFILE '(echo "$(RED)✗ Configuration validation failed$(NC)" && exit 1)' 'echo "$(RED)✗ Configuration validation failed$(NC)"' "$UNIT" "$GUARD -k makefile_validates_the_settings_that_exist_and_can_fail"
check "make install installs the CLI extra" $MAKEFILE "pip install -e '.[cli]'" "pip install -e ." "$UNIT" "$GUARD -k make_install_installs_the_cli_extra"
# Re-pointed 2026-09-21 (#1325 audit, doc 12): `init-db` gained the by-hand base fixture, so
# `-f scripts/setup_database.sql` is no longer the last -f on the line and this anchor matched
# nothing. Anchored on the LAST file fed instead, which is what the mutation has to follow.
check "every file the Makefile feeds psql exists" $MAKEFILE '-f tests/scripts/fixtures/legacy_by_hand.sql 2>&1' '-f tests/scripts/fixtures/legacy_by_hand.sql -f tests/scripts/fixtures/not_on_this_branch.sql 2>&1' "$UNIT" "$GUARD -k every_file_the_makefile_feeds_psql_exists"
check "init-db stands step 0 up before the by-hand base" $MAKEFILE '-f scripts/window/step0_bootstrap.sql -f scripts/window/step0_legacy_ddl_door.sql ' '' "$UNIT" "$GUARD -k init_db_is_the_lanes_own_sequence"
check "make dev does not gate a local worker on production's health" $MAKEFILE '	@echo "$(GREEN)✓ Environment file found$(NC)"' '	@echo "$(GREEN)✓ Environment file found$(NC)"
	@make check-health' "$UNIT" "$GUARD -k make_dev_does_not_gate"

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

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
