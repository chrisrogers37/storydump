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
from sqlalchemy.exc import DBAPIError

from src.exceptions import StorydumpError
from src.services.target import readers
from src.services.target._dbapi import constraint_violated

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

#: `ig_accounts.handle` is `VARCHAR(50)` (054), so this is the COLUMN's bound
#: rather than a policy of ours: a longer handle is refused by name here instead
#: of arriving at Postgres to be rejected as a 500. Deliberately not Instagram's
#: own 30-character limit — that is a provider rule this tier cannot verify, and
#: guessing it would refuse a handle the provider accepts.
HANDLE_MAX = 50

#: The provisional-identity prefix for a destination created from a TYPED handle
#: rather than from an OAuth connection (#1089, shape (b)).
#:
#: `uq_ig_account_live (workspace_id, provider_account_ref)` keys destination
#: identity on a column 054 calls "the REAL account" — a Meta id. Manual mode has
#: no Meta id and must still produce one row per feed, so the handle stands in.
#: Writing it BARE would collide the day OAuth supplies the real id: the same
#: feed would arrive under a different ref, producing two destinations and two
#: schedules against one Instagram account — exactly what the uniqueness exists
#: to prevent. The prefix makes the provisional identity explicit and greppable,
#: so that reconciliation is a query (`provider_account_ref LIKE 'manual:%'`)
#: rather than an archaeology exercise. It costs nothing today.
MANUAL_REF_PREFIX = "manual:"


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


def handle_from(value: object) -> str:
    """The Instagram handle a person typed, or a NAMED refusal.

    Normalises the two things people actually type — a leading ``@`` and
    surrounding whitespace — and refuses everything else rather than repairing
    it. Interior whitespace is a refusal and not a strip: `"two words"` is not a
    handle with a typo in it, it is not a handle, and silently making it
    `"twowords"` would create a destination for an account nobody named.

    It does NOT assert Instagram's own character rules. Those are a provider
    contract this tier cannot check, and a wrong guess refuses a real handle —
    the same reasoning `ACCOUNT_REF_MAX` records for the reference column.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProvisioningRefused("handle_required")
    handle = value.strip().lstrip("@").strip()
    if not handle:
        raise ProvisioningRefused("handle_required")
    if any(ch.isspace() for ch in handle):
        raise ProvisioningRefused("handle_malformed", "no spaces")
    if len(handle) > HANDLE_MAX:
        raise ProvisioningRefused("handle_too_long", f"max {HANDLE_MAX}")
    return handle


def manual_ref_for(handle: str) -> str:
    """The provisional `provider_account_ref` for a typed *handle*.

    Case-folded, and that is the idempotency half rather than tidiness:
    `uq_ig_account_live` is a byte comparison, so `@Foo` and `@foo` would be two
    destinations and two schedules against one feed. Instagram resolves a handle
    case-insensitively, so folding cannot merge two real accounts; not folding
    demonstrably splits one.

    The DISPLAY handle keeps the caller's own casing — it goes to the `handle`
    column, which keys nothing.
    """
    return f"{MANUAL_REF_PREFIX}{handle.casefold()}"


def _identity_for(
    provider_account_ref: object, handle: object
) -> tuple[str, Optional[str]]:
    """`(provider_account_ref, handle)` → the pair that lands in the row.

    Four cases, enumerable on purpose — the shape a signature with two optional
    arguments cannot state:

    ==========================  ============================================
    supplied                    result
    ==========================  ============================================
    a reference key at all      `account_ref_from` decides — it is the one
                                owner of what a reference may be, so a
                                malformed one is REFUSED rather than quietly
                                replaced by a derived handle. Any handle is
                                stored AS GIVEN.
    handle alone                a derived `manual:<handle>`, handle normalised
    neither                     `account_ref_required`
    ==========================  ============================================

    **An explicit reference wins for VALIDATION as well as for storage**, and
    that is the whole reason this is a function. An earlier revision normalised
    the handle before choosing a branch, which made a decorative display column
    able to refuse an identity-bearing create whose identity came from OAuth:
    `create_destination(ref="17841…", handle="two words")` raised
    `handle_malformed` and wrote nothing. `handle_from` therefore runs INSIDE
    the manual branch, where the handle is load-bearing, and nowhere else.
    """
    # SUPPLIED AT ALL, not supplied-and-well-formed. Testing the shape here
    # would restate `account_ref_from`'s own precondition, and restate it
    # weakly: an unquoted Meta id (`provider_account_ref=17841`, an ordinary
    # JSON mistake) fails an `isinstance` test, so a shape check would silently
    # DERIVE `manual:<handle>` for a caller who plainly meant to send a real id,
    # while a too-long string on the same path is refused. One rule, one owner.
    if provider_account_ref is not None or handle is None:
        return account_ref_from(provider_account_ref), (
            handle if isinstance(handle, str) else None
        )
    normalised = handle_from(handle)
    return manual_ref_for(normalised), normalised


async def create_destination(
    executor,
    *,
    workspace_id: str,
    provider_account_ref: Optional[str] = None,
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

    *provider_account_ref* is optional: supply it when a real Meta id is known
    (the OAuth path), or supply only *handle* and this derives a provisional
    ``manual:<handle>`` reference — see `MANUAL_REF_PREFIX` for why that is a
    prefix rather than the bare handle. An explicit reference always wins.

    The cursor is seeded in the SAME statement as the insert, not a follow-up
    UPDATE: a destination that exists with a NULL cursor between two statements
    is a destination the clock will never pick up if the second one fails, and
    that failure would leave no trace anywhere.
    """
    ref, display_handle = _identity_for(provider_account_ref, handle)
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
            "handle": display_handle,
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


