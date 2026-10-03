"""The "Deleting a user" runbook (`documentation/guides/deployment.md`), run
as written on the replayed advertised stream as the schema owner.

The two SQL blocks are read from the guide, so the guide cannot drift from what
was proven. A member who was removed from their workspace is deleted, with the
tokens they minted and the invitations they sent revoked. A person who approved
a story that has since finished cannot be deleted (the finished story is
frozen with the reference), and the erase block leaves a bare, disabled id with
no email, identities, sessions or live tokens.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest

from tests.scripts.conftest import (
    _scratch,
    replay_advertised_stream,
    seed_workspace_chain,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

GUIDE = Path(__file__).resolve().parents[2] / "documentation/guides/deployment.md"


def _runbook_blocks() -> tuple[str, str]:
    """The guide's delete block and its erase block, in that order."""
    text = GUIDE.read_text()
    section = text[text.index("**Deleting a user**") : text.index("## 7. Backup")]
    blocks = re.findall(r"```sql\n(.*?)```", section, re.S)
    assert len(blocks) == 2, "the runbook has a delete block and an erase block"
    return blocks[0], blocks[1]


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
        yield {"owner": replay_advertised_stream(db, owner_actor, admin_conn)}
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


def _person(world, ws, email):
    """A member with a Google identity, a session, a workspace token they
    minted and a pending invitation they sent."""
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
                " VALUES (%s, 'google', %s)",
                (user, f"sub-{email}"),
            )
            cur.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role)"
                " VALUES (%s, %s, 'admin')",
                (ws, user),
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
                "INSERT INTO workspace_invitations (workspace_id, token_hash,"
                " delivery_channel, email, expires_at, invited_by_user_id)"
                " VALUES (%s, %s, 'email', %s, now() + interval '7 days', %s)"
                " RETURNING id",
                (ws, f"invite-{email}", f"friend-of-{email}", user),
            )
            invitation = cur.fetchone()[0]
        conn.commit()
        return {"user": user, "token": token, "invitation": invitation}
    finally:
        conn.close()


def test_a_member_is_deleted_and_what_they_minted_or_sent_is_revoked(world):
    """The delete block for an admin who was never removed in the product: the
    membership cascade needs the actor the block sets, and the token and
    invitation the removal would have revoked are revoked in the block."""
    chain = seed_workspace_chain(psycopg2.connect(world["owner"]), "runbook-a")
    who = _person(world, chain["ws"], "member@example.com")
    delete, _erase = _runbook_blocks()

    _run(world, _for(delete, who["user"]))

    assert _one(world, "SELECT count(*) FROM users WHERE id = %s", (who["user"],)) == (
        0,
    )
    assert _one(
        world,
        "SELECT revoked_at IS NOT NULL FROM service_tokens WHERE id = %s",
        (who["token"],),
    ) == (True,)
    assert _one(
        world,
        "SELECT state FROM workspace_invitations WHERE id = %s",
        (who["invitation"],),
    ) == ("revoked",)


def test_a_person_with_a_finished_story_is_erased_instead(world):
    """The delete is refused for someone who approved a story that has since
    posted; the erase block then leaves a bare, disabled id with nothing that
    signs in or identifies them, and the finished story untouched."""
    chain = seed_workspace_chain(psycopg2.connect(world["owner"]), "runbook-b")
    who = _person(world, chain["ws"], "approver@example.com")
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
    delete, erase = _runbook_blocks()

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
    assert _one(
        world,
        "SELECT approved_by_user_id, state FROM post_intents WHERE id = %s",
        (posted,),
    ) == (who["user"], "posted")
