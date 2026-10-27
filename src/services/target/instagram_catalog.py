"""Complete past-posts observations and generation-scoped metadata erasure.

These transaction-only seams do no provider I/O and are not composed by default.
A parsed bundle contains allowlisted metadata only. Policy does not decide whether
valid raw observations land; captured authorization and source-use state do.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from sqlalchemy import text

from src.services.target import intent_ledger, readers, source_state, vocabulary
from src.services.target.instagram_past_posts_adapter import identity
from src.services.target.instagram_sources import PastPostsRefused, require_activation

#: The exclusion reason of a slide one complete read of its carousel lacked:
#: it is held out of the draw (its item `missing`), the next complete read that
#: lacks it too retires it, and a read that lists it again brings it back.
#: `fn_planned_misses` takes `missing` as final; that is safe because no live
#: story's slide is ever held out (`_held_slides`).
_UNLISTED = "unlisted"

_ID = re.compile(r"[0-9]{1,64}\Z")
_PERMALINK = re.compile(
    r"https://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+/?\Z"
)


@dataclass(frozen=True)
class PastPostsAsset:
    selected_asset_id: str
    media_kind: str
    mime_type: str | None = None
    file_size: int | None = None
    position: int = 1
    availability: str = "available"
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class PastPostsObservation:
    """Fully parsed response: identity/time required; absent optional data is NULL.

    A failed/partial parse must never construct this bundle. A complete response
    can omit an asset or optional metadata and remain an index-only observation.
    Caption, credited handles, provider URLs, cursors and tokens have no fields.
    """

    parent_media_id: str
    original_at: datetime
    assets: tuple[PastPostsAsset, ...] = ()
    slide_count: int | None = None
    slides_complete: bool = True
    permalink: str | None = None
    like_count: int | None = None
    comments_count: int | None = None
    attribution: str = "unknown"
    attribution_detector_version: int = 1
    availability: str = "available"
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class ObservationResult:
    accepted: bool
    reason: str | None = None
    media_item_id: str | None = None
    observation_revision: int | None = None
    #: What the landing did with the slides the read lacked: kept for the live
    #: stories that hold them, held out of the draw, and retired.
    kept: tuple[str, ...] = ()
    unlisted: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()


@dataclass(frozen=True)
class RedactionResult:
    observations_deleted: int
    items_redacted: int
    media_item_ids: tuple[str, ...]
    # Metadata-only result. Artifacts/attempts/operations still require cleanup.


@dataclass(frozen=True)
class TombstoneKeys:
    """Injected retained key ring; no environment or insecure default fallback."""

    current_version: int
    keys: Mapping[int, bytes] = field(repr=False)

    def __post_init__(self):
        copied = dict(self.keys)
        if (
            type(self.current_version) is not int
            or self.current_version not in copied
            or any(
                type(k) is not int or k < 1 or not isinstance(v, bytes) or len(v) < 32
                for k, v in copied.items()
            )
        ):
            raise PastPostsRefused("past_posts_tombstone_key_unavailable")
        object.__setattr__(self, "keys", MappingProxyType(copied))

    def digest(self, version: int, parent: str) -> str:
        key = self.keys.get(version)
        if key is None:
            raise PastPostsRefused("past_posts_tombstone_key_unavailable")
        payload = b"storydump:instagram-parent:v1\x00" + identity(parent).encode(
            "ascii"
        )
        return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _validate(observation: PastPostsObservation) -> None:
    def media_id(value):
        return isinstance(value, str) and _ID.fullmatch(value)

    def count(value):
        return value is None or type(value) is int and value >= 0

    o = observation
    if not isinstance(o, PastPostsObservation):
        raise PastPostsRefused("invalid_past_posts_observation")
    valid = (
        media_id(o.parent_media_id)
        and isinstance(o.original_at, datetime)
        and o.original_at.utcoffset() is not None
        and isinstance(o.assets, tuple)
        and (
            o.slide_count is None
            or type(o.slide_count) is int
            and 0 <= o.slide_count <= 20
        )
        and type(o.slides_complete) is bool
        and (
            o.permalink is None
            or isinstance(o.permalink, str)
            and _PERMALINK.fullmatch(o.permalink)
        )
        and count(o.like_count)
        and count(o.comments_count)
        and o.attribution in ("not_detected", "credited", "unknown")
        and type(o.attribution_detector_version) is int
        and o.attribution_detector_version > 0
        and o.availability in ("available", "unavailable", "excluded")
        and o.exclusion_reason
        in (
            None,
            "unsupported_media",
            "media_unavailable",
            "incomplete_observation",
            "asset_unavailable",
        )
    )
    ids, positions = set(), set()
    assets = o.assets if isinstance(o.assets, tuple) else ()
    for a in assets:
        valid = valid and (
            isinstance(a, PastPostsAsset)
            and media_id(a.selected_asset_id)
            and a.selected_asset_id not in ids
            and type(a.position) is int
            and 0 < a.position <= 20
            and a.position not in positions
            and a.media_kind in ("image", "video", "unknown")
            and a.mime_type
            in (None, "image/jpeg", "image/png", "video/mp4", "video/quicktime")
            and count(a.file_size)
            and a.availability in ("available", "unavailable", "excluded")
            and a.exclusion_reason
            in (
                None,
                "unsupported_media",
                "media_unavailable",
                "asset_unavailable",
                "origin_identity_conflict",
            )
            and (a.availability == "available") == (a.exclusion_reason is None)
            and (
                a.media_kind != "unknown"
                or a.availability == "excluded"
                and a.exclusion_reason == "unsupported_media"
            )
        )
        if isinstance(a, PastPostsAsset):
            ids.add(a.selected_asset_id)
            positions.add(a.position)
    valid = valid and (
        (
            o.slide_count is None
            and len(o.assets) <= 1
            and all(
                a.selected_asset_id == o.parent_media_id and a.position == 1
                for a in o.assets
            )
        )
        or (
            o.slide_count is not None
            and o.slide_count == len(o.assets)
            and {a.position for a in o.assets} == set(range(1, len(o.assets) + 1))
        )
    )
    if not valid:
        raise PastPostsRefused("invalid_past_posts_observation")


async def lock_past_posts(executor, *, workspace_id: str, source_id: str):
    """Common prefix, origin/source, then credential and generation rows.

    The first source read discovers its immutable origin under the workspace
    advisory lock. This is not admission: all state is re-read after row locks.
    Call only before any assignment/item/intent/job locks.
    """
    ws, sid = str(workspace_id), str(source_id)
    await source_state.lock_workspace_sources(executor, workspace_id=ws)
    source = await readers.row(
        executor,
        "SELECT origin_ig_account_id FROM media_sources WHERE workspace_id=:ws AND id=:sid AND provider='instagram_past_posts'",
        ws=ws,
        sid=sid,
    )
    if source is None:
        return None
    account = str(source["origin_ig_account_id"])
    await source_state.lock_workspace_sources(
        executor, workspace_id=ws, ig_account_ids=[account], source_ids=[sid]
    )
    credentials = await readers.rows(
        executor,
        "SELECT id, state, (expires_at IS NULL OR expires_at > now()) AS unexpired FROM "
        "oauth_credentials WHERE workspace_id=:ws AND ig_account_id=:account AND "
        "provider='ig_login' ORDER BY id FOR UPDATE",
        ws=ws,
        account=account,
    )
    generations = await readers.rows(
        executor,
        "SELECT * FROM instagram_past_posts_generations WHERE workspace_id=:ws AND source_id=:sid ORDER BY id FOR UPDATE",
        ws=ws,
        sid=sid,
    )
    source = await readers.row(
        executor,
        "SELECT s.*, a.state AS origin_state, a.provider_account_ref, EXISTS (SELECT 1 FROM "
        "media_source_destinations d WHERE d.workspace_id=s.workspace_id AND d.source_id=s.id AND "
        "d.ig_account_id=s.origin_ig_account_id AND d.state='active') AS assigned, "
        "COALESCE((SELECT ratio>0 FROM category_post_case_mix x WHERE "
        "x.workspace_id=s.workspace_id AND x.source_id=s.id AND x.effective_to IS NULL), false) AS "
        "enabled FROM media_sources s JOIN ig_accounts a ON a.workspace_id=s.workspace_id AND "
        "a.id=s.origin_ig_account_id WHERE s.workspace_id=:ws AND s.id=:sid",
        ws=ws,
        sid=sid,
    )
    return source, credentials, generations


def _drop(locked, generation_id, captured_epoch):
    if locked is None:
        return "past_posts_source_unavailable"
    source, credentials, generations = locked
    if (
        source["state"] != "active"
        or source["config"].get("removed")
        or not source["assigned"]
    ):
        return "past_posts_source_unavailable"
    if source["origin_state"] != "active" or source["provider_account_ref"].startswith(
        "manual:"
    ):
        return "past_posts_origin_unavailable"
    if (
        not credentials
        or credentials[0]["state"] != "active"
        or not credentials[0]["unexpired"]
    ):
        return "past_posts_credential_unavailable"
    if not source["enabled"]:
        return "past_posts_source_off"
    if source["source_use_epoch"] != captured_epoch or not any(
        str(g["id"]) == str(generation_id)
        and g["id"] == source["current_authorization_generation_id"]
        and g["lifecycle"] == "active"
        and g["credential_id"] == credentials[0]["id"]
        for g in generations
    ):
        return "past_posts_stale_capture"
    return None


async def write_observation(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    generation_id: str,
    captured_epoch: int,
    observation: PastPostsObservation,
    keys: TombstoneKeys,
    capability=None,
) -> ObservationResult:
    require_activation(capability)
    _validate(observation)
    async with source_state.source_savepoint(executor):
        locked = await lock_past_posts(
            executor, workspace_id=workspace_id, source_id=source_id
        )
        reason = _drop(locked, generation_id, captured_epoch)
        if reason:
            return ObservationResult(False, reason)
        return await _land(
            executor,
            str(workspace_id),
            str(source_id),
            str(generation_id),
            observation,
            keys,
        )


def _bundle_assets(o, previous, held=frozenset(), unlisted=frozenset()):
    """Keep unseen slides after a partial response; after a complete one,
    keep the `held` slides it lacks as they were, and the `unlisted` ones it
    lacks out of the draw (`_UNLISTED`), without position collisions."""
    observed_ids = {a.selected_asset_id for a in o.assets}
    # The response's positions win. Kept rows follow it, in their old order,
    # so the deferrable position key never collides.
    assets = list(o.assets)
    unseen = sorted(
        (
            r
            for r in previous
            if r["asset_media_id"] not in observed_ids
            and (
                not o.slides_complete
                or r["asset_media_id"] in held
                or r["asset_media_id"] in unlisted
            )
        ),
        key=lambda r: (r["position"], r["asset_media_id"]),
    )
    for row in unseen:
        out = row["asset_media_id"] in unlisted
        assets.append(
            PastPostsAsset(
                row["asset_media_id"],
                row["media_kind"],
                row["mime_type"],
                row["file_size"],
                position=len(assets) + 1,
                availability="unavailable" if out else row["availability"],
                exclusion_reason=_UNLISTED if out else row["exclusion_reason"],
            )
        )
    return assets


def expected_slide_count(slide_count, position):
    """The slide count a slide missing from a read is judged against: the
    fetch's `expected_slide_count`, from the index's count for the post and
    the slide's position. None for a single post.

    A landing puts the read's own slides at positions 1 to `slide_count`, and
    any slide it carries after them (`_bundle_assets`: unseen after a partial
    read, held after a complete one). A listed slide is judged against the
    count of the read that listed it. A carried slide, one past that count, is
    judged against the count plus itself. So no sibling's state moves another
    slide's count.
    """
    if slide_count is not None and position > slide_count:
        return slide_count + 1
    return slide_count


def _lacking(o, previous, missing, held):
    """What a read does with the slides it lacks that the index holds an item
    for (`missing`, id to item; empty for a partial read), beside the `held`
    ones a live story keeps: the slides it holds out of the draw, and the ones
    it retires (id to item).

    The two-read rule at sync: the first complete read of a carousel that lacks
    a drawable slide holds it out (`_UNLISTED`), and the next one that lacks it
    too retires it. An excluded slide is never drawn, so it retires on the
    first; so does a single post's asset, which is the post itself."""
    drawable = {
        r["asset_media_id"]
        for r in previous
        if r["availability"] != "excluded" and r["exclusion_reason"] != _UNLISTED
    }
    unlisted = (
        frozenset(mid for mid in missing if mid not in held and mid in drawable)
        if o.slide_count
        else frozenset()
    )
    retiring = {
        mid: item
        for mid, item in missing.items()
        if mid not in held and mid not in unlisted
    }
    return unlisted, retiring


