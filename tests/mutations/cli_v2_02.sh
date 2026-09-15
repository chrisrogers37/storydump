#!/bin/zsh
# Mutation battery for phase 02 of the v2 CLI (the read views): each mutation must make its named
# test FAIL ("killed"); the file is restored from the COMMITTED tree after each, so commit first.
# Run from the repo root with the sandbox off (units resolve DNS; the gates need the Docker
# postgres on 65433). `ONLY=<regex>` runs a subset. The tenant-predicate mutations are killed by
# the gate's BYPASSRLS arm — under the policies alone they would survive, which is the point.
set -u
cd /Users/chris/Projects/storydump || exit 2
KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
UNIT="env -u DB_HOST -u DB_USER -u DB_PASSWORD -u DB_NAME -u TEST_DB_NAME -u REQUIRE_TEST_DATABASE PYTHONDONTWRITEBYTECODE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 DB_PORT=65432 ENCRYPTION_KEY=$KEY .venv/bin/pytest -q -p no:cacheprovider --no-cov -x"
GATE="env PYTHONDONTWRITEBYTECODE=1 PATH=/opt/homebrew/opt/postgresql@15/bin:$PATH DB_HOST=localhost DB_PORT=65433 DB_USER=test_user DB_PASSWORD=test_password DB_NAME=storyline_ai TEST_DB_NAME=storyline_test REQUIRE_TEST_DATABASE=1 TELEGRAM_BOT_TOKEN=dummy TELEGRAM_CHANNEL_ID=1 ADMIN_TELEGRAM_CHAT_ID=1 ENCRYPTION_KEY=$KEY .venv/bin/pytest -q -p no:cacheprovider --no-cov -x"
mkdir -p /tmp/claude

check() {  # name file old new runner test-selector
  local name=$1 file=$2 old=$3 new=$4 runner=$5 sel=$6
  if [ -n "${ONLY:-}" ] && ! [[ "$name" =~ $ONLY ]]; then return; fi
  OLD="$old" NEW="$new" .venv/bin/python - "$file" <<'PY'
import os, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(); old = os.environ["OLD"]; new = os.environ["NEW"]
if s.count(old) != 1:
    print(f"MUTATION NOT APPLIED ({s.count(old)} matches)"); sys.exit(3)
p.write_text(s.replace(old, new, 1))
PY
  if [ $? -ne 0 ]; then echo "MUTATION NOT APPLIED: $name"; git checkout -- "$file"; return; fi
  rm -rf "$(dirname "$file")/__pycache__"
  if eval "$runner $sel" > /tmp/claude/mut.log 2>&1; then echo "SURVIVED (bad): $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; else echo "killed: $name  [$(grep -E '^=+ .*(passed|failed|error)' /tmp/claude/mut.log | tail -1)]"; fi
  cd /Users/chris/Projects/storydump && git checkout -- "$file"
}
OV=src/services/target/ops_views.py
OR=src/api/routes/ops.py
PR=src/api/principal.py
TU=tests/src/api/test_ops_routes.py
GT=tests/scripts/test_ops_views_gate.py
BYPASS="$GT -k without_row_level_security"

# --- the tenant predicates (killed by the BYPASSRLS arm) --------------------
check "story reads any workspace's intent" $OV '    " FROM post_intents i WHERE i.workspace_id = :ws AND i.id = :id"' '    " FROM post_intents i WHERE i.id = :id"' "$GATE" "$BYPASS"
check "cards forgets the workspace" $OV '    " WHERE o.workspace_id = :ws AND o.intent_id = :id"
    f" ORDER BY o.created_at LIMIT {STORY_ROWS}"
)


async def cards(' '    " WHERE o.intent_id = :id"
    f" ORDER BY o.created_at LIMIT {STORY_ROWS}"
)


