"""Provisioning — the two row writers a workspace needs before it can schedule
anything (#1041, narrowed to the destination half).

## What this closes

Measured on `main @ 878a269`: **zero** `INSERT INTO ig_accounts`, **zero**
`INSERT INTO media_sources`, and no SECURITY DEFINER door doing either.
`workspaces.py` READS both (`list_accounts`, `list_sources`) and nothing in
`src/` could write them, so the only writers were the test fixtures
(`tests/scripts/conftest.py::seed_intent_chain`) — which is the shape a gap
takes when the tests have to hand-build what production cannot. A signed-in
user with a workspace had nothing to post to and nothing to post.

## Destination, not connection — the distinction this module is scoped by

An `ig_accounts` row is a **destination**: the handle a workspace schedules
for. Its only NOT NULL columns are `workspace_id` and `provider_account_ref`,
`state` defaults to `active`, and `provider_account_ref` carries no format
CHECK. So a destination needs no credential, no OAuth round trip and no Meta
call to exist.

An `oauth_credentials` row is a **connection**: the grant that lets us publish
through Meta's API. It needs the #1041 redirect flow, Meta App Review, and an
unparked `publish_pipeline`.

**Only the destination is on the path to a closed loop**, because
`workspaces.api_publishing_enabled` defaults to FALSE and `approval_mode`
defaults to `manual`: a new workspace never touches Meta, `plan_slot` mints
intents that await approval, and `mark_posted` — already built — is how a human
closes them. This module writes destinations and sources. Connections are
milestone 2 and are deliberately absent.

## The seeding rule, which is the whole reason this writer is not a bare INSERT

`fn_clock_tick` selects `WHERE state = 'active' AND next_slot_at IS NOT NULL
AND next_slot_at <= now()` and then ADVANCES the cursor with
`fn_next_slot(a.next_slot_at, …)`. Every one of the three places that writes
`next_slot_at` in the whole schema (059, 062, 063) is that same advance —
**nothing anywhere seeds the first value.** A destination created with
`next_slot_at = NULL` is therefore invisible to the clock forever, and
`plan_slot` stays built-and-dead exactly as before.

So the destination writer seeds the cursor, using the SHIPPED `fn_next_slot`
against the SHIPPED effective-settings expression (059's own
`COALESCE(account, workspace)` ladder) rather than a second copy of either. A
private copy of the slot arithmetic is how the clock and the writer come to
disagree about when a workspace posts.

**What seeding does and does not start.** It starts *scheduling*: the clock
mints `plan_slot` jobs, which mint intents. On a default workspace those
intents are `awaiting_approval` and go nowhere until a human acts —
`api_publishing_enabled` is false, so the API publish path is not even
reachable. Callers that want a parked destination pass ``schedule=False`` and
get the old NULL cursor, which nothing will ever advance.

## Sources are Drive-shaped because the schema says so, not because we chose it

`ck_sources_provider` is closed to `'gdrive'` and `media_items.source_id` is
NOT NULL, so **every** media item hangs off a Drive source. There is no upload
path without a migration, and this module does not pretend otherwise: it writes
the source ROW. Reading the folder is the Drive seam (`WorkerDeps.drive`,
build-path #982) and belongs to that build.

## What audits, and what does not

`ig_accounts` is one of the five governance tables carrying
`trg_governance_audit` (055), so a destination write audits itself provided the
actor GUCs are set — which the unit of work does. **Nothing here writes
`audit_events` by hand**; a hand-written row would be a second, divergent trail.
`media_sources` deliberately carries no such trigger, so creating a source is
not audited. That asymmetry is the schema's, not this module's.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import text

from src.exceptions import StorydumpError
from src.services.target import readers

logger = logging.getLogger(__name__)

#: `media_sources.provider` — `ck_sources_provider`'s entire closed set today.
GDRIVE_PROVIDER = "gdrive"

#: `media_sources.config` shape for a `gdrive` source, per 054's own column
#: comment: `{v:1, folder_ref:text, root_name?:text}`.
GDRIVE_CONFIG_VERSION = 1

#: `ig_accounts.provider_account_ref` has no format CHECK, so this module owns
#: the only bound there is. Meta ids are ~17 digits; the cap is generous enough
#: never to reject a real one and small enough that the column cannot be used
#: as storage. Length only — asserting a digits-only shape here would refuse a
#: provider id format Meta has not shipped yet, on a column deliberately left
#: open (054: "the REAL account", opaque to us).
ACCOUNT_REF_MAX = 255


class ProvisioningRefused(StorydumpError):
    """A provisioning step the tier will not take, with the reason NAMED.

    Named rather than typed per case because every caller does the same thing
    with it — renders the reason — and the set is small enough to read whole.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Destinations — an `ig_accounts` row
