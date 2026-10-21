"""The archive's composition roots: the real object graph, built only when armed.

`build_worker` and `build_api` are what `src.worker` and `src.api.app` call once
at composition. Each asks `archive_activation.resolve` whether this process may
use the Instagram archive (it may wherever the HMAC key ring loads whole; there
is no record or switch) and logs its one line. Without the ring they return None
and the caller wires nothing, so the process stays on the fail-closed paths
every archive service already has. This module never builds a capability: it
passes on the one `archive_activation` returned.

Armed, they build the same graph the gates build by hand: the adapter, handed
the capture it serves and the request admission that re-checks that capture
around every provider call; the access provider, which decrypts the capture's
credential in a short tenant unit of work only after `check_source_read` has
passed; the sync, preparation and preview ports over them; and, for the worker,
the publish coordinator whose `selected_prepare` wrapper owns the bound-content
review route. Page size is explicit (25, not the adapter's 100).

The HTTP client is plain by the adapter's own rule (no credentials, cookies or
default parameters) and one per process; the caller closes it. Nothing here
connects: every provider call waits for a claimed job or a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import httpx

from src.services.target import (
    archive_activation,
    archive_preview,
    egress,
    instagram_graph,
    instagram_preparation,
    instagram_publish,
    instagram_sync,
    oauth_states,
    readers,
)
from src.services.target import source_read_admission as admission
from src.services.target.instagram_archive_adapter import (
    ArchiveAccess,
    InstagramArchiveAdapter,
)
from src.services.target.instagram_catalog import TombstoneKeys
from src.services.target.instagram_sources import ArchiveCapability
from src.services.target.media_adapters import MediaCredentialDead, MediaSourceFenceDrop
from src.services.target.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

#: The listing page: its budgets are written for 25, and the adapter's own
#: default is its ceiling of 100.
PAGE_SIZE = 25

#: The variable Railway sets on each service: the release whose run keys this
#: process's conversion and reconciliation jobs.
RELEASE_VAR = "RAILWAY_GIT_COMMIT_SHA"


@dataclass(frozen=True)
class WorkerArchive:
    """What an armed worker holds. `coordinator.selected_prepare` and
    `coordinator.fetch_selected` are the bound seams `WorkerDeps` and the card
    media fetch take; `preparation` is the object under them."""

    capability: ArchiveCapability
    keys: TombstoneKeys = field(repr=False)
    evidence: archive_activation.ArchiveActivationEvidence
    client: Any = field(repr=False)
    sync_ports: instagram_sync.ArchiveSyncPorts
    preparation: instagram_preparation.SelectedPreparation
    coordinator: instagram_publish.ArchivePipeline


@dataclass(frozen=True)
class ApiArchive:
    """What an armed API holds: the capability its routes read, the preview port
    behind the thumbnail route, and the client its lifespan closes."""

    capability: ArchiveCapability
    preview: archive_preview.ArchivePreview
    client: Any = field(repr=False)


def access_provider(engine, *, capture, capability, lock_timeout=None):
    """The adapter's `access_provider` for one capture: its account and token.

    The capture is re-checked and the credential it names read in ONE short
    tenant unit of work (`check_source_read` takes the source prefix, then the
    credential row is read under it); the token is decrypted after that closes.
    A capture the database no longer admits is a fence drop, never a read of a
    newer credential. The grant is handed back as the capture, which is what the
    request admission binds each call to, and with the app-scoped id its current
    authorization verified, when one is recorded: the proof of media ownership,
    read once for the turn.
    """

    async def access(source_id, *, workspace_id):
        if (
            str(source_id) != capture.source_id
            or str(workspace_id) != capture.workspace_id
        ):
            raise MediaSourceFenceDrop(reason="source_capture_mismatch")
        # Outside the transaction, and before it: a ring that cannot be built is
        # none of the credential's refusals (`ig_credentials.token_for_account`).
        ring = oauth_states.ring()
        try:
            async with unit_of_work(
                engine,
                capture.workspace_id,
                actor_kind="worker",
                lock_timeout=lock_timeout,
            ).begin() as session:
                await admission.check_source_read(
                    session, capture, capability=capability
                )
                # Whether it is the current, active, unexpired login is the
                # database's answer to `check_source_read` above, not asked here.
                row = await readers.row(
                    session,
                    "SELECT encrypted_payload FROM oauth_credentials"
                    " WHERE workspace_id = :ws AND id = :cid",
                    ws=capture.workspace_id,
                    cid=capture.credential_id,
                )
                # The app-scoped id this credential's current authorization
                # verified for this account, read with the credential, once per
                # turn: media ownership is judged against it, not a `/me` read
                # for each parent. None where nothing is recorded (an account
                # not yet reconciled), and ownership proves itself at `/me`.
                mapping = await readers.row(
                    session,
                    "SELECT subject_ref FROM meta_identity_mappings"
                    " WHERE workspace_id = :ws AND credential_id = :cid"
                    " AND is_current AND account_ref = :ref",
                    ws=capture.workspace_id,
                    cid=capture.credential_id,
                    ref=capture.account_ref,
                )
        except admission.SourceReadRefused as exc:
            raise MediaSourceFenceDrop(reason=exc.reason) from None
        try:
            token = ring.decrypt(row["encrypted_payload"])
        except Exception:  # noqa: BLE001 — no ring entry reads it; never echo why
            raise MediaCredentialDead("archive credential cannot be read") from None
        return ArchiveAccess(
            capture.account_ref,
            token,
            captured_authorization=capture,
            app_scoped_id=mapping["subject_ref"] if mapping else None,
        )

    return access


def adapter_factory(
    engine,
    *,
    capability,
    client,
    graph_version,
    metadata_only,
    page_size,
    policy,
):
    """`build(capture, admission_provider, lock_timeout=None)`: one adapter for
    one capture. The admission provider is the caller's, so a sync's carries its
    job and a preview's its lock timeout; the adapter refuses to exist without
    one. *graph_version* is the application's shared version
    (`instagram_graph.graph_version`), the value the publish adapter gets too."""

    def build(capture, admission_provider, *, lock_timeout=None):
        return InstagramArchiveAdapter(
            access_provider=access_provider(
                engine,
                capture=capture,
                capability=capability,
                lock_timeout=lock_timeout,
            ),
            client=client,
            graph_version=graph_version,
            policy=policy,
            page_size=page_size,
            admission_provider=admission_provider,
            metadata_only=metadata_only,
        )

    return build


async def _unused(*args, **kwargs):  # pragma: no cover - the probe never calls it
    raise AssertionError("the configuration probe made a request")


def _refuse_bad_configuration(client, graph_version, policy) -> None:
    """Build one adapter to throw away, so the constructor's own checks (the
    shared Graph version, a plain client) refuse at composition, by name, rather
    than at the first claimed job. An armed process with such a value is
    misconfigured, and booting it half-working is the worse failure (the key
    ring's is refused at boot the same way)."""
    InstagramArchiveAdapter(
        access_provider=_unused,
        client=client,
        graph_version=graph_version,
        policy=policy,
        page_size=PAGE_SIZE,
        admission_provider=_unused,
    )


def build_worker(
    *,
    engine,
    env: Mapping[str, str],
    client: Optional[httpx.AsyncClient] = None,
    policy: Optional[egress.EgressPolicy] = None,
) -> Optional[WorkerArchive]:
    """The worker's archive, or None. Logs the activation line either way.

    *client* and *policy* are the test seams (a mock transport and a policy that
    allows its host); production passes neither."""
    activation = archive_activation.resolve(env, release_sha=env.get(RELEASE_VAR))
    logger.info(activation.log_line)
    capability = activation.capability
    if capability is None:
        return None
    client = client if client is not None else httpx.AsyncClient()
    graph_version = instagram_graph.graph_version(env)
    _refuse_bad_configuration(client, graph_version, policy)

    def factory(metadata_only):
        return adapter_factory(
            engine,
            capability=capability,
            client=client,
            graph_version=graph_version,
            metadata_only=metadata_only,
            page_size=PAGE_SIZE,
            policy=policy,
        )

    metadata, content = factory(True), factory(False)

    async def prepare_sync(capture, admission_provider):
        return metadata(capture, admission_provider)

    async def prepare_selected(capture, config):
        # The selected-post fetch has no job: its admission is the capability's.
        return content(
            capture, admission.admission_provider(engine, capability=capability)
        )

    preparation = instagram_preparation.SelectedPreparation(
        instagram_preparation.ArchivePreparationPorts(
            capability, prepare_selected, activation.keys
        )
    )
    coordinator = instagram_publish.ArchivePipeline(
        capability=capability,
        fetch_selected=preparation.fetch_selected,
        prepare_selected=preparation.selected_prepare,
    )
    return WorkerArchive(
        capability=capability,
        keys=activation.keys,
        evidence=activation.evidence,
        client=client,
        sync_ports=instagram_sync.ArchiveSyncPorts(
            capability=capability,
            tombstone_keys=activation.keys,
            prepare_adapter=prepare_sync,
        ),
        preparation=preparation,
        coordinator=coordinator,
    )


def build_api(
    *,
    engine,
    env: Mapping[str, str],
    client: Optional[httpx.AsyncClient] = None,
    policy: Optional[egress.EgressPolicy] = None,
) -> Optional[ApiArchive]:
    """The API's archive, or None. Logs the activation line either way.

    The preview port's admission is the one the port hands each call, which
    carries its lock timeout, and the access provider waits no longer than it."""
    activation = archive_activation.resolve(env, release_sha=env.get(RELEASE_VAR))
    logger.info(activation.log_line)
    capability = activation.capability
    if capability is None:
        return None
    client = client if client is not None else httpx.AsyncClient()
    graph_version = instagram_graph.graph_version(env)
    _refuse_bad_configuration(client, graph_version, policy)
    content = adapter_factory(
        engine,
        capability=capability,
        client=client,
        graph_version=graph_version,
        metadata_only=False,
        page_size=PAGE_SIZE,
        policy=policy,
    )

    async def prepare_preview(capture, admission_provider):
        return content(
            capture,
            admission_provider,
            lock_timeout=archive_preview.PREVIEW_LOCK_TIMEOUT,
        )

    return ApiArchive(
        capability=capability,
        preview=archive_preview.ArchivePreview(
            archive_preview.ArchivePreviewPorts(capability, prepare_preview)
        ),
        client=client,
    )