async def attach_connected_identity(
    executor,
    *,
    workspace_id: str,
    ig_account_id: str,
    provider_account_ref: str,
    handle: Optional[str],
) -> None:
    """A destination becomes a CONNECTED destination (#1220 step 2).

    The row was created from a typed handle with a provisional
    ``manual:<handle>`` reference (`MANUAL_REF_PREFIX`); the OAuth callback now
    knows the real Meta id, and this is the reconciliation that note promised:
    the reference flips to the real id, the handle is refreshed from the
    profile when Instagram supplied one, and an account parked
    `reauth_required` comes back `active` — the `07` §2 reconnect edge, on the
    account the dispatcher actually reads. A `disabled` account (the remove
    edge, `02` "active ↔ disabled") comes back `active` the same way:
    connecting IS the enable, and a hidden row the clock skips must not
    swallow a fresh credential and stay hidden.

    **A destination is for ONE Instagram account, and this write cannot move
    it to another.** Two refusals make that true, both named `wrong_account`:

    - a row that already carries a real Meta id accepts only THAT id — a
      reconnect performed while signed in to some other Instagram account must
      not silently re-home the schedule, the history and the budget keys to
      a different feed (`07` §2: reconnect "pins the exact credential owner
      being replaced");
    - a row still on its typed handle accepts only the account whose username
      is that handle (case-folded) — connecting `manual:foo` while signed in
      as `@bar` would rename the destination to a feed nobody typed. When
      Instagram omits the username the check cannot run and the connect is
      allowed.

    `uq_ig_account_live (workspace_id, provider_account_ref)` decides the
    third case: the real id is already another live row in this workspace.
    That is two destinations for one Instagram feed, which the index exists
    to forbid, so it is refused as ``duplicate_destination`` rather than
    merged — merging would silently move a schedule. A workspace that is no
    longer active (offboarding) accepts no new credential either — "revoked
    immediately" must not be undone by a state issued before the offboard.
    """
    ref, display_handle, expected_manual = _connected_identity(
        provider_account_ref, handle
    )
    try:
        result = await executor.execute(
            text(
                "UPDATE ig_accounts"
                "   SET provider_account_ref = :ref,"
                "       handle = COALESCE(:handle, handle),"
                "       state = CASE WHEN state IN ('reauth_required', 'disabled')"
                "                    THEN 'active' ELSE state END"
                " WHERE id = :acct AND workspace_id = :ws AND state <> 'moved'"
                "   AND (provider_account_ref = :ref"
                "        OR (provider_account_ref LIKE :manual_prefix"
                # CAST — `.claude/rules/database.md` › bound parameters.
                "            AND (CAST(:expected_manual AS text) IS NULL"
                "                 OR provider_account_ref = :expected_manual)))"
                "   AND EXISTS (SELECT 1 FROM workspaces w"
                "                WHERE w.id = :ws AND w.state = 'active')"
            ),
            {
                "ref": ref,
                "handle": display_handle,
                "acct": str(ig_account_id),
                "ws": str(workspace_id),
                "manual_prefix": f"{MANUAL_REF_PREFIX}%",
                "expected_manual": expected_manual,
            },
        )
    except DBAPIError as exc:
        if constraint_violated(exc, "uq_ig_account_live"):
            raise ProvisioningRefused(
                "duplicate_destination",
                "that Instagram account is already another destination here",
            ) from exc
        raise
    if result.rowcount:
        return
    # Nothing moved: say WHY, because the remedies differ. One read, no
    # tenant widening — the same two keys the UPDATE bound.
    current = await readers.row(
        executor,
        "SELECT a.provider_account_ref, a.state, w.state AS workspace_state"
        "  FROM ig_accounts a JOIN workspaces w ON w.id = a.workspace_id"
        " WHERE a.id = :acct AND a.workspace_id = :ws",
        acct=str(ig_account_id),
        ws=str(workspace_id),
    )
    if (
        current is None
        or current["state"] == "moved"
        or current["workspace_state"] != "active"
    ):
        raise ProvisioningRefused("not_found", f"destination {ig_account_id}")
    raise ProvisioningRefused(
        "wrong_account",
        "this destination is for a different Instagram account than the one"
        " that signed in",
    )


