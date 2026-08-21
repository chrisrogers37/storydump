"""Every BLOCKER in the battery must be provably able to FAIL (#944 review).

**Why this file exists.** #944 shipped with `status_outside_the_1to1_map`,
counting `posting_history` rows whose status is outside the five the transform
maps 1:1. `check_history_status` admits *exactly those five*, so the check
returned 0 correctly and **could never return anything else** — a green light
nobody can make red, inside the instrument that is supposed to certify M.1c–e.
It was found by a reviewer seeding six deliberate faults, of which five fired.

A sample of six found one. The population was twenty. So the answer is not to
sample harder, it is to make the question structural: **a BLOCKER with no
registered seed fails this suite.** The next check anyone adds carries the same
burden on the day they add it, rather than waiting for the next review.

**What a seed must do.** Create the state the check exists to catch, on the real
replayed legacy schema. Each test asserts the count is **0 before** and
**nonzero after** — the before-assertion is what makes it evidence that the
*seed* caused the firing rather than the corpus already being dirty.

**Where a seed turns out to be impossible, that is the finding.** Do not delete
the assertion to make the suite green: a check whose failing state cannot exist
is either dead weight or is testing the wrong route, and both need a decision
recorded rather than a passing test.
"""

from __future__ import annotations

import re
import uuid

import psycopg2
import pytest

from scripts import m1_preflight as pf

pytestmark = [pytest.mark.integration]

CHAT_A = "aaaaaaaa-0000-0000-0000-000000000001"
CHAT_B = "aaaaaaaa-0000-0000-0000-000000000002"
USER_A = "bbbbbbbb-0000-0000-0000-000000000001"
MEDIA_A = "cccccccc-0000-0000-0000-000000000001"
ACCT_A = "dddddddd-0000-0000-0000-000000000001"


def _x(conn, sql, *params):
    with conn.cursor() as cur:
        cur.execute(sql, params or None)


def _chat(conn, cid=CHAT_A, tg=-9001, **cols):
    keys = "".join(f", {k}" for k in cols)
    marks = "".join(", %s" for _ in cols)
    _x(
        conn,
        f"INSERT INTO chat_settings (id, telegram_chat_id{keys})"
        f" VALUES (%s, %s{marks})",
        cid,
        tg,
        *cols.values(),
    )
    return cid


def _user(conn, uid=USER_A, tg=90001):
    _x(conn, "INSERT INTO users (id, telegram_user_id) VALUES (%s, %s)", uid, tg)
    return uid


def _media(conn, mid=MEDIA_A, chat=None, source_type="google_drive", **cols):
    cols.setdefault("source_identifier", "drive-file-1")
    cols.setdefault("mime_type", "image/jpeg")
    keys = "".join(f", {k}" for k in cols)
    marks = "".join(", %s" for _ in cols)
    _x(
        conn,
        "INSERT INTO media_items (id, file_path, file_name, file_size, file_hash,"
        f" source_type, chat_settings_id{keys})"
        f" VALUES (%s, '/p', 'n', 1, %s, %s, %s{marks})",
        mid,
        f"hash-{mid}",
        source_type,
        chat,
        *cols.values(),
    )
    return mid


def _history(conn, media, chat, status="posted"):
    _x(
        conn,
        "INSERT INTO posting_history (media_item_id, queue_created_at,"
        " queue_deleted_at, scheduled_for, posted_at, status, success,"
        " chat_settings_id) VALUES (%s, now(), now(), now(), now(), %s, true, %s)",
        media,
        status,
        chat,
    )