# ---------------------------------------------------------------------------


def account_ref_from(value: object) -> str:
    """The provider account reference a caller supplied, or a NAMED refusal.

    Trimmed, because a pasted id arrives with whitespace often enough to matter
    and `uq_ig_account_live` would treat `"123 "` and `"123"` as two different
    real accounts — two destinations, two schedules, one Instagram feed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningRefused("account_ref_required")
    ref = value.strip()
    if len(ref) > ACCOUNT_REF_MAX:
        raise ProvisioningRefused("account_ref_too_long", f"max {ACCOUNT_REF_MAX}")
    return ref


async def create_destination(
    executor,
    *,
    workspace_id: str,
    provider_account_ref: str,
    handle: Optional[str] = None,
    schedule: bool = True,
) -> tuple[str, bool]:
    """The destination for *provider_account_ref*. Returns ``(id, created)``.

    Keyed on `uq_ig_account_live (workspace_id, provider_account_ref) WHERE
    state <> 'moved'` — the PARTIAL index, inferred by REPEATING its predicate
    in the conflict target. The repetition is not decoration: without the
    predicate Postgres cannot infer a partial index at all, and with a
    different one it would infer nothing.

    Idempotent by construction, so adding a handle that is already a
    destination returns the existing row rather than a second schedule against
    one real Instagram feed. A `moved` row is invisible to the index, so a
    destination that once moved away and returns gets a NEW row rather than
    reviving a tombstone — 054's stated intent.

    The cursor is seeded in the SAME statement as the insert, not a follow-up
    UPDATE: a destination that exists with a NULL cursor between two statements
    is a destination the clock will never pick up if the second one fails, and
    that failure would leave no trace anywhere.
    """
    ref = account_ref_from(provider_account_ref)
    # `fn_next_slot(now(), …)` against 059's effective-settings ladder. Written
    # as a scalar subquery over `workspaces` because the settings live there
    # and the account row does not exist yet to join to.
    seed = (
        "(SELECT fn_next_slot(now(), COALESCE(:tz, w.tz),"
        "                     COALESCE(:hs, w.posting_hours_start),"
        "                     COALESCE(:he, w.posting_hours_end),"
        "                     COALESCE(:ppd, w.posts_per_day))"
        "   FROM workspaces w WHERE w.id = :ws)"
        if schedule
        else "NULL"
    )
    result = await executor.execute(
        text(
            "INSERT INTO ig_accounts"
            " (workspace_id, provider_account_ref, handle, next_slot_at)"
            f" VALUES (:ws, :ref, :handle, {seed})"
            " ON CONFLICT (workspace_id, provider_account_ref)"
            "   WHERE state <> 'moved'"
            " DO UPDATE SET handle = COALESCE(EXCLUDED.handle, ig_accounts.handle)"
            " RETURNING id, (xmax = 0) AS created, next_slot_at"
        ),
        {
            "ws": str(workspace_id),
            "ref": ref,
            "handle": handle,
            # Per-account overrides are not settable at creation; NULL means
            # "inherit the workspace column", which is what 054's schedule
            # columns document NULL to mean.
            "tz": None,
            "hs": None,
            "he": None,
            "ppd": None,
        },
    )
    row = result.mappings().one()
    if row["created"] and schedule and row["next_slot_at"] is None:
        # A POSTCONDITION on the seeding invariant, and deliberately NOT mapped
        # to a client status: reaching it means this module created a
        # destination the clock can never see, which is a programming error and
        # is answered as one (500 + log via `_unmapped`).
        #
        # NO CURRENTLY REACHABLE PATH PRODUCES IT, and that was measured rather
        # than assumed — an earlier version of this branch named "the workspace
        # was not visible" as the cause, which is provably wrong:
        #   - workspace absent    → `ig_accounts_workspace_id_fkey` violation
        #     fires first (measured: asyncpg ForeignKeyViolationError).
        #   - claim ≠ workspace_id → refused by `authorize_member`, which binds
        #     BOTH keys in its WHERE (`workspace_id = :ws AND user_id = :u`)
        #     and is what actually holds tenant isolation on this route.
        #     The `p_tenant` WITH CHECK on `ig_accounts` would refuse it too —
        #     but NOT ON THE DEPLOYED PATH, and that leg is therefore not load
        #     bearing here: production connects as `neondb_owner`, which owns
        #     these tables and is BYPASSRLS, and no migration sets FORCE ROW
        #     LEVEL SECURITY, so the policy is inert (#751; `02-domain-model.md`
        #     :1466 ratifies ENABLE-without-FORCE). The unbuilt F.4 posture —
        #     runtime login, definer door, no owner role, no BYPASSRLS — is
        #     what would make it stand.
        #   - a NULL settings column → `posts_per_day`, `posting_hours_*` and
        #     `tz` are all NOT NULL on `workspaces` (053), and `fn_safe_tz`
        #     degrades a decayed zone to UTC rather than returning NULL.
        # It is kept because it is cheap and because a future schema change
        # that breaks the seed would otherwise ship silently — the whole defect
        # this writer exists to fix.
        raise ProvisioningRefused(
            "slot_not_seeded",
            "the posting cursor could not be computed for a new destination",
        )
    return str(row["id"]), bool(row["created"])


# ---------------------------------------------------------------------------
# Sources — a `media_sources` row
# ---------------------------------------------------------------------------


def folder_ref_from(value: object) -> str:
    """The Drive folder id a person supplied, or a NAMED refusal.

    Accepts a bare id or a folder URL, because "paste the folder" means the
    address bar to everyone who has not read the Drive API docs, and storing
    `https://drive.google.com/drive/folders/<id>` as an id would fail much
    later, at the first list call, as something that reads like a Drive fault.

    A URL that is not a folder URL is REFUSED, not salvaged. The delimiter cut
    below runs whether or not the marker was found, so on a link without
    `/folders/` there is nothing to cut *from* and it chews the URL down to its
    scheme — `https://example.com/x` became the id `https:`. That is worse than
    the failure this function exists to prevent, because it is not one failure
    per bad paste: every markerless URL reduces to the same few tokens, so two
    UNRELATED folders collide on the idempotency key and the second paste
    silently returns the first source. One person, two links, one source, no
    signal.

    The guard is a denylist of characters no Drive id carries rather than an
    allowlist of ones it does: Google publishes no guarantee about the id
    charset, so "this still looks like a URL" is the claim we can actually
    defend, while an allowlist would risk refusing a legitimate id on a
    character nobody here anticipated. Refusing costs a person one clear error;
    accepting costs them a silently merged source.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningRefused("folder_required")
    ref = value.strip()
    marker = "/folders/"
    if marker in ref:
        ref = ref.split(marker, 1)[1]
    # A pasted URL keeps going after the id: `?usp=sharing`, a trailing slash,
    # a fragment. Cut at the first delimiter rather than trusting the paste.
    for delimiter in ("?", "#", "/"):
        ref = ref.split(delimiter, 1)[0]
    if not ref:
        raise ProvisioningRefused("folder_required")
    if any(ch in ref for ch in ":. \t\r\n"):
        raise ProvisioningRefused(
            "folder_not_a_drive_folder",
            "expected a Drive folder link or a bare folder id",
        )
    return ref