def _connected_identity(
    provider_account_ref: object, handle: object
) -> tuple[str, Optional[str], Optional[str]]:
    """What a CONNECTED identity is, once: `(real ref, display handle, the
    manual ref that handle would have been typed as)`. Both the per-row attach
    and the workspace-level adopt read the same three values."""
    ref = account_ref_from(provider_account_ref)
    try:
        display_handle = handle_from(handle) if handle else None
    except ProvisioningRefused:
        # A display column must not veto an identity-bearing write —
        # `_identity_for` records the regression this guards against. A
        # username the local rule refuses is stored as no username; the real
        # id still lands.
        display_handle = None
    manual_ref = manual_ref_for(display_handle) if display_handle else None
    return ref, display_handle, manual_ref


async def connect_destination(
    executor,
    *,
    workspace_id: str,
    provider_account_ref: str,
    handle: Optional[str],
    ig_account_id: Optional[str] = None,
) -> tuple[str, bool]:
    """Land an identity Instagram just returned on a destination. Returns
    ``(id, created)`` — the one function the callback calls for every grant.

    *ig_account_id* given: the state pinned this row (a per-destination
    connect or reconnect) and the identity attaches to it — or is refused by
    `attach_connected_identity`'s same-account rule.

    *ig_account_id* None: the workspace-level ADD (owner ruling 2026-09-04:
    destinations are added by connecting, so there is nothing to type first).
    Adopt before create. The account may already be a row here — under its
    real id (a reconnect through the workspace-level control) or under the
    typed ``manual:<handle>`` that named it before connecting existed — and a
    second row for one feed is the thing `uq_ig_account_live` forbids. The
    real id wins when both somehow exist. Adoption is the same
    `attach_connected_identity` write, so the same-account rule and the
    `reauth_required`/`disabled` → `active` flip are one code path; a row on
    another real id is out of reach by construction (the lookup never matches
    it). No row at all is a new, SCHEDULED destination under the real id —
    `create_destination`, with the handle Instagram supplied — and only in an
    `active` workspace (`workspace_inactive` otherwise).

    Known residue: a typed row whose handle is NOT the connected account's
    username (a renamed account, a typo) is not matched and stays a separate,
    credential-less destination beside the connected one. The remove command
    is its remedy; nothing here can tell that row from a deliberately parked
    second destination.
    """
    ref, display_handle, manual_ref = _connected_identity(provider_account_ref, handle)
    if ig_account_id is None:
        # One read: the workspace's state and the row this account already has
        # here, if any. The workspace check is the guard the pinned path gets
        # from `attach_connected_identity`'s WHERE — a state issued before an
        # offboard must not land a credential in a workspace being deleted.
        found = await readers.row(
            executor,
            "SELECT w.state AS workspace_state, a.id"
            "  FROM workspaces w"
            "  LEFT JOIN ig_accounts a"
            "    ON a.workspace_id = w.id AND a.state <> 'moved'"
            # No `:manual_ref IS NOT NULL` guard: a NULL compares to nothing,
            # and the guard shape cannot be typed — `.claude/rules/database.md`.
            "   AND (a.provider_account_ref = :ref OR a.provider_account_ref = :manual_ref)"
            " WHERE w.id = :ws"
            " ORDER BY (a.provider_account_ref = :ref) DESC NULLS LAST"
            " LIMIT 1",
            ws=str(workspace_id),
            ref=ref,
            manual_ref=manual_ref,
        )
        if found is None:
            raise ProvisioningRefused("not_found", f"workspace {workspace_id}")
        if found["workspace_state"] != "active":
            raise ProvisioningRefused(
                "workspace_inactive",
                "nothing can be connected to a workspace that is not active",
            )
        if found["id"] is None:
            return await create_destination(
                executor,
                workspace_id=workspace_id,
                provider_account_ref=ref,
                handle=display_handle,
                schedule=True,
            )
        ig_account_id = str(found["id"])
    await attach_connected_identity(
        executor,
        workspace_id=workspace_id,
        ig_account_id=ig_account_id,
        provider_account_ref=ref,
        handle=display_handle,
    )
    return str(ig_account_id), False