def _widen_constraint(table: str, conname: str):
    """Seed for a vocabulary check: actually move the constraint.

    Derived from the check's own expected set rather than hand-written per
    constraint — a hand-written copy is a third list to keep in sync, which is
    the defect this whole family exists to catch.
    """

    def seed(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
                " JOIN pg_class r ON r.oid = c.conrelid"
                " WHERE r.relname = %s AND c.conname = %s",
                (table, conname),
            )
            row = cur.fetchone()
            assert row, f"{table}.{conname} does not exist — cannot seed drift"
            values = sorted(set(re.findall(r"'([^']+)'::character varying", row[0])))
            assert values, f"could not parse a value set out of {conname}"
            col = re.match(r"CHECK \(\(\((\w+)\)", row[0])
            assert col, f"could not parse the column out of {conname}"
            literals = ", ".join("'%s'" % v for v in values + ["a_sixth_value"])
            cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {conname}")
            cur.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {conname}"
                f" CHECK ({col.group(1)} IN ({literals}))"
            )

    return seed


def _seed_rung4(conn):
    _chat(conn)  # a chat with no memberships at all


def _seed_ladder_disagreement(conn):
    _chat(conn)
    for role, active, tg in (("owner", False, 90001), ("member", True, 90002)):
        uid = str(uuid.uuid4())
        _user(conn, uid, tg)
        _x(
            conn,
            "INSERT INTO user_chat_memberships (user_id, chat_settings_id,"
            " instance_role, joined_at, is_active) VALUES (%s,%s,%s,now(),%s)",
            uid,
            CHAT_A,
            role,
            active,
        )


def _seed_account_orphan(conn):
    _x(
        conn,
        "INSERT INTO instagram_accounts (id, display_name, instagram_account_id)"
        " VALUES (%s, '@x', '123')",
        ACCT_A,
    )


def _seed_active_pointer_orphan(conn):
    _seed_account_orphan(conn)
    _chat(conn, active_instagram_account_id=ACCT_A)


def _seed_lock_mismatch(conn):
    _chat(conn, CHAT_A, -9001)
    _chat(conn, CHAT_B, -9002)
    _media(conn, chat=CHAT_A)
    _x(
        conn,
        "INSERT INTO media_posting_locks (media_item_id, chat_settings_id)"
        " VALUES (%s, %s)",
        MEDIA_A,
        CHAT_B,
    )


def _seed_history_mismatch(conn):
    _chat(conn, CHAT_A, -9001)
    _chat(conn, CHAT_B, -9002)
    _media(conn, chat=CHAT_A)
    _history(conn, MEDIA_A, CHAT_B)


def _seed_dedup_archive(conn):
    _x(conn, "CREATE TABLE posting_history_dedup_archive (id int)")
    _x(conn, "INSERT INTO posting_history_dedup_archive VALUES (1)")


#: check key -> a callable that creates the state the check exists to catch.
SEEDS = {
    "chat_settings.tz_discarded_by_g_tz": lambda c: _chat(
        c, posting_timezone="Mars/Olympus"
    ),
    "user_chat_memberships.rung4_quarantine_feed": _seed_rung4,
    "user_chat_memberships.ladder_reading_disagreement": _seed_ladder_disagreement,
    "chat_settings.gdrive_chats_with_null_root": lambda c: _chat(
        c, media_source_type="google_drive", media_source_root=None
    ),
    "instagram_accounts.accounts_outside_the_pair_set": _seed_account_orphan,
    "chat_settings.active_pointer_orphans": _seed_active_pointer_orphan,
    "media_items.non_gdrive_source_types": lambda c: _media(
        c, chat=_chat(c), source_type="local"
    ),
    "media_items.null_source_identifier_on_gdrive": lambda c: _media(
        c, chat=_chat(c), source_identifier=None
    ),
    "media_items.unmappable_media_kind": lambda c: _media(
        c, chat=_chat(c), mime_type="application/pdf"
    ),
    "media_items.null_tenant": lambda c: _media(c, chat=None),
    "media_posting_locks.workspace_mismatch_vs_media": _seed_lock_mismatch,
    "category_post_case_mix.null_tenant": lambda c: _x(
        c,
        "INSERT INTO category_post_case_mix (category, ratio, chat_settings_id)"
        " VALUES ('memes', 1.0, NULL)",
    ),
    "posting_history.null_tenant": lambda c: _history(
        c, _media(c, chat=_chat(c)), None
    ),
    "posting_history.workspace_mismatch_vs_media": _seed_history_mismatch,
    "audit_log.null_tenant": lambda c: _x(
        c,
        "INSERT INTO audit_log (entity_type, action, chat_settings_id)"
        " VALUES ('lock', 'create', NULL)",
    ),
    "user_interactions.unresolvable_chat_ids": lambda c: _x(
        c,
        "INSERT INTO user_interactions (interaction_type, interaction_name,"
        " telegram_chat_id) VALUES ('command', 'n', NULL)",
    ),
    "onboarding_sessions.live_sessions_blocking_the_drop": lambda c: _x(
        c,
        "INSERT INTO onboarding_sessions (user_id, expires_at)"
        " VALUES (%s, now() + interval '1 day')",
        _user(c),
    ),
    "chat_settings.incomplete_onboarding_blocking_the_drop": lambda c: _chat(
        c, onboarding_completed=False
    ),
    "posting_history_dedup_archive.undispositioned_rows": _seed_dedup_archive,
}

