"""The transit store (#915).

The workspace-scoped, signed, reaped Cloudinary transit layer the publish
pipeline stages media through. Credentials are passed per call, never set on
the process-global ``cloudinary.config()`` (#1216).

## What each requirement is, in this module's terms

- Every upload names ``folder=ws/{workspace_id}``. The workspace
  id is validated as a UUID before the folder string is composed: workspace
  isolation is only as strong as the prefix, and an id carrying path syntax
  (``../``, ``/``) would escape it. ``workspaces.id`` IS a UUID, so the guard
  refuses nothing legitimate.
- Delivery URLs are SIGNED and NON-EXPIRING:
  ``sign_url=True`` and deliberately no ``auth_token``/``expires_at``. The
  time-limit property attaches to the ASSET (destroy-on-post, the
  hard-TTL sweep); destruction 404s every URL regardless of signature, which
  is why :meth:`destroy` passes ``invalidate=True`` — a CDN-cached copy that
  survives destruction would quietly extend the capped lifetime.
- ``type=authenticated`` on upload, delivery, destroy AND the
  sweep listing. The type is part of the asset's identity at Cloudinary: a
  destroy or listing that omits it targets the public namespace and silently
  misses every transit asset.
- Server-signed per-request upload
  parameters, zero preset objects. The SDK signs each request from the
  credentials passed PER CALL (never read from ``cloudinary.config()``), and
  no ``upload_preset`` exists anywhere.
- :meth:`destroy` is the pipeline's inline
  reap-on-success door; :meth:`list_stale` + :meth:`destroy_asset` are the
  ``lister``/``deleter`` pair `scheduler.execute_reap_transit_assets`
  injects. Discovery is one GLOBAL paged walk over the
  ``ws/`` prefix per provider resource_type — never per-workspace listings
  (the cost of discovery must not grow with the customer count).

## Seams, stated

The five SDK callables (``upload_fn``, ``destroy_fn``, ``resources_fn`` — the
sweep's listing — ``resource_fn`` — one asset's details, the past-posts lookup's
door — and ``url_fn``) are injectable for the same reason the reconciler's
``poll`` and the reap executor's ``lister``/``deleter`` are: the properties
under test are THIS module's parameter sets, and a real provider client would
make the gate a network test. The defaults bind to the real
``cloudinary.uploader``/``api``/``utils`` doors (pinned by an identity test).
The no-reference Drive path is deliberately OFF the provider-ops permit rail: upload
and destroy are recoverable effects — a duplicate transit upload is a second
short-lived asset the sweep reaps, a missed destroy is caught by the sweep.

The legacy SDK is synchronous; its async methods run it via ``asyncio.to_thread``. These
calls do not ride the egress floor (the SDK owns its own HTTP), so the
transaction-per-checkpoint discipline for transit is the CALLER's to hold:
upload/destroy outside any open unit of work. The floor cannot see SDK
internals and will not catch a violation here — named rather than implied.

An explicit preallocated past-posts reference instead uses one owned async HTTP
transmission through the egress floor. Its uncertainty is not retryable Drive
orphan behavior. Durable attempt/completion ownership and remote settlement are
caller responsibilities; this transport does not implement or prove them.

The cleanup of such a reference has two typed doors, both on the SDK seams
because a read and a delete are recoverable where the one-send upload is not:
:meth:`TransitStore.past_posts_lookup` answers what the provider holds for it (the
creation evidence, or None) and :meth:`TransitStore.destroy_past_posts` keeps
``destroyed`` and ``not_found`` apart, which :meth:`TransitStore.destroy`
deliberately does not. Neither answer settles an admitted upload: a ``not
found`` is absence at that instant only. The order (settle, destroy, verify)
is the caller's.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import hmac
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional

from src.exceptions.base import StorydumpError
from src.services.target import egress, media_validation
from src.services.target.egress import TIMEOUT_CLASSES
from src.utils.datetime_utils import ensure_utc, ms_since, parse_iso_timestamp

logger = logging.getLogger(__name__)


#: The Instagram story frame: 1080 × 1920, 9:16.
STORY_WIDTH = 1080
STORY_HEIGHT = 1920
#: The blur behind the picture.
STORY_BLUR = 2000
#: What Meta accepts for a story, by kind — the derivation's delivered format.
#: How long `ready` waits for the story frame to serve, per media kind (a
#: video frame derives in the background and takes longer), and how often it
#: asks. The worker's lease (90 s) is extended by the heartbeat meanwhile.
READY_BUDGET_S = {"image": 20.0, "video": 60.0}
READY_POLL_S = 1.0
STORY_FORMATS = {"image": "jpg", "video": "mp4"}
#: What the readiness probe READS of the frame: the first bytes,
#: enough for the format's signature, never the file — a video frame is tens
#: of megabytes. A HEAD proved headers only; the CDN answers a miss with a
#: 404 whose body is a placeholder GIF, and a relabelled or truncated body
#: would pass a headers-only check. Cloudinary honours ranges (206); the
#: floor's byte cap is the backstop should a range ever be ignored.
PROBE_RANGE_BYTES = 1024
PROBE_MAX_RESPONSE_BYTES = 64 * 1024
#: The float: a refused fetch is retried with a url Meta has never seen for
#: this story — the same chain plus `n` of this no-op step: a distinct signed
#: url and a distinct derived asset, identical bytes.
URL_VARIANT_STEP = {"dpr": "1.0"}
#: An ISO base-media file (MP4/MOV) opens with a 4-byte size and a 4-byte box
#: type; `ftyp` is usual, but `free`/`skip`/`wide`/`mdat`/`moov` first are
#: legal and appear.
_ISO_BMFF_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide")


@dataclass(frozen=True)
class ProbeAnswer:
    """One probe's reading of the delivery url: status and content type (the
    headers-only seam) and the first bytes, the total length when the answer
    names it, the CDN's request id and how long the read took — what the
    ledger keeps beside Meta's own answer.

    *bytes_read* says whether the body was READ and so must carry the kind's
    signature: True for a ranged read (an empty body then fails — a frame the
    edge is still filling), False for a headers-only answer (a HEAD). Unset,
    it follows *head*: an injected probe that hands bytes is judged on them."""

    status: int
    content_type: str
    head: bytes = b""
    length: Optional[int] = None
    request_id: Optional[str] = None
    elapsed_ms: Optional[int] = None
    bytes_read: Optional[bool] = None

    @property
    def judges_bytes(self) -> bool:
        return bool(self.head) if self.bytes_read is None else self.bytes_read


class Readiness:
    """`ready()`'s answer: true iff the frame serves as media, carrying the
    LAST probe's observation for the ledger. Truthiness serves the call sites
    that test it (`if not await ready(...)`)."""

    __slots__ = ("ok", "observation")

    def __init__(self, ok: bool, observation: dict):
        self.ok = bool(ok)
        self.observation = observation

    def __bool__(self) -> bool:
        return self.ok

    def __repr__(self) -> str:
        return f"Readiness({self.ok}, {self.observation})"


def magic_of(head: bytes) -> Optional[str]:
    """The format the first bytes announce: 'jpeg', 'png', 'mp4', or None."""
    if head[:2] == b"\xff\xd8":
        return "jpeg"
    if head[:4] == b"\x89PNG":
        return "png"
    if len(head) >= 8 and head[4:8] in _ISO_BMFF_BOXES:
        return "mp4"
    return None


def serves_media(answer: ProbeAnswer, media_kind: str) -> bool:
    """Whether the answer IS the frame: a 200/206 with the kind's content
    type and — when the body was read — a body that opens with the kind's
    signature (an empty one fails). A headers-only answer is judged on the
    headers alone."""
    if answer.status not in (200, 206):
        return False
    ctype = answer.content_type.lower()
    if media_kind == "video":
        if not ctype.startswith("video/"):
            return False
        return magic_of(answer.head) == "mp4" if answer.judges_bytes else True
    if not ctype.startswith(("image/jpeg", "image/png")):
        return False
    if not answer.judges_bytes:
        return True
    return magic_of(answer.head) in ("jpeg", "png")


def _as_answer(raw: Any, *, elapsed_ms: int) -> ProbeAnswer:
    """A probe may answer the pair `(status, content_type)` or a
    full :class:`ProbeAnswer`; either becomes one shape. Anything else
    raises, which `ready()` reads as "not yet"."""
    if isinstance(raw, ProbeAnswer):
        if raw.elapsed_ms is None:
            return dataclasses.replace(raw, elapsed_ms=elapsed_ms)
        return raw
    status, content_type = raw
    return ProbeAnswer(int(status), str(content_type or ""), elapsed_ms=elapsed_ms)


def _total_length(headers: Any) -> Optional[int]:
    """The frame's full length when a range answer names it —
    `Content-Range: bytes 0-1023/137673`; a 416's `bytes */137673` served
    nothing and names nothing here. (A 200's `Content-Length` is dropped by
    the floor — it described the wire body — so it is not read.)"""
    content_range = str(headers.get("content-range", "") or "").strip()
    if content_range.startswith("bytes ") and "/" in content_range:
        served, _, total = content_range[len("bytes ") :].partition("/")
        if served.strip() != "*" and total.strip().isdecimal():
            return int(total.strip())
    return None


def story_transformation(transit_asset_ref: str, *, media_kind: str) -> list[dict]:
    """How Meta sees the asset: scaled to fit the story's frame and padded to
    9:16 over a blurred, filled copy of itself.

    An image uses an UNDERLAY of the same asset filled to
    the frame and blurred, the picture scaled to the width and padded over
    it: blurred bars above and below for a landscape or square picture,
    left and right for one taller than 9:16 (`c_pad` never crops). The asset
    is `authenticated`, so the layer names it as such and the whole signed
    URL authorises the layer (Cloudinary: an authenticated layer needs the
    signed URL, no separate signature). A video is trimmed to the longest
    story first, so nothing past it is framed or encoded, then takes
    Cloudinary's own blurred-background padding, then is encoded to the story
    spec, all from the validation matrix (`media_validation.story_video_encoding`).
    Every step is a dict, so the SDK never renders a named transformation.
    """
    if media_kind not in STORY_FORMATS:
        raise ValueError(f"unknown media_kind {media_kind!r}")
    if media_kind == "video":
        encoding = media_validation.story_video_encoding()
        slowest, fastest = encoding["fps"]
        return [
            {"duration": encoding["longest_seconds"]},
            {"crop": "limit", "width": STORY_WIDTH, "height": STORY_HEIGHT},
            {
                "crop": "pad",
                "width": STORY_WIDTH,
                "height": STORY_HEIGHT,
                "background": f"blurred:{STORY_BLUR}:15",
            },
            {
                "video_codec": ":".join(encoding["video_codec"]),
                "audio_codec": encoding["audio_codec"],
                "audio_frequency": encoding["audio_hz"],
                "fps": f"{slowest}-{fastest}",
                "bit_rate": encoding["video_bits_per_second"],
            },
        ]
    underlay = "authenticated:" + transit_asset_ref.replace("/", ":")
    return [
        # FIRST fit the picture into the frame. A layer's canvas is the base's
        # size, so a 4032 × 3024 phone photo behind a 1080 × 1920 underlay
        # would hide the underlay entirely and the pad below would fall back
        # to white bars (#1283).
        {"crop": "limit", "width": STORY_WIDTH, "height": STORY_HEIGHT},
        {"underlay": underlay},
        {
            "crop": "fill",
            "width": STORY_WIDTH,
            "height": STORY_HEIGHT,
            "effect": f"blur:{STORY_BLUR}",
        },
        {"flags": "layer_apply"},
        {"crop": "limit", "width": STORY_WIDTH},
        {
            "crop": "pad",
            "width": STORY_WIDTH,
            "height": STORY_HEIGHT,
            "gravity": "center",
        },
    ]


class TransitError(StorydumpError):
    """The transit store's upload failed; the cause is chained. Retryable by
    the pipeline's ladder, bounded at a human."""


