"""The activation funnel door counts onboarding across every workspace (092, #1481).

`fn_activation_funnel(p_since, p_stall)` answers how far the people who signed
up since `p_since` got (signed in, workspace created, Instagram connected,
folder added, first approval) and how many stalled at each stage, in five rows
of counts. The question has no tenant and every table it reads is
tenant-scoped or user-plane, so it is a SECURITY DEFINER door owned by
`svc_maintenance` and executable by `svc_worker` alone: the login the `psql`
escape hatch connects as, and not the API's.

One estate, seeded through the stream login (the tables' owner) with every
timestamp relative to `now()`, across workspaces with different owners:

- COMPLETE: a workspace, an account, a folder, and a story the person posted
  by hand, walked through the ledger's own edges so that the approval is the
  audit trigger's row;
- STALLED: a workspace 100 hours old and nothing since;
- FRESH: the same gap, 10 hours old, not yet a stall;
- FOLDER FIRST: a folder added 5 hours ago in a workspace 90 hours old, and no
  account;
- MEMBER: a member of COMPLETE's workspace who owns none, signed up 200 hours
  ago;
- SUSPENDED: the owner of a suspended workspace that holds an account, a
  folder and an approval, signed up 6 days ago;
- DISABLED and EARLY: a disabled person and one who signed up before the
  window, each owning a workspace.
"""

from __future__ import annotations

import uuid

import psycopg2
import psycopg2.errors
import pytest

from tests.scripts.conftest import (
    _scratch,
    as_user,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

#: The operator's query, as `reading-the-ledger.md` prints it.
FUNNEL = "SELECT * FROM fn_activation_funnel(now() - interval '30 days')"

STAGES = (
    "signed in",
    "workspace created",
    "Instagram connected",
    "folder added",
    "first approval",
)

#: The seeded estate at the default 72-hour window, as
#: (o_ordinal, o_stage, o_reached, o_stalled). Reached: six people signed up in
#: the window (DISABLED and EARLY are not counted); four of them own an active
#: workspace (MEMBER owns none, and SUSPENDED's workspace is not active); only
#: COMPLETE connected an account; COMPLETE and FOLDER FIRST added a folder; only
#: COMPLETE approved. Stalled: SUSPENDED at stage 1, STALLED at stage 2.
EXPECTED = [
    (1, "signed in", 6, 1),
    (2, "workspace created", 4, 1),
    (3, "Instagram connected", 1, 0),
    (4, "folder added", 2, 0),
    (5, "first approval", 1, 0),
]

#: The tenant-scoped tables the door reads.
TENANT_TABLES = (
    "workspaces",
    "workspace_members",
    "ig_accounts",
    "media_sources",
    "audit_events",
)


def _person(cur, *, ago: str, state: str = "active") -> str:
    cur.execute(
        "INSERT INTO users (state, created_at) VALUES (%s, now() - %s::interval)"
        " RETURNING id",
        (state, ago),
    )
    return cur.fetchone()[0]


def _workspace(cur, owner: str, name: str, *, ago: str, state: str = "active") -> str:
    """A workspace and its owner row, the pair `ct_workspaces_owner_at_insert`
    requires by commit."""
    cur.execute(
        "INSERT INTO workspaces (name, state, created_at)"
        " VALUES (%s, %s, now() - %s::interval) RETURNING id",
        (name, state, ago),
    )
    ws = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role)"
        " VALUES (%s, %s, 'owner')",
        (ws, owner),
    )
    return ws


def _folder(cur, ws: str, *, ago: str) -> None:
    cur.execute(
        "INSERT INTO media_sources (workspace_id, provider, config, created_at)"
        " VALUES (%s, 'gdrive', '{\"v\": 1}', now() - %s::interval)",
        (ws, ago),
    )


def _instagram(cur, ws: str, ref: str, *, ago: str) -> None:
    cur.execute(
        "INSERT INTO ig_accounts (workspace_id, provider_account_ref, created_at)"
        " VALUES (%s, %s, now() - %s::interval)",
        (ws, ref, ago),
    )