# Every vocabulary check seeds the same way: move the constraint for real.
for _c in pf.CHECKS:
    if _c.name.startswith("vocabulary_drift_"):
        SEEDS[f"{_c.table}.{_c.name}"] = _widen_constraint(
            _c.table, _c.name[len("vocabulary_drift_") :]
        )

BLOCKERS = [f"{c.table}.{c.name}" for c in pf.CHECKS if c.kind == pf.BLOCKER]


def _seed_owner_demotion(conn):
    """Two owner rows: the ladder picks the earlier, the later demotes to admin."""
    _chat(conn)
    for tg, joined in ((90001, "2026-01-01"), (90002, "2026-01-02")):
        uid = str(uuid.uuid4())
        _user(conn, uid, tg)
        _x(
            conn,
            "INSERT INTO user_chat_memberships (user_id, chat_settings_id,"
            " instance_role, joined_at, is_active) VALUES (%s,%s,'owner',%s,true)",
            uid,
            CHAT_A,
            joined,
        )


def _seed_membership(conn, role="member", active=True):
    _chat(conn)
    uid = _user(conn)
    _x(
        conn,
        "INSERT INTO user_chat_memberships (user_id, chat_settings_id,"
        " instance_role, joined_at, is_active) VALUES (%s,%s,%s,now(),%s)",
        uid,
        CHAT_A,
        role,
        active,
    )


def _seed_token(conn, with_chat=True, with_account=True):
    chat = _chat(conn) if with_chat else None
    acct = None
    if with_account:
        _seed_account_orphan(conn)
        acct = ACCT_A
    _x(
        conn,
        "INSERT INTO api_tokens (service_name, token_type, token_value, issued_at,"
        " chat_settings_id, instagram_account_id)"
        " VALUES ('instagram', 'access_token', 'x', now(), %s, %s)",
        chat,
        acct,
    )


def _seed_lock(conn, reason="skip", until="now() + interval '1 day'"):
    _media(conn, chat=_chat(conn))
    _x(
        conn,
        "INSERT INTO media_posting_locks (media_item_id, chat_settings_id,"
        f" lock_reason, locked_until) VALUES (%s, %s, %s, {until})",
        MEDIA_A,
        CHAT_A,
        reason,
    )


def _seed_service_run(conn, service="SchedulerService", method="select_and_send"):
    _x(
        conn,
        "INSERT INTO service_runs (service_name, method_name, status)"
        " VALUES (%s, %s, 'completed')",
        service,
        method,
    )


