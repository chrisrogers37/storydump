"""The read views behind `storydump story|cards|floating|account|jobs|outbox|burst|posture`
(phase 02 of the v2 CLI, `documentation/planning/2026-09-15-cli-v2/02_reads.md`).

Each view is a bounded, tenant-scoped read — one statement per list it
returns (a story's timeline is four, a burst's sections six): an explicit
``workspace_id = :ws`` predicate on every table it touches, no
cross-workspace join, and a ``LIMIT`` or a time window on every statement. They run under the caller's
tenant claim as ``svc_ingress`` — the gate proves them against the replayed
schema with a second workspace seeded — so the F.4 posture switch cannot
change what they return. Two tables sit outside row-level security and are
NEVER read here: ``rate_counters`` (no workspace column) and the system rows
of ``jobs`` (``workspace_id IS NULL``; the explicit predicate excludes them).

The queries are the production probes the float investigation was validated
with (`../probes/*.sql`), made tenant-scoped and bounded. Every row carries
``workspace_id`` so the CLI's one envelope shape holds whether it read one
workspace or looped over several.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from src.services.target import readers

#: `floating`'s default and ceiling; every other list is windowed by `since`.
FLOATING_LIMIT = 100
FLOATING_LIMIT_MAX = 500
#: The bound inside a story's own timeline (audit rows) and per-section lists.
STORY_ROWS = 500
SECTION_ROWS = 500
SAMPLES = 5

_INTENT = (
    "SELECT i.id, i.workspace_id, i.state, i.publish_step, i.cap_consumed_on,"
    " i.attempts_by_step, i.entered_state_at, i.schedule_slot_at, i.ig_account_id,"
    " i.media_item_id, i.last_error"
    " FROM post_intents i WHERE i.workspace_id = :ws AND i.id = :id"
)

_AUDIT = (
    "SELECT a.created_at AS at, a.from_state, a.to_state, a.actor_kind,"
    " a.actor_user_id, a.channel, a.detail"
    " FROM audit_events a WHERE a.workspace_id = :ws AND a.entity_id = :id"
    f" ORDER BY a.id LIMIT {STORY_ROWS}"
)

_OPERATIONS = (
    "SELECT o.created_at AS at, o.op_kind, o.generation, o.state,"
    " NULLIF(o.response_ref->>'url_variant', '')::int AS url_variant,"
    " o.response_ref->>'error' AS error,"
    " o.response_ref->'meta'->>'subcode' AS subcode,"
    " NULLIF(o.response_ref->>'elapsed_ms', '')::int AS elapsed_ms"
    " FROM provider_operations o WHERE o.workspace_id = :ws AND o.intent_id = :id"
    f" ORDER BY o.created_at, o.generation LIMIT {STORY_ROWS}"
)

_STORY_CARDS = (
    "SELECT o.created_at AS at, o.binding_id, o.kind, o.state,"
    " o.external_message_ref, o.attempts, o.payload->>'outcome_text' AS outcome_text,"
    " o.payload->>'supersedes_ref' AS supersedes_ref"
    " FROM channel_outbox o WHERE o.workspace_id = :ws AND o.intent_id = :id"
    f" ORDER BY o.created_at LIMIT {STORY_ROWS}"
)


async def story(conn, *, workspace_id: str, intent_id: str) -> list[dict[str, Any]]:
    """One story's whole timeline: the intent, its audit rows (the
    `cli_command` rows included), its provider operations and its cards.
    Zero rows when the story is not this workspace's — never a 404 from the
    view; the caller decides what absence means."""
    intent = await readers.row(conn, _INTENT, ws=workspace_id, id=intent_id)
    if intent is None:
        return []
    params = {"ws": workspace_id, "id": intent_id}
    return [
        {
            "workspace_id": workspace_id,
            "intent": intent,
            "audit": await readers.rows(conn, _AUDIT, **params),
            "operations": await readers.rows(conn, _OPERATIONS, **params),
            "cards": await readers.rows(conn, _STORY_CARDS, **params),
        }
    ]


_CARDS = (
    "SELECT o.workspace_id, o.id, o.binding_id, b.channel, b.external_ref, o.kind,"
    " o.state, o.external_message_ref, o.attempts,"
    " o.payload->>'outcome_text' AS outcome_text,"
    " o.payload->>'supersedes_ref' AS supersedes_ref, o.created_at, o.updated_at"
    " FROM channel_outbox o"
    " JOIN channel_bindings b ON b.workspace_id = o.workspace_id AND b.id = o.binding_id"
    " WHERE o.workspace_id = :ws AND o.intent_id = :id"
    f" ORDER BY o.created_at LIMIT {STORY_ROWS}"
)


async def cards(conn, *, workspace_id: str, intent_id: str) -> list[dict[str, Any]]:
    """A story's cards on every binding, adopted twins included, in send order."""
    return await readers.rows(conn, _CARDS, ws=workspace_id, id=intent_id)


