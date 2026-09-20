#!/bin/zsh
# Mutation battery for the fleet-health doors (081, #751): each behaviour has one named mutation that
# must make its test FAIL ("killed") — and must PASS on the clean tree first, or the verdict is
# BASELINE RED, not a kill; a selector that selects nothing is NO TEST SELECTED, never a kill. Files
# are restored from the COMMITTED tree after each, so commit first. Needs the Docker test Postgres
# (the DB recipe): the killers replay the advertised stream and run the surfaces as svc_ingress.
# `STORYDUMP_ROOT` points the battery at a worktree.
set -u
ROOT=${STORYDUMP_ROOT:-/Users/chris/Projects/storydump}
PY=/Users/chris/Projects/storydump/.venv/bin/python
cd "$ROOT" || exit 2
KEY=$($PY -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHANNEL_ID -u ADMIN_TELEGRAM_CHAT_ID -u WORKER_IMPL -u TARGET_DATABASE_URL PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 PYTHONDONTWRITEBYTECODE=1 ENCRYPTION_KEY=$KEY $PY -m pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude
RAN=0
EXPECTED=$(grep -cE '^(check|check2) "' "$0")
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
DOC=documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md
MANIFEST=scripts/advertised_ddl_manifest.json
remanifest() {  # the §24 block's sha follows the doc, as a real edit would (the ratchet otherwise refuses the replay)
  $PY - <<'REMAN'
import json, pathlib, sys
sys.path.insert(0, ".")
from scripts.advertised_ddl import extract_blocks
doc = pathlib.Path("documentation/planning/2026-08-02-consolidated-design-plan/07-security-model.md")
last = extract_blocks(doc)[-1]
p = pathlib.Path("scripts/advertised_ddl_manifest.json"); m = json.loads(p.read_text())
[e for e in m["blocks"] if e["label"].startswith("§24")][0]["sha256"] = last.sha256
p.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
REMAN
}
check2() {  # name old new test-selector — a mutation of the plan's replayed block, manifest re-classified
  local name=$1 old=$2 new=$3 sel=$4
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  RAN=$((RAN + 1))
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local base=$?
  if grep -qE '/ 0 selected|no tests ran' /tmp/claude/mut.log; then echo "NO TEST SELECTED (bad): $name"; return; fi
  if [ $base -ne 0 ]; then echo "BASELINE RED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; return; fi
  if ! mutate "$DOC" "$old" "$new"; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$DOC"; return; fi
  remanifest
  eval "$UNIT $sel" > /tmp/claude/mut.log 2>&1; local rc=$?
  verdict "$name" $rc
  cd "$ROOT" && git checkout -- "$DOC" "$MANIFEST"
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

GATE=tests/scripts/test_fleet_health_doors_gate.py
LANE="tests/scripts/test_lineage_lane.py -k one_run_applies_the_whole_corpus"

# The surfaces read the estate through the doors — each read going direct again is blind as svc_ingress.
check "posting_freshness stops reading the door's landings" src/services/target/posting_health.py 'SELECT o_posted_ever AS posted_ever,' 'SELECT 0::bigint AS posted_ever,' "$GATE -k posting_surfaces_see_the_estate"
check "destinations counts ig_accounts directly again" src/services/target/posting_health.py 'SELECT o_accounts_active AS accounts_active,' "SELECT (SELECT count(*) FROM ig_accounts WHERE state = 'active') AS accounts_active," "$GATE -k posting_surfaces_see_the_estate"
check "scheduling_lag counts ig_accounts directly again" src/services/target/scheduling_health.py '   o_accounts_active AS accounts_active,' "   (SELECT count(*) FROM ig_accounts WHERE state = 'active') AS accounts_active," "$GATE -k scheduling_surfaces_see_the_estate"
check "the outbox count goes direct again" src/services/target/backpressure.py 'text("SELECT fn_health_outbox_pending()")' "text(\"SELECT count(*) FROM channel_outbox WHERE state = 'pending'\")" "$GATE -k scheduling_surfaces_see_the_estate"
check "the ready lanes go direct again (the worker's own signal too)" src/services/target/backpressure.py '  FROM fn_health_ready_lanes()' "  FROM (SELECT lane AS o_lane, count(*) AS o_ready, EXTRACT(EPOCH FROM max(now() - run_at)) AS o_oldest_age FROM jobs WHERE state = 'ready' AND run_at <= now() GROUP BY lane) t" "$GATE -k worker_login_reads_the_backpressure"
# The landing filter is pinned on the LIVE door in the replayed world: mutating the plan's block (what
# the gates replay) and re-classifying it in the manifest, then reading pg_proc, is the one honest kill.
check2 "the live door's landing filter loses the dry-run exclusion" "  SELECT count(*) FILTER (WHERE state = 'posted' AND published_via NOT IN ('legacy_backfill', 'dry_run'))," "  SELECT count(*) FILTER (WHERE state = 'posted' AND published_via <> 'legacy_backfill')," "$GATE -k live_door_filters_landings"
# The file's own adoption probes: the runner refuses a file that does not leave what it claims.
check "the one policy svc_maintenance lacked is not created" scripts/migrations/081_fleet_health_doors.sql 'CREATE POLICY p_maint_accts ON ig_accounts FOR SELECT TO svc_maintenance USING (true);' '-- (no policy)' "$LANE"
check "the worker loses EXECUTE on a backpressure door" scripts/migrations/081_fleet_health_doors.sql 'GRANT EXECUTE ON FUNCTION fn_health_ready_lanes() TO svc_ingress, svc_worker;' 'GRANT EXECUTE ON FUNCTION fn_health_ready_lanes() TO svc_ingress;' "$LANE"
check "the CREATE bracket is left open on svc_maintenance" scripts/migrations/081_fleet_health_doors.sql 'REVOKE CREATE ON SCHEMA public FROM svc_maintenance;' '-- (bracket left open)' "$LANE"

echo "ran $RAN of $EXPECTED mutations${ONLY:+ (ONLY=$ONLY)}"
