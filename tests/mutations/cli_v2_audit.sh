#!/bin/zsh
# Mutation battery for the audit fold of the v2 CLI (2026-09-16: the findings of the system review,
# each behaviour it fixed or pinned): each mutation must make its named test FAIL ("killed"); the file is restored from
# the COMMITTED tree after each, so commit first. Run from the repo root with the sandbox off
# (units resolve DNS). `ONLY=<regex>` runs a subset. Every mutation here is killed by a unit test;
# the gate `tests/scripts/test_cli_writes_gate.py` proves the same verbs against the real app and
# is run whole, not mutated (the port's own fences are phase 01's battery).
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE PYTHONDONTWRITEBYTECODE=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude

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
  # Two mutations of the SAME size written within the same second leave the interpreter a
  # bytecode cache it considers valid (mtime + size match) — purge it.
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$runner $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' /tmp/claude/mut.log | tail -1)
  # A selector that matches no test exits non-zero too — that is not a kill. Nor is a kill by a
  # collection or fixture error a test's verdict; both are flagged for a human to read.
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' /tmp/claude/mut.log; then echo "KILLED BY ERROR (check): $name  [$summary]"
  else echo "killed: $name  [$summary]"; fi
  cd "$ROOT" && git checkout -- "$file"
}
WR=storydump_cli/commands/writes.py
EN=storydump_cli/commands/env.py
WH=storydump_cli/webhook.py
RW=storydump_cli/railway.py
WA=storydump_cli/watch.py
OU=storydump_cli/output.py
MA=storydump_cli/main.py
CL=storydump_cli/client.py
VO=src/services/target/vocabulary.py
TW=tests/storydump_cli/test_writes.py
TE=tests/storydump_cli/test_env.py
TH=tests/storydump_cli/test_webhook.py
TM=tests/storydump_cli/test_main.py
TC=tests/storydump_cli/test_client.py
TO=tests/storydump_cli/test_output.py
TA=tests/storydump_cli/test_watch.py
TR=tests/storydump_cli/test_reads.py
TP=tests/storydump_cli/test_help.py
TV=tests/src/services/target/test_vocabulary.py
TD=tests/test_agent_docs.py

# --- the surviving mutants of the review's test-quality lane, now killed -----------
check "an interrupt is exit 0" $MA '            code=vocabulary.EXIT_USAGE,
            reason="interrupted",' '            code=vocabulary.EXIT_OK,
            reason="interrupted",' "$UNIT" "$TM -k interrupt_outside_a_watch"