def _item_state(asset, conflict):
    """The state a landed slide gives its item: available while it may be
    drawn, `missing` while a complete read holds it out (`_UNLISTED`), and
    unsupported otherwise."""
    if conflict:
        return "unsupported"
    if asset.availability == "available":
        return "available"
    return "missing" if asset.exclusion_reason == _UNLISTED else "unsupported"


async def _held_slides(executor, ws, missing):
    """The slides a complete read lacks (`missing`, id to item) whose item a
    live story holds.

    A slide is its own entity: one read that lacks it is no proof it left. So
    while a story that is neither terminal nor asked to cancel holds the
    slide, the index keeps it for that story's own fetch to judge
    (`instagram_past_posts_adapter`'s two-read proof), whatever the read: a
    full carousel's and a first-slide source's included. No sync reply
    retires a slide a live story holds.
    """
    if not missing:
        return frozenset()
    rows = await readers.rows(
        executor,
        "SELECT DISTINCT media_item_id FROM post_intents WHERE workspace_id=:ws "
        "AND media_item_id=ANY(CAST(:items AS uuid[])) AND NOT cancel_requested "
        f"AND {intent_ledger.NOT_TERMINAL}",
        ws=ws,
        items=sorted(set(missing.values())),
    )
    holding = {str(row["media_item_id"]) for row in rows}
    return frozenset(mid for mid, item in missing.items() if item in holding)