@dataclass(frozen=True)
class PastPostsUploadEvidence:
    """Matching affirmative provider creation; never an allocated reference alone."""

    public_id: str
    asset_id: str
    version: int
    resource_type: str
    delivery_type: str
    signature: str


class PastPostsUploadUncertain(StorydumpError):
    """A past-posts upload lacks matching affirmative creation evidence.

    Distinct from Drive's retryable TransitError: retrying this attempt is not
    authorized. Cancellation also leaves the caller's durable attempt unsettled.
    :meth:`TransitStore.past_posts_lookup` raises it too, for a provider answer
    that does not identify the asset asked about.
    """


_PAST_POSTS_UPLOAD_RESPONSE_BYTES = 64 * 1024

#: The one reference shape a past-posts upload can have created (and the database
#: admits): ``ws/{canonical lowercase uuid}/{20-64 lowercase hex}``. The workspace
#: is not a separate argument to the cleanup doors, so the shape is checked here.
_PAST_POSTS_REF = re.compile(
    r"ws/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[0-9a-f]{20,64}"
)

#: A name the provider issues that this module puts in a URL path or an evidence
#: record: the cloud name a past-posts upload is sent to, the asset id an answer
#: carries. One component of at most 128 letters, digits, ``_`` and ``-``.
_PROVIDER_COMPONENT = re.compile(r"[a-zA-Z0-9_-]{1,128}")