def _approval_row(cur, ws: str, *, ago: str, detail: str | None = None) -> None:
    """An `awaiting_approval -> approved` audit row for a story in *ws*.
    Without *detail* it has the shape `trg_intent_audit` writes; with one, the
    shape of a row a service writes by hand (`audit.record` always carries a
    detail)."""
    cur.execute(
        "INSERT INTO audit_events (workspace_id, entity_kind, entity_id,"
        " from_state, to_state, actor_kind, detail, created_at)"
        " VALUES (%s, 'post_intent', %s, 'awaiting_approval', 'approved', 'user',"
        " %s::jsonb, now() - %s::interval)",
        (ws, str(uuid.uuid4()), detail, ago),
    )


def _seed(conn) -> None:
    """The estate in the module docstring, committed."""
    complete = seed_workspace_chain(conn, "funnel-complete")
    with conn.cursor() as cur:
        # The prompt, then the person's "Posted myself" in manual mode, through
        # the ledger's own edges: every audit row is the trigger's, and only
        # the last one is an approval.
        cur.execute("SET LOCAL app.actor_kind = 'system'")
        for state in ("prompt_pending", "awaiting_approval"):
            cur.execute(
                "UPDATE post_intents SET state = %s WHERE id = %s",
                (state, complete["intent"]),
            )
        cur.execute("SET LOCAL app.actor_kind = 'user'")
        cur.execute("SET LOCAL app.actor_user_id = %s", (str(complete["user"]),))
        cur.execute(
            "UPDATE post_intents SET state = 'posted', published_via = 'manual',"
            " cap_consumed_on = current_date WHERE id = %s",
            (complete["intent"],),
        )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.actor_kind = 'migration'")
        member = _person(cur, ago="200 hours")
        cur.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES (%s, %s, 'member')",
            (complete["ws"], member),
        )
        stalled = _person(cur, ago="101 hours")
        _workspace(cur, stalled, "funnel-stalled", ago="100 hours")
        fresh = _person(cur, ago="11 hours")
        _workspace(cur, fresh, "funnel-fresh", ago="10 hours")
        folder_first = _person(cur, ago="91 hours")
        ws = _workspace(cur, folder_first, "funnel-folder-first", ago="90 hours")
        _folder(cur, ws, ago="5 hours")
        suspended = _person(cur, ago="6 days")
        ws = _workspace(
            cur, suspended, "funnel-suspended", ago="6 days", state="suspended"
        )
        _instagram(cur, ws, "acct-funnel-suspended", ago="6 days")
        _folder(cur, ws, ago="6 days")
        _approval_row(cur, ws, ago="6 days")
        disabled = _person(cur, ago="5 days", state="disabled")
        _workspace(cur, disabled, "funnel-disabled", ago="5 days")
        early = _person(cur, ago="40 days")
        _workspace(cur, early, "funnel-early", ago="40 days")
    conn.commit()


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """The replayed stream and the seeded estate, once for the module. Every
    test reads it; the one that writes rolls its transaction back."""
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        conn = psycopg2.connect(stream)
        try:
            _seed(conn)
        finally:
            conn.close()
        yield {
            # svc_migration, the tables' owner: it seeds past the policies, and
            # reaches the door through its membership of svc_maintenance
            # (step0_bootstrap.sql).
            "stream": stream,
            "worker": as_user(db, "svc_worker"),
            "ingress": as_user(db, "svc_ingress"),
        }
    finally:
        gen.close()


def _read(dsn: str, sql: str = FUNNEL) -> list[tuple]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


def _reached(cur) -> list[int]:
    cur.execute(FUNNEL)
    return [row[2] for row in cur.fetchall()]


def test_the_stage_counts_on_the_seeded_estate(world):
    """Read as the worker's login, the door's one grantee. EXPECTED says where
    each number comes from; what each person proves:

    - MEMBER is signed in and not stalled, though idle for 200 hours: a stall
      would have made stage 1's count two;
    - SUSPENDED is the stall at stage 1, the positive control for MEMBER's
      exemption. Were the suspended workspace counted, SUSPENDED would reach
      every stage instead;
    - STALLED and FRESH have the same gap, and only the older one stalls;
    - FOLDER FIRST counts at the folder stage without the account stage, and is
      not stalled: its folder, 5 hours old, is its latest activity, though its
      workspace is 90 hours old;
    - DISABLED and EARLY would each add a person at stages 1 and 2 and a stall
      at stage 2.
    """
    assert _read(world["worker"]) == EXPECTED