async def _land(executor, ws, sid, gid, o, keys):
    """Replace one post bundle with its independently identified asset items.

    Every provider-sized set crosses SQL as JSON once. Item locks are taken in
    UUID order, and no statement is issued inside a slide loop.
    """
    params = {"ws": ws, "sid": sid, "gid": gid, "parent": o.parent_media_id}
    current = await readers.row(
        executor,
        "SELECT data_generation_id,observation_revision FROM instagram_media_index "
        "WHERE workspace_id=:ws AND source_id=:sid AND parent_media_id=:parent FOR UPDATE",
        ws=ws,
        sid=sid,
        parent=o.parent_media_id,
    )
    previous = await readers.rows(
        executor,
        "SELECT a.asset_media_id,a.position,a.media_kind,a.availability,"
        "a.exclusion_reason,a.media_item_id,m.mime_type,m.file_size "
        "FROM instagram_media_assets a LEFT JOIN media_items m "
        "ON m.workspace_id=a.workspace_id AND m.id=a.media_item_id "
        "WHERE a.workspace_id=:ws AND a.source_id=:sid "
        "AND a.parent_media_id=:parent ORDER BY a.asset_media_id FOR UPDATE OF a",
        ws=ws,
        sid=sid,
        parent=o.parent_media_id,
    )
    observed_ids = {a.selected_asset_id for a in o.assets}
    # Every slide the bundle can hold was observed now or held before.
    wanted_ids = sorted(
        {o.parent_media_id} | observed_ids | {row["asset_media_id"] for row in previous}
    )
    version_rows = await readers.rows(
        executor,
        "SELECT DISTINCT key_version,"
        "max(CASE WHEN source_id=:sid THEN observation_revision END) OVER () "
        "AS source_max FROM instagram_item_tombstones "
        "WHERE workspace_id=:ws ORDER BY key_version",
        ws=ws,
        sid=sid,
    )
    versions = [row["key_version"] for row in version_rows]
    source_tombstone_revision = (
        int(version_rows[0]["source_max"])
        if version_rows and version_rows[0]["source_max"] is not None
        else 0
    )
    wanted = [
        {"asset_id": mid, "version": version, "digest": keys.digest(version, mid)}
        for mid in wanted_ids
        for version in versions
    ]
    matches = (
        await readers.rows(
            executor,
            "SELECT w.asset_id,t.source_id,t.media_item_id,t.observation_revision "
            "FROM jsonb_to_recordset(CAST(:wanted AS jsonb)) "
            "AS w(asset_id text,version integer,digest text) "
            "JOIN instagram_item_tombstones t ON t.workspace_id=:ws "
            "AND t.key_version=w.version AND t.parent_hmac=w.digest "
            "ORDER BY t.media_item_id",
            ws=ws,
            wanted=json.dumps(wanted),
        )
        if wanted
        else []
    )
    by_tombstone = {}
    for row in matches:
        by_tombstone.setdefault(row["asset_id"], []).append(row)
    tombstone_ids = [str(row["media_item_id"]) for row in matches]
    hashes = [identity(mid) for mid in wanted_ids]
    existing = await readers.rows(
        executor,
        "SELECT id,source_id,content_hash FROM media_items WHERE workspace_id=:ws "
        "AND (content_hash=ANY(CAST(:hashes AS text[])) "
        "OR id=ANY(CAST(:ids AS uuid[]))) ORDER BY id FOR UPDATE",
        ws=ws,
        hashes=hashes,
        ids=tombstone_ids,
    )
    by_hash = {row["content_hash"]: row for row in existing}
    by_id = {str(row["id"]): row for row in existing}
    previous_ids = {}
    for row in previous:
        mid = row["asset_media_id"]
        found = by_hash.get(identity(mid))
        if row["media_item_id"] is not None:
            previous_ids[mid] = str(row["media_item_id"])
        elif found is not None and str(found["source_id"]) == sid:
            previous_ids[mid] = str(found["id"])
    # The slides a complete read lacks that the index holds an item for; a
    # partial read proves nothing about the slides it did not reach.
    missing = (
        {mid: item for mid, item in previous_ids.items() if mid not in observed_ids}
        if o.slides_complete
        else {}
    )
    held = await _held_slides(executor, ws, missing)
    unlisted, retiring = _lacking(o, previous, missing, held)
    assets = _bundle_assets(o, previous, held, unlisted)
    # A list that keeps a slide the read lacked is no longer one complete read;
    # its count stays what the read listed.
    list_complete = o.slides_complete and not held and not unlisted
    kept_ids = {a.selected_asset_id for a in assets}
    revision = (
        max(
            [
                int(current["observation_revision"]) if current else 0,
                source_tombstone_revision,
            ]
            + [int(row["observation_revision"]) for row in matches]
        )
        + 1
    )
    rows = []
    for asset in assets:
        mid = asset.selected_asset_id
        found = by_hash.get(identity(mid))
        tombstones = by_tombstone.get(mid, ())
        own = {
            str(t["media_item_id"]) for t in tombstones if str(t["source_id"]) == sid
        }
        known = previous_ids.get(mid)
        if found is not None and str(found["source_id"]) == sid:
            own.add(str(found["id"]))
        if known is not None:
            own.add(known)
        if len(own) > 1:
            raise PastPostsRefused("past_posts_identity_inconsistent")
        conflict = asset.media_kind != "unknown" and (
            found is not None
            and str(found["source_id"]) != sid
            or any(str(t["source_id"]) != sid for t in tombstones)
        )
        project_item = next(iter(own), None) if not conflict else None
        if project_item is not None and (
            project_item not in by_id or str(by_id[project_item]["source_id"]) != sid
        ):
            raise PastPostsRefused("past_posts_identity_inconsistent")
        item = None if asset.media_kind == "unknown" else project_item
        slide = o.slide_count is not None
        name = (
            vocabulary.PAST_POSTS_SLIDE_NAME.format(
                position=asset.position, count=o.slide_count
            )
            if slide and list_complete
            else vocabulary.PAST_POSTS_SLIDE_NAME_UNKNOWN.format(
                position=asset.position
            )
            if slide
            else vocabulary.PAST_POSTS_ITEM_NAME
        )
        rows.append(
            {
                "asset_id": mid,
                "position": asset.position,
                "kind": asset.media_kind,
                "mime": asset.mime_type,
                "size": asset.file_size,
                "availability": "excluded" if conflict else asset.availability,
                "reason": "origin_identity_conflict"
                if conflict
                else asset.exclusion_reason,
                "state": _item_state(asset, conflict),
                "item": item,
                "project_item": project_item,
                "hash": identity(mid),
                "name": name,
            }
        )
    post_reason = o.exclusion_reason or (
        "incomplete_observation" if not rows and o.availability == "available" else None
    )
    if rows and all(row["reason"] == "origin_identity_conflict" for row in rows):
        post_reason = "origin_identity_conflict"
    post_availability = (
        "excluded"
        if post_reason == "origin_identity_conflict"
        or not rows
        and o.availability == "available"
        and list_complete
        else o.availability
    )
    await executor.execute(
        text(
            "INSERT INTO instagram_media_index (workspace_id,source_id,parent_media_id,"
            "data_generation_id,observation_revision,original_at,permalink,like_count,"
            "comments_count,attribution,attribution_detector_version,availability,"
            "exclusion_reason,slide_count,slides_complete) VALUES "
            "(:ws,:sid,:parent,:gid,:revision,:at,:link,:likes,:comments,:attribution,"
            ":detector,:availability,:reason,:slide_count,:slides_complete) "
            "ON CONFLICT (workspace_id,source_id,parent_media_id) DO UPDATE SET "
            "data_generation_id=EXCLUDED.data_generation_id,"
            "observation_revision=EXCLUDED.observation_revision,"
            "original_at=EXCLUDED.original_at,permalink=EXCLUDED.permalink,"
            "like_count=EXCLUDED.like_count,comments_count=EXCLUDED.comments_count,"
            "observed_at=now(),attribution=EXCLUDED.attribution,"
            "attribution_detector_version=EXCLUDED.attribution_detector_version,"
            "availability=EXCLUDED.availability,exclusion_reason=EXCLUDED.exclusion_reason,"
            "slide_count=EXCLUDED.slide_count,slides_complete=EXCLUDED.slides_complete"
        ),
        {
            **params,
            "revision": revision,
            "at": o.original_at,
            "link": o.permalink,
            "likes": o.like_count,
            "comments": o.comments_count,
            "attribution": o.attribution,
            "detector": o.attribution_detector_version,
            "availability": post_availability,
            "reason": post_reason,
            "slide_count": o.slide_count,
            "slides_complete": list_complete,
        },
    )
    if o.slides_complete:
        for mid, item in retiring.items():
            if any(
                str(t["source_id"]) != sid or str(t["media_item_id"]) != item
                for t in by_tombstone.get(mid, ())
            ):
                raise PastPostsRefused("past_posts_identity_inconsistent")
        removed = [
            {
                "asset_id": mid,
                "item": item,
                "digest": keys.digest(keys.current_version, mid),
            }
            for mid, item in retiring.items()
        ]
        if removed:
            await executor.execute(
                text(
                    "INSERT INTO instagram_item_tombstones (workspace_id,source_id,"
                    "media_item_id,key_version,parent_hmac,observation_revision) "
                    "SELECT CAST(:ws AS uuid),CAST(:sid AS uuid),x.item,"
                    ":version,x.digest,:revision FROM jsonb_to_recordset(CAST(:removed AS jsonb)) "
                    "AS x(item uuid,digest text) "
                    "ON CONFLICT (workspace_id,source_id,key_version,parent_hmac) "
                    "DO UPDATE SET observation_revision=GREATEST("
                    "instagram_item_tombstones.observation_revision,EXCLUDED.observation_revision)"
                ),
                {
                    **params,
                    "version": keys.current_version,
                    "revision": revision,
                    "removed": json.dumps(removed),
                },
            )
        await executor.execute(
            text(
                "UPDATE media_items m SET state='removed' FROM instagram_media_assets a "
                "WHERE a.workspace_id=:ws AND a.source_id=:sid AND a.parent_media_id=:parent "
                "AND a.asset_media_id <> ALL(CAST(:kept AS text[])) "
                "AND m.workspace_id=a.workspace_id AND m.source_id=a.source_id "
                "AND (m.id=a.media_item_id OR m.content_hash='instagram_past_posts:media:'||a.asset_media_id)"
            ),
            {**params, "kept": sorted(kept_ids)},
        )
        await executor.execute(
            text(
                "DELETE FROM instagram_media_assets WHERE workspace_id=:ws AND source_id=:sid "
                "AND parent_media_id=:parent AND asset_media_id <> ALL(CAST(:kept AS text[]))"
            ),
            {**params, "kept": sorted(kept_ids)},
        )
    if rows:
        payload = json.dumps(rows)
        await executor.execute(
            text(
                "UPDATE media_items m SET source_data_generation_id=CAST(:gid AS uuid),"
                "source_projection_redacted=false,content_hash=x.hash,provider_file_ref=x.asset_id,"
                "file_name=x.name,media_kind=CASE WHEN x.kind IN ('image','video') "
                "THEN x.kind ELSE m.media_kind END,mime_type=x.mime,file_size=x.size,"
                "state=x.state,"
                "last_listed_at=now(),thumbnail_url=NULL,category=NULL,folder_path=NULL,"
                "title=NULL,caption=NULL,generated_caption=NULL,link_url=NULL,tags=NULL,"
                "custom_metadata=NULL FROM jsonb_to_recordset(CAST(:items AS jsonb)) "
                "AS x(asset_id text,kind text,mime text,size bigint,availability text,"
                "state text,item uuid,project_item uuid,hash text,name text) WHERE m.workspace_id=:ws "
                "AND m.source_id=:sid AND m.id=x.project_item"
            ),
            {**params, "items": payload},
        )
        await executor.execute(
            text(
                "INSERT INTO media_items (workspace_id,source_id,source_data_generation_id,"
                "content_hash,provider_file_ref,file_name,media_kind,mime_type,file_size,state,"
                "last_listed_at) SELECT CAST(:ws AS uuid),CAST(:sid AS uuid),CAST(:gid AS uuid),"
                "x.hash,x.asset_id,x.name,x.kind,x.mime,x.size,'available',now() "
                "FROM jsonb_to_recordset(CAST(:items AS jsonb)) AS "
                "x(asset_id text,kind text,mime text,size bigint,availability text,item uuid,"
                "project_item uuid,hash text,name text) WHERE x.item IS NULL AND x.availability='available' "
                "AND x.kind IN ('image','video') ORDER BY x.hash "
                "ON CONFLICT ON CONSTRAINT uq_media_dedup DO NOTHING"
            ),
            {**params, "items": payload},
        )
        linked = await readers.rows(
            executor,
            "SELECT id,source_id,content_hash FROM media_items WHERE workspace_id=:ws "
            "AND content_hash=ANY(CAST(:hashes AS text[])) ORDER BY id FOR UPDATE",
            ws=ws,
            hashes=[row["hash"] for row in rows],
        )
        linked_by_hash = {row["content_hash"]: row for row in linked}
        for row in rows:
            found = linked_by_hash.get(row["hash"])
            if (
                found is not None
                and str(found["source_id"]) != sid
                and row["kind"] != "unknown"
            ):
                row["availability"] = "excluded"
                row["reason"] = "origin_identity_conflict"
                row["item"] = None
            elif (
                found is not None
                and row["availability"] != "excluded"
                and row["kind"] != "unknown"
            ):
                row["item"] = str(found["id"])
        if post_reason != "origin_identity_conflict" and all(
            row["reason"] == "origin_identity_conflict" for row in rows
        ):
            post_reason = "origin_identity_conflict"
            await executor.execute(
                text(
                    "UPDATE instagram_media_index SET availability='excluded',"
                    "exclusion_reason='origin_identity_conflict' WHERE workspace_id=:ws "
                    "AND source_id=:sid AND parent_media_id=:parent"
                ),
                params,
            )
        await executor.execute(
            text(
                "INSERT INTO instagram_media_assets (workspace_id,source_id,parent_media_id,"
                "asset_media_id,data_generation_id,observation_revision,position,media_kind,"
                "availability,exclusion_reason,media_item_id) "
                "SELECT CAST(:ws AS uuid),CAST(:sid AS uuid),:parent,x.asset_id,"
                "CAST(:gid AS uuid),:revision,x.position,x.kind,x.availability,x.reason,x.item "
                "FROM jsonb_to_recordset(CAST(:assets AS jsonb)) AS "
                "x(asset_id text,position integer,kind text,availability text,reason text,item uuid) "
                "ON CONFLICT (workspace_id,source_id,asset_media_id) DO UPDATE SET "
                "parent_media_id=EXCLUDED.parent_media_id,"
                "data_generation_id=EXCLUDED.data_generation_id,"
                "observation_revision=EXCLUDED.observation_revision,"
                "position=EXCLUDED.position,media_kind=EXCLUDED.media_kind,"
                "availability=EXCLUDED.availability,exclusion_reason=EXCLUDED.exclusion_reason,"
                "media_item_id=EXCLUDED.media_item_id,validation_state='pending',"
                "validation_generation_id=NULL,validation_observation_revision=NULL,"
                "validation_fingerprint=NULL,validated_at=NULL,validation_capture=NULL"
            ),
            {**params, "revision": revision, "assets": json.dumps(rows)},
        )
    first_item = next((row["item"] for row in rows if row["item"]), None)
    return ObservationResult(
        True,
        post_reason,
        first_item,
        revision,
        kept=tuple(sorted(held)),
        unlisted=tuple(sorted(unlisted)),
        retired=tuple(sorted(retiring)),
    )


