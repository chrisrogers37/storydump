#!/bin/zsh
# Mutation battery for phase 03 of the v2 CLI (the write verbs, health, deploys, webhook, doctor,
# the deletion): each mutation must make its named test FAIL ("killed"); the file is restored from
# the COMMITTED tree after each, so commit first. Run from the repo root with the sandbox off
# (units resolve DNS). `ONLY=<regex>` runs a subset. Every mutation here is killed by a unit test;
# the gate `tests/scripts/test_cli_writes_gate.py` proves the same verbs against the real app and
# is run whole, not mutated (the port's own fences are phase 01's battery).
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE PYTHONDONTWRITEBYTECODE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
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
RG=src/channels/telegram_webhook_registration.py
TW=tests/storydump_cli/test_writes.py
TE=tests/storydump_cli/test_env.py
TH=tests/storydump_cli/test_webhook.py
TP=tests/storydump_cli/test_help.py
TV=tests/src/services/target/test_vocabulary.py

# --- the write verbs --------------------------------------------------------------
check "the key forgets the story" $WR '    parts = [command, intent_id]' '    parts = [command]' "$UNIT" "$TW -k deterministic_key_is_a_function"
check "resolve forgets the resolution" $WR '        parts.append(resolution)' '        pass' "$UNIT" "$TW -k deterministic_key_is_a_function"
check "resolve forgets the episode" $WR '        parts.append(str(episode))' '        pass' "$UNIT" "$TW -k later_review_of_the_same_story"
check "the workspace verbs share one key" $WR '    return f"{command}:{workspace_id}:{identity}"' '    return f"{command}:{workspace_id}"' "$UNIT" "$TW -k second_pause_after_a_resume"
check "a conflict names no way out" $MA '    "admission_conflict": "pass --idempotency-key <a new key> to send a different command",' '    "admission_conflict": "see storydump --help",' "$UNIT" "$TW -k conflicting_key_names_the_override"
check "the key never reaches the port" $CL '            headers={IDEMPOTENCY_HEADER: idempotency_key},' '            headers={},' "$UNIT" "$TW -k posts_its_command_with_the_deterministic_key"
check "a shared name picks the first workspace" $WR '    if len(ids) > 1:' '    if False:' "$UNIT" "$TW -k shared_by_two_workspaces_needs_the_id"
check "a long key reaches the port" $WR '    if not key or len(key) > IDEMPOTENCY_KEY_MAX:' '    if not key:' "$UNIT" "$TW -k over_the_ports_limit"
check "the verdict rides every retry" $WR '    if not_posted:
        args["verdict"] = NOT_POSTED' '    if True:
        args["verdict"] = NOT_POSTED' "$UNIT" "$TW -k verdict_only_when_asked"
check "the override is ignored" $WR '    if key is None:' '    if True:' "$UNIT" "$TW -k option_is_the_deliberate_second_execution"
check "a lost answer names no fixing verb" $MA '    "may_have_posted": (
        "look at Instagram, then storydump resolve <story> retry --not-posted"
        " or storydump resolve <story> posted"
    ),' '    "may_have_posted": "look at Instagram",' "$UNIT" "$TW -k names_the_resolve_verb"
check "a sync of an unknown source says story" $WR '        if verb == "sync" and exc.reason == "not_found":' '        if False:' "$UNIT" "$TW -k unknown_source_says_source"
check "a settled story's answer has no sentence" $VO '    "answered": "nothing changed — the story had already answered",' '' "$UNIT" "$TW -k settled_story_answers"
check "the replay sentence is the executed one" $VO '        outcome, outcome
    )' '        "executed", outcome
    )' "$UNIT" "$TV -k replay_sentence_is_the_outcomes_own"