check "a 2xx that is not JSON is an empty answer" $CL '            if body is None:
                raise Unreachable(status, None, "the answer was not the API'"'"'s JSON")' '            if body is None:
                body = {}' "$UNIT" "$TC -k not_json_is_unreachable"
check "a failed deployment list is no deployments" $RW '        if code != 0:
            raise RailwayUnavailable(
                f"railway deployment list failed for {service}:"' '        if False:
            raise RailwayUnavailable(
                f"railway deployment list failed for {service}:"' "$UNIT" "$TE -k failed_deployment_list_is_exit_5"
check "a registered URL elsewhere is fine" $WH '    if registered != url:' '    if False:' "$UNIT" "$TH -k registered_url_that_is_not_the_expected"
check "doctor reads a 4xx health as ok" $EN '        checks["api"] = (
            "wrong",
            f"{runtime.api_url} answered {exc.status}: {exc.detail}",' '        checks["api"] = (
            "ok",
            f"{runtime.api_url} answered {exc.status}: {exc.detail}",' "$UNIT" "$TE -k 4xx_health_as_wrong"
check "deploys ends on the services it saw" $EN '        return set(latest) >= set(SERVICES) and all(' '        return all(' "$UNIT" "$TE -k waits_for_a_service_with_no_rows"
check "a non-object surface is well" $EN '    if not isinstance(payload, dict):
        return False, {"state": "unreachable", "detail": "no answer"}' '    if not isinstance(payload, dict):
        return True, {"state": "unreachable", "detail": "no answer"}' "$UNIT" "$TE -k not_an_object_as_not_well"
check "a port answer without an outcome is executed" $WR '    if not isinstance(outcome, str) or not outcome:
        raise Unreachable(200, None, "the answer was not the command port'"'"'s")' '    if not isinstance(outcome, str) or not outcome:
        outcome = "executed"' "$UNIT" "$TW -k without_an_outcome_is_exit_4"

# --- health: the webhook verdict ------------------------------------------------------
check "a failed registration is well" $EN '            why = registration.get("error") or "not registered"
            return False, {"state": "unregistered", "detail": f"not registered: {why}"}' '            why = registration.get("error") or "not registered"
            return True, {"state": "unregistered", "detail": f"not registered: {why}"}' "$UNIT" "$TE -k failed_to_register_as_not_well"
check "a backlog behind an error is well" $EN '        if error and isinstance(pending, int) and pending > 0:
            return False, {' '        if error and isinstance(pending, int) and pending > 0:
            return True, {' "$UNIT" "$TE -k undelivered_backlog"
check "the webhook verdict never rides the report" $EN '    verdicts["webhook"] = webhook_verdict(surfaces.get("api"))' '    pass' "$UNIT" "$TE -k one_envelope_with_the_three_payloads"

# --- deploys: the commit, the statuses, the deadline -------------------------------------
check "a full hash never matches" $EN '    have = str(row.get("commit_hash") or "").lower()' '    have = str(row.get("commit") or "").lower()' "$UNIT" "$TE -k matches_a_commit_by_its_full_hash"
check "the row forgets its full hash" $RW '        "commit_hash": full if isinstance(full, str) and full else None,' '        "commit_hash": full[:7] if isinstance(full, str) and full else None,' "$UNIT" "$TE -k matches_a_commit_by_its_full_hash"
check "a removed deployment is not a failure" $RW 'FAILED_STATUSES: tuple[str, ...] = ("FAILED", "CRASHED", "REMOVED")' 'FAILED_STATUSES: tuple[str, ...] = ("FAILED", "CRASHED")' "$UNIT" "$TE -k removed_deployment_as_failed"
check "the deadline never fires" $WA '                and (runtime.now_fn() - started).total_seconds() >= deadline
            ):
                raise Failure(' '                and False
            ):
                raise Failure(' "$UNIT" "$TE -k exceeds_its_timeout"

# --- doctor: whose fault -----------------------------------------------------------------
check "a 5xx on the principal blames the token" $EN '            except Unreachable as exc:
                # before ApiError, its base class: a 5xx or a dropped
                # connection on /me/principal is the API'"'"'s failure, never
                # "the API refuses this token"
                checks["token"] = ("skipped", f"could not be checked: {exc.detail}", "")' '            except Unreachable as exc:
                checks["token"] = ("wrong", f"could not be checked: {exc.detail}", "")' "$UNIT" "$TE -k blames_the_api_not_the_token"

# --- the dispatcher ------------------------------------------------------------------------
check "--json after a bad value is prose" $MA '    if "--json" in argv:
        runtime.json_mode = True' '    pass' "$UNIT" "$TM -k usage_error_after_json"
check "a closed pipe is a failure" $MA '    except BrokenPipeError:' '    except MemoryError:' "$UNIT" "$TM -k closed_pipe"
check "a config directory error is a traceback" storydump_cli/storage.py '        except OSError as exc:
            # a file where the config directory should be' '        except MemoryError as exc:
            # a file where the config directory should be' "$UNIT" "$TM -k cannot_be_written_is_usage"
check "a bare 403 is a login problem" $MA '    if exc.status == 403:
        # a reason-less 403 is the role floor' '    if False:
        # a reason-less 403 is the role floor' "$UNIT" "$TW -k reason_less_403"
check "a 429 is a refusal" $VO '    if status == 429:' '    if False:' "$UNIT" "$TV -k rate_limited_answer_is_unreachable"
check "a non-ASCII key reaches the transport" $WR '    if not key.isascii() or not key.isprintable():' '    if False:' "$UNIT" "$TW -k not_printable_ascii"
check "the 422 list is lost" $CL '        if isinstance(detail, list):' '        if False:' "$UNIT" "$TC -k 422_keeps"

# --- the watch ------------------------------------------------------------------------------
check "a shrinking failed group ends the watch" $WA '                if earlier is None or watched.worse(earlier, change["row"]):' '                if True:' "$UNIT" "$TA -k shrinks_is_a_change_that_does_not_end"
check "a lost answer is not mid-flight" $WA '        return row.get("state") in ("publishing", "publishing_ambiguous")' '        return row.get("state") == "publishing"' "$UNIT" "$TA -k keeps_watching_a_lost_answer"
check "one transient failure ends the watch" $WA '                if unanswered > TRANSIENT_RETRIES:' '                if True:' "$UNIT" "$TA -k retried_before_it_ends"
check "transient failures are retried forever" $WA '                if unanswered > TRANSIENT_RETRIES:' '                if False:' "$UNIT" "$TA -k failure_that_persists_ends"

# --- output, the window, the docs -------------------------------------------------------------
check "a driver suffix escapes redaction" $OU 'DATABASE_URL_PATTERN = re.compile(r"postgres(?:ql)?(?:\+\w+)?://\S+")' 'DATABASE_URL_PATTERN = re.compile(r"postgres(?:ql)?://\S+")' "$UNIT" "$TO -k every_dialect"
check "a token row that is not an object raises" $OU '        if not isinstance(row, dict):
            table.add_row(_text(row, "?"), "?", "?", "?", "?", "?")
            continue' '        pass' "$UNIT" "$TO -k tokens_renderer_survives"
check "the truncation note is silent" $OU '        if cut:' '        if False:' "$UNIT" "$TR -k older_rows_are_omitted"
check "the window has no slack" $VO 'WINDOW_SLACK = dt.timedelta(minutes=5)' 'WINDOW_SLACK = dt.timedelta(0)' "$UNIT" "$TV -k slightly_behind_is_clamped"
check "the window slack is unbounded" $VO '    if start > anchor + WINDOW_SLACK:' '    if False:' "$UNIT" "$TV -k slack_is_bounded"
check "the health help forgets its bounds" $EN '    Two bounds against the pollers: this is one reading, so there is no watch' '    Two bounds against the pollers: this is a single read, so there is no watch' "$UNIT" "$TP -k health_help_states_the_two_bounds"
check "the satellites are unpinned" $TD 'SATELLITES = (".claude/QUICK_REFERENCE.md", ".claude/PROJECT_CONTEXT.md")' 'SATELLITES = ()' "$UNIT" "$TD -k unpinned_never_run_list"
check "the webhook group has no --json" $EN '@click.group()
@global_options
def webhook() -> None:' '@click.group()
def webhook() -> None:' "$UNIT" "$TH -k json_before_the_group"

# --- round 2: the architecture lane (one spelling, the closed set of reasons, the boundary) -----
TV2=tests/src/services/target/test_vocabulary.py
TI=tests/storydump_cli/test_import_boundary.py
AP=src/api/app.py
#: The API's startup registration moved into the channel it belongs to
#: (#1335, TD-C11), and the anchor below moved with it.
RG=src/channels/telegram_webhook_registration.py
ST=src/services/target/service_tokens.py
OV=src/services/target/ops_views.py
check "the startup registration reads a Telegram variable by a literal" $RG '    token = env.get(TOKEN_VAR)
    secret = env.get(SECRET_VAR)' '    token = env.get("TARGET_TELEGRAM_BOT_TOKEN")
    secret = env.get(SECRET_VAR)' "$UNIT" "$TV2 -k literal_outside_the_vocabulary"
check "an undocumented reason passes the envelope check" $VO '        if error["reason"] not in CLI_REASONS:' '        if False:' "$UNIT" "$TV2 -k refuses_an_undocumented_reason"
check "a CLI reason leaves the closed set" $VO '    "interrupted",
    "refused",
)' '    "refused",
)' "$UNIT" "$TV2 -k own_reasons_are_documented"
check "the token bounds are spelled twice" $ST 'NAME_MAX = vocabulary.TOKEN_NAME_MAX' 'NAME_MAX = 80' "$UNIT" "$TV2 -k token_bounds_are_read_by_reference"
check "the floating limits are spelled twice" $OV 'from src.services.target.vocabulary import FLOATING_LIMIT, FLOATING_LIMIT_MAX' 'from src.services.target.vocabulary import FLOATING_LIMIT_MAX
FLOATING_LIMIT = 100' "$UNIT" "$TV2 -k floating_limits_are_read_by_reference"
check "the project id is spelled twice" $RW 'PROJECT_ID = RAILWAY_PROJECT_ID' 'PROJECT_ID = "33d1ccca-353c-4236-8d39-0d8fd916f054"' "$UNIT" "$TV2 -k spelled_once"
check "a third-party package joins the CLI" $CL 'import httpx' 'import httpx
import pydantic' "$UNIT" "$TI -k on_the_allowlist"
check "a monitor stops being stdlib-only" scripts/posting_monitor.py 'import urllib.request' 'import urllib.request
import httpx' "$UNIT" "$TI -k standard_library_only"
check "the workspace key drops the workspace" $WR '    return f"{command}:{workspace_id}:{identity}"' '    return f"{command}:{identity}"' "$UNIT" "$TW -k shared_fixture_both_doors_read"
check "a verb loses its renderer" $OU '    "doctor": _render_doctor,' '' "$UNIT" "$TO -k every_verb_kind_has_a_renderer"

# --- round 3: the two review lenses ---------------------------------------------------------
TG=tests/test_agent_docs.py
check "a saturated pool loses its sentence" $MA '        if exc.reason and exc.reason in vocabulary.REASON_SENTENCES:' '        if False:' "$UNIT" "$TM -k saturated_pool"
check "the timeout is never checked" $EN '    if timeout is not None and not watching:
        raise click.UsageError("--timeout only means something with --watch", ctx=ctx)' '    pass' "$UNIT" "$TE -k timeout_without_watch"
check "the sampler failing is well" $EN '            return False, {
                "state": "sampler_failed",' '            return True, {
                "state": "sampler_failed",' "$UNIT" "$TE -k failed_webhook_sampler"
check "a satellite may name a command anywhere" $TG '    return _named_invocations(_never_bullets(text))' '    return _named_invocations(text)' "$UNIT" "$TG -k named_only_in_a_safe_list"
check "a retry that fails again is not a new failure" $WA '    return int(new.get("job_attempts") or 0) > int(old.get("job_attempts") or 0)' '    return False' "$UNIT" "$TA -k retry_that_fails_again"
check "a shuffled failed row ends the watch" $WA '        "floating", _by_id, _failed_floating, _empty_twice, worse=_job_died' '        "floating", _by_id, _failed_floating, _empty_twice' "$UNIT" "$TA -k retry_that_fails_again"
check "a dropped principal call leaves the API ok" $EN '                checks["api"] = (
                    "wrong",
                    f"{runtime.api_url} {answered}: {exc.detail}",' '                checks["api"] = (
                    "ok",
                    f"{runtime.api_url} {answered}: {exc.detail}",' "$UNIT" "$TE -k never_says_ok_over_an_unchecked_token"
check "a skipped registration ignores the live sample" $EN '        if registration.get("skipped") and not isinstance(live, dict):' '        if registration.get("skipped"):' "$UNIT" "$TE -k skipped_registration_with_a_live_backlog"
check "a railway blip ends the watch" $WA '            except (Unreachable, RailwayUnavailable):' '            except Unreachable:' "$UNIT" "$TE -k rides_out_a_railway_blip"
check "an empty commit matches nothing forever" $EN '    if commit is not None and not commit.strip():' '    if False:' "$UNIT" "$TE -k empty_commit_is_usage"
check "a 5xx health is missing" $EN '            # it answered, badly: a 5xx from /health is a wrong API, not a missing one
            checks["api"] = (
                "wrong",' '            # it answered, badly: a 5xx from /health is a wrong API, not a missing one
            checks["api"] = (
                "missing",' "$UNIT" "$TE -k 5xx_health_as_wrong_not_missing"
