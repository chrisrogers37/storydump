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
        result = await asyncio.to_thread(
            self._upload_fn,
            content,
            folder=folder,
            type="authenticated",
            resource_type=resource_type,
            overwrite=False,
            timeout=self._sdk_timeout_s,
            **self._credentials,
        )
        return result["public_id"]

    # -- FC-3.2 (D38): delivery ------------------------------------------------

    def delivery_url(self, transit_asset_ref: str, *, media_kind: str) -> str:
        """The signed, non-expiring delivery URL Meta pulls the asset from."""
        url, _options = self._url_fn(
            transit_asset_ref,
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