def test_the_result_is_counts_and_names_no_one(world):
    """What leaves the door: a stage's place and name and two counts. No
    column can carry an id, a name or an email, and the one text column holds
    the five stage names and nothing else."""
    conn = psycopg2.connect(world["worker"])
    try:
        with conn.cursor() as cur:
            cur.execute(FUNNEL)
            columns = [(c.name, c.type_code) for c in cur.description]
            stages = [row[1] for row in cur.fetchall()]
    finally:
        conn.close()
    # by type OID: int4, text, int8, int8
    assert columns == [
        ("o_ordinal", 23),
        ("o_stage", 25),
        ("o_reached", 20),
        ("o_stalled", 20),
    ]
    assert stages == list(STAGES)


def test_an_empty_window_still_answers_every_stage(world):
    """Five rows whatever the cohort: nobody signed up in the future."""
    rows = _read(
        world["worker"], "SELECT * FROM fn_activation_funnel(now() + interval '1 day')"
    )
    assert rows == [(k, stage, 0, 0) for k, stage in enumerate(STAGES, start=1)]


def test_the_stall_window_is_the_callers(world):
    """At eight hours FRESH's 10-hour gap stalls too, while FOLDER FIRST, whose
    latest activity is 5 hours old, still does not. Nothing reached moves."""
    rows = _read(
        world["worker"],
        "SELECT * FROM fn_activation_funnel(now() - interval '30 days',"
        " interval '8 hours')",
    )
    assert [row[3] for row in rows] == [1, 2, 0, 0, 0]
    assert [row[2] for row in rows] == [row[2] for row in EXPECTED]


def test_only_the_worker_login_may_call_the_door(world):
    """`svc_worker` reads the estate through the door; `svc_ingress`, the API's
    login, is refused, so no route can serve the counts. The vacuity guard:
    with no tenant set, the worker's login sees no row of the tenant-scoped
    tables the door reads, so the counts it gets came through the door."""
    assert _read(world["worker"]) == EXPECTED
    hidden = {
        table: _read(world["worker"], f"SELECT count(*) FROM {table}")[0][0]
        for table in TENANT_TABLES
    }
    assert hidden == dict.fromkeys(TENANT_TABLES, 0)
    with pytest.raises(
        psycopg2.errors.InsufficientPrivilege, match="fn_activation_funnel"
    ):
        _read(world["ingress"])


def test_a_hand_written_audit_row_is_not_an_approval(world):
    """A row a service writes by hand (`audit.record`) always carries a
    `detail`; the intent trigger's rows carry none, and only those are a
    person's approval. Measured in one transaction on the stream login, which
    sees its own uncommitted rows through the door, and rolled back, so the
    module's estate is untouched.

    A new person with a workspace and a hand-written `awaiting_approval ->
    approved` row is seen (signed in, workspace created) and approves nothing.
    The control: the trigger's shape of the same row, in the same workspace,
    approves."""
    conn = psycopg2.connect(world["stream"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.actor_kind = 'migration'")
            before = _reached(cur)
            person = _person(cur, ago="2 hours")
            ws = _workspace(cur, person, "funnel-hand-written", ago="1 hour")
            _approval_row(cur, ws, ago="30 minutes", detail='{"v": 1}')
            hand_written = _reached(cur)
            _approval_row(cur, ws, ago="30 minutes")
            trigger_shaped = _reached(cur)
    finally:
        conn.rollback()
        conn.close()
    assert [now - then for now, then in zip(hand_written, before)] == [1, 1, 0, 0, 0]
    assert [now - then for now, then in zip(trigger_shaped, before)] == [
        1,
        1,
        0,
        0,
        1,
    ]
