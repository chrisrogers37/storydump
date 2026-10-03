"""The "Deleting a user" runbook (`documentation/guides/deployment.md`), run
as written on the replayed advertised stream as the schema owner.

The three SQL blocks are read from the guide, so the guide cannot drift from
what was proven. The listing names every live workspace token with no recorded
minter. A member or an admin is deleted, whether or not they were removed
through the product first, with the tokens they minted and the invitations
they sent or were sent revoked, the admission of their email dropped and the
binding to their private chat ended. An owner is refused while their
workspace exists. A person who approved a story that has since finished
cannot be deleted (the finished story is frozen with the reference), and the
erase block leaves a bare, disabled id with no email, identities, sessions,
live tokens, link attempts, admission, private-chat binding or pending
invitations.
"""

from __future__ import annotations

import asyncio
import re
import zlib
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from src.services.target import workspaces
from tests.scripts.conftest import (
    _scratch,
    as_user,
    in_tenant,
    replay_advertised_stream,
    seed_workspace_chain,
    set_test_passwords,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

GUIDE = Path(__file__).resolve().parents[2] / "documentation/guides/deployment.md"


def _runbook_blocks() -> tuple[str, str, str]:
    """The guide's unattributed-token listing, its delete block and its erase
    block, in that order."""
    text = GUIDE.read_text()
    section = text[text.index("**Deleting a user**") : text.index("## 7. Backup")]
    blocks = re.findall(r"```sql\n(.*?)```", section, re.S)
    assert len(blocks) == 3, "the runbook has a listing, a delete and an erase block"
    return tuple(blocks)


def _for(block: str, user) -> list[str]:
    """The block's statements for *user*, without BEGIN/COMMIT: the test owns
    the transaction, so a refusal rolls back exactly as the operator's would."""
    sql = block.replace("'…'", f"'{user}'")
    return [
        s.strip()
        for s in sql.split(";")
        if s.strip() and s.strip().upper() not in ("BEGIN", "COMMIT")
    ]


@pytest.fixture(scope="module")
def world(admin_conn, owner_actor):
    gen = _scratch(admin_conn, owner=owner_actor, roles=[])
    db = next(gen)
    try:
        owner = replay_advertised_stream(db, owner_actor, admin_conn)
        set_test_passwords(admin_conn)
        yield {"owner": owner, "ingress": as_user(db, "svc_ingress")}
    finally:
        gen.close()


def _run(world, statements, params=()):
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _one(world, sql, params=()):
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    finally:
        conn.close()


def _all(world, sql, params=()):
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _person(world, chain, email, role="admin"):
    """A member with Google and Telegram identities, a session, an unfinished
    Telegram link, the owner's admission of their email, a workspace token
    they minted, the workspace's binding to their private chat, a pending
    invitation they sent, and two pending invitations to them from the owner
    (by email and by Telegram id)."""
    ws = chain["ws"]
    tg = zlib.crc32(email.encode())
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO users (primary_email) VALUES (%s) RETURNING id", (email,)
            )
            user = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_identities (user_id, provider, external_id)"
                " VALUES (%s, 'google', %s), (%s, 'telegram', %s)",
                (user, f"sub-{email}", user, str(tg)),
            )
            cur.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, %s)",
                (ws, user, role),
            )
            cur.execute(
                "INSERT INTO signup_admissions (email) VALUES (%s)", (email.lower(),)
            )
            cur.execute(
                "INSERT INTO oauth_states (state, user_id, provider, purpose,"
                " expires_at) VALUES (%s, %s, 'telegram', 'link',"
                " now() + interval '15 minutes')",
                (f"link-{email}", user),
            )
            cur.execute(
                "INSERT INTO session_tokens (token_hash, user_id, expires_at)"
                " VALUES (%s, %s, now() + interval '1 day')",
                (f"session-{email}", user),
            )
            cur.execute(
                "INSERT INTO service_tokens (name, token_hash, role, workspace_id,"
                " created_by_user_id) VALUES ('ops', %s, 'operator', %s, %s)"
                " RETURNING id",
                (f"token-{email}", ws, user),
            )
            token = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO channel_bindings (workspace_id, channel, external_ref)"
                " VALUES (%s, 'telegram_dm', %s) RETURNING id",
                (ws, str(tg)),
            )
            dm = cur.fetchone()[0]
            invitations = []
            for channel, address, tg_id, by in (
                ("email", f"friend-of-{email}", None, user),
                ("email", email.lower(), None, chain["user"]),
                ("telegram", None, tg, chain["user"]),
            ):
                cur.execute(
                    "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                    " delivery_channel, email, invited_tg_user_id, expires_at,"
                    " invited_by_user_id)"
                    " VALUES (%s, %s, %s, %s, %s, now() + interval '7 days', %s)"
                    " RETURNING id",
                    (
                        ws,
                        f"invite-{channel}-{address}-{email}",
                        channel,
                        address,
                        tg_id,
                        by,
                    ),
                )
                invitations.append(cur.fetchone()[0])
        conn.commit()
        return {
            "user": user,
            "email": email.lower(),
            "token": token,
            "dm": dm,
            "invitations": invitations,
        }
    finally:
        conn.close()