#: media_items.media_kind (`ck_media_kind`) → Cloudinary resource_type.
_RESOURCE_TYPES = {"image": "image", "video": "video"}

#: Time bound for EVERY SDK door (upload, destroy, listing, lookup), seconds. The
#: SDK is not behind the egress floor, so the floor's 'upload' class rides here —
#: SOURCED from it, not copied, so retuning the floor cannot silently orphan
#: this module. Applied to every door because the rationale is door-
#: independent: a hung destroy stalls the pipeline's terminal step inside its
#: 120 s lease, a hung listing stalls the sweep tick. There is no
#: Cloudinary-specific bound (#915).
DEFAULT_SDK_TIMEOUT_S = TIMEOUT_CLASSES["upload"]

#: One provider listing page per call, the API maximum — fewer round trips
#: per sweep.
_PAGE_SIZE = 500

#: The provider resource_types one sweep walks — deduped once, not per call
#: (a future media_kind mapping onto an existing resource_type must not add
#: a duplicate full paged walk).
_SWEEP_RESOURCE_TYPES = tuple(sorted(set(_RESOURCE_TYPES.values())))

#: How many unreadable rows the sweep's error names by example. The count it
#: gives is always whole; a corpus-wide format shift would otherwise print
#: every row on every tick.
_UNREAD_SAMPLE = 5


class TransitDestroyRefused(StorydumpError):
    """The provider answered a destroy politely with a non-ok result. Raised
    by the sweep's deleter so `execute_reap_transit_assets` counts only
    completed destroys — a refusal silently counted as reaped would overstate
    the sweep and strand the asset uncounted. :meth:`TransitStore.destroy_past_posts`
    raises it for any answer other than ``ok`` or ``not found``."""


