"""Instagram Login past-posts read contract (#1644).

This adapter returns candidates, not ranked/approved media. It is not in the
`media_adapters` registry: `past_posts_composition` builds one per capture, for
the past-posts source's sync, preparation and preview ports, and only when
`past_posts_activation` arms the process. The account/token resolver must derive
ownership from the tenant's source row, never from editable config. No network
call here runs in a database session.

Only time watermarks and IDs cross a page boundary. Graph paging cursors are
consumed in memory when an overlapping timestamp repeats a page, never stored.
The newest 10,000 posts are the documented listing ceiling, not a complete
inventory: absence from this scan cannot certify that an old post was deleted.

The backward walk never goes below Instagram's launch (`LAUNCH_FLOOR`), and a
window that lands nothing is not followed by its empty neighbours: one probe
asks for the newest post below it and the walk resumes there, or ends. The
probe reads a time, never a post, so it changes no coverage claim.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import (
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TYPE_CHECKING,
)
from urllib.parse import parse_qs, urlsplit

import httpx

from src.services.target import egress, vocabulary
from src.services.target.media_adapters import (
    MediaCredentialDead,
    MediaRetryableError,
    MediaTerminalError,
    MediaPage,
    MediaTurnCounters,
    MediaSourceFenceDrop,
    MediaTurnBudgetExhausted,
    MediaSelectedAssetChanged,
    MediaSelectedAssetGone,
    MediaGraphResponseTooLarge,
    MEDIA_FAILURES,
)

if TYPE_CHECKING:
    from src.services.target.instagram_catalog import PastPostsObservation

PROVIDER = "instagram_past_posts"
LISTING_CEILING = 10_000
PAGE_SIZE = 100
MAX_PAGE_REQUESTS = 20
MAX_OBSERVED_PARENTS = 100
CHECKPOINT_VERSION = 2
ATTRIBUTION_DETECTOR_VERSION = 1
MAX_CAPTION_CHARACTERS = 8192
CAROUSEL_SLIDE_LIMIT = 20
# Stay comfortably below Meta's recommended six-month query interval. A window
# that lands nothing is skipped by one probe (`_skip_gap`), and the walk ends at
# the launch floor, so an old or sparse account is bounded by its posts.
WINDOW_SECONDS = 90 * 24 * 60 * 60
#: Instagram launched on 2010-10-06 (UTC): no post is older, so no window ends
#: below it. A checkpoint whose window already starts under the floor finishes
#: that window and ends rather than walk lower.
LAUNCH_FLOOR = int(datetime(2010, 10, 6, tzinfo=timezone.utc).timestamp())
_ID = re.compile(r"[0-9]{1,64}\Z")
_CREDIT = re.compile(r"\b(?:via|credits?|cr)\s*[:\-]?\s*@([A-Za-z0-9_.]{1,30})", re.I)
_FIELDS = "id,media_type,media_url,timestamp,permalink,like_count,comments_count"
_FETCH_FIELDS = "id,owner,media_type,media_url"
_EXPIRED_LINK = frozenset({401, 403, 404, 410})
# One explicit preview is its own bounded read, never the selected-post
# fetch: at most two Graph lookups and one CDN download of at most 2 MiB, none
# retried. A video's preview is its Graph `thumbnail_url` image, never the video.
PREVIEW_GRAPH_REQUESTS = 2
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
PREVIEW_BUDGET_S = 8.0
_PREVIEW_FIELDS = _FETCH_FIELDS + ",thumbnail_url"
_PREVIEW_CHILD_FIELDS = "id,media_type,media_url,thumbnail_url"


@dataclass(frozen=True)
class PastPostsAccess:
    """One source's account and grant, resolved together under its tenant."""

    account_ref: str
    token: str = field(repr=False)
    captured_authorization: object = field(default=None, repr=False)
    #: The app-scoped id the source's current authorization verified for this
    #: account (`meta_identity_mappings`), when one is recorded. Read once, with
    #: the credential, when the turn is set up; None is "not recorded", never
    #: "none exists", and ownership then falls back to the same-token `/me` proof.
    app_scoped_id: Optional[str] = field(default=None, repr=False)


AccessProvider = Callable[..., Awaitable[PastPostsAccess]]


class AdmissionProvider(Protocol):
    async def __call__(
        self, source_id: str, *, workspace_id: str, access: PastPostsAccess
    ) -> egress.RequestAdmission: ...


class _TurnExhausted(MediaTurnBudgetExhausted):
    """No further dispatch or parent work fits this turn."""

    def __init__(self, turn):
        super().__init__(
            turn.counters(),
            reason="parent_replay_budget"
            if turn.listed_parents >= turn.parent_limit
            else "graph_attempt_budget",
        )


@dataclass
class _Turn:
    access: PastPostsAccess
    admission: egress.RequestAdmission
    graph_limit: int
    parent_limit: int
    graph_attempts: int = 0
    listed_parents: int = 0
    observed_parents: int = 0
    cdn_attempts: int = 0
    source_fence_drop: int = 0
    #: This turn's one same-token `/me` read, kept for the turn only: the proof
    #: of an app-scoped owner when no verified id is recorded. Never persisted.
    profile: Optional[dict] = None
    profile_read: bool = False

    @property
    def remaining(self):
        return self.graph_limit - self.graph_attempts

    def counters(self):
        return MediaTurnCounters(
            self.graph_attempts,
            self.observed_parents,
            listed_parents=self.listed_parents,
            cdn_attempts=self.cdn_attempts,
            source_fence_drop=self.source_fence_drop,
        )

    def guard(self, *, graph=True):
        return _DispatchAdmission(self, graph)