def _pending(world, who) -> int:
    return _one(
        world,
        "SELECT count(*) FROM workspace_invitations"
        " WHERE id = ANY(%s::uuid[]) AND state = 'pending'",
        ([str(i) for i in who["invitations"]],),
    )[0]


def _admitted(world, who) -> int:
    return _one(
        world,
        "SELECT count(*) FROM signup_admissions WHERE email = %s",
        (who["email"],),
    )[0]


def _dm_state(world, who) -> str:
    return _one(
        world, "SELECT state FROM channel_bindings WHERE id = %s", (who["dm"],)
    )[0]


def _remove(world, chain, user):
    """Settings › Members → Remove: the service the command port calls, as
    the production role in the tenant."""
    return asyncio.run(
        in_tenant(
            world["ingress"],
            chain["ws"],
            chain["user"],
            lambda s: workspaces.remove_member(
                s,
                workspace_id=str(chain["ws"]),
                user_id=str(user),
                by_user_id=str(chain["user"]),
            ),
        )
    )


@pytest.mark.parametrize("role", ["member", "admin"])
@pytest.mark.parametrize("removed_first", [False, True], ids=["direct", "removed"])
def test_a_member_is_deleted_and_their_tokens_and_invitations_are_revoked(
    world, role, removed_first
):
    """The delete block for a member or an admin, whether or not the product
    removed them first: the membership cascade needs the actor the block sets,
    and the token they minted and the invitations they sent or were sent end
    revoked."""
    chain = seed_workspace_chain(
        psycopg2.connect(world["owner"]), f"runbook-{role}-{removed_first}"
    )
    who = _person(world, chain, f"{role}-{removed_first}@example.com", role)
    if removed_first:
        assert _remove(world, chain, who["user"]) == role
    _listing, delete, _erase = _runbook_blocks()

    _run(world, _for(delete, who["user"]))

    assert _one(world, "SELECT count(*) FROM users WHERE id = %s", (who["user"],)) == (
        0,
    )
    assert _one(
        world,
        "SELECT revoked_at IS NOT NULL FROM service_tokens WHERE id = %s",
        (who["token"],),
    ) == (True,)
    assert _pending(world, who) == 0
    assert _admitted(world, who) == 0
    assert _dm_state(world, who) == "revoked"