#: COUNT checks get the same treatment as blockers. A count that can never move
#: is not as dangerous as a blocker that can never fire — it gates nothing — but
#: it is still a number a reader takes as measured, and "always 0" and "measured
#: 0" are indistinguishable on the page. Same discipline, one tier down.
COUNT_SEEDS = {
    "chat_settings.dm_rooted_tenants": lambda c: _chat(c, tg=7778889990),
    "chat_settings.total": lambda c: _chat(c),
    "chat_settings.name_defaulted_by_sd12": lambda c: _chat(c),
    "chat_settings.tz_defaulted_by_g_tz": lambda c: _chat(c),
    "users.total": _user,
    "users.null_is_active": lambda c: _x(
        c,
        "INSERT INTO users (id, telegram_user_id, is_active) VALUES (%s, 90009, NULL)",
        USER_A,
    ),
    "user_chat_memberships.total": lambda c: _seed_membership(c),
    "user_chat_memberships.inactive_rows_dropped": lambda c: _seed_membership(
        c, active=False
    ),
    "user_chat_memberships.legacy_owner_rows_demoted_to_admin": _seed_owner_demotion,
    "instagram_accounts.total": _seed_account_orphan,
    "api_tokens.fanout_pair_set_size": lambda c: _seed_token(c),
    "api_tokens.total_snapshotted_never_transformed": lambda c: _seed_token(
        c, with_account=False
    ),
    "media_items.total": lambda c: _media(c, chat=_chat(c)),
    "media_items.null_is_active": lambda c: _media(c, chat=_chat(c), is_active=None),
    "media_posting_locks.total": lambda c: _seed_lock(c),
    "media_posting_locks.live_recent_locks_fork_e_sizer": lambda c: _seed_lock(
        c, reason="recent_post"
    ),
    "media_posting_locks.expired_rows_not_carried": lambda c: _seed_lock(
        c, until="now() - interval '1 day'"
    ),
    "category_post_case_mix.total": lambda c: _x(
        c,
        "INSERT INTO category_post_case_mix (category, ratio, chat_settings_id)"
        " VALUES ('memes', 1.0, %s)",
        _chat(c),
    ),
    "category_post_case_mix.is_current_vs_effective_to_disagreement": lambda c: _x(
        c,
        "INSERT INTO category_post_case_mix (category, ratio, is_current,"
        " effective_to, chat_settings_id) VALUES ('memes', 1.0, true, now(), %s)",
        _chat(c),
    ),
    "posting_history.total": lambda c: _history(c, _media(c, chat=_chat(c)), CHAT_A),
    "posting_history.status_success_disagreement": lambda c: _x(
        c,
        "INSERT INTO posting_history (media_item_id, queue_created_at,"
        " queue_deleted_at, scheduled_for, posted_at, status, success,"
        " chat_settings_id) VALUES (%s, now(), now(), now(), now(),"
        " 'posted', false, %s)",
        _media(c, chat=_chat(c)),
        CHAT_A,
    ),
    "posting_history.attribution_rows_needing_the_timeline": lambda c: _x(
        c,
        "INSERT INTO posting_history (media_item_id, queue_created_at,"
        " queue_deleted_at, scheduled_for, posted_at, status, success,"
        " posting_method, chat_settings_id) VALUES (%s, now(), now(), now(),"
        " now(), 'posted', true, 'instagram_api', %s)",
        _media(c, chat=_chat(c)),
        CHAT_A,
    ),
    "service_runs.attribution_timeline_source_rows": lambda c: _seed_service_run(
        c, "InstagramAccountService", "switch_account"
    ),
    "service_runs.total_consumed_never_landed": lambda c: _seed_service_run(c),
    "posting_queue.rows_the_queue_clause_drops": lambda c: _x(
        c,
        "INSERT INTO posting_queue (media_item_id, scheduled_for, status,"
        " chat_settings_id) VALUES (%s, now(), 'pending', %s)",
        _media(c, chat=_chat(c)),
        CHAT_A,
    ),
    "audit_log.total_to_audit_events": lambda c: _x(
        c,
        "INSERT INTO audit_log (entity_type, action, chat_settings_id)"
        " VALUES ('lock', 'create', %s)",
        _chat(c),
    ),
    "user_interactions.total_to_audit_events": lambda c: _x(
        c,
        "INSERT INTO user_interactions (interaction_type, interaction_name,"
        " telegram_chat_id) VALUES ('command', 'n', -9001)",
    ),
    "onboarding_sessions.total_rows_snapshot_only": lambda c: _x(
        c,
        "INSERT INTO onboarding_sessions (user_id, expires_at)"
        " VALUES (%s, now() - interval '1 day')",
        _user(c),
    ),
}