class TransitStore:
    """Transit assets: workspace-scoped signed upload, signed
    non-expiring delivery, typed destroy, and the stale walk."""

    def __init__(
        self,
        *,
        cloud_name: str,
        api_key: str,
        api_secret: str,
        sdk_timeout_seconds: float = DEFAULT_SDK_TIMEOUT_S,
        now_fn: Optional[Callable[[], datetime]] = None,
        upload_fn: Optional[Callable[..., dict]] = None,
        destroy_fn: Optional[Callable[..., dict]] = None,
        resources_fn: Optional[Callable[..., dict]] = None,
        resource_fn: Optional[Callable[..., dict]] = None,
        url_fn: Optional[Callable[..., tuple]] = None,
        probe_fn: Optional[Callable[[str], Any]] = None,
        probe_resolver: Optional[Callable[[str], list]] = None,
        past_posts_resolver: Optional[Callable[[str], list]] = None,
    ):
        for name, value in (
            ("cloud_name", cloud_name),
            ("api_key", api_key),
            ("api_secret", api_secret),
        ):
            if not value or not str(value).strip():
                raise ValueError(
                    f"TransitStore requires {name} — a store without credentials "
                    "must fail at construction, not mid-pipeline on the first upload"
                )
        # Per-call credentials: the SDK signs each request from THESE,
        # never from process-global ``cloudinary.config()``. Splatted at every
        # call site, which copies into the call's kwargs.
        self._credentials = {
            "cloud_name": cloud_name,
            "api_key": api_key,
            "api_secret": api_secret,
        }
        self._sdk_timeout_s = sdk_timeout_seconds
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

        if None in (upload_fn, destroy_fn, resources_fn, resource_fn, url_fn):
            import cloudinary.api
            import cloudinary.uploader
            import cloudinary.utils

            upload_fn = upload_fn or cloudinary.uploader.upload
            destroy_fn = destroy_fn or cloudinary.uploader.destroy
            resources_fn = resources_fn or cloudinary.api.resources
            resource_fn = resource_fn or cloudinary.api.resource
            url_fn = url_fn or cloudinary.utils.cloudinary_url
        self._upload_fn = upload_fn
        self._destroy_fn = destroy_fn
        self._resources_fn = resources_fn
        #: ``resources_fn`` LISTS (the sweep); this READS one asset (the
        #: past-posts lookup). One letter apart, two different Admin API doors.
        self._resource_fn = resource_fn
        self._url_fn = url_fn
        #: `ready`'s probe: `url -> ProbeAnswer` (or the pair
        #: `(status, content_type)`); the default is an egress-floored ranged
        #: GET of the first bytes (tests inject a script). *probe_resolver*
        #: is the floor's DNS seam for that default — tests hand it a static
        #: answer so no name is looked up.
        self._probe_fn = probe_fn or self._default_probe
        self._probe_client = None
        self._probe_resolver = probe_resolver
        self._past_posts_resolver = past_posts_resolver

    @staticmethod
    def _resource_type(media_kind: str) -> str:
        try:
            return _RESOURCE_TYPES[media_kind]
        except KeyError:
            raise ValueError(
                f"not a transit media_kind: {media_kind!r} (ck_media_kind is "
                f"closed to {sorted(_RESOURCE_TYPES)})"
            ) from None

    @staticmethod
    def _workspace_folder(workspace_id: str) -> str:
        """``ws/{workspace_id}`` — parsed, canonical form.

        The PARSED UUID is interpolated, never the raw string: ``uuid.UUID``
        accepts uppercase/braced/urn: spellings, and composing from the raw
        input would split one workspace across sibling folders."""
        try:
            parsed = uuid.UUID(str(workspace_id))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                f"not a workspace_id: {workspace_id!r} — the folder "
                "prefix accepts only a UUID (workspaces.id)"
            ) from None
        return f"ws/{parsed}"

    @staticmethod
    def _past_posts_target(public_id: Any, resource_type: Any) -> None:
        """Refuse, before any provider call, a reference or resource_type an
        past-posts upload cannot have created. A destroy or lookup aimed at the
        wrong namespace (``raw``) answers ``not found`` for an asset that
        exists, and the caller reads that as absence; the Admin API puts the
        public id in the request path, so one carrying path syntax would name
        another endpoint."""
        if (
            not isinstance(public_id, str)
            or _PAST_POSTS_REF.fullmatch(public_id) is None
        ):
            raise ValueError(
                "past-posts reference must be canonical and workspace scoped"
            )
        if resource_type not in _SWEEP_RESOURCE_TYPES:
            raise ValueError(
                f"not a past-posts resource_type: {resource_type!r} "
                f"(closed to {list(_SWEEP_RESOURCE_TYPES)})"
            )

    def _creation_evidence(
        self, result: Any, *, public_id: str, resource_type: str
    ) -> Optional[PastPostsUploadEvidence]:
        """The creation evidence a provider's answer amounts to for the past-posts
        asset asked about, or None when the answer does not identify it: not a
        mapping, another public id or resource_type, not ``authenticated``, or
        no usable asset id or version. The one reading of an answer, for the
        upload's and the lookup's alike.

        The signature is computed here, from the public id, the version and this
        store's secret (sha1, signature version 1), never taken from the answer.
        """
        import cloudinary.utils

        if not (
            isinstance(result, dict)
            and result.get("public_id") == public_id
            and result.get("resource_type") == resource_type
            and result.get("type") == "authenticated"
            and isinstance(result.get("asset_id"), str)
            and _PROVIDER_COMPONENT.fullmatch(result["asset_id"]) is not None
            and type(result.get("version")) is int
            and 0 < result["version"] <= 2**63 - 1
        ):
            return None
        return PastPostsUploadEvidence(
            public_id=public_id,
            asset_id=result["asset_id"],
            version=result["version"],
            resource_type=resource_type,
            delivery_type="authenticated",
            signature=cloudinary.utils.api_sign_request(
                {"public_id": public_id, "version": result["version"]},
                self._credentials["api_secret"],
                algorithm="sha1",
                signature_version=1,
            ),
        )

    # -- the upload ------------------------------------------------------------

    async def upload(
        self,
        content: bytes,
        *,
        workspace_id: str,
        media_kind: str,
        preallocated_ref: Optional[str] = None,
    ) -> str | PastPostsUploadEvidence:
        """Upload one transit asset; returns the provider public id — the
        value the pipeline persists as ``post_intents.transit_asset_ref``.
        An explicit preallocated_ref selects the past-posts source one-send path and
        returns structured matching creation evidence instead of a bare ref.
        Its durable admitted attempt and completion owner belong to the caller.

        Call OUTSIDE any open unit of work (checkpoint discipline; the module
        docstring names why the floor cannot enforce it here).
        """
        folder = self._workspace_folder(workspace_id)
        resource_type = self._resource_type(media_kind)
        if preallocated_ref is not None:
            return await self._past_posts_upload(
                content,
                folder=folder,
                resource_type=resource_type,
                media_kind=media_kind,
                ref=preallocated_ref,
            )
        # The public id is minted HERE, not by Cloudinary, so the story frame
        # can be derived eagerly in the same upload (the underlay names the
        # asset itself). Meta fetches the frame within a second of the
        # container call: a frame derived on demand takes seconds to first
        # byte, and the first fetch of a fresh asset can answer with an error
        # image ("could not be fetched", 9004/2207052). An eager frame takes a
        # fraction of a second.
        ref = f"{folder}/{uuid.uuid4().hex[:20]}"
        eager = [
            {
                "transformation": story_transformation(ref, media_kind=media_kind),
                "format": STORY_FORMATS[media_kind],
            }
        ]
        try:
            result = await asyncio.to_thread(
                self._upload_fn,
                content,
                public_id=ref,
                type="authenticated",
                resource_type=resource_type,
                overwrite=False,
                eager=eager,
                # An image frame derives in well under the upload timeout; a
                # video frame can take longer than a synchronous eager allows,
                # so it derives in the background and `ready` waits for it.
                eager_async=media_kind != "image",
                timeout=self._sdk_timeout_s,
                **self._credentials,
            )
        except Exception as exc:  # noqa: BLE001 — the SDK's zoo becomes one type
            # The SDK raises its own family (and plain OSErrors on the wire);
            # the pipeline routes on TYPED failures only, so an untyped one
            # here would crash the job into the loop's unbounded reschedule
            # (#1276). One type, the cause preserved.
            raise TransitError(
                f"transit upload failed: {type(exc).__name__}: {exc}"
            ) from exc
        return result["public_id"]

    async def _past_posts_upload(
        self, content, *, folder, resource_type, media_kind, ref
    ):
        """One owned async multipart send; attempt durability belongs to the caller."""
        import cloudinary.utils
        import httpx

        if type(content) is not bytes or not content:
            raise ValueError("past-posts upload requires nonempty immutable bytes")
        if (
            not isinstance(ref, str)
            or re.fullmatch(re.escape(folder) + r"/[0-9a-f]{20,64}", ref) is None
        ):
            raise ValueError(
                "past-posts reference must be canonical and workspace scoped"
            )
        cloud = self._credentials["cloud_name"]
        if not isinstance(cloud, str) or _PROVIDER_COMPONENT.fullmatch(cloud) is None:
            raise ValueError(
                "past-posts cloud_name must be one provider path component"
            )
        # Neutralize SDK transformation defaults without mutating its config.
        neutral = {"responsive_width": False, "dpr": None, "use_fetch_format": False}
        eager = cloudinary.utils.build_eager(
            [
                {
                    **neutral,
                    "transformation": [
                        {**step, **neutral}
                        for step in story_transformation(ref, media_kind=media_kind)
                    ],
                    "format": STORY_FORMATS[media_kind],
                }
            ]
        )
        params = cloudinary.utils.cleanup_params(
            {
                "timestamp": str(int(self._now_fn().timestamp())),
                "public_id": ref,
                "type": "authenticated",
                "overwrite": False,
                "eager": eager,
                "eager_async": media_kind != "image",
            }
        )
        params["signature"] = cloudinary.utils.api_sign_request(
            params,
            self._credentials["api_secret"],
            algorithm="sha1",
            signature_version=2,
        )
        params["api_key"] = self._credentials["api_key"]
        policy = egress.EgressPolicy(
            timeout_class="upload",
            total_budget_s=TIMEOUT_CLASSES["upload"],
            max_attempts=1,
            max_response_bytes=_PAST_POSTS_UPLOAD_RESPONSE_BYTES,
            allowed_hosts=frozenset({"api.cloudinary.com"}),
            allow_redirects=False,
        )
        seam = (
            {}
            if self._past_posts_resolver is None
            else {"resolver": self._past_posts_resolver}
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                trust_env=False,
                auth=None,
                follow_redirects=False,
            ) as client:
                response = await egress.request(
                    client,
                    "POST",
                    f"https://api.cloudinary.com/v1_1/{cloud}/{resource_type}/upload",
                    policy=policy,
                    data=params,
                    files={"file": ("file", content)},
                    **seam,
                )
            if response.status_code != 200:
                raise PastPostsUploadUncertain(
                    "past-posts upload provider response uncertain"
                )
            result = response.json()
        except PastPostsUploadUncertain:
            raise
        except Exception:  # noqa: BLE001 — no error establishes remote no-effect
            raise PastPostsUploadUncertain(
                "past-posts upload response uncertain"
            ) from None
        evidence = self._creation_evidence(
            result, public_id=ref, resource_type=resource_type
        )
        # The answer also carries the provider's own signature of the creation,
        # which must be the one this store computes.
        sent = result.get("signature") if isinstance(result, dict) else None
        if (
            evidence is None
            or not isinstance(sent, str)
            or re.fullmatch(r"[0-9a-f]{40}", sent) is None
            or not hmac.compare_digest(evidence.signature, sent)
        ):
            raise PastPostsUploadUncertain("past-posts upload identity uncertain")
        return evidence

    # -- readiness -------------------------------------------------------------

    async def ready(
        self,
        transit_asset_ref: str,
        *,
        media_kind: str,
        budget_s: Optional[float] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        variant: int = 0,
    ) -> Readiness:
        """Whether the story frame at :meth:`delivery_url` serves as media
        NOW — the check the pipeline makes before it hands Meta the URL.
        Polls every `READY_POLL_S` until the probe answers 200/206 with the
        kind's content type and (when it reads bytes) the kind's signature,
        or *budget_s* is spent. A probe error is "not yet", never an
        exception: the caller's ladder decides. The answer is truthy iff
        ready and carries the last probe's observation for the ledger."""
        url = self.delivery_url(
            transit_asset_ref, media_kind=media_kind, variant=variant
        )
        if budget_s is None:
            budget_s = READY_BUDGET_S.get(media_kind, READY_BUDGET_S["image"])
        deadline = self._now_fn() + timedelta(seconds=budget_s)
        polls = 0
        while True:
            polls += 1
            started = time.perf_counter()
            try:
                answer = _as_answer(
                    await self._probe_fn(url),
                    elapsed_ms=ms_since(started),
                )
            except Exception as exc:  # noqa: BLE001 — a failed probe, or an answer of no known shape, is "not yet"
                answer = ProbeAnswer(
                    0,
                    f"probe failed: {type(exc).__name__}",
                    elapsed_ms=ms_since(started),
                )
            observation = {
                "status": answer.status,
                "content_type": answer.content_type,
                "length": answer.length,
                "request_id": answer.request_id,
                "elapsed_ms": answer.elapsed_ms,
                "polls": polls,
                "bytes_read": answer.judges_bytes,
                "magic": magic_of(answer.head) if answer.head else None,
            }
            if serves_media(answer, media_kind):
                return Readiness(True, observation)
            if self._now_fn() >= deadline:
                return Readiness(False, observation)
            await sleep(READY_POLL_S)

    async def _default_probe(self, url: str) -> ProbeAnswer:
        """One egress-floored ranged GET of the delivery url's first bytes:
        status, content type, the bytes the signature check needs, the total
        length the range answer names, the CDN's request id and the read's
        duration. The floor owns redirects (it refuses cross-host hops); the
        host allow-list is the url's own, which the SDK built from our cloud
        name, and the private-address block still guards it. The byte cap
        bounds the body should the range ever be ignored."""
        from urllib.parse import urlparse

        from src.services.target import egress

        host = urlparse(url).hostname or ""
        policy = egress.EgressPolicy(
            timeout_class="standard",
            total_budget_s=float(TIMEOUT_CLASSES["standard"]),
            max_attempts=1,
            max_response_bytes=PROBE_MAX_RESPONSE_BYTES,
            allowed_hosts=frozenset({host}),
        )
        if self._probe_client is None:
            import httpx

            self._probe_client = httpx.AsyncClient()
        seam = {"resolver": self._probe_resolver} if self._probe_resolver else {}
        started = time.perf_counter()
        try:
            response = await egress.request(
                self._probe_client,
                "GET",
                url,
                policy=policy,
                headers={"Range": f"bytes=0-{PROBE_RANGE_BYTES - 1}"},
                **seam,
            )
            bytes_read = True
        except egress.ResponseTooLarge:
            # The range was ignored and the whole frame came back past the
            # cap: a HEAD answers the headers instead, and
            # `bytes_read=False` on the ledger says the bytes were not judged.
            response = await egress.request(
                self._probe_client, "HEAD", url, policy=policy, **seam
            )
            bytes_read = False
        length = _total_length(response.headers)
        if length is None and bytes_read and response.status_code == 200:
            length = len(response.content)  # the whole frame, under the cap
        return ProbeAnswer(
            status=int(response.status_code),
            content_type=str(response.headers.get("content-type", "")),
            head=bytes(response.content[:PROBE_RANGE_BYTES]) if bytes_read else b"",
            length=length,
            request_id=response.headers.get("x-request-id") or None,
            elapsed_ms=ms_since(started),
            bytes_read=bytes_read,
        )

    # -- delivery --------------------------------------------------------------

    def delivery_url(
        self, transit_asset_ref: str, *, media_kind: str, variant: int = 0
    ) -> str:
        """The signed, non-expiring delivery URL Meta pulls the asset from —
        framed for a story (:func:`story_transformation`): the signature
        covers the transformation, so the framed derivation is the only one
        an authenticated asset serves. *variant* `n > 0` appends `n` no-op
        steps (`URL_VARIANT_STEP`): a url Meta has never seen for this
        story, with the same bytes behind it (the float)."""
        if variant < 0:
            raise ValueError(f"a url variant counts up from zero, not {variant}")
        url, _options = self._url_fn(
            transit_asset_ref,
            transformation=story_transformation(
                transit_asset_ref, media_kind=media_kind
            )
            + [URL_VARIANT_STEP] * variant,
            # Meta's story spec: a JPEG image, an MP4/MOV video. The source
            # may be anything Drive holds (HEIC from a phone, WEBP, WEBM);
            # the derivation is delivered in the format Meta accepts, and
            # the signature covers the format too.
            format=STORY_FORMATS[media_kind],
            sign_url=True,
            type="authenticated",
            resource_type=self._resource_type(media_kind),
            secure=True,
            **self._credentials,
        )
        return url

    # -- destruction -----------------------------------------------------------

    async def destroy(self, transit_asset_ref: str, *, media_kind: str) -> bool:
        """Destroy one transit asset; True iff the asset is gone NOW.

        'not found' counts as gone — a prior destroy or the sweep won, and the
        goal state (no asset, every URL 404) holds. Any other result is False;
        the sweep is the retry. That reading fits the Drive path and not
        a past-posts reference, where it cannot tell destroyed from never created
        yet: :meth:`destroy_past_posts` keeps the two answers apart.
        """
        return await self._destroy(transit_asset_ref, self._resource_type(media_kind))

    async def destroy_asset(self, asset: dict[str, Any]) -> None:
        """The `execute_reap_transit_assets` deleter: destroys one
        :meth:`list_stale` row. Raises :class:`TransitDestroyRefused` on a
        polite non-ok provider answer (and lets SDK errors propagate), so the
        executor counts only completed destroys and leaves every refusal to
        the next sweep."""
        if not await self._destroy(asset["public_id"], asset["resource_type"]):
            raise TransitDestroyRefused(
                f"provider refused to destroy {asset['public_id']!r}; "
                "left for the next sweep"
            )

    async def _destroy(self, public_id: str, resource_type: str) -> bool:
        result = await self._destroy_call(public_id, resource_type)
        return result.get("result") in ("ok", "not found")

    async def _destroy_call(self, public_id: str, resource_type: str) -> Any:
        """The one spelling of the SDK destroy call, shared by :meth:`destroy`,
        :meth:`destroy_asset` and :meth:`destroy_past_posts`: the authenticated
        type, ``invalidate``, the time bound and this store's
        own credentials. Returns the SDK's answer unread."""
        return await asyncio.to_thread(
            self._destroy_fn,
            public_id,
            type="authenticated",
            resource_type=resource_type,
            invalidate=True,
            timeout=self._sdk_timeout_s,
            **self._credentials,
        )

    # -- past-posts references: the typed cleanup doors ---------------------------

    async def destroy_past_posts(
        self, public_id: str, *, resource_type: str
    ) -> Literal["destroyed", "not_found"]:
        """Destroy one past-posts transit asset and say which answer the provider
        gave: ``'destroyed'`` for ``ok``, ``'not_found'`` for ``not found``.
        Any other answer is a polite refusal and raises
        :class:`TransitDestroyRefused`; an SDK error (timeout, auth, rate
        limit) propagates unchanged, as from :meth:`destroy_asset` — it is not
        an answer, so it is neither of the two.

        ``'not_found'`` only proves absence at that instant. It does not settle
        an upload that was admitted and may still land (a held request, a lost
        answer): the asset can appear after this call returns, so it never
        shows that no upload was made. Settle the attempt first
        (:meth:`past_posts_lookup`), destroy what it created, then confirm
        absence with a lookup.

        Both answers are the goal state to the Drive path's :meth:`destroy`,
        which says only whether the asset is gone; this keeps them apart so a
        cleanup can record what it saw. Call OUTSIDE any open unit of work.
        """
        self._past_posts_target(public_id, resource_type)
        result = await self._destroy_call(public_id, resource_type)
        answer = result.get("result") if isinstance(result, dict) else None
        if answer == "ok":
            return "destroyed"
        if answer == "not found":
            return "not_found"
        raise TransitDestroyRefused(
            f"provider answered neither ok nor not found to the destroy of "
            f"{public_id!r}; the asset may still exist"
        )

    async def past_posts_lookup(
        self, public_id: str, *, resource_type: str
    ) -> Optional[PastPostsUploadEvidence]:
        """What the provider holds for one past-posts reference RIGHT NOW: the
        creation evidence for it, or None.

        The asset's details are read through ``resource_fn`` as the
        ``authenticated`` type (a read without it targets the public
        namespace and finds nothing). The evidence is built from the answer
        with its signature computed here, from the public id, the version and
        this store's secret (sha1, signature version 1) — the same check the
        upload makes of the signature it is sent, so a lookup and the upload
        that created the asset yield equal evidence.

        None is returned ONLY when the provider itself answers ``not found``,
        and like :meth:`destroy_past_posts`'s it proves absence at that instant
        only: an upload still in flight can create the asset afterwards.
        Everything else raises, because none of it is absence and a cleanup
        reads None as "gone": an SDK error (timeout, auth, rate limit, 5xx)
        propagates unchanged, and an answer that does not identify the asset
        asked about (another public id or resource_type, not ``authenticated``,
        no usable asset id or version) raises :class:`PastPostsUploadUncertain`.

        Call OUTSIDE any open unit of work.
        """
        self._past_posts_target(public_id, resource_type)
        import cloudinary.exceptions

        try:
            result = await asyncio.to_thread(
                self._resource_fn,
                public_id,
                type="authenticated",
                resource_type=resource_type,
                timeout=self._sdk_timeout_s,
                **self._credentials,
            )
        except cloudinary.exceptions.NotFound:
            return None
        evidence = self._creation_evidence(
            result, public_id=public_id, resource_type=resource_type
        )
        if evidence is None:
            raise PastPostsUploadUncertain("past-posts lookup identity uncertain")
        return evidence

    # -- discovery -------------------------------------------------------------

    async def list_stale(self, *, older_than_seconds: int) -> list[dict[str, Any]]:
        """Every transit asset older than the TTL — one GLOBAL paged walk over
        the ``ws/`` prefix per resource_type, filtered by ``created_at``.

        Rows carry exactly what :meth:`destroy_asset` needs. Reaping needs an
        age that was READ: a row whose ``created_at`` is missing, not a string
        or not ISO-8601 is KEPT, and the sweep names such rows in one error.
        ``created_at`` is rendered by the provider — one serializer for every
        row — so an unreadable age is the signature of a format shift across
        the whole corpus far more often than of one garbled row, and failing
        toward reaping would destroy every in-flight asset mid-publish on the
        next tick: the backstop must not be the outage. Kept is the
        recoverable failure — an asset outlives its TTL only until the parse
        reads its age, and the error repeats on every sweep until then.
        """
        cutoff = self._now_fn() - timedelta(seconds=older_than_seconds)
        stale: list[dict[str, Any]] = []
        unread = 0
        examples: list[tuple[Any, Any]] = []
        for resource_type in _SWEEP_RESOURCE_TYPES:
            cursor: Optional[str] = None
            while True:
                page = await asyncio.to_thread(
                    self._resources_fn,
                    type="authenticated",
                    resource_type=resource_type,
                    prefix="ws/",
                    max_results=_PAGE_SIZE,
                    next_cursor=cursor,
                    timeout=self._sdk_timeout_s,
                    **self._credentials,
                )
                for row in page.get("resources", []):
                    raw = row.get("created_at")
                    created = self._created_at(raw)
                    if created is None:
                        unread += 1
                        if unread <= _UNREAD_SAMPLE:
                            examples.append((row.get("public_id"), raw))
                    elif created < cutoff:
                        stale.append(
                            {
                                "public_id": row["public_id"],
                                "resource_type": resource_type,
                            }
                        )
                cursor = page.get("next_cursor")
                if not cursor:
                    break
        if unread:
            logger.error(
                "transit sweep: %d transit asset(s) NOT REAPED — no age could be "
                "read from created_at, so none is proven past the TTL; e.g. %s",
                unread,
                "; ".join(
                    f"{public_id} created_at={raw!r}" for public_id, raw in examples
                ),
            )
        return stale

    @staticmethod
    def _created_at(value: Any) -> Optional[datetime]:
        """The asset's upload instant, or None when *value* yields no age.
        :func:`parse_iso_timestamp` reads every fraction width and offset
        spelling alike on every interpreter, so a benign re-spelling keeps the
        sweep reaping rather than merely safe."""
        if not isinstance(value, str):
            return None
        try:
            return ensure_utc(parse_iso_timestamp(value))
        except ValueError:
            return None