def test_the_listing_names_every_live_unattributed_workspace_token(world):
    """The listing finds the live workspace tokens with no recorded minter,
    including one in a workspace the person was already removed from, and
    not a revoked one or one whose minter is recorded."""
    chain = seed_workspace_chain(psycopg2.connect(world["owner"]), "runbook-l1")
    who = _person(world, chain, "lister@example.com", "admin")
    assert _remove(world, chain, who["user"]) == "admin"
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            tokens = {}
            for label, revoked in (("unattributed", False), ("revoked", True)):
                cur.execute(
                    "INSERT INTO service_tokens (name, token_hash, role,"
                    " workspace_id, revoked_at) VALUES (%s, %s, 'operator', %s,"
                    " CASE WHEN %s THEN now() END) RETURNING id",
                    (label, f"listing-{label}", chain["ws"], revoked),
                )
                tokens[label] = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    listing, _delete, _erase = _runbook_blocks()

    # The listing names no one: it takes no placeholder.
    assert "'…'" not in listing
    (sql,) = _for(listing, who["user"])
    listed = {row[0] for row in _all(world, sql)}

    assert tokens["unattributed"] in listed
    assert not listed & {tokens["revoked"], who["token"]}


def test_an_owner_is_refused_while_their_workspace_exists(world):
    """The delete block for a workspace's owner fails at commit and changes
    nothing: the step the runbook says comes first."""
    chain = seed_workspace_chain(psycopg2.connect(world["owner"]), "runbook-owner")
    _listing, delete, _erase = _runbook_blocks()

    with pytest.raises(psycopg2.Error, match="has no owner at commit"):
        _run(world, _for(delete, chain["user"]))

    assert _one(
        world, "SELECT count(*) FROM users WHERE id = %s", (chain["user"],)
    ) == (1,)


def test_a_person_with_a_finished_story_is_erased_instead(world):
    """The delete is refused for someone who approved a story that has since
    posted; the erase block then leaves a bare, disabled id with nothing that
    signs in or identifies them, and the finished story untouched."""
    chain = seed_workspace_chain(psycopg2.connect(world["owner"]), "runbook-b")
    who = _person(world, chain, "approver@example.com")
    # A story they approved that has since posted, born terminal the way only
    # the migration actor may write history.
    conn = psycopg2.connect(world["owner"])
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.actor_kind = 'migration'")
            cur.execute(
                "INSERT INTO media_items (workspace_id, source_id, content_hash,"
                " file_name, media_kind, provider_file_ref)"
                " VALUES (%s, %s, 'hash-posted', 'p.jpg', 'image', 'ref-posted')"
                " RETURNING id",
                (chain["ws"], chain["src"]),
            )
            media = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO post_intents (workspace_id, ig_account_id,"
                " media_item_id, provider_account_ref, approval_mode,"
                " schedule_slot_at, state, published_via, approved_by_user_id)"
                " VALUES (%s, %s, %s, 'acct-runbook-b', 'manual', now(), 'posted', 'legacy_backfill', %s)"
                " RETURNING id",
                (chain["ws"], chain["iga"], media, who["user"]),
            )
            posted = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    _listing, delete, erase = _runbook_blocks()

    with pytest.raises(psycopg2.Error, match="terminal"):
        _run(world, _for(delete, who["user"]))

    _run(world, _for(erase, who["user"]))

    assert _one(
        world, "SELECT primary_email, state FROM users WHERE id = %s", (who["user"],)
    ) == (None, "disabled")
    assert _one(
        world, "SELECT count(*) FROM user_identities WHERE user_id = %s", (who["user"],)
    ) == (0,)
    assert _one(
        world,
        "SELECT count(*) FROM session_tokens WHERE user_id = %s AND revoked_at IS NULL",
        (who["user"],),
    ) == (0,)
    assert _one(
        world,
        "SELECT revoked_at IS NOT NULL FROM service_tokens WHERE id = %s",
        (who["token"],),
    ) == (True,)
    assert _pending(world, who) == 0
    assert _admitted(world, who) == 0
    assert _dm_state(world, who) == "revoked"
    assert _one(
        world, "SELECT count(*) FROM oauth_states WHERE user_id = %s", (who["user"],)
    ) == (0,)
    assert _one(
        world,
        "SELECT approved_by_user_id, state FROM post_intents WHERE id = %s",
        (posted,),
    ) == (who["user"], "posted")