async def cards(' "$GATE" "$BYPASS"
check "floating lists every workspace" $OV '    " WHERE i.workspace_id = :ws AND i.state = '"'"'approved'"'"' AND i.cap_consumed_on IS NOT NULL"' '    " WHERE i.state = '"'"'approved'"'"' AND i.cap_consumed_on IS NOT NULL"' "$GATE" "$BYPASS"
check "account answers for another workspace's handle" $OV '    " WHERE a.workspace_id = :ws"
    "   AND (a.id::text = :key OR ltrim(a.handle, '"'"'@'"'"') = ltrim(:key, '"'"'@'"'"'))"' '    " WHERE (a.id::text = :key OR ltrim(a.handle, '"'"'@'"'"') = ltrim(:key, '"'"'@'"'"'))"' "$GATE" "$BYPASS"
check "jobs counts the fleet, system rows included" $OV '    " FROM jobs j WHERE j.workspace_id = :ws AND j.created_at >= :since"' '    " FROM jobs j WHERE j.created_at >= :since"' "$GATE" "$BYPASS"
check "outbox reads every binding" $OV '    " WHERE o.workspace_id = :ws AND o.created_at >= :since"' '    " WHERE o.created_at >= :since"' "$GATE" "$BYPASS"
check "burst's taps cross workspaces" $OV '    " WHERE a.workspace_id = :ws AND a.entity_kind = '"'"'post_intent'"'"'"' '    " WHERE a.entity_kind = '"'"'post_intent'"'"'"' "$GATE" "$BYPASS"

# --- shapes and bounds (the ingress arm) -----------------------------------
check "a ready job carries samples" $OV '    " CASE WHEN j.state IN ('"'"'failed'"'"', '"'"'review_required'"'"') THEN"' '    " CASE WHEN true THEN"' "$GATE" "$GT -k returns_only_this_workspaces_rows"
check "the handle must carry the @ to match" $OV '    "   AND (a.id::text = :key OR ltrim(a.handle, '"'"'@'"'"') = ltrim(:key, '"'"'@'"'"'))"' '    "   AND (a.id::text = :key OR a.handle = :key)"' "$GATE" "$GT -k returns_only_this_workspaces_rows"
check "burst forgets the window's outcomes" $OV '    for row in await readers.rows(conn, _OUTCOMES, **params):' '    for row in []:' "$GATE" "$GT -k returns_only_this_workspaces_rows"
check "floating forgets the waiting job" $OV '    "      AND j.state IN ('"'"'ready'"'"', '"'"'leased'"'"', '"'"'failed'"'"')"' '    "      AND j.state IN ('"'"'leased'"'"', '"'"'failed'"'"')"' "$GATE" "$GT -k returns_only_this_workspaces_rows"
check "floating hides a float whose retry died" $OV '    "      AND j.state IN ('"'"'ready'"'"', '"'"'leased'"'"', '"'"'failed'"'"')"' '    "      AND j.state IN ('"'"'ready'"'"', '"'"'leased'"'"')"' "$GATE" "$GT -k returns_only_this_workspaces_rows"
check "floating prefers the dead job over the live one" $OV '    "    ORDER BY (j.state IN ('"'"'ready'"'"', '"'"'leased'"'"')) DESC, j.run_at DESC LIMIT 1) j ON true"' '    "    ORDER BY (j.state IN ('"'"'ready'"'"', '"'"'leased'"'"')) ASC, j.run_at DESC LIMIT 1) j ON true"' "$GATE" "$GT -k returns_only_this_workspaces_rows"
check "the ledger is reported present when unreadable" $OV '        if found["ok"]:
            ledger = "present"' '        if True:
            ledger = "present"' "$UNIT" "tests/src/services/target/test_ops_views.py -k unreadable_not_guessed"

# --- the routes ---------------------------------------------------------------
check "an ops route leaves the allowlist" $PR '        ("GET", "/api/v1/ops/workspaces/{ws}/burst"),
' '' "$UNIT" "$TU -k every_ops_route_is_admitted"
check "a service identity reads any workspace" $OR '        require_own_workspace(principal, str(ws))
        async with v1._open_tenant(request, str(ws), principal) as session:' '        async with v1._open_tenant(request, str(ws), principal) as session:' "$UNIT" "$TU -k without_a_membership"
check "a service identity is gated on a membership it has not got" $OR '    if principal.is_service_identity:
        require_own_workspace(principal, str(ws))' '    if False:
        require_own_workspace(principal, str(ws))' "$UNIT" "$TU -k without_a_membership"
check "the window defaults to thirty hours" $OR 'DEFAULT_WINDOW = dt.timedelta(hours=3)' 'DEFAULT_WINDOW = dt.timedelta(hours=30)' "$UNIT" "$TU -k defaults_to_three_hours"
check "a negative window is accepted" $OR '_RELATIVE = re.compile(r"^(\d+)([mhd])$")' '_RELATIVE = re.compile(r"^(-?\d+)([mhd])$")' "$UNIT" "$TU -k anything_else_is_refused"
check "a naive timestamp is accepted" $OR '    if parsed.tzinfo is None:
        raise ValueError(f"a window needs a zone: {value!r}")' '    if False:
        raise ValueError(f"a window needs a zone: {value!r}")' "$UNIT" "$TU -k anything_else_is_refused"
check "a bad window is a 500" $OR '    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))' '    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))' "$UNIT" "$TU -k bad_window_is_422"
check "floating's limit is unbounded" $OR '    limit: int = Query(ops_views.FLOATING_LIMIT, ge=1, le=ops_views.FLOATING_LIMIT_MAX),' '    limit: int = Query(ops_views.FLOATING_LIMIT, ge=1),' "$UNIT" "$TU -k clamps_its_limit"

# --- the fold ------------------------------------------------------------------
VO=src/services/target/vocabulary.py
WA=storydump_cli/watch.py
RD=storydump_cli/commands/reads.py
check "a window may be wider than thirty days" $VO '    if anchor - start > dt.timedelta(days=MAX_WINDOW_DAYS):' '    if False:' "$UNIT" "tests/src/services/target/test_vocabulary.py -k everything_else_is_refused"
check "a window may start in the future" $VO '    if start > anchor:' '    if False:' "$UNIT" "tests/src/services/target/test_vocabulary.py -k everything_else_is_refused"
check "an overflow escapes the grammar" $VO '    except (ValueError, OverflowError):' '    except ValueError:' "$UNIT" "tests/src/api/test_ops_routes.py -k overflowing_window"
check "burst stamps the caller's workspace on its rows" $OV '            rows.append({"section": section, **row})' '            rows.append({"section": section, **row, "workspace_id": workspace_id})' "$UNIT" "tests/src/services/target/test_ops_views.py -k merged_in_time_order"
check "the ledger is probed by name" $OV '        "SELECT has_schema_privilege(current_user, n.oid, '"'"'USAGE'"'"')"' '        "SELECT has_schema_privilege(current_user, '"'"'runner'"'"', '"'"'USAGE'"'"')"' "$GATE" "$GT -k every_ledger_state"
check "a dropped policy vanishes from posture" $OV '        " WHERE n.nspname = '"'"'public'"'"' AND c.relkind = '"'"'r'"'"'"
        "   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"' '        " WHERE n.nspname = '"'"'public'"'"' AND c.relkind = '"'"'r'"'"' AND c.relrowsecurity"
        "   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"' "$GATE" "$GT -k every_ledger_state"
check "a burst permit's generation leaves the key" $WA '        row.get("generation"),
        row.get("waiting_id"),' '        None,
        row.get("waiting_id"),' "$UNIT" "tests/storydump_cli/test_watch.py -k two_permits_and_two_siblings"
check "a failure present at the baseline is fatal" $WA '            failure = watched.failed(fresh) if previous is not None else None' '            failure = watched.failed(rows)' "$UNIT" "tests/storydump_cli/test_watch.py -k printed_not_fatal"
check "the interval has no floor" $RD '        type=click.FloatRange(min=1),' '        type=click.FloatRange(min=0, min_open=True),' "$UNIT" "tests/storydump_cli/test_reads.py -k below_one_second"
check "only the first workspace of a name is read" $RD '    named = [str(ws["id"]) for ws in listed if ws.get("name") == workspace]' '    named = [str(ws["id"]) for ws in listed if ws.get("name") == workspace][:1]' "$UNIT" "tests/storydump_cli/test_reads.py -k shared_by_two_workspaces"
