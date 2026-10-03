#!/bin/zsh
# Mutation battery for the outbox's failure record and the delivery health surface (093, #1482): each
# behaviour has one named mutation that must make its named test FAIL ("killed"), and the test must PASS
# on the clean tree first, or the verdict is BASELINE RED, not a kill; a selector that selects nothing
# is NO TEST SELECTED, never a kill. Files are restored from the COMMITTED tree after each, so commit
# first, and run it in its own worktree (`STORYDUMP_ROOT=<worktree>`, `STORYDUMP_PYTHON=<a venv's python>`).
#
# Two recipes. UNIT points at port 65432, where nothing listens, so it never touches a database. GATE
# needs the test PostgreSQL and takes the connection from the environment the battery is started in
# (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, TEST_DB_NAME: `AGENTS.md` › Testing), so no
# credential is ever spelled on a command line; the killers replay the advertised stream and act as
# svc_worker and svc_ingress.
set -u
ROOT=${STORYDUMP_ROOT:-${0:A:h:h:h}}
PY=${STORYDUMP_PYTHON:-$ROOT/venv/bin/python}
cd "$ROOT" || exit 2
LOG=${TMPDIR:-/tmp}/storydump-mutation-$$.log
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE -u TARGET_DATABASE_URL DB_PORT=65432 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
GATE="env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u TARGET_DATABASE_URL REQUIRE_TEST_DATABASE=1 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
RAN=0
EXPECTED=$(grep -cE '^(check|check2) "' "$0")
verdict() {
  local name=$1 rc=$2
  local summary; summary=$(grep -E '^=+ .*(passed|failed|error|deselected|no tests ran)' "$LOG" | tail -1)
  if grep -qE '/ 0 selected|no tests ran' "$LOG"; then echo "NO TEST SELECTED (bad): $name  [$summary]"
  elif [ $rc -eq 0 ]; then echo "SURVIVED (bad): $name  [$summary]"
  elif ! grep -qE '^=+ .*[0-9]+ failed' "$LOG"; then echo "KILLED BY ERROR (check): $name  [$summary]"
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
DOC=documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md
MANIFEST=scripts/advertised_ddl_manifest.json
remanifest() {  # the §36 block's sha follows the doc, as a real edit would (the ratchet otherwise refuses the replay)
  $PY - <<'REMAN'
import json, pathlib, sys
sys.path.insert(0, ".")
from scripts.advertised_ddl import extract_blocks
doc = pathlib.Path("documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md")
last = extract_blocks(doc)[-1]
p = pathlib.Path("scripts/advertised_ddl_manifest.json"); m = json.loads(p.read_text())
[e for e in m["blocks"] if e["label"].startswith("§36")][0]["sha256"] = last.sha256
p.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
REMAN
}
baseline() {  # recipe selector -> 0 when the clean tree passes it
  eval "$1 $2" > "$LOG" 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' "$LOG"; then echo "NO TEST SELECTED (bad): $3"; return 1; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $3  [$(grep -E '^=+ .*(passed|failed|error)' "$LOG" | tail -1)]"; return 1; fi
}
check2() {  # name old new recipe selector — a mutation of the plan's replayed block, manifest re-classified
  local name=$1 old=$2 new=$3 recipe=$4 sel=$5
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  baseline "$recipe" "$sel" "$name" || return
  if ! mutate "$DOC" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$DOC"; return; fi
  remanifest
  eval "$recipe $sel" > "$LOG" 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$DOC" "$MANIFEST"
}
check() {  # name file old new recipe selector
  local name=$1 file=$2 old=$3 new=$4 recipe=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  baseline "$recipe" "$sel" "$name" || return
  if ! mutate "$file" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  eval "$recipe $sel" > "$LOG" 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$file"
}

OUTBOX=src/services/target/outbox.py
MONITOR=scripts/delivery_monitor.py
HEALTH=src/services/target/delivery_health.py
RECORD=tests/src/services/target/test_outbox_failure_record.py
MON_TESTS=tests/scripts/test_delivery_monitor.py
DB=tests/scripts/test_outbox_failure_record_gate.py
LANE="tests/scripts/test_lineage_lane.py -k one_run_applies_the_whole_corpus"

# settle() records the class and the provider's code in the one CAS that leaves `sending`.
check "a dead token is filed as a lost response again" $OUTBOX '    failure_class = "credential_dead"' '    failure_class = "ambiguous"' "$UNIT" "$RECORD -k credential_dead"
check "settle records no failure at all" $OUTBOX '    if extra.get("failure"):' '    if False:' "$UNIT" "$RECORD -k recorded_in_the_update_that_leaves_sending"
check "the provider's code is dropped" $OUTBOX '        params["fcode"] = error_code' '        params["fcode"] = None' "$UNIT" "$RECORD -k 5xx"
check "the transport stops carrying a 401's code" src/channels/telegram_transport.py 'raise TelegramAuthDead(f"{method}: {code} {description}", code=code)' 'raise TelegramAuthDead(f"{method}: {code} {description}")' "$UNIT" "$RECORD -k credential_dead"
check "the transport stops carrying a 429's code" src/channels/telegram_transport.py $'scope="chat" if has_chat else "global",\n                code=code,' 'scope="chat" if has_chat else "global",' "$UNIT" "$RECORD -k rate_limited"
check "an error keeps a code of any type" $OUTBOX '        self.code = code if valid else None' '        self.code = code' "$UNIT" "$RECORD -k anything_else_is_none"
check "a foreign error's code is recorded as the provider's" $OUTBOX '        if isinstance(error, ChannelSendError)' '        if hasattr(error, "code")' "$UNIT" "$RECORD -k foreign_error"
# The other writer, against the real table as svc_worker.
check "a stranded row is recorded with no time" $OUTBOX '"       last_failed_at = now()"' '"       last_failed_at = NULL"' "$GATE" "$DB -k stranded_row_is_recorded"
# The doors, mutated in the plan's replayed block and read as svc_ingress.
check2 "a deferral counts as alerting" "         count(*) FILTER (WHERE state IN ('failed', 'ambiguous'))" "         count(*)" "$GATE" "$DB -k counted_across_workspaces"
check2 "the failures window is not clamped" "   WHERE last_failed_at >= now() - make_interval(secs => LEAST(GREATEST(p_window_seconds, 60), 86400))" "   WHERE last_failed_at >= now() - make_interval(secs => p_window_seconds)" "$GATE" "$DB -k window_is_clamped"
check2 "the sent door counts failed rows as sent" "   WHERE state = 'sent'" "   WHERE state IN ('sent', 'failed')" "$GATE" "$DB -k sent_door_counts_the_window"
check2 "PUBLIC keeps EXECUTE on the failures door" "REVOKE ALL ON FUNCTION fn_health_outbox_failures(p_window_seconds integer) FROM PUBLIC;" "-- (PUBLIC keeps EXECUTE)" "$GATE" "tests/scripts/test_rls_runtime_harness.py -k catalog_agrees_on_every_door"
# The file's own adoption probes: the runner refuses a file that does not leave what it claims.
check "the worker loses EXECUTE on the failures door" scripts/migrations/093_outbox_failure_record.sql 'GRANT EXECUTE ON FUNCTION fn_health_outbox_failures(p_window_seconds integer) TO svc_ingress, svc_worker;' 'GRANT EXECUTE ON FUNCTION fn_health_outbox_failures(p_window_seconds integer) TO svc_ingress;' "$GATE" "$LANE"
# What the route serves: the poller's wire contract and the alerting count.
check "the body loses sent_in_window" $HEALTH '        "sent_in_window": int(sent),' '' "$UNIT" "tests/src/api/test_app_factory.py -k pollers_strictness"
check "the alerting count sums the deferrals" $HEALTH '        "failed_or_ambiguous": sum(e["alerting"] for e in by_class.values()),' '        "failed_or_ambiguous": sum(e["rows"] for e in by_class.values()),' "$UNIT" "tests/src/services/target/test_delivery_health.py -k a_429_hour_alerts_on_nothing"
# The monitor: fires at 5, clears after two polls at 1 or fewer, repeats at 6 hours.
check "the alert fires at 6" $MONITOR 'DEFAULT_RAISE_AT = 5' 'DEFAULT_RAISE_AT = 6' "$UNIT" "$MON_TESTS -k five_fires_on_the_first_reading"
check "one quiet poll clears" $MONITOR 'CLEAR_POLLS = 2' 'CLEAR_POLLS = 1' "$UNIT" "$MON_TESTS -k one_quiet_poll_is_not_a_recovery"
check "the clear line moves to 2" $MONITOR 'DEFAULT_CLEAR_AT = 1' 'DEFAULT_CLEAR_AT = 2' "$UNIT" "$MON_TESTS -k two_is_not_quiet_enough"
check "a standing failure repeats at 5 hours" scripts/posting_monitor.py 'REALERT_AFTER_S = 6 * 3600' 'REALERT_AFTER_S = 5 * 3600' "$UNIT" "$MON_TESTS -k repeats_at_six_hours_and_not_before"
check "a reading in the dwell pages failing again" $MONITOR '        now_is = CLEARING if failing and run < CLEAR_POLLS else DELIVERING' '        now_is = FAILING if failing and run < CLEAR_POLLS else DELIVERING' "$UNIT" "$MON_TESTS -k quiet_reading_never_pages_failing"

rm -f "$LOG"
echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
