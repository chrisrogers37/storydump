"""L.5 slice 2 — the FC-3 transit store (#915; `00` FC-3, `03` D28/D38).

The workspace-scoped, signed, reaped Cloudinary transit layer the publish
pipeline stages media through. The legacy ``cloud_storage.py`` is deliberately
NOT extended: it uploads into a global app folder with public delivery, which
is three FC-3 requirements short (no ``ws/{workspace_id}/`` scoping, no
``type=authenticated``, unsigned delivery), and it mutates process-global
``cloudinary.config()`` — a per-call-credential store cannot ride on that.

## What each FC-3 requirement is, in this module's terms

- **FC-3.1** — every upload names ``folder=ws/{workspace_id}``. The workspace
  id is validated as a UUID before the folder string is composed: D28's
  isolation is only as strong as the prefix, and an id carrying path syntax
  (``../``, ``/``) would escape it. ``workspaces.id`` IS a UUID, so the guard
  refuses nothing legitimate.
- **FC-3.2 (amended by D38)** — delivery URLs are SIGNED and NON-EXPIRING:
  ``sign_url=True`` and deliberately no ``auth_token``/``expires_at``. The
  time-limit property attaches to the ASSET (FC-3.5 destroy-on-post, FC-3.6
  hard-TTL sweep); destruction 404s every URL regardless of signature, which
  is why :meth:`destroy` passes ``invalidate=True`` — a CDN-cached copy that
  survives destruction would quietly extend the lifetime D38 caps.
- **FC-3.3** — ``type=authenticated`` on upload, delivery, destroy AND the
  sweep listing. The type is part of the asset's identity at Cloudinary: a
  destroy or listing that omits it targets the public namespace and silently
  misses every transit asset.
- **FC-3.4 (mechanism per D28)** — server-signed per-request upload
  parameters, zero preset objects. The SDK signs each request from the
  credentials passed PER CALL (never read from ``cloudinary.config()``), and
  no ``upload_preset`` exists anywhere.
- **FC-3.5 / FC-3.6** — :meth:`destroy` is the pipeline's inline
  reap-on-success door; :meth:`list_stale` + :meth:`destroy_asset` are the
  ``lister``/``deleter`` pair `scheduler.execute_reap_transit_assets`
  (merged at L.7) injects. Discovery is one GLOBAL paged walk over the
  ``ws/`` prefix per provider resource_type — never per-workspace listings
  (`04`: the cost of discovery must not grow with the customer count).

## Seams, stated

The four SDK callables are injectable for the same reason the reconciler's
``poll`` and the reap executor's ``lister``/``deleter`` are: the properties
under test are THIS module's parameter sets, and a real provider client would
make the gate a network test. The defaults bind to the real
``cloudinary.uploader``/``api``/``utils`` doors (pinned by an identity test).
Cloudinary is deliberately OFF the provider-ops permit rail (`02` §6): upload
and destroy are recoverable effects — a duplicate transit upload is a second
short-lived asset the sweep reaps, a missed destroy is caught by the sweep.

The SDK is synchronous; async methods run it via ``asyncio.to_thread``. These
calls do not ride the egress floor (the SDK owns its own HTTP), so the
transaction-per-checkpoint discipline for transit is the CALLER's to hold:
upload/destroy outside any open unit of work. The floor cannot see SDK
internals and will not catch a violation here — named rather than implied.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from src.exceptions.base import StorydumpError
from src.services.target.egress import TIMEOUT_CLASSES
from src.utils.datetime_utils import ensure_utc


#: The Instagram story frame (owner, 2026-09-10 — parity with the legacy
#: `cloud_storage.get_story_optimized_url`): 1080 × 1920, 9:16.
STORY_WIDTH = 1080
STORY_HEIGHT = 1920
#: The blur behind the picture: the legacy tier's value, kept.
STORY_BLUR = 2000
#: What Meta accepts for a story, by kind — the derivation's delivered format.
#: How long `ready` waits for the story frame to serve, per media kind (a
#: video frame derives in the background and takes longer), and how often it
#: asks. The worker's lease (90 s) is extended by the heartbeat meanwhile.
READY_BUDGET_S = {"image": 20.0, "video": 60.0}
READY_POLL_S = 1.0
STORY_FORMATS = {"image": "jpg", "video": "mp4"}


def story_transformation(transit_asset_ref: str, *, media_kind: str) -> list[dict]:
    """How Meta sees the asset: scaled to fit the story's frame and padded to
    9:16 over a blurred, filled copy of itself.

    An image uses the legacy chain — an UNDERLAY of the same asset filled to
    the frame and blurred, the picture scaled to the width and padded over
    it: blurred bars above and below for a landscape or square picture,
    left and right for one taller than 9:16 (`c_pad` never crops). The asset
    is `authenticated`, so the layer names it as such and the whole signed
    URL authorises the layer (Cloudinary: an authenticated layer needs the
    signed URL, no separate signature). A video takes Cloudinary's own
    blurred-background padding — NEW behaviour, not parity: the legacy tier
    framed images only and sent videos raw.
    """
    if media_kind not in STORY_FORMATS:
        raise ValueError(f"unknown media_kind {media_kind!r}")
    if media_kind == "video":
        return [
            {"crop": "limit", "width": STORY_WIDTH, "height": STORY_HEIGHT},
            {
                "crop": "pad",
                "width": STORY_WIDTH,
                "height": STORY_HEIGHT,
                "background": f"blurred:{STORY_BLUR}:15",
            },
        ]
    underlay = "authenticated:" + transit_asset_ref.replace("/", ":")
    return [
        # FIRST fit the picture into the frame. A layer's canvas is the base's
        # size, so a 4032 × 3024 phone photo behind a 1080 × 1920 underlay
        # would hide the underlay entirely and the pad below would fall back
        # to white bars — the legacy chain had exactly that defect (review of
        # #1283, verified on Cloudinary's demo cloud).
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


#: media_items.media_kind (`ck_media_kind`) → Cloudinary resource_type.
_RESOURCE_TYPES = {"image": "image", "video": "video"}

#: Time bound for EVERY SDK door (upload, destroy, listing), seconds. The SDK
#: is not behind the egress floor, so the floor's 'upload' class rides here —
#: SOURCED from it, not copied, so retuning the floor cannot silently orphan
#: this module. Applied to all three doors because the rationale is door-
#: independent: a hung destroy stalls the pipeline's terminal step inside its
#: 120 s lease, a hung listing stalls the FC-3.6 sweep tick. `05` carries no
#: Cloudinary-specific row — flagged in #915 rather than invented.
DEFAULT_SDK_TIMEOUT_S = TIMEOUT_CLASSES["upload"]

#: One provider listing page per call, the API maximum — fewer round trips
#: per sweep (the legacy module pages at this size too).
_PAGE_SIZE = 500

#: The provider resource_types one sweep walks — deduped once, not per call
#: (a future media_kind mapping onto an existing resource_type must not add
#: a duplicate full paged walk).
_SWEEP_RESOURCE_TYPES = tuple(sorted(set(_RESOURCE_TYPES.values())))


class TransitDestroyRefused(StorydumpError):
    """The provider answered a destroy politely with a non-ok result. Raised
    by the sweep's deleter so `execute_reap_transit_assets` counts only
    completed destroys — a refusal silently counted as reaped would overstate
    the sweep and strand the asset uncounted."""


class TransitStore:
    """FC-3 transit assets: workspace-scoped signed upload, signed
    non-expiring delivery, typed destroy, and the FC-3.6 stale walk."""

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
        url_fn: Optional[Callable[..., tuple]] = None,
        probe_fn: Optional[Callable[[str], Any]] = None,
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
        # Per-call credentials (D28): the SDK signs each request from THESE,
        # never from process-global ``cloudinary.config()``. Splatted at every
        # call site, which copies into the call's kwargs.
        self._credentials = {
            "cloud_name": cloud_name,
            "api_key": api_key,
            "api_secret": api_secret,
        }
        self._sdk_timeout_s = sdk_timeout_seconds
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

        if None in (upload_fn, destroy_fn, resources_fn, url_fn):
            import cloudinary.api
            import cloudinary.uploader
            import cloudinary.utils

            upload_fn = upload_fn or cloudinary.uploader.upload
            destroy_fn = destroy_fn or cloudinary.uploader.destroy
            resources_fn = resources_fn or cloudinary.api.resources
            url_fn = url_fn or cloudinary.utils.cloudinary_url
        self._upload_fn = upload_fn
        self._destroy_fn = destroy_fn
        self._resources_fn = resources_fn
        self._url_fn = url_fn
        #: `ready`'s probe: `url -> (status, content_type)`; the default is an
        #: egress-floored GET of the first bytes (tests inject a script).
        self._probe_fn = probe_fn or self._default_probe
        self._probe_client = None

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
        """``ws/{workspace_id}`` (FC-3.1) — parsed, canonical form.

        The PARSED UUID is interpolated, never the raw string: ``uuid.UUID``
        accepts uppercase/braced/urn: spellings, and composing from the raw
        input would split one workspace across sibling folders."""
        try:
            parsed = uuid.UUID(str(workspace_id))
        except (ValueError, AttributeError, TypeError):
            raise ValueError(
                f"not a workspace_id: {workspace_id!r} — the FC-3.1 folder "
                "prefix accepts only a UUID (workspaces.id)"
            ) from None
        return f"ws/{parsed}"

    # -- FC-3.1 / 3.3 / 3.4: the upload ----------------------------------------

    async def upload(
        self, content: bytes, *, workspace_id: str, media_kind: str
    ) -> str:
        """Upload one transit asset; returns the provider public id — the
        value the pipeline persists as ``post_intents.transit_asset_ref``.

        Call OUTSIDE any open unit of work (checkpoint discipline; the module
        docstring names why the floor cannot enforce it here).
        """
        folder = self._workspace_folder(workspace_id)
        resource_type = self._resource_type(media_kind)
        # The public id is minted HERE, not by Cloudinary, so the story frame
        # can be derived eagerly in the same upload (the underlay names the
        # asset itself). Investigation of 2026-09-11: Meta fetches the frame
        # within a second of the container call; a frame derived on demand
        # took 2.4–3.1 s to first byte, and the first fetch of a fresh asset
        # sometimes answered with an error image — 4 of 7 publishes lost to
        # "could not be fetched" (9004/2207052). Eager: 0.1–0.25 s.
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
            # (#1276 review). One type, the cause preserved.
            raise TransitError(
                f"transit upload failed: {type(exc).__name__}: {exc}"
            ) from exc
        return result["public_id"]

    # -- readiness (2026-09-11 investigation) -----------------------------------

    async def ready(
        self,
        transit_asset_ref: str,
        *,
        media_kind: str,
        budget_s: Optional[float] = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> bool:
        """Whether the story frame at :meth:`delivery_url` serves as media
        NOW — the check the pipeline makes before it hands Meta the URL.
        Polls every `READY_POLL_S` until the probe answers 200/206 with an
        image or video content type, or *budget_s* is spent. A probe error
        is "not yet", never an exception: the caller's ladder decides."""
        url = self.delivery_url(transit_asset_ref, media_kind=media_kind)
        if budget_s is None:
            budget_s = READY_BUDGET_S.get(media_kind, READY_BUDGET_S["image"])
        deadline = self._now_fn() + timedelta(seconds=budget_s)
        while True:
            try:
                status, content_type = await self._probe_fn(url)
            except Exception as exc:  # noqa: BLE001 — a probe failure is "not yet"
                status, content_type = 0, f"probe failed: {type(exc).__name__}"
            if status in (200, 206) and str(content_type or "").lower().startswith(
                ("image/jpeg", "image/png", "video/")
            ):
                return True
            if self._now_fn() >= deadline:
                return False
            await sleep(READY_POLL_S)

    async def _default_probe(self, url: str) -> tuple[int, str]:
        """One egress-floored HEAD of the delivery URL — status and content
        type, no body (a video frame can be tens of megabytes and the floor
        caps bodies). The floor owns redirects (it refuses cross-host hops);
        the host allow-list is the URL's own, which the SDK built from our
        cloud name, and the private-address block still guards it."""
        from urllib.parse import urlparse

        from src.services.target import egress

        host = urlparse(url).hostname or ""
        policy = egress.EgressPolicy(
            timeout_class="standard",
            total_budget_s=float(TIMEOUT_CLASSES["standard"]),
            max_attempts=1,
            allowed_hosts=frozenset({host}),
        )
        if self._probe_client is None:
            import httpx

            self._probe_client = httpx.AsyncClient()
        response = await egress.request(self._probe_client, "HEAD", url, policy=policy)
        return int(response.status_code), str(response.headers.get("content-type", ""))

    # -- FC-3.2 (D38): delivery ------------------------------------------------

    def delivery_url(self, transit_asset_ref: str, *, media_kind: str) -> str:
        """The signed, non-expiring delivery URL Meta pulls the asset from —
        framed for a story (:func:`story_transformation`): the signature
        covers the transformation, so the framed derivation is the only one
        an authenticated asset serves."""
        url, _options = self._url_fn(
            transit_asset_ref,
            transformation=story_transformation(
                transit_asset_ref, media_kind=media_kind
            ),
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

    # -- FC-3.5 / FC-3.6: destruction ------------------------------------------

    async def destroy(self, transit_asset_ref: str, *, media_kind: str) -> bool:
        """Destroy one transit asset; True iff the asset is gone NOW.

        'not found' counts as gone — a prior destroy or the sweep won, and the
        goal state (no asset, every URL 404) holds. Any other result is False;
        the FC-3.6 sweep is the retry.
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
        result = await asyncio.to_thread(
            self._destroy_fn,
            public_id,
            type="authenticated",
            resource_type=resource_type,
            invalidate=True,
            timeout=self._sdk_timeout_s,
            **self._credentials,
        )
        return result.get("result") in ("ok", "not found")

    # -- FC-3.6: discovery -----------------------------------------------------

    async def list_stale(self, *, older_than_seconds: int) -> list[dict[str, Any]]:
        """Every transit asset older than the TTL — one GLOBAL paged walk over
        the ``ws/`` prefix per resource_type, filtered by ``created_at``.

        Rows carry exactly what :meth:`destroy_asset` needs. A row whose
        ``created_at`` cannot be parsed is treated as STALE, not skipped: the
        sweep is the backstop, and an unparseable age must fail toward
        reaping a transit asset, never toward immortalizing one.
        """
        cutoff = self._now_fn() - timedelta(seconds=older_than_seconds)
        stale: list[dict[str, Any]] = []
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
                    if self._is_stale(row.get("created_at"), cutoff):
                        stale.append(
                            {
                                "public_id": row["public_id"],
                                "resource_type": resource_type,
                            }
                        )
                cursor = page.get("next_cursor")
                if not cursor:
                    break
        return stale

    @staticmethod
    def _is_stale(created_at: Optional[str], cutoff: datetime) -> bool:
        """Fail-toward-stale is a ROW-level rule: only a genuinely garbled
        timestamp reaps early. The parse is general ISO-8601 (fractional
        seconds, explicit offsets), NOT one frozen format string — under a
        strict single-format parse a benign provider format shift would read
        the ENTIRE corpus as stale on the next tick and destroy in-flight
        transit media mid-publish; the backstop must not be the outage."""
        if not created_at:
            return True
        try:
            created = ensure_utc(
                datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            )
        except ValueError:
            return True
        return created < cutoff