class _DispatchAdmission:
    def __init__(self, turn, graph):
        self.turn, self.graph = turn, graph

    async def before_dispatch(self):
        if self.graph and not self.turn.remaining:
            raise _TurnExhausted(self.turn)
        try:
            ticket = await self.turn.admission.before_dispatch()
        except MediaSourceFenceDrop as exc:
            raise self._fence_drop(exc) from None
        if self.graph:
            self.turn.graph_attempts += 1
        else:
            self.turn.cdn_attempts += 1
        return ticket

    async def after_return(self, ticket):
        try:
            await self.turn.admission.after_return(ticket)
        except MediaSourceFenceDrop as exc:
            raise self._fence_drop(exc) from None

    def _fence_drop(self, exc):
        self.turn.source_fence_drop = 1
        return MediaSourceFenceDrop(self.turn.counters(), reason=exc.reason)


class _CaptionUnsupported(MediaTerminalError):
    pass


def identity(media_id: str) -> str:
    """Identity dedup within a workspace, not byte dedup against other files.

    Drive uses unprefixed MD5 values. This namespace cannot collide with them.
    Each carousel child uses its own media id as its item identity.
    """
    return f"{PROVIDER}:media:{_media_id(media_id)}"


def _media_id(value) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MediaTerminalError("Instagram media/account ID is invalid")
    return value


#: Graph writes UTC offsets without a colon (`2022-12-31T12:00:00+0000`).
#: Python 3.10's `fromisoformat` accepts only `+00:00` (3.11 takes both), and
#: 3.10 is this repository's floor and CI's interpreter.
_COMPACT_OFFSET = re.compile(r"([+-][0-9]{2})([0-9]{2})$")