_FLOATING = (
    "SELECT i.workspace_id, i.id, i.publish_step, i.attempts_by_step,"
    " i.cap_consumed_on, i.entered_state_at,"
    " j.state AS job_state, j.run_at AS job_run_at, j.attempts AS job_attempts,"
    " w.detail->>'class' AS last_wait_class,"
    " NULLIF(w.detail->>'rung', '')::int AS last_wait_rung, w.created_at AS last_wait_at"
    " FROM post_intents i"
    " LEFT JOIN LATERAL ("
    "   SELECT j.state, j.run_at, j.attempts FROM jobs j"
    # `IN (...)`, not `=`: the producer pin (`test_operator_floor_preconditions`)
    # greps the `kind =` assignment form for MINTERS of the job; this is a read.
    "    WHERE j.workspace_id = i.workspace_id AND j.kind IN ('publish_pipeline')"
    "      AND j.payload->>'intent_id' = i.id::text"
    "      AND j.state IN ('ready', 'leased', 'failed')"
    "    ORDER BY (j.state IN ('ready', 'leased')) DESC, j.run_at DESC LIMIT 1) j ON true"
    " LEFT JOIN LATERAL ("
    "   SELECT a.detail, a.created_at FROM audit_events a"
    "    WHERE a.workspace_id = i.workspace_id AND a.entity_id = i.id"
    "      AND a.detail->>'event' = 'float_wait'"
    "    ORDER BY a.id DESC LIMIT 1) w ON true"
    " WHERE i.workspace_id = :ws AND i.state = 'approved' AND i.cap_consumed_on IS NOT NULL"
    " ORDER BY j.run_at NULLS LAST, i.entered_state_at LIMIT :lim"
)


async def floating(
    conn, *, workspace_id: str, limit: int = FLOATING_LIMIT
) -> list[dict[str, Any]]:
    """Approved stories carrying a debit — waiting between attempts — with the
    job that will retry them (or, when none is live, the failed one: a float
    whose retry died is the failure `--watch` exits 6 on) and the last
    wait's class and rung."""
    # the route refuses a limit above the ceiling (422); a direct caller is
    # clamped, so the statement is bounded whoever asks
    return await readers.rows(
        conn,
        _FLOATING,
        ws=workspace_id,
        lim=max(1, min(int(limit), FLOATING_LIMIT_MAX)),
    )


_ACCOUNT = (
    "SELECT a.workspace_id, a.id, a.handle,"
    " COALESCE(a.posts_per_day, w.posts_per_day) AS posts_per_day,"
    " COALESCE(a.tz, w.tz) AS tz, a.next_slot_at,"
    " (SELECT jsonb_build_object('local_date', d.local_date, 'count', d.count,"
    "                            'cap_at_write', d.cap_at_write)"
    "    FROM daily_post_counts d"
    "   WHERE d.workspace_id = a.workspace_id AND d.ig_account_id = a.id"
    "     AND d.local_date = (now() AT TIME ZONE COALESCE(a.tz, w.tz, 'UTC'))::date"
    " ) AS today,"
    " (SELECT COALESCE(jsonb_agg(jsonb_build_object('id', p.id, 'state', p.state,"
    "                                               'entered_state_at', p.entered_state_at)"
    "                  ORDER BY p.entered_state_at DESC), '[]'::jsonb)"
    "    FROM (SELECT p.id, p.state, p.entered_state_at FROM post_intents p"
    "           WHERE p.workspace_id = a.workspace_id AND p.ig_account_id = a.id"
    "           ORDER BY p.entered_state_at DESC LIMIT 20) p"
    " ) AS recent"
    " FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
    " WHERE a.workspace_id = :ws"
    "   AND (a.id::text = :key"
    "        OR lower(ltrim(a.handle, '@')) = lower(ltrim(:key, '@')))"
    " ORDER BY a.created_at LIMIT 20"
)


async def account(conn, *, workspace_id: str, key: str) -> list[dict[str, Any]]:
    """An Instagram account by handle (with or without the @) or id: its
    effective cap and zone, the next slot, today's debit bucket and the last
    twenty stories' outcomes."""
    return await readers.rows(conn, _ACCOUNT, ws=workspace_id, key=key.strip())