# --- health ---------------------------------------------------------------------
check "an unwell surface is still exit 0" $EN '    return EXIT_OK if ok else EXIT_API_UNREACHABLE' '    return EXIT_OK' "$UNIT" "$TE -k exits_4_when_a_surface_is_not_well"
check "a bad status word is well" $EN '        well = status is not None and str(status).lower() in WELL_WORDS' '        well = True' "$UNIT" "$TE -k bad_api_status"
check "the scheduling verdict is ignored" $EN '        return verdict.state not in SCHEDULING_ALERTS, {' '        return True, {' "$UNIT" "$TE -k exits_4_when_a_surface_is_not_well"
check "the posting verdict is ignored" $EN '        return verdict.state not in POSTING_ALERTS, {' '        return True, {' "$UNIT" "$TE -k silence_as_not_well"
check "an errored surface is well" $EN '        return False, {"state": "unreachable", "detail": str(payload["error"])}' '        return True, {"state": "unreachable", "detail": str(payload["error"])}' "$UNIT" "$TE -k answers_503"
check "a quiet estate pages" $EN '        scheduling_monitor.WORKER_DOWN,
    }' '        scheduling_monitor.WORKER_DOWN,
        scheduling_monitor.NO_SIGNAL,
    }' "$UNIT" "$TE -k quiet_estate"

# --- deploys --------------------------------------------------------------------
check "deploys reads before checking the login" $EN '    rail.whoami()
    project = rail.linked_project()' '    project = rail.linked_project()
    rail.whoami()' "$UNIT" "$TE -k lists_the_latest_deployment_per_service"
check "another project is read anyway" $RW '        if name != PROJECT_NAME or project_id != PROJECT_ID:' '        if False:' "$UNIT" "$TE -k another_linked_project"
check "a logged-out account is read anyway" $RW '            raise RailwayUnavailable("not logged in to Railway", LOGIN_FIX)' '            pass' "$UNIT" "$TE -k logged_out_railway"
check "a missing binary is not said" $RW '            raise RailwayUnavailable("the railway binary is not installed", INSTALL_FIX)' '            raise RailwayUnavailable("railway failed", INSTALL_FIX)' "$UNIT" "$TE -k missing_railway_binary"
check "the version is the whole line" $RW '    return match.group(1) if match else (text or "").strip()' '    return (text or "").strip()' "$UNIT" "$TE -k reads_the_fixtures_version"
check "the watch ends before both succeed" $EN '            str(row.get("status")) in DONE_STATUSES and _of_commit(row, commit)' '            True' "$UNIT" "$TE -k ends_0_when_both_latest_deploys_succeed"
check "the commit is ignored" $EN '    if commit is None:
        return True' '    if True:
        return True' "$UNIT" "$TE -k commit_waits_for_that_commits_deploys"
check "rows are read in the binary's order" $RW '        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)' '        pass' "$UNIT" "$TE -k sorts_newest_first"
check "another environment is read" $RW '            ENVIRONMENT,' '            "staging",' "$UNIT" "$TE -k sorts_newest_first"
check "a hung binary is a traceback" $RW '        except subprocess.TimeoutExpired:' '        except MemoryError:' "$UNIT" "$TE -k hung_binary"
check "a notice before the JSON is a failure" $RW '    return json.loads(text[min(starts) :])' '    return json.loads(text)' "$UNIT" "$TE -k notice_before_the_json"
check "a failed deploy at the first read is ignored" $EN '    failed_on_baseline=True,' '    failed_on_baseline=False,' "$UNIT" "$TE -k ends_6_when_a_latest_deploy_failed"
check "the watch never judges a baseline" $WA '            if previous is None and watched.failed_on_baseline:' '            if False:' "$UNIT" "$TE -k ends_6_when_a_latest_deploy_failed"
check "the deploys watch is keyed by workspace" $EN '    scope="service",' '    scope="workspace_id",' "$UNIT" "$TE -k watch_json_is_one_envelope_per_read"

# --- webhook --------------------------------------------------------------------
check "the redaction eats a sync key" $OU 'TELEGRAM_TOKEN_PATTERN = re.compile(r"(?<![\w-])(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}\b")' 'TELEGRAM_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}\b")' "$UNIT" "tests/storydump_cli/test_output.py -k sync_key_survives"
check "the bot token survives redaction" $OU '    text = TELEGRAM_TOKEN_PATTERN.sub("<bot token>", text)' '    pass' "$UNIT" "$TH -k bot_token_in_any_rendered_string"
check "an http door is accepted" $WH '    if urlsplit(chosen).scheme != "https":' '    if False:' "$UNIT" "$TH -k http_url_is_refused"
check "redirects are followed" $WH '    with httpx.Client(follow_redirects=False, timeout=TIMEOUT_S) as http:' '    with httpx.Client(follow_redirects=True, timeout=TIMEOUT_S) as http:' "$UNIT" "$TH -k never_follows_redirects"
check "the wrong bot is registered anyway" $WH '        if not _bot(report, token, expected):' '        if False:' "$UNIT" "$TH -k refuses_a_token_that_is_not_the_configured_bot"
check "the backlog is dropped by default" $WH '                "drop_pending_updates": "true" if drop_pending else "false",' '                "drop_pending_updates": "true",' "$UNIT" "$TH -k keeping_the_backlog"
check "an unset secret passes the door check" $WH '            "failed",
            f"NOT CHECKED — {TELEGRAM_SECRET_VAR} is not set in this shell",' '            "skipped",
            f"NOT CHECKED — {TELEGRAM_SECRET_VAR} is not set in this shell",' "$UNIT" "$TH -k unset_secret_means_the_door_was_not_checked"