def _timestamp(value) -> int:
    try:
        text = _COMPACT_OFFSET.sub(r"\1:\2", value.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            raise ValueError("timestamp has no zone")
        return int(dt.timestamp())
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise MediaRetryableError(
            "Instagram listing has an invalid timestamp"
        ) from None


def _count(value) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None


def _media_type(value) -> str:
    """A requested type must be present before exclusion can be asserted."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        raise MediaRetryableError(
            "Instagram media response has incomplete type evidence"
        )
    return value


def _carrier_fields_valid(cp) -> bool:
    """Validate the generic sync envelope without storing it as provider state."""
    if ("walk" in cp) != ("started_at" in cp):
        return False
    if "last_whole_started_at" in cp and "walk" not in cp:
        return False
    if "walk" in cp and (
        not isinstance(cp["walk"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", cp["walk"])
    ):
        return False
    for name in ("started_at", "last_whole_started_at"):
        if name not in cp:
            continue
        value = cp[name]
        if not isinstance(value, str) or len(value) > 128:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                return False
        except (ValueError, OverflowError):
            return False
    return True


def attribution(caption) -> tuple[str, list[str]]:
    """Only derive positive matches; lack of a caption is unknown.

    'not_detected' is deliberately not a claim of ownership. Meta may
    omit captions or strip mention markers, and a regex cannot establish rights.
    The caller must not persist the caption itself.
    """
    if (
        not isinstance(caption, str)
        or not caption.strip()
        or len(caption) > MAX_CAPTION_CHARACTERS
        or not any(c.isprintable() and not c.isspace() for c in caption)
    ):
        return "unknown", []
    handles = sorted({m.lower() for m in _CREDIT.findall(caption)})
    return ("credited" if handles else "not_detected"), handles


class InstagramPastPostsAdapter:
    def __init__(
        self,
        *,
        access_provider: AccessProvider,
        client: httpx.AsyncClient,
        graph_version: str,
        policy: Optional[egress.EgressPolicy] = None,
        page_size: int = PAGE_SIZE,
        clock: Callable[[], float] = time.time,
        admission_provider: Optional[AdmissionProvider] = None,
        graph_limit: Optional[int] = None,
        parent_limit: int = MAX_OBSERVED_PARENTS,
        enrich_captions: bool = False,
        metadata_only: bool = False,
    ):
        # The composition root must pass the application's shared Graph version.
        # There is intentionally no past-posts-only version default.
        if not re.fullmatch(r"v[0-9]+\.0", graph_version):
            raise ValueError("invalid shared Graph version")
        if not 1 <= page_size <= PAGE_SIZE:
            raise ValueError("page_size must be between 1 and 100")
        self._access_provider = access_provider
        self._client = client
        self._require_plain_client()
        if admission_provider is None:
            raise MediaTerminalError("past-posts request admission is required")
        graph_limit = MAX_PAGE_REQUESTS if graph_limit is None else graph_limit
        if type(graph_limit) is not int or not 1 <= graph_limit <= MAX_PAGE_REQUESTS:
            raise ValueError("graph_limit must tighten the 20-attempt ceiling")
        if (
            type(parent_limit) is not int
            or not 1 <= parent_limit <= MAX_OBSERVED_PARENTS
        ):
            raise ValueError("parent_limit must tighten the 100-parent ceiling")
        if type(enrich_captions) is not bool:
            raise ValueError("enrich_captions must be boolean")
        if type(metadata_only) is not bool:
            raise ValueError("metadata_only must be boolean")
        self._metadata_only = metadata_only
        self._admission_provider = admission_provider
        self._graph_limit, self._parent_limit = graph_limit, parent_limit
        self._enrich_captions = enrich_captions
        self._base = f"https://graph.instagram.com/{graph_version}"
        self._policy = replace(policy or egress.EgressPolicy(), allow_redirects=False)
        self._page_size = page_size
        self._clock = clock

    def initial_checkpoint(self) -> dict:
        return {"v": CHECKPOINT_VERSION}

    def normalize_checkpoint(self, checkpoint) -> dict:
        """Normalize one scan lane before dispatch or durable progress writes."""
        return self._checkpoint(checkpoint)

    def checkpoint_incomplete(self, checkpoint) -> bool:
        return bool(checkpoint and checkpoint.get("more"))

    def checkpoint_whole(self, checkpoint) -> bool:
        # A 10K-limited/windowed listing never proves whole-account absence.
        return False

    def _require_plain_client(self) -> None:
        # httpx merges client defaults into each request. Omitting an explicit
        # header at the CDN is insufficient when the injected client carries
        # auth, cookies or token query parameters. Never mutate a shared client.
        if (
            self._client.auth is not None
            or self._client.params
            or self._client.cookies
            or any(
                name in self._client.headers
                for name in ("authorization", "proxy-authorization", "cookie")
            )
        ):
            raise MediaTerminalError(
                "past-posts source requires a client without credentials or default parameters"
            )

    async def _access(self, source_id: str, workspace_id: str) -> PastPostsAccess:
        if not workspace_id or not source_id:
            raise MediaTerminalError("past-posts source and workspace are required")
        access = await self._access_provider(source_id, workspace_id=workspace_id)
        _media_id(access.account_ref)
        return access

    async def _turn(
        self, source_id: str, workspace_id: str, *, graph_limit: Optional[int] = None
    ) -> _Turn:
        access = await self._access(source_id, workspace_id)
        admission = await self._admission_provider(
            source_id, workspace_id=workspace_id, access=access
        )
        if not callable(getattr(admission, "before_dispatch", None)) or not callable(
            getattr(admission, "after_return", None)
        ):
            raise MediaTerminalError("past-posts request admission is required")
        limit = (
            self._graph_limit
            if graph_limit is None
            else min(graph_limit, self._graph_limit)
        )
        return _Turn(access, admission, limit, self._parent_limit)

    async def _graph(
        self,
        path: str,
        turn: _Turn,
        params: dict,
        *,
        caption=False,
        policy: Optional[egress.EgressPolicy] = None,
    ) -> dict:
        self._require_plain_client()
        try:
            response = await egress.request(
                self._client,
                "GET",
                f"{self._base}/{path}",
                params=params,
                headers={"Authorization": f"Bearer {turn.access.token}"},
                policy=policy or self._policy,
                admission=turn.guard(),
            )
        except egress.EgressBudgetExhausted:
            raise MediaRetryableError("Instagram read budget exhausted") from None
        except egress.ResponseTooLarge:
            raise MediaGraphResponseTooLarge() from None
        try:
            body = response.json()
        except ValueError:
            raise MediaRetryableError(
                "Instagram returned an invalid response"
            ) from None
        if not isinstance(body, dict):
            raise MediaRetryableError("Instagram returned an invalid response")
        error = body.get("error") or {}
        if error or response.is_error:
            code = error.get("code") if isinstance(error, dict) else None
            if code in (190, 102) or response.status_code == 401:
                raise MediaCredentialDead("Reconnect this Instagram account")
            if (
                response.status_code >= 500
                or response.status_code == 429
                or code in (4, 17, 32, 613, 80002)
                or (isinstance(error, dict) and error.get("is_transient") is True)
            ):
                raise MediaRetryableError("Instagram temporarily refused the read")
            if (
                caption
                and code == 100
                and isinstance(error, dict)
                and isinstance(error.get("message"), str)
                and re.search(
                    r"nonexisting field\s*\(caption\)", error["message"], re.I
                )
            ):
                raise _CaptionUnsupported("caption_field_unsupported")
            raise MediaTerminalError("Instagram media is unavailable to this account")
        return body

    def _checkpoint(self, checkpoint):
        if checkpoint is not None and not isinstance(checkpoint, Mapping):
            raise MediaTerminalError("invalid past-posts watermark")
        cp = dict(checkpoint or {})
        version = cp.get("v", 1)
        if type(version) is not int or version not in (1, CHECKPOINT_VERSION):
            raise MediaTerminalError("unsupported past-posts checkpoint version")
        durable = {
            "upper",
            "until",
            "window_start",
            "seen",
            "boundary",
            "more",
            "ceiling_reached",
        }
        reserved = {"walk", "started_at", "last_whole_started_at"}
        # Empty/bare checkpoints (possibly wrapped in a valid new carrier)
        # initialize a scan. Once v2 has ANY provider progress it must carry
        # the full watermark; missing frozen bounds can never be reminted.
        # v1/unversioned predecessor checkpoints retain their explicit legacy
        # defaulting path, and every output upgrades to the complete v2 shape.
        if not _carrier_fields_valid(cp) or (
            version == CHECKPOINT_VERSION
            and set(cp) - {"v"} - reserved
            and not durable <= cp.keys()
        ):
            raise MediaTerminalError("invalid past-posts watermark")
        upper = cp.get("upper", int(self._clock()) + 1)
        until = cp.get("until", upper)
        seen = cp.get("seen", 0)
        boundary = cp.get("boundary", [])
        start = (
            cp.get("window_start", max(0, until - WINDOW_SECONDS))
            if type(until) is int
            else None
        )
        allowed = durable | reserved | {"v", "pending_parent", "pending_at"}
        if (
            set(cp) - allowed
            or type(upper) is not int
            or type(until) is not int
            or type(seen) is not int
            or not 0 <= seen <= LISTING_CEILING
            or not 0 < until <= upper
            or type(start) is not int
            or not 0 <= start < until
            or until - start > WINDOW_SECONDS
            or not isinstance(boundary, list)
            or len(boundary) > LISTING_CEILING
            or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in boundary)
            or len(set(boundary)) != len(boundary)
            or len(boundary) > seen
            or (
                "ceiling_reached" in cp
                and cp["ceiling_reached"] != (seen == LISTING_CEILING)
            )
            or (seen == LISTING_CEILING and cp.get("more", False))
            or ("more" in cp and type(cp["more"]) is not bool)
            or ("ceiling_reached" in cp and type(cp["ceiling_reached"]) is not bool)
            or (("pending_parent" in cp) != ("pending_at" in cp))
            or (
                "pending_parent" in cp
                and (
                    not isinstance(cp["pending_parent"], str)
                    or not _ID.fullmatch(cp["pending_parent"])
                    or type(cp["pending_at"]) is not int
                    or not start <= cp["pending_at"] < until
                    or cp["pending_parent"] in boundary
                    or cp.get("more") is not True
                    or seen == LISTING_CEILING
                )
            )
        ):
            raise MediaTerminalError("invalid past-posts watermark")
        return {
            "v": CHECKPOINT_VERSION,
            "upper": upper,
            "until": until,
            "window_start": start,
            "boundary": sorted(set(boundary)),
            "seen": seen,
            "more": cp.get("more", True),
            "ceiling_reached": seen == LISTING_CEILING,
            **(
                {"pending_parent": cp["pending_parent"], "pending_at": cp["pending_at"]}
                if "pending_parent" in cp
                else {}
            ),
        }

    @staticmethod
    def _advance(cp, rows):
        result = {
            k: v for k, v in cp.items() if k not in ("pending_parent", "pending_at")
        }
        if rows:
            oldest = rows[-1][0]
            ids = {row["id"] for ts, row in rows if ts == oldest}
            if oldest + 1 == cp["until"]:
                ids.update(cp["boundary"])
            result.update(
                until=oldest + 1, boundary=sorted(ids), seen=cp["seen"] + len(rows)
            )
        result["ceiling_reached"] = result["seen"] == LISTING_CEILING
        result["more"] = not result["ceiling_reached"]
        return result

    async def _parent_page(self, turn, cp, novel, *, more):
        observations, candidates, completed, diagnostics = [], [], [], []
        pending = None
        for ts, row in novel:
            helpers = (
                0
                if self._metadata_only
                else int(row.get("media_type") == "CAROUSEL_ALBUM")
                + int(self._enrich_captions)
            )
            if turn.remaining < helpers:
                pending = (row["id"], ts)
                break
            try:
                observation, diagnostic = await self._observe(
                    row, ts, turn, metadata_only=self._metadata_only
                )
            except MediaSourceFenceDrop:
                raise
            except MediaRetryableError as exc:
                # Complete earlier parents survive a bounded helper retry.
                # The interrupted parent is re-fetched by ID next turn; never
                # downgrade grant death or a stale authorization to unknown.
                pending = (row["id"], ts)
                if not isinstance(exc, MediaTurnBudgetExhausted):
                    diagnostics.append("parent_read_retry_pending")
                break
            observations.append(observation)
            turn.observed_parents += 1
            completed.append((ts, row))
            if diagnostic is not None:
                diagnostics.append(diagnostic)
            candidates.extend(
                self._candidate(observation, asset)
                for asset in observation.assets
                if asset.availability == "available"
            )
        result_cp = self._advance(cp, completed)
        if pending:
            result_cp.update(
                pending_parent=pending[0], pending_at=pending[1], more=True
            )
        elif not more and not result_cp["ceiling_reached"]:
            result_cp = self._next_window(result_cp)
        return MediaPage(
            candidates,
            result_cp,
            tuple(observations),
            turn.counters(),
            tuple(dict.fromkeys(diagnostics)),
            "stable_parent_pending"
            if pending
            else "listing_ceiling"
            if result_cp["ceiling_reached"]
            else "metadata_page"
            if self._metadata_only
            else "page",
        )

    async def enrich_parents(
        self, parent_media_ids: Sequence[str], *, source_id: str, workspace_id: str
    ) -> MediaPage[PastPostsObservation]:
        """Read queued stable parents, as many as the turn's budget affords.

        One turn, one access read and one request budget for the whole batch,
        in the order given; never advances a listing checkpoint. Caption is
        optional only on these targeted requests. A recognized field refusal
        permits a counted base read; grant death and other refusals do not. The
        caller lands the entire bundle before capturing validation.

        Before a request the turn cannot afford, the batch yields: the parent it
        was reading, and every later one, stay queued, and what completed is
        returned. A parent the provider refused for good is `failed` and the
        batch goes on, so one bad parent cannot starve the rest. A parent whose
        read is retryable is `deferred`, and the batch ends there, so a provider
        that is throttling or failing is not asked again this turn. A first
        parent that cannot fit a whole budget is deferred too, or it would be
        read first in every turn.
        """
        ids = [_media_id(mid) for mid in parent_media_ids]
        turn = await self._turn(source_id, workspace_id)
        observations, candidates = [], []
        diagnostics, deferred, failed = [], [], []
        try:
            for index, mid in enumerate(ids):
                if not turn.remaining or turn.listed_parents >= turn.parent_limit:
                    break
                try:
                    observation, diagnostic = await self._enrich(mid, turn)
                except MediaSourceFenceDrop:
                    raise
                except MediaTurnBudgetExhausted:
                    if not index:
                        deferred.append(mid)
                    break
                except MediaRetryableError:
                    deferred.append(mid)
                    diagnostics.append("parent_read_retry_pending")
                    break
                except MediaTerminalError:
                    failed.append(mid)
                    continue
                observations.append(observation)
                candidates.extend(
                    self._candidate(observation, asset)
                    for asset in observation.assets
                    if asset.availability == "available"
                )
                if diagnostic:
                    diagnostics.append(diagnostic)
        except MediaSourceFenceDrop:
            raise
        except MEDIA_FAILURES as exc:
            exc.counters = turn.counters()
            raise
        return MediaPage(
            candidates,
            {},
            tuple(observations),
            turn.counters(),
            tuple(dict.fromkeys(diagnostics)),
            "parent_enriched" if observations else "parent_enrichment_pending",
            deferred=tuple(deferred),
            failed=tuple(failed),
        )

    async def _enrich(self, mid: str, turn: _Turn):
        """One stable parent's complete bundle, or the typed reason it is not."""
        try:
            parent = await self._graph(
                mid, turn, {"fields": _FIELDS + ",owner,caption"}, caption=True
            )
            state, _ = attribution(parent.get("caption"))
            diagnostic = "caption_unavailable" if state == "unknown" else None
        except _CaptionUnsupported:
            parent = await self._graph(mid, turn, {"fields": _FIELDS + ",owner"})
            state, diagnostic = "unknown", "caption_field_unsupported"
        turn.listed_parents += 1
        if parent.get("id") != mid:
            raise MediaRetryableError("Instagram parent identity changed")
        ts = _timestamp(parent.get("timestamp"))
        if not await self._owned_by_account(parent.get("owner"), turn):
            raise MediaTerminalError("Instagram media belongs to another account")
        observation, diagnostic = await self._observe(
            parent, ts, turn, enrichment=(state, diagnostic)
        )
        turn.observed_parents += 1
        return observation, diagnostic

    async def list_changes(
        self, config: Mapping, checkpoint, *, source_id: str, workspace_id: str
    ) -> MediaPage[PastPostsObservation]:
        cp = self._checkpoint(checkpoint)
        if cp["seen"] == LISTING_CEILING:
            return MediaPage([], {**cp, "more": False}, outcome="listing_ceiling")
        turn = await self._turn(source_id, workspace_id)
        try:
            if "pending_parent" in cp:
                # The stable parent is re-read; no partially parsed bundle, URL or
                # opaque provider cursor crosses a checkpoint boundary.
                row = await self._graph(
                    cp["pending_parent"], turn, {"fields": _FIELDS + ",owner"}
                )
                if (
                    row.get("id") != cp["pending_parent"]
                    or _timestamp(row.get("timestamp")) != cp["pending_at"]
                ):
                    raise MediaRetryableError("Instagram continuation identity changed")
                if not await self._owned_by_account(row.get("owner"), turn):
                    raise MediaTerminalError(
                        "Instagram media ownership could not be established"
                    )
                turn.listed_parents += 1
                return await self._parent_page(
                    turn, cp, [(cp["pending_at"], row)], more=True
                )
            after = None
            cursors_seen = set()
            prior_ids = set(cp["boundary"])
            while turn.remaining and turn.listed_parents < turn.parent_limit:
                limit = min(self._page_size, turn.parent_limit - turn.listed_parents)
                params = {
                    "fields": _FIELDS,
                    "since": max(0, cp["window_start"] - 1),
                    "until": cp["until"],
                    "limit": limit,
                }
                if after is not None:
                    params["after"] = after
                body = await self._graph(
                    f"{turn.access.account_ref}/media", turn, params
                )
                rows = body.get("data")
                if not isinstance(rows, list) or len(rows) > limit:
                    raise MediaRetryableError(
                        "Instagram listing has no bounded data array"
                    )
                paging = body.get("paging") or {}
                if not isinstance(paging, dict):
                    raise MediaRetryableError("Instagram listing has invalid paging")
                more = bool(paging.get("next"))
                novel, page_ids = [], set()
                previous_ts = cp["until"]
                for row in rows:
                    if not isinstance(row, dict):
                        raise MediaRetryableError(
                            "Instagram listing has an invalid item"
                        )
                    mid = _media_id(row.get("id"))
                    ts = _timestamp(row.get("timestamp"))
                    if (
                        ts > cp["until"]
                        or ts < max(0, cp["window_start"] - 1)
                        or ts > previous_ts
                    ):
                        raise MediaRetryableError(
                            "Instagram listing did not honor its time boundary"
                        )
                    previous_ts = ts
                    if (
                        cp["window_start"] <= ts < cp["until"]
                        and mid not in prior_ids
                        and mid not in page_ids
                    ):
                        novel.append((ts, row))
                    page_ids.add(mid)
                turn.listed_parents += len(rows)
                if novel:
                    remaining = LISTING_CEILING - cp["seen"]
                    return await self._parent_page(
                        turn, cp, novel[:remaining], more=more or len(novel) > remaining
                    )
                if not more:
                    return MediaPage(
                        [], await self._skip_gap(turn, cp), counters=turn.counters()
                    )
                cursors = paging.get("cursors") or {}
                after = cursors.get("after") if isinstance(cursors, dict) else None
                if not after and not rows:
                    next_until = self._next_time(paging.get("next"), turn.access)
                    if (
                        next_until is not None
                        and cp["window_start"] < next_until + 1 < cp["until"]
                    ):
                        cp.update(until=next_until + 1, boundary=[])
                        prior_ids = set()
                        continue
                if (
                    not isinstance(after, str)
                    or not after
                    or len(after) > 4096
                    or after in cursors_seen
                ):
                    raise MediaRetryableError("Instagram pagination made no progress")
                cursors_seen.add(after)
            # Oversized timestamp groups cannot honestly be replayed arbitrarily
            # within these ceilings. No checkpoint advancement/completion is claimed.
            raise _TurnExhausted(turn)
        except MEDIA_FAILURES as exc:
            # Attempted/observed work is transient evidence, never a committed
            # checkpoint. Preserve the original typed exception and routing.
            exc.counters = turn.counters()
            raise

    @staticmethod
    def _next_window(checkpoint: dict) -> dict:
        """The window just below this one: never below the launch floor."""
        start = checkpoint["window_start"]
        if start <= LAUNCH_FLOOR:
            return {**checkpoint, "more": False}
        return {
            **checkpoint,
            "until": start,
            "window_start": max(LAUNCH_FLOOR, start - WINDOW_SECONDS),
            "boundary": [],
            "more": True,
        }

    async def _skip_gap(self, turn: _Turn, cp: dict) -> dict:
        """Where the walk resumes after a window that landed no parent.

        The adjacent window is the default. One probe, a one-row listing of the
        newest post below this window (`until` its start, no `since`), jumps
        the gap: the next window ends just after that post, with a fresh
        boundary, inside the frozen `upper`; no post means history is done. The
        probe is one more request under the same admission, counted against
        this turn's request and parent budgets, and it is only made when both
        still hold: a turn that cannot afford it takes the adjacent window, the
        default. A refused or failed probe raises with nothing advanced; the
        next attempt lists this window again. It reads a time, never a post,
        so skipping a gap claims nothing about the posts it skips.
        """
        adjacent = self._next_window(cp)
        if (
            not adjacent["more"]
            or not turn.remaining
            or turn.listed_parents >= turn.parent_limit
        ):
            return adjacent
        start = cp["window_start"]
        body = await self._graph(
            f"{turn.access.account_ref}/media",
            turn,
            {"fields": "id,timestamp", "until": start, "limit": 1},
        )
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) > 1:
            raise MediaRetryableError("Instagram listing has no bounded data array")
        if not rows:
            return {**cp, "more": False}
        if not isinstance(rows[0], dict):
            raise MediaRetryableError("Instagram listing has an invalid item")
        _media_id(rows[0].get("id"))
        ts = _timestamp(rows[0].get("timestamp"))
        turn.listed_parents += 1
        if ts > start:
            raise MediaRetryableError(
                "Instagram listing did not honor its time boundary"
            )
        if ts == start:
            # An inclusive bound answered with this window's own edge; one row
            # cannot see past it, so take the adjacent window.
            return adjacent
        if ts < LAUNCH_FLOOR:
            return {**cp, "more": False}
        until = ts + 1
        return {
            **cp,
            "until": until,
            "window_start": max(LAUNCH_FLOOR, until - WINDOW_SECONDS),
            "boundary": [],
            "more": True,
        }

    def _next_time(self, value, access: PastPostsAccess) -> Optional[int]:
        if not isinstance(value, str):
            return None
        try:
            parsed = urlsplit(value)
            expected = urlsplit(f"{self._base}/{access.account_ref}/media")
            if (
                parsed.scheme != "https"
                or parsed.netloc != expected.netloc
                or parsed.path != expected.path
            ):
                return None
            values = parse_qs(parsed.query).get("until", [])
            return int(values[0]) if len(values) == 1 and values[0].isdigit() else None
        except ValueError:
            return None

    async def _children(
        self,
        row: dict,
        turn: _Turn,
        *,
        fields: str = "id,media_type,media_url",
        policy: Optional[egress.EgressPolicy] = None,
    ) -> tuple[tuple[dict, ...], bool]:
        if _media_type(row.get("media_type")) != "CAROUSEL_ALBUM":
            return (row,), True
        children = await self._graph(
            f"{_media_id(row.get('id'))}/children",
            turn,
            {"fields": fields, "limit": CAROUSEL_SLIDE_LIMIT},
            policy=policy,
        )
        data = children.get("data")
        if not isinstance(data, list) or len(data) > CAROUSEL_SLIDE_LIMIT:
            raise MediaRetryableError("Instagram children response is incomplete")
        for child in data:
            if not isinstance(child, dict):
                raise MediaRetryableError("Instagram child response is incomplete")
            try:
                _media_id(child.get("id"))
            except MediaTerminalError as exc:
                raise MediaRetryableError(
                    "Instagram child identity is incomplete"
                ) from exc
            _media_type(child.get("media_type"))
        paging = children.get("paging") or {}
        if not isinstance(paging, dict):
            raise MediaRetryableError("Instagram children paging is invalid")
        return tuple(data), not bool(paging.get("next"))

    async def _observe(
        self,
        parent: dict,
        ts: int,
        turn: _Turn,
        *,
        metadata_only: bool = False,
        enrichment: Optional[tuple[str, Optional[str]]] = None,
    ):
        # Local import breaks catalog's existing identity import cycle. The
        # retained typed bundle itself has no caption/handle/URL/cursor fields.
        from src.services.target.instagram_catalog import (
            PastPostsAsset,
            PastPostsObservation,
        )

        deferred_carousel = (
            metadata_only and _media_type(parent.get("media_type")) == "CAROUSEL_ALBUM"
        )
        parts, complete = (
            ((), False) if deferred_carousel else await self._children(parent, turn)
        )
        assets = []
        for position, part in enumerate(parts, 1):
            kind = {"IMAGE": "image", "VIDEO": "video"}.get(part.get("media_type"))
            url = part.get("media_url")
            available = bool(kind and isinstance(url, str) and url)
            assets.append(
                PastPostsAsset(
                    part["id"],
                    kind or "unknown",
                    "image/jpeg"
                    if kind == "image"
                    else "video/mp4"
                    if kind == "video"
                    else None,
                    position=position,
                    availability="available"
                    if available
                    else "unavailable"
                    if kind
                    else "excluded",
                    exclusion_reason=None
                    if available
                    else "media_unavailable"
                    if kind
                    else "unsupported_media",
                )
            )
        state, diagnostic = enrichment or ("unknown", None)
        if enrichment is None and self._enrich_captions and not metadata_only:
            try:
                enriched = await self._graph(
                    parent["id"], turn, {"fields": "id,caption"}, caption=True
                )
                if enriched.get("id") != parent["id"]:
                    raise MediaRetryableError("Instagram caption identity changed")
                state, _ = attribution(enriched.get("caption"))
                if state == "unknown":
                    diagnostic = "caption_unavailable"
            except _CaptionUnsupported:
                diagnostic = "caption_field_unsupported"
        permalink = parent.get("permalink")
        if not isinstance(permalink, str) or not re.fullmatch(
            r"https://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+/?",
            permalink,
        ):
            permalink = None
        availability = (
            "available"
            if any(a.availability == "available" for a in assets)
            else "unavailable"
        )
        reason = None if availability == "available" else "media_unavailable"
        if assets and all(a.availability == "excluded" for a in assets):
            availability, reason = "excluded", "unsupported_media"
        if deferred_carousel:
            availability, reason = "available", "incomplete_observation"
        return PastPostsObservation(
            parent["id"],
            datetime.fromtimestamp(ts, timezone.utc),
            tuple(assets),
            len(parts) if parent.get("media_type") == "CAROUSEL_ALBUM" else None,
            complete,
            permalink,
            _count(parent.get("like_count")),
            _count(parent.get("comments_count")),
            state,
            ATTRIBUTION_DETECTOR_VERSION,
            availability,
            reason,
        ), diagnostic

    @staticmethod
    def _candidate(observation, asset) -> dict:
        mid, kind = asset.selected_asset_id, asset.media_kind
        return {
            "ref": mid,
            "content_hash": identity(mid),
            "kind": kind,
            # The name an item is stored under (`instagram_catalog._project`):
            # never the Graph id, which the candidate's `ref` already carries.
            "name": vocabulary.PAST_POSTS_ITEM_NAME,
            "mime_type": asset.mime_type,
            "past_posts": {
                "media_id": observation.parent_media_id,
                "selected_asset_id": mid,
                "position": asset.position,
                "original_at": observation.original_at.isoformat(),
                "permalink": observation.permalink,
                "like_count": observation.like_count,
                "comments_count": observation.comments_count,
                "attribution": observation.attribution,
                "attribution_detector_version": observation.attribution_detector_version,
            },
        }

    @staticmethod
    def _selected_part(parts, complete, selected_asset_id):
        if selected_asset_id is None:
            return parts[0] if parts else {}
        for part in parts:
            if part.get("id") == selected_asset_id:
                return part
        if complete:
            raise MediaSelectedAssetGone()
        raise MediaSelectedAssetChanged()

    async def fetch_bytes(
        self,
        config: Mapping,
        file_ref: str,
        *,
        source_id: str,
        workspace_id: str,
        max_bytes: int,
        selected_asset_id: Optional[str] = None,
        on_counters: Optional[Callable[[MediaTurnCounters], None]] = None,
    ) -> tuple[bytes, str, Optional[str]]:
        """Fetch current bytes; bound validation/effect callers supply selection.

        Validation, upload and card consumers must supply their
        captured selected_asset_id. Freshly changed carousel bytes cannot
        inherit the previous child's evidence. No consumer is wired here. A
        preview is :meth:`fetch_preview`, never this full fetch.
        on_counters is a synchronous local observer (no I/O), called once even
        on failure/cancellation. Observer exceptions cannot alter read outcomes.
        """
        turn = None
        try:
            mid = _media_id(file_ref)
            if selected_asset_id is not None:
                _media_id(selected_asset_id)
            if type(max_bytes) is not int or max_bytes <= 0:
                raise MediaTerminalError("media byte cap must be positive")
            turn = await self._turn(source_id, workspace_id)
            for attempt in range(2):
                parent = await self._graph(mid, turn, {"fields": _FETCH_FIELDS})
                owner = parent.get("owner")
                if not await self._owned_by_account(owner, turn):
                    raise MediaTerminalError(
                        "Instagram media ownership could not be established"
                    )
                if parent.get("id") != mid:
                    raise MediaTerminalError(
                        "Instagram returned a different media item"
                    )
                parts, complete = await self._children(parent, turn)
                part = self._selected_part(parts, complete, selected_asset_id)
                kind = {"IMAGE": "image", "VIDEO": "video"}.get(part.get("media_type"))
                url = part.get("media_url")
                if kind is None or not isinstance(url, str) or not url:
                    raise MediaTerminalError(
                        "Instagram does not provide this media file"
                    )
                policy = self._download_policy(url, max_bytes)
                try:
                    # Never forward the Instagram token to the CDN.
                    self._require_plain_client()
                    response = await egress.request(
                        self._client,
                        "GET",
                        url,
                        policy=policy,
                        admission=turn.guard(graph=False),
                    )
                except egress.ResponseTooLarge:
                    raise MediaTerminalError(
                        "Instagram media exceeds the byte cap"
                    ) from None
                except egress.EgressBudgetExhausted:
                    raise MediaRetryableError(
                        "Instagram download budget exhausted"
                    ) from None
                if response.status_code in _EXPIRED_LINK and attempt == 0:
                    continue
                if response.status_code in _EXPIRED_LINK:
                    raise MediaTerminalError("Instagram media file is unavailable")
                if response.is_error:
                    raise MediaRetryableError("Instagram media download failed")
                mime = response.headers.get("content-type", "").split(";")[0].lower()
                expected = {
                    "image": {"image/jpeg"},
                    "video": {"video/mp4", "video/quicktime"},
                }
                if mime not in expected[kind] or not response.content:
                    raise MediaTerminalError(
                        "Instagram returned unsupported media bytes"
                    )
                # This is a byte fetch, not video eligibility. Ingest activation
                # must validate duration/codec before admitting video candidates.
                return (
                    response.content,
                    f"Instagram {mid}.{'jpg' if kind == 'image' else 'mp4'}",
                    mime,
                )
            raise AssertionError("unreachable")
        finally:
            if on_counters is not None:
                snapshot = turn.counters() if turn is not None else MediaTurnCounters()
                try:
                    on_counters(snapshot)
                except BaseException:
                    # A diagnostic observer must not mask an original provider,
                    # fence or cancellation exception, or replace a successful read.
                    pass

    async def fetch_preview(
        self,
        file_ref: str,
        *,
        source_id: str,
        workspace_id: str,
        selected_asset_id: Optional[str] = None,
    ) -> bytes:
        """One explicit preview's bytes: a JPEG the caller still decodes.

        Its own budget, never the selected-post fetch's: at most
        PREVIEW_GRAPH_REQUESTS lookups and one CDN download of at most
        PREVIEW_MAX_BYTES, each admitted like every past-posts read and none
        retried — an expired link is the page's placeholder, not a re-read.
        An image's preview is its own media_url; a video's is the Graph
        thumbnail_url image, so the video file is never requested. A carousel
        without a verified owner mapping cannot spend a third Graph request
        on both the profile and its children, so it fails closed.
        """
        mid = _media_id(file_ref)
        if selected_asset_id is not None:
            _media_id(selected_asset_id)
        turn = await self._turn(
            source_id, workspace_id, graph_limit=PREVIEW_GRAPH_REQUESTS
        )
        policy = replace(
            self._policy,
            timeout_class="fast",
            total_budget_s=PREVIEW_BUDGET_S,
            max_attempts=1,
        )
        parent = await self._graph(
            mid, turn, {"fields": _PREVIEW_FIELDS}, policy=policy
        )
        if parent.get("id") != mid:
            raise MediaTerminalError("Instagram returned a different media item")
        owner = parent.get("owner")
        known_owner = isinstance(owner, dict) and owner.get("id") in (
            turn.access.account_ref,
            turn.access.app_scoped_id,
        )
        if (
            _media_type(parent.get("media_type")) == "CAROUSEL_ALBUM"
            and not known_owner
        ):
            raise MediaTerminalError("Instagram preview needs a third Graph request")
        if not await self._owned_by_account(owner, turn, policy=policy):
            raise MediaTerminalError(
                "Instagram media ownership could not be established"
            )
        parts, complete = await self._children(
            parent, turn, fields=_PREVIEW_CHILD_FIELDS, policy=policy
        )
        part = self._selected_part(parts, complete, selected_asset_id)
        field = {"IMAGE": "media_url", "VIDEO": "thumbnail_url"}.get(
            part.get("media_type")
        )
        url = part.get(field) if field else None
        if not isinstance(url, str) or not url:
            raise MediaTerminalError("Instagram provides no preview for this media")
        download = replace(
            self._download_policy(url, PREVIEW_MAX_BYTES),
            timeout_class="fast",
            total_budget_s=PREVIEW_BUDGET_S,
            max_attempts=1,
        )
        try:
            # Never forward the Instagram token to the CDN.
            self._require_plain_client()
            response = await egress.request(
                self._client,
                "GET",
                url,
                policy=download,
                admission=turn.guard(graph=False),
            )
        except egress.ResponseTooLarge:
            raise MediaTerminalError("Instagram preview exceeds the byte cap") from None
        except egress.EgressBudgetExhausted:
            raise MediaRetryableError("Instagram preview download failed") from None
        if response.status_code in _EXPIRED_LINK:
            raise MediaTerminalError("Instagram preview link is unavailable")
        if response.is_error:
            raise MediaRetryableError("Instagram preview download failed")
        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if mime != "image/jpeg" or not response.content:
            raise MediaTerminalError("Instagram returned unsupported preview bytes")
        return response.content

    async def _owned_by_account(
        self, owner, turn: _Turn, *, policy: Optional[egress.EgressPolicy] = None
    ) -> bool:
        access = turn.access
        if not isinstance(owner, dict):
            return False
        owner_id = owner.get("id")
        if not isinstance(owner_id, str) or not _ID.fullmatch(owner_id):
            return False
        if owner_id == access.account_ref:
            return True
        # Instagram Login can return the app-scoped `id` as media owner,
        # while OAuth stores the account's `user_id`. The id the current
        # authorization verified is the proof, and no read is needed for it.
        if access.app_scoped_id is not None:
            return owner_id == access.app_scoped_id
        # None is recorded: prove the mapping with this SAME token, once for the
        # whole turn; a username or a merely readable post is no proof.
        if turn.profile is None:
            if turn.profile_read:
                # The turn's one read failed and was not repeated.
                return False
            turn.profile_read = True
            turn.profile = await self._graph(
                "me", turn, {"fields": "id,user_id"}, policy=policy
            )
        return (
            str(turn.profile.get("user_id")) == access.account_ref
            and turn.profile.get("id") == owner_id
        )

    def _download_policy(self, url: str, max_bytes: int) -> egress.EgressPolicy:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            valid = (
                parsed.scheme == "https"
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
                and any(
                    host.endswith(suffix)
                    for suffix in (".cdninstagram.com", ".fbcdn.net")
                )
            )
        except ValueError:
            valid = False
        if not valid:
            raise MediaTerminalError("Instagram returned an untrusted media host")
        return replace(
            self._policy,
            allowed_hosts=frozenset({host}),
            max_response_bytes=max_bytes,
            timeout_class="upload",
            total_budget_s=120,
        )