_JOBS = (
    "SELECT j.workspace_id, j.kind, j.lane, j.state, count(*) AS count,"
    " min(j.run_at) AS oldest_run_at,"
    " CASE WHEN j.state IN ('failed', 'review_required') THEN"
    "   (SELECT jsonb_agg(s) FROM ("
    "      SELECT x.id, x.attempts, x.run_at, p.last_error AS error"
    "        FROM jobs x"
    "        LEFT JOIN post_intents p ON p.workspace_id = x.workspace_id"
    "         AND p.id::text = x.payload->>'intent_id'"
    "       WHERE x.workspace_id = j.workspace_id AND x.kind = j.kind"
    "         AND x.lane = j.lane AND x.state = j.state AND x.created_at >= :since"
    f"       ORDER BY x.run_at DESC LIMIT {SAMPLES}) s)"
    " END AS samples"
    " FROM jobs j WHERE j.workspace_id = :ws"
    "   AND (j.state IN ('ready', 'leased') OR j.created_at >= :since)"
    " GROUP BY j.workspace_id, j.kind, j.lane, j.state"
    " ORDER BY j.kind, j.lane, j.state"
)


async def jobs(conn, *, workspace_id: str, since: dt.datetime) -> list[dict[str, Any]]:
    """This workspace's jobs, counted by kind × lane × state: everything
    still owed (`ready`, `leased`) at any age — a stuck job is the one to see
    — and the finished ones minted in the window; the oldest runnable and a
    few failed samples (a publish job's sample carries its story's
    `last_error`). System singletons have no workspace and never appear."""
    return await readers.rows(conn, _JOBS, ws=workspace_id, since=since)


_OUTBOX = (
    "SELECT o.workspace_id, o.binding_id, b.channel, b.external_ref, o.kind, o.state,"
    " count(*) AS count, min(o.created_at) AS oldest_created_at"
    " FROM channel_outbox o"
    " JOIN channel_bindings b ON b.workspace_id = o.workspace_id AND b.id = o.binding_id"
    " WHERE o.workspace_id = :ws"
    "   AND o.state IN ('pending', 'sending', 'ambiguous', 'failed')"
    "   AND (o.state <> 'failed' OR o.created_at >= :since)"
    " GROUP BY o.workspace_id, o.binding_id, b.channel, b.external_ref, o.kind, o.state"
    " ORDER BY o.state, o.kind, b.external_ref"
)


async def outbox(
    conn, *, workspace_id: str, since: dt.datetime
) -> list[dict[str, Any]]:
    """The rows still owed or lost on every binding: pending, sending and
    ambiguous at any age (the owed ones), failed in the window. No paced
    holds in this release (`rate_counters` is outside row-level security)."""
    return await readers.rows(conn, _OUTBOX, ws=workspace_id, since=since)


# Every section selects the table's OWN workspace_id — never a stamp from the
# parameter — so a row that crossed workspaces would carry the other one's id
# and the gate's bypass arm would see it.
_TAPS = (
    "SELECT a.workspace_id, a.created_at AS at, a.entity_id AS intent_id,"
    " a.from_state, a.to_state,"
    " a.actor_kind, a.channel"
    " FROM audit_events a"
    " WHERE a.workspace_id = :ws AND a.entity_kind = 'post_intent'"
    "   AND a.from_state = 'awaiting_approval' AND a.created_at >= :since"
    f" ORDER BY a.created_at LIMIT {SECTION_ROWS}"
)
_PERMITS = (
    "SELECT o.workspace_id, o.created_at AS at, o.intent_id, o.generation, o.state,"
    " NULLIF(o.response_ref->>'url_variant', '')::int AS url_variant,"
    " o.response_ref->>'error' AS error,"
    " o.response_ref->'meta'->>'subcode' AS subcode,"
    " NULLIF(o.response_ref->>'elapsed_ms', '')::int AS elapsed_ms"
    " FROM provider_operations o"
    " WHERE o.workspace_id = :ws AND o.op_kind = 'container_create'"
    "   AND o.created_at >= :since"
    f" ORDER BY o.created_at LIMIT {SECTION_ROWS}"
)
_WAITS = (
    "SELECT a.workspace_id, a.created_at AS at, a.entity_id AS intent_id,"
    " a.detail->>'class' AS wait_class, NULLIF(a.detail->>'rung', '')::int AS rung,"
    " NULLIF(a.detail->>'seconds', '')::int AS seconds,"
    " a.detail->>'next_run_at' AS next_run_at"
    " FROM audit_events a"
    " WHERE a.workspace_id = :ws AND a.detail->>'event' = 'float_wait'"
    "   AND a.created_at >= :since"
    f" ORDER BY a.created_at LIMIT {SECTION_ROWS}"
)
_SIBLINGS = (
    "SELECT p.workspace_id, p.created_at AS at, p.entity_id AS intent_id,"
    " p.entity_id AS posted_id, w.entity_id AS waiting_id,"
    " w.detail->>'class' AS wait_class"
    " FROM audit_events p"
    " JOIN post_intents pi ON pi.workspace_id = p.workspace_id AND pi.id = p.entity_id"
    " JOIN audit_events w ON w.workspace_id = p.workspace_id"
    "  AND w.detail->>'event' = 'float_wait' AND w.entity_id <> p.entity_id"
    "  AND w.created_at >= CAST(:since AS timestamptz) - interval '1 day'"
    "  AND w.created_at < p.created_at"
    "  AND (w.detail->>'next_run_at')::timestamptz > p.created_at"
    " JOIN post_intents wi ON wi.workspace_id = w.workspace_id AND wi.id = w.entity_id"
    "  AND wi.ig_account_id = pi.ig_account_id"
    " WHERE p.workspace_id = :ws AND p.to_state = 'posted' AND p.created_at >= :since"
    f" ORDER BY p.created_at LIMIT {SECTION_ROWS}"
)
_REVIEWS = (
    "SELECT a.workspace_id, a.created_at AS at, a.entity_id AS intent_id,"
    " a.from_state, i.last_error"
    " FROM audit_events a"
    " JOIN post_intents i ON i.workspace_id = a.workspace_id AND i.id = a.entity_id"
    " WHERE a.workspace_id = :ws AND a.to_state = 'review_required'"
    "   AND a.created_at >= :since"
    f" ORDER BY a.created_at LIMIT {SECTION_ROWS}"
)
_OUTCOMES = (
    "SELECT i.workspace_id, i.state, count(*) AS count FROM post_intents i"
    " WHERE i.workspace_id = :ws AND i.entered_state_at >= :since"
    " GROUP BY i.workspace_id, i.state ORDER BY count DESC, i.state"
)


