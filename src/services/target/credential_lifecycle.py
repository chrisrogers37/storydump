"""W5d/W5e — the credential lifecycle executors (#942, `02` §5 + D31).

Two executors and one provider door. `refresh_credential` consumes the clock's
weekly mints: load the token, ask the provider for a fresh one, swap in place.
`reauth_prompt` consumes the 062 cadence leg's mints: tell the human, through
the workspace's own bindings, that posting for an account is paused until they
reconnect. Between them, a credential can no longer die silently — which was
the dated consequence of these kinds having no executor (`work_loop`'s parked
lanes accumulated exactly this work).

## Transaction discipline (`02` §5)

"A database transaction never spans a provider call." The refresh executor is
three phases — load+commit, provider call, write+commit — each phase on its
own transaction from :func:`work_loop.poller_session_factory` (the same shape
`deliver_outbox` uses: the lane's session is for the job row; the work rides
tenant-GUC'd sessions of its own). The egress floor enforces this structurally:
a provider call inside an open UoW transaction raises, so getting this wrong
here is loud, not latent.

## The failure taxonomy is D31's, and it routes three ways

- **Definitive auth-class rejection** (:class:`RefreshRejected` — the provider
  ANSWERED and the answer is "this credential is bad"): `mark_dead` performs
  both flips in one transaction — credential → ``expired``, account →
  ``reauth_required``. No inline prompt mint: the 062 clock leg is the single
  producer of `reauth_prompt` jobs and picks the account up on its next tick
  (marker NULL = immediately eligible), so prompting has exactly one door.
- **Transient** (network faults, 429, 5xx — no definitive answer): raise, and
  the lane's ladder retries with the attempt consumed. Never `mark_dead` on a
  fault: D31 is explicit that transient classes must not take the liveness
  edge.
- **Undecryptable** (the ring cannot read the stored payload):
  :func:`ig_login_oauth.load_credential` already performed and committed the
  dead flips before raising; the job's work is therefore DONE — the executor
  returns success rather than retrying a payload that cannot become readable.

The token never appears in any raise, log, or error detail, on any path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import text

from src.exceptions.base import StorydumpError
from src.services.target import egress, outbox
from src.services.target import ig_login_oauth as oauth

logger = logging.getLogger(__name__)


class RefreshRejected(StorydumpError):
    """The provider definitively rejected the credential (auth-class answer).

    Carries the HTTP status only — never the token, never the response body
    (a provider error body can echo request parameters back).
    """

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"provider rejected the credential (HTTP {status_code})")


class RefreshTransient(StorydumpError):
    """No definitive answer (429/5xx). The lane's ladder owns the retry."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"transient refresh failure (HTTP {status_code})")


#: Auth-class statuses on the refresh endpoint. 400 is included deliberately:
#: the request shape is fixed (two params built by `refresh_params`), so a 400
#: here can only be about the token. 429 and 5xx are transient by D31.
_REJECTED_STATUSES = frozenset({400, 401, 403})


async def ig_refresh(
    token: str, *, client: Optional[httpx.AsyncClient] = None
) -> tuple[str, Optional[datetime]]:
    """The real refresh door: `graph.instagram.com/refresh_access_token`.

    Returns ``(new_token, expires_at)``. A per-call client is deliberate —
    refreshes run at a weekly cadence per credential, so pooling would manage
    a connection that is nearly always idle. *client* exists for tests.
    """
    params = oauth.refresh_params(token)
    policy = egress.EgressPolicy(timeout_class="fast")
    if client is not None:
        resp = await egress.request(
            client, "GET", oauth.REFRESH_URL, policy=policy, params=params
        )
    else:
        async with httpx.AsyncClient() as own:
            resp = await egress.request(
                own, "GET", oauth.REFRESH_URL, policy=policy, params=params
            )
    if resp.status_code == 200:
        body = resp.json()
        new_token = body.get("access_token")
        if not new_token:
            # A 200 with no token is not an answer; retry, never mark dead.
            raise RefreshTransient(200)
        expires_at = None
        if body.get("expires_in") is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=int(body["expires_in"])
            )
        return new_token, expires_at
    if resp.status_code in _REJECTED_STATUSES:
        raise RefreshRejected(resp.status_code)
    raise RefreshTransient(resp.status_code)