async def get_or_create_media_source(
    executor,
    *,
    workspace_id: str,
    folder_ref: str,
    root_name: Optional[str] = None,
) -> tuple[str, bool]:
    """The source for *folder_ref* in this workspace. ``(id, created)``.

    Idempotent on the folder, and ATOMICALLY so. Two sources pointing at one
    Drive folder in one workspace would ingest every file twice, and the naive
    read-then-insert is racy on a key no unique index covers: `config` is JSONB
    and `uq_sources_ws_id` keys on the row's own id, so there is nothing for
    `ON CONFLICT` to infer.

    A transaction-scoped advisory lock closes that without a migration. The key
    is derived from workspace + folder, so creates for DIFFERENT folders never
    contend, and the lock dies with the transaction whatever happens — there is
    no release path to get wrong. `hashtextextended` rather than `hashtext`
    because the 32-bit variant collides often enough at estate scale to
    serialize unrelated folders now and then, which is a performance surprise
    nobody would trace back to here.

    `state` plays no part in the match: a paused or errored source for the same
    folder is still that folder's source, and reusing it is the intended
    repair rather than a second row racing it.
    """
    ref = folder_ref_from(folder_ref)
    await executor.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"media_source:{workspace_id}:{GDRIVE_PROVIDER}:{ref}"},
    )
    existing = await readers.row(
        executor,
        "SELECT id FROM media_sources"
        " WHERE workspace_id = :ws AND provider = :provider"
        "   AND config->>'folder_ref' = :folder",
        ws=str(workspace_id),
        provider=GDRIVE_PROVIDER,
        folder=ref,
    )
    if existing is not None:
        return str(existing["id"]), False

    config: dict[str, object] = {"v": GDRIVE_CONFIG_VERSION, "folder_ref": ref}
    if root_name:
        config["root_name"] = root_name
    result = await executor.execute(
        text(
            "INSERT INTO media_sources (workspace_id, provider, config)"
            " VALUES (:ws, :provider, CAST(:config AS jsonb)) RETURNING id"
        ),
        {
            "ws": str(workspace_id),
            "provider": GDRIVE_PROVIDER,
            "config": json.dumps(config),
        },
    )
    return str(result.scalar_one()), True