async def burst(conn, *, workspace_id: str, since: dt.datetime) -> list[dict[str, Any]]:
    """The window as one timeline — taps and their outcomes, container
    permits with the url variant and the answer, float waits, siblings that
    posted past a waiter, review cards raised — and the window's outcome
    counts last. Each row names its section; the CLI groups on it."""
    params = {"ws": workspace_id, "since": since}
    rows: list[dict[str, Any]] = []
    for section, sql in (
        ("tap", _TAPS),
        ("permit", _PERMITS),
        ("float_wait", _WAITS),
        ("sibling", _SIBLINGS),
        ("review", _REVIEWS),
    ):
        for row in await readers.rows(conn, sql, **params):
            rows.append({"section": section, **row})
    rows.sort(key=lambda r: r["at"])
    for row in await readers.rows(conn, _OUTCOMES, **params):
        rows.append({"section": "outcome", "at": None, "intent_id": None, **row})
    return rows


async def posture(conn) -> dict[str, Any]:
    """Not tenant data: the runner's ledger (read under the grant the runner
    makes for the API role — fork F7), the connected role and whether it
    bypasses row-level security, every tenant-plane table with its RLS
    state, and the SECURITY DEFINER census. A missing ledger (a replayed
    gate database) is reported, never guessed. Deployment internals, by
    design, to any authenticated principal (`07` §1)."""
    role = await readers.row(
        conn,
        'SELECT current_user AS "user",'
        " (SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user)"
        " AS bypassrls",
    )
    # the catalogs, by oid: resolving the NAME of a table in a schema the
    # role has no USAGE on is itself a permission error (`to_regclass` raised
    # it), and a role without the grant is exactly the case to report
    found = await readers.row(
        conn,
        "SELECT has_schema_privilege(current_user, n.oid, 'USAGE')"
        "   AND has_table_privilege(current_user, c.oid, 'SELECT') AS ok"
        "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'runner' AND c.relname = 'schema_migrations'",
    )
    migrations: list[dict[str, Any]] = []
    ledger = "absent"
    if found is not None:
        if found["ok"]:
            ledger = "present"
            migrations = await readers.rows(
                conn,
                "SELECT version, checksum, applied_at, status"
                " FROM runner.schema_migrations ORDER BY version",
            )
        else:
            ledger = "unreadable"
    # every tenant-plane table — the ones with a workspace column — whether or
    # not RLS is on, so a table whose policies were dropped shows `enabled:
    # false` instead of vanishing from the list
    rls = await readers.rows(
        conn,
        'SELECT c.relname AS "table", c.relrowsecurity AS enabled,'
        " c.relforcerowsecurity AS forced"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind = 'r'"
        "   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid"
        "               AND a.attname = 'workspace_id' AND NOT a.attisdropped)"
        " ORDER BY c.relname",
    )
    doors = await readers.rows(
        conn,
        "SELECT p.proname AS name, r.rolname AS owner"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        " JOIN pg_roles r ON r.oid = p.proowner"
        " WHERE n.nspname = 'public' AND p.prosecdef ORDER BY p.proname",
    )
    return {
        "ledger": ledger,
        "migrations": migrations,
        "role": role,
        "rls": rls,
        "doors": doors,
    }