async def refresh_credential(deps, session, job) -> str:
    """Executor for the clock's `refresh_credential` mints (`02` §5 :1162)."""
    from src.services.target.work_loop import poller_session_factory

    payload = job.get("payload") or {}
    credential_id = str(payload["credential_id"])
    factory = poller_session_factory(deps.engine, str(job["workspace_id"]))

    # Phase 1 — load, own transaction, committed before any provider talk.
    try:
        async with factory() as s:
            token = await oauth.load_credential(s, credential_id=credential_id)
    except oauth.CredentialUndecryptable:
        # load_credential already flipped credential+account and committed;
        # retrying cannot make the payload readable. The job is done.
        logger.warning(
            "refresh_credential %s: credential %s undecryptable — dead flips"
            " already committed by load_credential",
            job["id"],
            credential_id,
        )
        return "undecryptable"
    except oauth.OAuthStateRefused:
        logger.warning(
            "refresh_credential %s: credential %s has no row — nothing to refresh",
            job["id"],
            credential_id,
        )
        return "missing"

    # Phase 2 — the provider call, outside any transaction (floor-enforced).
    try:
        new_token, expires_at = await deps.refresh(token)
    except RefreshRejected as exc:
        # Phase 3b — D31's liveness edge: both flips, one transaction. The
        # 062 clock leg mints the reauth prompt on its next tick.
        async with factory() as s:
            await oauth.mark_dead(s, credential_id=credential_id)
            await s.commit()
        logger.warning(
            "refresh_credential %s: credential %s definitively rejected"
            " (HTTP %s) — marked dead, account flipped to reauth_required",
            job["id"],
            credential_id,
            exc.status_code,
        )
        return "rejected"

    # Phase 3a — swap in place (same row id, `07` §2: no zero-credential
    # window). The next tick's mint re-arms next_refresh_at.
    async with factory() as s:
        await oauth.swap_credential(
            s, credential_id=credential_id, token=new_token, expires_at=expires_at
        )
        await s.commit()
    return "refreshed"


async def reauth_prompt(deps, session, job) -> str:
    """Executor for the 062 cadence leg's mints (`02` §5 :1165, `06` §5).

    Enqueues one `notification` outbox row per push binding. No provider call
    happens here — the sender delivers; this executor only writes rows, so a
    single transaction covers the check and the enqueue together.
    """
    from src.services.target import prompts
    from src.services.target.work_loop import poller_session_factory

    payload = job.get("payload") or {}
    account_id = str(payload["ig_account_id"])
    factory = poller_session_factory(deps.engine, str(job["workspace_id"]))

    async with factory() as s:
        row = (
            (
                await s.execute(
                    text(
                        "SELECT state, workspace_id, provider_account_ref"
                        " FROM ig_accounts WHERE id = :a"
                    ),
                    {"a": account_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            logger.warning(
                "reauth_prompt %s: account %s has no row", job["id"], account_id
            )
            return "missing"
        if row["state"] != "reauth_required":
            # Reconnected (or moved on) between mint and execution — a stale
            # prompt must not fire. Success: the state machine already won.
            return "stale"
        bindings = await prompts.push_bindings(s, str(row["workspace_id"]))
        if not bindings:
            # No surface to say it on. The marker was stamped at mint, so the
            # cadence re-prompts next week — by then a binding may exist.
            logger.warning(
                "reauth_prompt %s: workspace %s has no push binding — nothing enqueued",
                job["id"],
                row["workspace_id"],
            )
            return "no-surface"
        body = (
            f"⚠️ Instagram connection for {row['provider_account_ref']} needs"
            " re-authorization. Posting for this account is paused until you"
            " reconnect it from the dashboard."
        )
        for binding_id in bindings:
            await outbox.enqueue(
                s,
                workspace_id=str(row["workspace_id"]),
                binding_id=binding_id,
                kind="notification",
                payload={"v": 1, "text": body},
            )
        await s.commit()
    return "prompted"
