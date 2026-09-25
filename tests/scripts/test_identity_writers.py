"""X.3 — the three LIVE writers, driven against the real replayed target schema.

Sign-in's identity leg (`identity.upsert_google_identity`), the web session
token (`sessions.issue` / `resolve` / `revoke`) and the `create_workspace`
command (`workspaces.create_workspace`), each executed AS `svc_ingress` under
the printed RLS — the login the production ingress runs as. A writer that has
only ever been unit-tested has not met the policy that decides whether its
INSERT lands, and every one of these three writes into a table whose policy is
doing real work.

**This file used to prove all of that about code production does not run.**
Until #1325 it drove a psycopg2 twin — `identity_provisioning`,
`web_sessions`, `workspace_provisioning` — that had zero callers under `src/`
and was kept alive by this file alone. So the repository's one RLS gate on
sign-in proved a retired lane while the lane production runs had no gate at
all, and the two had already drifted. The twin is deleted and the assertions
are re-pointed at the live async writers; where the two lanes behave
differently, what is asserted below is **what the live lane does**, with the
difference named in the test (#1325 audit, TD-B1/TD-C3).

**Two doors, because sign-in precedes tenancy.** `identity` and `sessions`
write user/auth-plane tables (`058` class 3: role-scoped `USING (true)`) before
any workspace exists, which is what `conftest.in_user_plane` opens — the shape
`src/api/routes/auth.py` and `src/api/principal.py` open, an `engine.begin()`
with no GUCs. `workspaces.create_workspace` runs inside a claimed tenant, which
is `conftest.in_tenant` — the shape `src/api/routes/v1.py` opens, a unit of
work on the pre-assigned id.

**Every `in_user_plane` / `in_tenant` call COMMITS**, unlike the rolled-back
psycopg2 transaction this file used to run in. Subjects and emails are
therefore unique per test: the module's schema accumulates rows, and
`uq_identity_per_provider` / `uq_users_primary_email` are global.

Subject discipline throughout (the F.3 convention): any connection whose
result depends on which login it is asserts `current_user` before the
assertion rides on it — both fixtures do it in one place.

**Positive controls, because a passing negative proves nothing on its own.**
Two of the facts here are absences — the ownership invariant refusing an
ownerless workspace, and RLS refusing a foreign tenant. Both are paired with
the corresponding success on the same connection in the same schema, so a
failure to insert for some unrelated reason cannot read as the guard working.

**`svc_ingress` is the posture F.4 INTENDS, not the role production connects
as, and the tenant-isolation assertions here therefore do not transfer to the
deployed configuration.** Measured on production: the API connects as
`neondb_owner`, which owns the tenant tables and carries BYPASSRLS; no
migration sets FORCE ROW LEVEL SECURITY, and nothing in `src/` issues
`SET ROLE`. So `p_tenant` is inert on the deployed path and
`test_rls_refuses_a_workspace_that_is_not_the_claimed_tenant` proves a property
of `svc_ingress`, not of production. This is ratified, not a new hole —
`02-domain-model.md`:1466 allows ENABLE without FORCE and #751 tracks the gap,
whose compensating control is the unbuilt F.4. Running this file under
`svc_ingress` is still worth doing — it is the only place the intended posture
is exercised at all — but it must not be read as evidence about the running
system. (The same caveat, in the same words, heads
`tests/scripts/test_provisioning_gate.py`.)
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import psycopg2
import pytest
from sqlalchemy import text

from src.exceptions.tenancy import TenantResolutionError
from src.services.target import identity, sessions, vocabulary, workspaces
from src.services.target.workspaces import InvalidWorkspaceArgs
from tests.scripts.conftest import (
    _scratch,
    as_user,
    fetch_one,
    in_tenant,
    in_user_plane,
    replay_advertised_stream,
    set_test_passwords,
    txn,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    """A replayed target schema with the seven roles, and nothing else.

    Deliberately EMPTY of identity rows: these are the writers, so every user,
    workspace and session a test needs is made by the code under test. A
    seeded fixture would hide the case that matters — the very first row.
    """
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        stream = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"stream": stream, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


# --- drivers -----------------------------------------------------------------


def user_plane(world, fn, *, actor_user_id=None):
    """One committed `svc_ingress` transaction with no tenant — the shape the
    sign-in writers run in."""
    return asyncio.run(in_user_plane(world["ingress"], fn, actor_user_id=actor_user_id))


def tenant(world, ws, user, fn):
    """One committed unit of work on *ws* as *user* — the shape the workspace
    writer runs in."""
    return asyncio.run(in_tenant(world["ingress"], str(ws), str(user), fn))


def upsert(world, *, sub, email=None, display_name=None):
    return user_plane(
        world,
        lambda c: identity.upsert_google_identity(
            c, sub=sub, email=email, display_name=display_name
        ),
    )


def owner(world, sql, params=None):
    """Ground truth, read back as the schema owner — outside the policies, so
    an empty read is an absent row rather than an invisible one."""
    return fetch_one(world["stream"], sql, params)


def _claim(cur, tenant_id, actor_kind="user", channel="web"):
    """Set the tenant/actor context with RAW SQL, for the positive controls.

    Deliberately NOT `unit_of_work.apply_gucs`. The controls exist to prove the
    DATABASE refuses an ownerless workspace and a foreign tenant, and a control
    that reached the database through the helper the writers use would be
    measuring that helper as well — so a broken `apply_gucs` would redden the
    control and read as "the trigger fired", which is the one reading a
    positive control must never produce.
    """
    cur.execute("SET LOCAL app.tenant_id = %s", (str(tenant_id),))
    cur.execute("SET LOCAL app.actor_kind = %s", (actor_kind,))
    cur.execute("SET LOCAL app.channel = %s", (channel,))


class TestTheIdentityWriter:
    """`users` + `user_identities` from a verified OIDC subject."""

    def test_first_sign_in_creates_both_rows_keyed_on_the_subject(self, world):
        user_id = upsert(
            world, sub="sub-first", email="first@example.com", display_name="A"
        )
        provider, external_id, display, verified = owner(
            world,
            "SELECT provider, external_id, display_name,"
            " verified_at IS NOT NULL FROM user_identities WHERE user_id = %s",
            (user_id,),
        )
        email, state = owner(
            world, "SELECT primary_email, state FROM users WHERE id = %s", (user_id,)
        )
        assert (provider, external_id) == ("google", "sub-first")
        assert display == "A" and verified is True
        # D32: the SUBJECT is the key and the email is metadata beside it.
        assert external_id != email
        assert (email, state) == ("first@example.com", "active")

    def test_returning_subject_reuses_the_user_and_refreshes(self, world):
        first = upsert(world, sub="sub-return", display_name="old")
        before = owner(
            world,
            "SELECT verified_at FROM user_identities WHERE external_id = %s",
            ("sub-return",),
        )[0]
        again = upsert(world, sub="sub-return", display_name="new")
        after, display = owner(
            world,
            "SELECT verified_at, display_name FROM user_identities"
            " WHERE external_id = %s",
            ("sub-return",),
        )
        assert again == first
        assert (
            owner(world, "SELECT count(*) FROM users WHERE id = %s", (first,))[0] == 1
        )
        # STRICTLY greater — the test's own name says "refreshes", and `>=`
        # does not assert a refresh (#1364's sweep: the same weak comparison
        # let a mutant that stopped touching the row entirely go green).
        assert after > before and display == "new"

    def test_an_absent_display_name_KEEPS_the_stored_one(self, world):
        """A sign-in that carries no name must not erase the name we hold.

        `google_oidc.py:292` already collapses absent, non-string and blank to
        ``None`` before the writer sees it, so ``None`` here means exactly one
        thing: *this token told us nothing about the name*. Telling us nothing
        is not the same as telling us the name is empty, and only the second
        would justify a write.

        Until #1364 the returning branch spelled
        ``SET verified_at = now(), display_name = :dn`` unconditionally, so a
        token without `name` wrote NULL over a stored name. The retired sync
        twin kept it; re-homing this gate onto the live writer is what exposed
        the divergence.
        """
        upsert(world, sub="sub-keep", display_name="kept")
        upsert(world, sub="sub-keep", display_name=None)
        assert (
            owner(
                world,
                "SELECT display_name FROM user_identities WHERE external_id = %s",
                ("sub-keep",),
            )[0]
            == "kept"
        )

    def test_a_later_sign_in_still_verifies_when_it_carries_no_name(self, world):
        """Keeping the name must not cost the `verified_at` bump.

        The two live in one UPDATE, so a fix that guards the whole statement
        rather than the one column would silently stop recording that the
        identity was seen. This is that regression's tripwire.

        STRICTLY greater, and that is the whole test. Under the guarded-
        statement fix the row is not touched at all, so `verified_at` comes
        back EQUAL — which `>=` accepts. Checked against that mutant: with
        `>=` it passed and the tripwire was decorative.
        """
        upsert(world, sub="sub-verify", display_name="kept")
        before = owner(
            world,
            "SELECT verified_at FROM user_identities WHERE external_id = %s",
            ("sub-verify",),
        )[0]
        upsert(world, sub="sub-verify", display_name=None)
        after, display = owner(
            world,
            "SELECT verified_at, display_name FROM user_identities"
            " WHERE external_id = %s",
            ("sub-verify",),
        )
        assert after > before and display == "kept"

    def test_primary_email_fills_when_empty_and_never_overwrites(self, world):
        first = upsert(world, sub="sub-mail")
        assert (
            owner(world, "SELECT primary_email FROM users WHERE id = %s", (first,))[0]
            is None
        )
        upsert(world, sub="sub-mail", email="filled@example.com")
        upsert(world, sub="sub-mail", email="changed@example.com")
        # A provider changing the claim must not repoint a set account.
        assert (
            owner(world, "SELECT primary_email FROM users WHERE id = %s", (first,))[0]
            == "filled@example.com"
        )

    def test_the_fill_itself_leaves_a_populated_address_alone(self, world):
        """The `AND primary_email IS NULL` half of the fill, pinned directly.

        `upsert_google_identity` only reaches `_fill_primary_email` when the
        row's address is NULL, so the guard inside the UPDATE is unreachable
        through the public writer — a mutation battery on this file found
        that removing it changed nothing observable. It is defence in depth
        and worth keeping, so it is driven at its own door: a user whose
        address is already set, a fill with a different unheld address, and
        the stored value unmoved.
        """
        user_id = upsert(world, sub="sub-fill-guard", email="held@example.com")
        user_plane(
            world,
            lambda c: identity._fill_primary_email(
                c, user_id=user_id, email="second@example.com"
            ),
        )
        assert (
            owner(world, "SELECT primary_email FROM users WHERE id = %s", (user_id,))[0]
            == "held@example.com"
        )

    def test_a_colliding_email_refuses_and_never_merges(self, world):
        """D35, on both paths that can hit it: a brand-new subject whose email
        is taken, and a returning subject whose empty email is taken.

        `IdentityCollision` is a plain `StorydumpError` with **no `reason`
        attribute** — the twin's `IdentityProvisioningError("email_belongs_to_
        another")` does not carry over. The type and the never-merged half are
        the substantive claim.
        """
        incumbent = upsert(world, sub="sub-incumbent", email="shared@example.com")
        with pytest.raises(identity.IdentityCollision):
            upsert(world, sub="sub-newcomer", email="shared@example.com")

        later = upsert(world, sub="sub-later")
        with pytest.raises(identity.IdentityCollision):
            upsert(world, sub="sub-later", email="shared@example.com")

        # Neither refusal merged anything, and neither left a partial row: the
        # refusal raises inside the transaction, which rolls back whole.
        assert (
            owner(
                world,
                "SELECT user_id FROM user_identities WHERE external_id = %s",
                ("sub-newcomer",),
            )
            is None
        )
        assert (
            owner(world, "SELECT primary_email FROM users WHERE id = %s", (later,))[0]
            is None
        )
        assert (
            owner(
                world,
                "SELECT count(*) FROM users WHERE primary_email = %s",
                ("shared@example.com",),
            )[0]
            == 1
        )
        assert (
            owner(world, "SELECT id FROM users WHERE id = %s", (incumbent,)) is not None
        )

    def test_two_concurrent_first_sign_ins_converge_on_one_user(self, world):
        """The race, run for real on two connections, against the LIVE lane.

        The twin let the database be the authority and caught
        `uq_users_primary_email` on the way back. The live writer takes
        ``pg_advisory_xact_lock(hashtext('identity:google:<sub>'))`` as its
        first statement, so the second caller BLOCKS until the first commits
        and its own lookup then hits — the converge is by serialization, not
        by a caught unique violation. Two `in_user_plane` transactions under
        `asyncio.gather` is what makes that real rather than asserted.
        """
        sub = "sub-race"

        async def both():
            async def one(name):
                return await in_user_plane(
                    world["ingress"],
                    lambda c: identity.upsert_google_identity(
                        c, sub=sub, email=None, display_name=name
                    ),
                )

            return await asyncio.gather(one("A"), one("B"))

        a, b = asyncio.run(both())
        assert a == b, "two first sign-ins for one subject made two users"
        assert (
            owner(
                world,
                "SELECT count(*) FROM user_identities WHERE external_id = %s",
                (sub,),
            )[0]
            == 1
        )
        assert owner(world, "SELECT count(*) FROM users WHERE id = %s", (a,))[0] == 1

    def test_an_empty_subject_is_refused_and_a_blank_one_is_NOT(self, world):
        """**A second divergence, asserted rather than fixed.**

        The twin refused a whitespace-only subject in Python by name
        (`missing_subject`). The live writer refuses only a FALSY one, with a
        `ValueError`, and `user_identities.external_id` carries no non-blank
        CHECK — so a `"   "` subject is written. The database is the authority
        and no Python pre-check is added here (#1325 audit: record the
        difference, do not fix it in a cleanup PR).
        """
        before = owner(world, "SELECT count(*) FROM users")[0]
        with pytest.raises(ValueError):
            upsert(world, sub="")
        assert owner(world, "SELECT count(*) FROM users")[0] == before

        blank_sub = "   "
        made = upsert(world, sub=blank_sub)
        assert (
            owner(
                world,
                "SELECT user_id::text FROM user_identities WHERE external_id = %s",
                (blank_sub,),
            )[0]
            == made
        ), "a whitespace subject is refused by nothing on the live lane"


class TestTheSessionWriter:
    """`session_tokens`: mint, resolve, revoke, slide."""

    def _user(self, world, tag):
        return upsert(world, sub=f"sub-sess-{tag}")

    def _issue(self, world, user_id):
        return user_plane(world, lambda c: sessions.issue(c, user_id=user_id))

    def _resolve(self, world, value):
        return user_plane(
            world,
            lambda c: sessions.resolve(c, token_hash=sessions.token_hash(value)),
        )

    def test_mint_stores_only_the_hash_and_authenticates(self, world):
        user_id = self._user(world, "mint")
        value = self._issue(world, user_id)
        digest = hashlib.sha256(value.encode()).hexdigest()
        assert sessions.token_hash(value) == digest

        stored, owner_id, last_seen, roughly_ttl = owner(
            world,
            "SELECT token_hash, user_id::text, last_seen_at,"
            " expires_at > now() + make_interval(secs => %s)"
            " FROM session_tokens WHERE user_id = %s",
            (sessions.SESSION_TTL_SECONDS - 3600, user_id),
        )
        # The raw value is nowhere in the row.
        assert stored == digest and value not in stored
        assert owner_id == user_id and last_seen is None and roughly_ttl is True

        live = self._resolve(world, value)
        assert live.user_id == user_id

    def test_an_unknown_token_is_invalid_not_empty(self, world):
        """A returned status object on the twin; a RAISE on the live lane."""
        with pytest.raises(TenantResolutionError) as err:
            self._resolve(world, "never-minted")
        assert err.value.reason == "invalid_session"

    def test_revoke_is_idempotent_and_keeps_the_first_instant(self, world):
        """`sessions.revoke` returns a bool, not a status object. The "keeps
        the first instant" half still holds, and the live statement's
        ``WHERE ... revoked_at IS NULL`` is what guarantees it."""
        user_id = self._user(world, "revoke")
        value = self._issue(world, user_id)
        h = sessions.token_hash(value)

        assert user_plane(world, lambda c: sessions.revoke(c, token_hash=h)) is True
        first_kill = owner(
            world, "SELECT revoked_at FROM session_tokens WHERE token_hash = %s", (h,)
        )[0]
        assert first_kill is not None

        assert user_plane(world, lambda c: sessions.revoke(c, token_hash=h)) is False
        assert (
            user_plane(
                world,
                lambda c: sessions.revoke(
                    c, token_hash=sessions.token_hash("stale-cookie")
                ),
            )
            is False
        )
        assert (
            owner(
                world,
                "SELECT revoked_at FROM session_tokens WHERE token_hash = %s",
                (h,),
            )[0]
            == first_kill
        )

        with pytest.raises(TenantResolutionError) as err:
            self._resolve(world, value)
        assert err.value.reason == "revoked_session"

    def test_an_expired_token_reports_expired_and_is_not_revived_by_the_read(
        self, world
    ):
        user_id = self._user(world, "expired")
        value = self._issue(world, user_id)
        h = sessions.token_hash(value)
        with txn(world["stream"]) as c, c.cursor() as cur:
            cur.execute(
                "UPDATE session_tokens SET expires_at = now() - interval '1 hour'"
                " WHERE token_hash = %s",
                (h,),
            )
            c.commit()
        with pytest.raises(TenantResolutionError) as err:
            self._resolve(world, value)
        assert err.value.reason == "expired_session"
        # The read's slide CTE excludes a dead row, so presenting an expired
        # token must not revive it.
        assert (
            owner(
                world,
                "SELECT expires_at < now() FROM session_tokens WHERE token_hash = %s",
                (h,),
            )[0]
            is True
        )

    def test_resolve_slides_the_window_and_the_throttle_holds_the_second_read(
        self, world
    ):
        """**There is no `touch_session` on the live lane.** `sessions.resolve`
        slides inline, throttled by `RENEW_THROTTLE_SECONDS`, so the twin's
        separate touch door becomes two assertions about one function: a read
        past the throttle slides `expires_at` and stamps `last_seen_at`; a
        read inside the window does not.
        """
        user_id = self._user(world, "slide")
        value = self._issue(world, user_id)
        h = sessions.token_hash(value)

        with txn(world["stream"]) as c, c.cursor() as cur:
            cur.execute(
                "UPDATE session_tokens SET expires_at = now() + interval '1 hour'"
                " WHERE token_hash = %s RETURNING expires_at",
                (h,),
            )
            shortened = cur.fetchone()[0]
            c.commit()

        # `last_seen_at` is NULL on a fresh row, which is past the throttle.
        slid = self._resolve(world, value)
        assert slid.user_id == user_id
        after_first, seen_first = owner(
            world,
            "SELECT expires_at, last_seen_at FROM session_tokens WHERE token_hash = %s",
            (h,),
        )
        assert after_first > shortened, "the first read did not slide the window"
        assert seen_first is not None

        # Inside the throttle: same window, same stamp, one round trip saved.
        self._resolve(world, value)
        after_second, seen_second = owner(
            world,
            "SELECT expires_at, last_seen_at FROM session_tokens WHERE token_hash = %s",
            (h,),
        )
        assert (after_second, seen_second) == (after_first, seen_first), (
            f"the throttle did not hold: {seen_first} -> {seen_second}"
        )

        # Past it again, with the stamp backdated by more than the throttle.
        with txn(world["stream"]) as c, c.cursor() as cur:
            cur.execute(
                "UPDATE session_tokens SET last_seen_at ="
                " now() - make_interval(secs => %s) WHERE token_hash = %s",
                (sessions.RENEW_THROTTLE_SECONDS * 2, h),
            )
            c.commit()
        self._resolve(world, value)
        after_third, seen_third = owner(
            world,
            "SELECT expires_at, last_seen_at FROM session_tokens WHERE token_hash = %s",
            (h,),
        )
        assert after_third > after_second and seen_third > seen_first

    def test_a_disabled_user_is_refused_at_the_one_ingress_gate(self, world):
        """**New coverage the live lane makes possible.** The retired
        `web_sessions.authenticate_session` never looked at `users.state`;
        `sessions.resolve` denies a `disabled` user by name — "the ONE ingress
        gate" `02` §1 names, and `053`'s comment on `users.state` — so a
        disabled account cannot reach any route, workspace-scoped or not.
        Until this PR it had no gate coverage at all.
        """
        user_id = self._user(world, "disabled")
        value = self._issue(world, user_id)
        assert self._resolve(world, value).user_id == user_id  # live before

        with txn(world["stream"]) as c, c.cursor() as cur:
            cur.execute("UPDATE users SET state = 'disabled' WHERE id = %s", (user_id,))
            c.commit()

        with pytest.raises(TenantResolutionError) as err:
            self._resolve(world, value)
        assert err.value.reason == "disabled_user"

    def test_a_drawn_session_value_never_wears_the_api_token_prefix(
        self, world, monkeypatch
    ):
        """**New coverage.** A session value starting with
        `vocabulary.TOKEN_PREFIX` would be routed to the API token resolver
        (`src/api/principal.py`) and fail every server-side call until the next
        sign-in, so `sessions.new_token` re-draws. Deterministic: the first
        draw wears the prefix, the second does not, and the second is what
        comes back — and what reaches `session_tokens`.
        """
        clean = "clean-draw-value"
        draws = iter([vocabulary.TOKEN_PREFIX + "wearing-the-prefix", clean])
        monkeypatch.setattr(sessions.secrets, "token_urlsafe", lambda n: next(draws))

        user_id = self._user(world, "prefix")
        value = self._issue(world, user_id)
        assert value == clean
        assert not value.startswith(vocabulary.TOKEN_PREFIX)
        assert (
            owner(
                world,
                "SELECT count(*) FROM session_tokens WHERE token_hash = %s",
                (sessions.token_hash(clean),),
            )[0]
            == 1
        )


class TestTheWorkspaceWriter:
    """`workspaces` + `workspace_members`, in one unit of work, under RLS."""

    def _make(self, world, *, name, tz=None, sub=None, owner_user_id=None, ws_id=None):
        """The production shape: the id is pre-assigned by the adapter, the
        unit of work claims it, and the writer is handed the same id
        (`src/api/routes/v1.py::create_workspace`)."""
        user_id = owner_user_id
        if user_id is None:
            user_id = upsert(world, sub=sub)
        ws_id = ws_id or str(uuid.uuid4())
        made = tenant(
            world,
            ws_id,
            user_id,
            lambda s: workspaces.create_workspace(
                s,
                owner_user_id=user_id,
                name=name,
                tz=tz,
                workspace_id=ws_id,
            ),
        )
        return user_id, made

    def test_create_workspace_commits_with_its_owner_and_audit_trail(self, world):
        user_id, ws_id = self._make(world, name="Owner's Space", sub="sub-ws-owner")
        # The commit is where the deferred owner invariant is checked. It is
        # the assertion, not a teardown step: `in_tenant` commits, and an
        # ownerless workspace would raise there.
        name, state, tz = owner(
            world, "SELECT name, state, tz FROM workspaces WHERE id = %s", (ws_id,)
        )
        member = owner(
            world,
            "SELECT user_id::text, role FROM workspace_members WHERE workspace_id = %s",
            (ws_id,),
        )
        with txn(world["stream"]) as c, c.cursor() as cur:
            cur.execute(
                "SELECT entity_kind, actor_kind, channel FROM audit_events"
                " WHERE workspace_id = %s ORDER BY id",
                (ws_id,),
            )
            audit = cur.fetchall()
        assert (name, state, tz) == ("Owner's Space", "active", "UTC")
        assert member == (user_id, "owner")
        assert [(k, a, c) for k, a, c in audit] == [
            ("workspace", "user", "web"),
            ("member", "user", "web"),
        ]

    def test_an_ownerless_workspace_cannot_commit(self, world):
        """POSITIVE CONTROL for the invariant this writer relies on rather
        than re-implements: the same schema, the same ingress login, one
        INSERT short."""
        orphan = "11111111-2222-3333-4444-555555555555"
        conn = psycopg2.connect(world["ingress"])
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == "svc_ingress"
                _claim(cur, orphan)
                cur.execute(
                    "INSERT INTO workspaces (id, name) VALUES (%s, 'ownerless')",
                    (orphan,),
                )
            with pytest.raises(psycopg2.errors.CheckViolation) as err:
                conn.commit()
            assert "no owner" in str(err.value) or "without an owner" in str(err.value)
            conn.rollback()
        finally:
            conn.close()

    def test_rls_refuses_a_workspace_that_is_not_the_claimed_tenant(self, world):
        """POSITIVE CONTROL that the policy is live: the same INSERT lands
        when the GUC names it and is refused when it names another id. The
        whole reason this file exists."""
        mine = "aaaaaaaa-0000-0000-0000-000000000001"
        other = "aaaaaaaa-0000-0000-0000-000000000002"
        conn = psycopg2.connect(world["ingress"])
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == "svc_ingress"
                _claim(cur, other)
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cur.execute(
                        "INSERT INTO workspaces (id, name) VALUES (%s, 'foreign')",
                        (mine,),
                    )
            conn.rollback()
            with conn.cursor() as cur:
                _claim(cur, mine)
                cur.execute(
                    "INSERT INTO workspaces (id, name) VALUES (%s, 'own')", (mine,)
                )
            conn.rollback()
        finally:
            conn.close()

    def test_a_blank_name_is_refused_by_the_boundary(self, world):
        """`InvalidWorkspaceArgs`, not the twin's
        `TenantProvisioningError("invalid_name")` — the type changed with the
        lane; the refusal did not."""
        user_id = upsert(world, sub="sub-ws-blank")
        with pytest.raises(InvalidWorkspaceArgs):
            self._make(world, name="  ", owner_user_id=user_id)

    def test_a_missing_owner_is_refused_by_the_DATABASE_not_by_python(self, world):
        """**A divergence, asserted rather than fixed.** The twin refused an
        empty `owner_user_id` in Python by name (`missing_owner`); the live
        writer has no such check, and the refusal is the `workspace_members`
        insert failing on the uuid column. A typed Python pre-check is a
        behaviour change and is not added here (#1325 audit)."""
        with pytest.raises(Exception) as err:
            self._make(world, name="ownerless", owner_user_id="")
        assert not isinstance(err.value, InvalidWorkspaceArgs), (
            "the live lane grew a Python refusal — update this test deliberately"
        )

    def test_an_invalid_timezone_is_refused_by_the_schema(self, world):
        """`ck_ws_tz_valid` fires; `workspaces._write` translates the check
        violation into `InvalidWorkspaceArgs` (the twin surfaced the raw
        driver error)."""
        with pytest.raises(InvalidWorkspaceArgs):
            self._make(world, name="bad tz", sub="sub-badtz", tz="Mars/Olympus")

    def test_a_named_timezone_is_stored(self, world):
        _user, ws_id = self._make(
            world, name="ny", sub="sub-goodtz", tz="America/New_York"
        )
        assert (
            owner(world, "SELECT tz FROM workspaces WHERE id = %s", (ws_id,))[0]
            == "America/New_York"
        )

    def test_a_null_tz_is_stored_as_UTC_by_the_writers_own_COALESCE(self, world):
        """**New coverage, and an observation for a later finding.** The live
        writer spells ``COALESCE(:tz, 'UTC')`` in Python where the twin left a
        NULL `tz` to the column default. The stored value is the same either
        way, so nothing changes here — but it is a second home for a default
        the schema owns, and removing the COALESCE would change what a NULL
        `tz` writes. Recorded, not fixed (#1325 audit)."""
        _user, ws_id = self._make(world, name="default tz", sub="sub-nulltz", tz=None)
        assert owner(world, "SELECT tz FROM workspaces WHERE id = %s", (ws_id,))[0] == (
            "UTC"
        )


class TestTheThreeWritersCompose:
    """Sign-up end to end: identity, then session, then tenant — the order the
    edge performs them in, across the two doors it opens."""

    def test_sign_up_then_sign_in_reaches_the_new_workspace(self, world):
        async def sign_in(conn):
            user_id = await identity.upsert_google_identity(
                conn, sub="sub-e2e", email="e2e@example.com", display_name="E"
            )
            return user_id, await sessions.issue(conn, user_id=user_id)

        # The sign-in leg, in ONE user-plane transaction — exactly what
        # `src/api/routes/auth.py`'s Google callback does.
        user_id, value = user_plane(world, sign_in)

        ws_id = str(uuid.uuid4())
        tenant(
            world,
            ws_id,
            user_id,
            lambda s: workspaces.create_workspace(
                s, owner_user_id=user_id, name="E2E", workspace_id=ws_id
            ),
        )

        # A tenant-less moment is representable: the token authenticates
        # without naming any workspace at all. Resolving that user INTO the
        # new workspace is the async lane's gate (`tenant_resolution.
        # authorize_member`), driven by the X.2 gate and
        # `test_tenant_resolution.py` through the real API.
        live = user_plane(
            world,
            lambda c: sessions.resolve(c, token_hash=sessions.token_hash(value)),
        )
        assert live.user_id == user_id

        # And the tenant leg landed for that user: the membership is readable
        # through the user-plane door the dashboard uses, with no tenant
        # claimed — `fn_memberships_for_caller()` reading `app.actor_user_id`.
        mine = user_plane(
            world,
            lambda c: workspaces.list_for_user(c, user_id=user_id),
            actor_user_id=user_id,
        )
        assert [(m["id"], m["role"]) for m in mine] == [(uuid.UUID(ws_id), "owner")]

    def test_the_ingress_login_is_what_ran_all_of_it(self, world):
        """Subject discipline, asserted once for the file: both doors assert
        `current_user` internally, and this is the positive control that the
        assertion is reachable — a driver that connected as the owner would
        fail here rather than quietly bypassing every policy above."""

        async def who(conn):
            return (await conn.execute(text("SELECT current_user"))).scalar()

        assert user_plane(world, who) == "svc_ingress"