#: `ig_login_oauth.PROVIDER`, spelled here rather than imported: that module
#: imports this one for `attach_connected_identity`.
IG_LOGIN_PROVIDER = "ig_login"

#: Live intent states `trg_intent_guard` lets go straight to `cancelled`
#: (`post_intent_transitions`, 055) — and the two it does not, where only the
#: `cancel_requested` overlay can act. Closed literals, interpolated as such.
CANCEL_OUTRIGHT_STATES: tuple[str, ...] = (
    "scheduled",
    "prompt_pending",
    "awaiting_approval",
    "approved",
    "review_required",
)
CANCEL_ON_TOUCH_STATES: tuple[str, ...] = ("publishing", "publishing_ambiguous")


def _sql_list(states: tuple[str, ...]) -> str:
    return ", ".join(f"'{state}'" for state in states)


async def disable_destination(
    executor, *, workspace_id: str, ig_account_id: str
) -> dict:
    """Remove, in the port's terms (owner decision 2026-09-04): `02`'s
    "active ↔ disabled" edge, disabling half. Returns what else moved.

    Four writes, one transaction, in this order: the destination leaves the
    clock's scan (`state = 'disabled'` — `fn_clock_tick` reads `active` only,
    so nothing further is minted for it); its Instagram credential is revoked
    locally, so the refresh cadence stops touching a token nobody wants
    refreshed (the remote revoke is a provider call and does not belong inside
    a unit of work — `disconnect_account`'s scope statement); its live intents
    are cancelled outright through the ledger's own edge, so nothing lingers
    in the Queue (`06` "awaiting_approval cards … suppressed"); an in-flight
    publish is flagged `cancel_requested` instead — the overlay `cancel` uses —
    and the pipeline finishes it at its next checkpoint.

    The row stays. `oauth_credentials` and the intent history hang off it, and
    `connect_destination` adopting a `disabled` row is how the account comes
    back — `attach_connected_identity` flips it `active`. Refused by name when
    nothing moved: `already_disabled` (a stale screen), or `not_found` (no
    such destination here, or a `moved` tombstone).
    """
    disabled = await executor.execute(
        text(
            "UPDATE ig_accounts SET state = 'disabled'"
            " WHERE id = :acct AND workspace_id = :ws"
            "   AND state IN ('active', 'reauth_required')"
        ),
        {"acct": str(ig_account_id), "ws": str(workspace_id)},
    )
    if not disabled.rowcount:
        current = await readers.row(
            executor,
            "SELECT state FROM ig_accounts WHERE id = :acct AND workspace_id = :ws",
            acct=str(ig_account_id),
            ws=str(workspace_id),
        )
        if current is None or current["state"] == "moved":
            raise ProvisioningRefused("not_found", f"destination {ig_account_id}")
        raise ProvisioningRefused(
            "already_disabled", f"destination {ig_account_id} is already disabled"
        )
    revoked = await executor.execute(
        text(
            "UPDATE oauth_credentials SET state = 'revoked'"
            " WHERE workspace_id = :ws AND ig_account_id = :acct"
            "   AND provider = :provider AND state <> 'revoked'"
        ),
        {
            "acct": str(ig_account_id),
            "ws": str(workspace_id),
            "provider": IG_LOGIN_PROVIDER,
        },
    )
    # Cancelled OUTRIGHT, through the edge `trg_intent_guard` admits from every
    # live state but the two publishing ones. Nothing in the web reads
    # `cancel_requested`, and no worker touches an `awaiting_approval` card, so
    # a flag alone would leave the removed account's cards approvable forever.
    cancelled = await executor.execute(
        text(
            "UPDATE post_intents SET state = 'cancelled'"
            " WHERE workspace_id = :ws AND ig_account_id = :acct"
            f"   AND state IN ({_sql_list(CANCEL_OUTRIGHT_STATES)})"
        ),
        {"acct": str(ig_account_id), "ws": str(workspace_id)},
    )
    # An in-flight publish cannot be yanked; the pipeline honours the flag at
    # its next checkpoint — the overlay `cancel` uses.
    flagged = await executor.execute(
        text(
            "UPDATE post_intents SET cancel_requested = true"
            " WHERE workspace_id = :ws AND ig_account_id = :acct"
            f"   AND NOT cancel_requested AND state IN ({_sql_list(CANCEL_ON_TOUCH_STATES)})"
        ),
        {"acct": str(ig_account_id), "ws": str(workspace_id)},
    )
    return {
        "credential_revoked": bool(revoked.rowcount),
        "intents_cancelled": cancelled.rowcount,
        "intents_flagged": flagged.rowcount,
    }