async def retire_selected_asset(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    generation_id: str,
    observation_revision: int,
    asset_media_id: str,
    media_item_id: str,
    keys: TombstoneKeys,
) -> bool:
    """Retire one proved-gone slide under the caller's canonical source locks.

    The caller holds the source, item, intent and provenance locks. A stale
    selected-ID capture changes nothing; a match keeps identity through a
    tombstone and leaves sibling assets untouched.
    """
    ws, sid = str(workspace_id), str(source_id)
    params = dict(
        ws=ws,
        sid=sid,
        gid=str(generation_id),
        rev=observation_revision,
        asset=asset_media_id,
        item=str(media_item_id),
    )
    async with source_state.source_savepoint(executor):
        selected = await readers.row(
            executor,
            "SELECT a.parent_media_id FROM instagram_media_assets a "
            "JOIN media_sources s ON s.workspace_id=a.workspace_id AND s.id=a.source_id "
            "AND s.current_authorization_generation_id=a.data_generation_id "
            "WHERE a.workspace_id=:ws AND a.source_id=:sid "
            "AND a.data_generation_id=CAST(:gid AS uuid) "
            "AND a.observation_revision=:rev AND a.asset_media_id=:asset "
            "AND a.media_item_id=CAST(:item AS uuid) FOR UPDATE OF a",
            **params,
        )
        if selected is None:
            return False
        versions = await readers.rows(
            executor,
            "SELECT DISTINCT key_version FROM instagram_item_tombstones "
            "WHERE workspace_id=:ws ORDER BY key_version",
            ws=ws,
        )
        wanted = [
            {
                "version": row["key_version"],
                "digest": keys.digest(row["key_version"], asset_media_id),
            }
            for row in versions
        ]
        if wanted:
            matches = await readers.rows(
                executor,
                "SELECT t.source_id,t.media_item_id FROM "
                "jsonb_to_recordset(CAST(:wanted AS jsonb)) AS w(version integer,digest text) "
                "JOIN instagram_item_tombstones t ON t.workspace_id=:ws "
                "AND t.key_version=w.version AND t.parent_hmac=w.digest",
                ws=ws,
                wanted=json.dumps(wanted),
            )
            if any(
                str(row["source_id"]) != sid
                or str(row["media_item_id"]) != params["item"]
                for row in matches
            ):
                raise PastPostsRefused("past_posts_identity_inconsistent")
        deleted = await executor.execute(
            text(
                "DELETE FROM instagram_media_assets WHERE workspace_id=:ws AND source_id=:sid "
                "AND data_generation_id=CAST(:gid AS uuid) AND observation_revision=:rev "
                "AND asset_media_id=:asset AND media_item_id=CAST(:item AS uuid)"
            ),
            params,
        )
        if deleted.rowcount != 1:
            raise PastPostsRefused("past_posts_stale_capture")
        digest = keys.digest(keys.current_version, asset_media_id)
        tombstone = await executor.execute(
            text(
                "INSERT INTO instagram_item_tombstones (workspace_id,source_id,media_item_id,"
                "key_version,parent_hmac,observation_revision) VALUES "
                "(CAST(:ws AS uuid),CAST(:sid AS uuid),CAST(:item AS uuid),:version,:digest,:rev) "
                "ON CONFLICT (workspace_id,source_id,key_version,parent_hmac) DO UPDATE "
                "SET observation_revision=GREATEST(instagram_item_tombstones.observation_revision,"
                "EXCLUDED.observation_revision) WHERE "
                "instagram_item_tombstones.media_item_id=EXCLUDED.media_item_id "
                "RETURNING media_item_id"
            ),
            {**params, "version": keys.current_version, "digest": digest},
        )
        if tombstone.scalar_one_or_none() is None:
            raise PastPostsRefused("past_posts_identity_inconsistent")
        item = await executor.execute(
            text(
                "UPDATE media_items SET state='removed' WHERE workspace_id=:ws "
                "AND source_id=:sid AND id=CAST(:item AS uuid)"
            ),
            params,
        )
        if item.rowcount != 1:
            raise PastPostsRefused("past_posts_identity_inconsistent")
        await executor.execute(
            text(
                "UPDATE instagram_media_index SET slides_complete=false WHERE workspace_id=:ws "
                "AND source_id=:sid AND parent_media_id=:parent "
                "AND data_generation_id=CAST(:gid AS uuid) AND observation_revision=:rev "
                "AND slide_count IS NOT NULL"
            ),
            {**params, "parent": selected["parent_media_id"]},
        )
        return True