COUNTS = [f"{c.table}.{c.name}" for c in pf.CHECKS if c.kind == pf.COUNT]


@pytest.fixture()
def conn(replayed_db):
    c = psycopg2.connect(replayed_db)
    c.autocommit = True
    yield c
    c.close()


def _value(report, key):
    for r in report.results:
        if f"{r.check.table}.{r.check.name}" == key:
            return r.value
    raise AssertionError(f"no check named {key} in the report")


class TestEveryBlockerCanBeMadeToFire:
    def test_every_blocker_has_a_registered_seed(self):
        """The structural half. A BLOCKER with no seed is a check nobody has
        shown can fail, which is indistinguishable from one that cannot."""
        missing = [k for k in BLOCKERS if k not in SEEDS]
        assert missing == [], (
            "these BLOCKER checks have no positive control: "
            + ", ".join(missing)
            + ". Add a seed that creates the state the check exists to catch,"
            " or record why the check should be removed."
        )

    @pytest.mark.parametrize("key", BLOCKERS)
    def test_the_seed_makes_the_blocker_fire(self, key, conn):
        seed = SEEDS[key]

        before = pf.run(conn)
        assert _value(before, key) in (0, None), (
            f"{key} is already firing on an EMPTY corpus — the after-assertion"
            " below would then pass without the seed doing anything"
        )
        # `None` is the third state, and it is legitimate here: an
        # `optional_table` check whose table is absent from the replayed corpus
        # is SKIPPED, not answered, so it holds no value. That is exactly the
        # distinction `Result.reportable` exists to keep, and it is not-firing
        # for this test's purposes — seeding the table is what the seed does.

        seed(conn)
        after = pf.run(conn)

        assert _value(after, key) > 0, (
            f"{key} did not fire after seeding the state it exists to catch."
            " Either the seed does not create that state, or the state cannot"
            " exist — which is the `status_outside_the_1to1_map` finding again"
            " and needs recording, not a weakened assertion."
        )
        assert key in {f"{r.check.table}.{r.check.name}" for r in after.blockers}
        assert after.exit_code() == pf.HALT


class TestEveryCountCanBeMadeToMove:
    """Same question one tier down, because ari's ask was every check, not
    every blocker: what state makes this number non-zero, and can it exist?

    DISCLOSURE checks are deliberately NOT covered here, and saying so is the
    point rather than leaving a silent gap. They are censuses: they assert
    nothing, carry no verdict, and an empty one is itself informative. Two of
    them (`fork_c_collapse_counts`, `schema_version.head_version`) are
    un-grouped aggregates that return a row on an empty table by construction,
    so "seeding makes a row appear" is not even expressible for them.
    """

    def test_every_count_has_a_registered_seed(self):
        missing = [k for k in COUNTS if k not in COUNT_SEEDS]
        assert missing == [], (
            "these COUNT checks have no positive control: " + ", ".join(missing)
        )

    @pytest.mark.parametrize("key", COUNTS)
    def test_the_seed_makes_the_count_move(self, key, conn):
        before = pf.run(conn)
        assert _value(before, key) in (0, None)

        COUNT_SEEDS[key](conn)
        after = pf.run(conn)

        assert _value(after, key) > 0, (
            f"{key} stayed at zero after seeding the state it counts. A count"
            " that cannot move reads as a measurement and is not one."
        )