check "a 400 at the door is a refusal" $WH '    if status == 400:' '    if status == 200:' "$UNIT" "$TH -k reports_the_bot_the_webhook_and_the_api_door"
check "a redirect at the door passes" $WH '    elif 300 <= status < 400:' '    elif False:' "$UNIT" "$TH -k redirect_from_the_door"
check "a bad cap does not name its variable" $WH '        raise BadVariable(str(exc)) from None' '        raise BadVariable("bad") from None' "$UNIT" "$TH -k cap_outside_telegrams_range"
check "the taps are not asked for" $WH '                "allowed_updates": json.dumps(list(ALLOWED_UPDATES)),' '                "allowed_updates": json.dumps(["message"]),' "$UNIT" "$TH -k sends_the_allowed_updates"
check "a failed check is ok" $WH '        return all(check["state"] != "failed" for check in self.checks)' '        return True' "$UNIT" "$TH -k 403_from_the_api"
check "register skips the status checks" $WH '        _status_checks(report, env, token, door)
    except BotApiError as exc:
        report.add("bot_api", "failed", str(exc))
    return report.data()


def deregister(' '        pass
    except BotApiError as exc:
        report.add("bot_api", "failed", str(exc))
    return report.data()


def deregister(' "$UNIT" "$TH -k register_ends_with_the_status_check"

# --- doctor ---------------------------------------------------------------------
check "doctor without a token exits 0" $EN '    "token": EXIT_NOT_AUTHORIZED,' '    "token": EXIT_OK,' "$UNIT" "$TE -k without_a_token_says_missing"
check "doctor misses the missing migrations" $EN '                missing = sorted(repo - applied)' '                missing = []' "$UNIT" "$TE -k names_the_migrations_the_ledger_lacks"
check "the checkout behind the deployment passes" $EN '                elif extra:' '                elif False:' "$UNIT" "$TE -k checkout_behind_the_deployment"
check "a logged-out railway is wrong" $EN '        missing = "not installed" in exc.detail or "not logged in" in exc.detail' '        missing = "not installed" in exc.detail' "$UNIT" "$TE -k logged_out_railway_says_missing"
check "doctor invents a checkout" $EN '    if not directory.is_dir():
        return None' '    if not directory.is_dir():
        return set()' "$UNIT" "$TE -k outside_a_checkout_skips"
check "a skipped check fails the doctor" $EN '    ok = all(row["state"] in ("ok", "skipped") for row in ordered)' '    ok = all(row["state"] == "ok" for row in ordered)' "$UNIT" "$TE -k outside_a_checkout_skips"

# --- the help, the spellings, the deletion ----------------------------------------
check "the help lists no sections" $MA '        for title, names in SECTIONS:' '        for title, names in ():' "$UNIT" "$TP -k lists_every_verb_once"
check "the registration spells its own updates" $RG '    ALLOWED_UPDATES,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_WEBHOOK_URL,' '    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_WEBHOOK_URL,' "$UNIT" "$TV -k re_exports_them_unchanged"
check "bot_matches is case-sensitive" $VO '    return username.lstrip("@").lower() == expected.lstrip("@").lower()' '    return username.lstrip("@") == expected.lstrip("@")' "$UNIT" "$TV -k ignores_case"
check "the cap's range is open" $VO '    if not 1 <= value <= 100:' '    if not 0 <= value <= 100:' "$UNIT" "$TV -k outside_the_range"