_REDACTION_ITEM_LOOKUP = (
    "SELECT m.id, m.provider_file_ref, "
    "GREATEST(COALESCE(a.observation_revision, 0),"
    "COALESCE((SELECT max(t.observation_revision) FROM instagram_item_tombstones t "
    "WHERE t.workspace_id=m.workspace_id AND t.source_id=m.source_id AND t.media_item_id=m.id),0),1) "
    "AS observation_revision FROM media_items m LEFT JOIN instagram_media_assets a "
    "ON a.workspace_id=m.workspace_id AND a.source_id=m.source_id AND "
    "(a.media_item_id=m.id OR a.asset_media_id=m.provider_file_ref) "
    "AND a.data_generation_id=m.source_data_generation_id WHERE "
    "m.workspace_id=:ws AND m.source_id=:sid AND m.source_data_generation_id=ANY(CAST(:gids AS "
    "uuid[])) ORDER BY m.id FOR UPDATE OF m"
)


async def redact_generations(
    executor,
    *,
    workspace_id: str,
    source_id: str,
    generation_ids: frozenset[str],
    keys: TombstoneKeys,
    capability=None,
) -> RedactionResult:
    """Erase only frozen, fenced generations; never declare a receipt complete.

    Erasure closes, so it needs no capability: a rollback still owes the
    deletion. Landing an observation (write_observation) still does.
    """
    async with source_state.source_savepoint(executor):
        locked = await lock_past_posts(
            executor, workspace_id=workspace_id, source_id=source_id
        )
        scope = frozenset(str(g) for g in generation_ids)
        available = (
            {
                str(g["id"])
                for g in locked[2]
                if g["lifecycle"] in ("deletion_pending", "deleted")
            }
            if locked
            else set()
        )
        if not scope or not scope <= available:
            raise PastPostsRefused("past_posts_generation_scope_unavailable")
        params = {"ws": str(workspace_id), "sid": str(source_id), "gids": sorted(scope)}
        items = await readers.rows(executor, _REDACTION_ITEM_LOOKUP, **params)
        # One executemany for the whole scope, not a round trip per item (a scope
        # holds thousands) while the workspace's source locks are held. Each row
        # still runs the statement on its own, so rows that share a key merge.
        tombstones = [
            {
                "ws": params["ws"],
                "sid": params["sid"],
                "item": str(item["id"]),
                "version": keys.current_version,
                "digest": keys.digest(keys.current_version, item["provider_file_ref"]),
                "revision": int(item["observation_revision"]),
            }
            for item in items
        ]
        if tombstones:
            await executor.execute(
                text(
                    "INSERT INTO instagram_item_tombstones (workspace_id, source_id, media_item_id, "
                    "key_version, parent_hmac, observation_revision) VALUES (:ws, :sid, :item, :version, "
                    ":digest, :revision) ON CONFLICT (workspace_id, source_id, key_version, parent_hmac) DO "
                    "UPDATE SET observation_revision=GREATEST(instagram_item_tombstones.observation_revision, "
                    "EXCLUDED.observation_revision)"
                ),
                tombstones,
            )
        deleted = await executor.execute(
            text(
                "DELETE FROM instagram_media_index WHERE workspace_id=:ws AND source_id=:sid AND data_generation_id=ANY(CAST(:gids AS uuid[]))"
            ),
            params,
        )
        erased = await executor.execute(
            text(
                "UPDATE media_items SET source_data_generation_id=NULL, source_projection_redacted=true, "
                "provider_file_ref='redacted:' || id::text, file_name='redacted:' || id::text, "
                "content_hash='redacted:' || id::text, media_kind='image', state='removed', "
                "mime_type=NULL, file_size=NULL, last_listed_at=NULL, thumbnail_url=NULL, category=NULL, "
                "folder_path=NULL, title=NULL, caption=NULL, generated_caption=NULL, link_url=NULL, "
                "tags=NULL, custom_metadata=NULL WHERE workspace_id=:ws AND source_id=:sid AND "
                "source_data_generation_id=ANY(CAST(:gids AS uuid[]))"
            ),
            params,
        )
        return RedactionResult(
            deleted.rowcount, erased.rowcount, tuple(str(i["id"]) for i in items)
        )
