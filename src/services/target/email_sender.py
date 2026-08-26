"""X.3 — the `EmailSender` port (`07` §1) and the `send_email` executor.

The out-of-band channel for a **web-born** workspace. `06` §3 routes
notifications to "the workspace's bindings", and #1092 established what that
means in practice: `push_bindings` selects `channel_bindings` rows and **nothing
in the tier writes that table**, so for the default workspace shape every
notification we produce lands nowhere. Email is the channel the plan already
chose, and the address already exists — `users.primary_email` is filled from the
**verified** Google claim at every sign-in, so we hold a deliverable address for
every account that has ever signed in.

## What this module is, and what it is not

It is the transport and the job that drains it. It is **not** a producer: nothing
here decides that an email should be sent. The producers (invitations — `06` §2;
the timeliness halves of D3/D4 — `06` §5) enqueue `send_email` jobs and this
executes them.

**It does not know about jobs, deliberately.** :class:`ResendSender` is a plain
callable, so a caller that must not depend on the jobs table — operator alerting
for an outage that has broken the jobs table is the live case (#1099) — can call
`send()` directly and synchronously. The executor below is one caller of the
port, not the port itself. That split is the reason this is two objects rather
than one.

## The provider is a decision that has not been made

`07` §1 names Resend as the default and says outright that "a new external
service is a flagged decision, not an assumption: the owner ack is OPEN ... the
port keeps the provider swappable until it lands." So this ships **inert without
credentials**: :func:`sender_from_env` returns None when they are absent, the
registry parks `send_email` with a reason naming exactly what is missing. The day
the ack lands it is a config change, not a build — which is the whole point of
writing the port before the provider.

**Where that reason is visible is narrower than it looks, and an earlier draft of
this docstring had it backwards.** The worker's `live_kinds` line lists kinds with
NO parked reason (`worker.py`: `if not hasattr(e, "reason")`), so a parked kind is
ABSENT from it rather than named in it, and the reason string itself first reaches
a log only when a job of the kind is claimed — which, for a kind with no producer,
is never. What an operator can read today is the absence.

It is inert, never a silent no-op. A sender that accepted sends and dropped them
would report success for mail nobody received, which is worse than not having a
channel at all: it converts a visible gap into an invisible one.

## Egress: a narrower policy, not a wider default

`egress.DEFAULT_ALLOWED_HOSTS` is a closed set whose own comment calls it "the
load-bearing control until #871 lands". This module therefore declares its own
single-host `allowed_hosts` on its policy rather than widening the shared
default: the module that talks to the provider is the only thing that gains reach
to it, and swapping providers touches one file. Widening the global set would
hand every other caller the same reach for no reason.

## Budget

`05`: provider-wide **90/day** on the `email_global` scope, key `''` — headroom
under the free tier's 100/day hard pause, which pauses *sending* rather than
erroring, and would strand invitation delivery exactly during a
cohort-onboarding burst. Over budget the job **defers** (`05`: "the job defers on
its retry schedule") rather than failing, and the deferral **restores the
attempt** the claim consumed, because `jobs.reschedule_job` records the rule: a
deferral is normal operation, not a failure, and the attempts budget is R8's
retryable-failure budget.

**The debit commits BEFORE the provider call, and that is forced rather than
chosen.** `egress.request` refuses to run inside an open transaction — `02` §5's
transaction-per-checkpoint rule, enforced in code — so the send cannot share a
transaction with anything. The executor therefore owns its own transactions and
the loop's session is only the finalization context, which is the shape the
publish pipeline already uses for the same reason.

Two consequences, one of them a real cost:

- **A failed send DOES consume a budget slot.** The debit is committed by the
  time the provider is called, so a send that fails and retries spends two. That
  is the conservative direction — the ceiling exists to stay under a provider
  pause, and over-counting keeps us under it — but it is a cost, not a
  neutrality, and a burst of provider failures will reach the ceiling faster
  than the send count suggests.
- **An over-budget check costs nothing.** `rate_counters.increment`'s
  `WHERE rc.count < :limit` means the refused hit never increments, so a job
  that defers has not spent anything.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.exceptions.base import RefusalError
from src.services.target import egress, jobs, rate_counters
from src.services.target.egress import EgressPolicy
from src.services.target.unit_of_work import apply_gucs

logger = logging.getLogger(__name__)

#: The provider `07` §1 names as the default. One host, declared here rather
#: than in the shared allowlist — see the module docstring.
PROVIDER_HOST = "api.resend.com"
SEND_URL = f"https://{PROVIDER_HOST}/emails"

#: This module's own reach, not the shared default's — see the module docstring.
DEFAULT_POLICY = EgressPolicy(
    timeout_class="standard", allowed_hosts=frozenset({PROVIDER_HOST})
)

#: `02` §6 counter coordinates and the `05` ceiling. The key is `''` because the
#: budget is provider-wide: one bucket for the whole deployment, not per tenant.
BUDGET_SCOPE = "email_global"
BUDGET_KEY = ""
BUDGET_LIMIT = 90
BUDGET_WINDOW_SECONDS = 24 * 3600

#: `05`: 3 attempts, backoff 1/5/15 min. Indexed by the attempt already
#: consumed; past the end the last rung repeats.
RETRY_LADDER_SECONDS = (60, 300, 900)


class EmailRefused(RefusalError):
    """A send this tier will not make. ``reason`` is a closed vocabulary —
    unknown_template | template_params_missing | payload_malformed |
    recipient_invalid | provider_rejected | provider_response_malformed — and
    no recipient address or provider body ever rides in the message."""

    _prefix = "email refused"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


#: `template` → (subject, body) builders over the payload's `params`.
#:
#: A CLOSED set, and unknown names are refused by name rather than rendered as
#: something generic: a send is an outbound message to a real person, and the
#: failure mode of a permissive renderer is an email that goes out saying the
#: wrong thing. The registry starts at the one template `06` §2 specifies
#: params for; producers that need another add it here with their producer.
def _invitation(params: Mapping[str, Any]) -> tuple[str, str]:
    workspace = _required(params, "workspace_name")
    accept_url = _required(params, "accept_url")
    inviter = params.get("inviter_name")
    opener = (
        f"{inviter} has invited you"
        if isinstance(inviter, str) and inviter.strip()
        else "You have been invited"
    )
    return (
        f"{opener} to {workspace} on Storydump",
        f"{opener} to join the workspace {workspace} on Storydump.\n\n"
        f"Accept the invitation:\n{accept_url}\n\n"
        "If you were not expecting this, you can ignore this message — "
        "nothing happens until you accept.\n",
    )


TEMPLATES = {"invitation": _invitation}


def _required(params: Mapping[str, Any], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EmailRefused("template_params_missing", name)
    return value.strip()


def render(template: object, params: object) -> tuple[str, str]:
    """`(subject, body)` for a payload's template, or a NAMED refusal."""
    if not isinstance(template, str) or template not in TEMPLATES:
        raise EmailRefused("unknown_template", str(template))
    if not isinstance(params, Mapping):
        raise EmailRefused("template_params_missing", "params must be an object")
    return TEMPLATES[template](params)


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


@dataclass
class ResendSender:
    """`send(to, subject, body) -> provider_message_ref`, over the egress floor.

    Holds no session and takes no job: this is the transport, and the reason it
    is separable is that an operator-alerting caller must be able to reach it
    when the jobs table is exactly what is broken (#1099).
    """

    api_key: str
    sender: str
    #: Injected by tests so the egress floor runs for real against a fake
    #: transport — the `TelegramTransport` convention. None means own one per
    #: send, which is right for a job that sends at single-digits-per-day.
    client: Optional[httpx.AsyncClient] = None
    #: Override for the floor policy below. The seam `GoogleDriveAdapter` uses,
    #: and it exists for a specific cost: the SSRF guard resolves every
    #: allowlisted host BEFORE the request, so a test that fakes only the
    #: transport still performs a live `getaddrinfo` for the provider host —
    #: real network in a unit test, and slow. `EgressPolicy.without(
    #: enforce_private_address_block=False)` skips it; the host allowlist is
    #: checked first and still applies.
    policy: Optional[EgressPolicy] = None

    async def send(self, *, to: str, subject: str, body: str) -> str:
        if not isinstance(to, str) or "@" not in to:
            raise EmailRefused("recipient_invalid")
        owned = self.client is None
        client = self.client or httpx.AsyncClient()
        try:
            response = await egress.request(
                client,
                "POST",
                SEND_URL,
                policy=self.policy or DEFAULT_POLICY,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "from": self.sender,
                    "to": [to],
                    "subject": subject,
                    "text": body,
                },
            )
        finally:
            if owned:
                await client.aclose()
        if response.status_code >= 400:
            # The body is not logged and not carried: a provider error body is
            # where a recipient address ends up, and this reason reaches logs.
            raise EmailRefused("provider_rejected", f"http_{response.status_code}")
        try:
            ref = (response.json() or {}).get("id")
        except ValueError:
            ref = None
        if not isinstance(ref, str) or not ref:
            # A 2xx with no id is a broken contract, not a success. Reporting it
            # as sent would record delivery for mail we cannot trace.
            raise EmailRefused("provider_response_malformed")
        return ref


def sender_from_env(env: Mapping[str, str]) -> Optional[ResendSender]:
    """The configured sender, or **None** when the provider is not wired.

    None is the honest answer and the registry turns it into a parked kind with
    a reason. Both values are required together: a key with no sender address
    cannot send, and returning a half-configured sender would fail at the
    provider instead of at composition, where it is readable.
    """
    api_key = (env.get("RESEND_API_KEY") or "").strip()
    sender = (env.get("EMAIL_FROM") or "").strip()
    if not api_key or not sender:
        return None
    return ResendSender(api_key=api_key, sender=sender)


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


@asynccontextmanager
async def system_session(engine):
    """One committed transaction for a TENANT-LESS job.

    Not `unit_of_work(...)`: that factory REFUSES an empty tenant id on purpose
    — `02` §7 RLS reads `app.tenant_id`, so a tenant-less unit would be a
    widened query, and L.0 makes that unrepresentable rather than discouraged.
    A `send_email` row genuinely has no tenant: it is a system kind under `02`
    §5's classing rule (payload-complete, zero tenant reads at execution) and
    `ck_jobs_system_kinds` is a biconditional, so `workspace_id` is NULL by
    constraint.

    So this applies the GUCs directly with the empty tenant id — the same
    convention `worker.make_session_for` uses for exactly these rows, and it is
    fail-closed: under any tenant policy an empty `app.tenant_id` matches
    nothing rather than everything.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        async with session.begin():
            await apply_gucs(session, tenant_id="", actor_kind="system")
            yield session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff_seconds(attempts: int) -> int:
    """The `05` ladder rung for a job that has already consumed *attempts*.

    No guard on the input: `jobs.attempts` is NOT NULL integer and reaches here
    unchanged from the claimed row. The repo's two other ladder sites
    (`publish_pipeline`, `reconciler`) are stricter still — one indexes bare,
    the other raises — and mapping a malformed producer to "retry in 60s" would
    convert a bug class into a silent retry.
    """
    return RETRY_LADDER_SECONDS[min(attempts, len(RETRY_LADDER_SECONDS) - 1)]


async def execute_send_email(
    job: Mapping[str, Any],
    *,
    sender,
    engine,
    now=_utcnow,
) -> Optional[str]:
    """Drain one `send_email` job. Returns the provider ref, or None if deferred.

    Takes **no session**. Every database touch here opens its own transaction
    off *engine* and commits it, because the provider call cannot
    legally happen inside one (`egress.request` raises on an open transaction,
    `02` §5) and the over-budget path rewrites the job row — which, done in the
    loop's session, would be rolled back by that session's own finalization
    failing on a row no longer leased.
    """
    payload = job.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise EmailRefused("payload_malformed")
    to = payload.get("to")
    if not isinstance(to, str) or "@" not in to:
        raise EmailRefused("recipient_invalid")
    subject, body = render(payload.get("template"), payload.get("params"))

    async with system_session(engine) as budget_session:
        counted = await rate_counters.increment(
            budget_session,
            scope=BUDGET_SCOPE,
            key=BUDGET_KEY,
            window_start=rate_counters.window_start(now(), BUDGET_WINDOW_SECONDS),
            limit=BUDGET_LIMIT,
        )
    if counted is None:
        run_at = now() + timedelta(seconds=backoff_seconds(job.get("attempts")))
        async with system_session(engine) as deferral:
            await jobs.reschedule_job(
                deferral,
                job["id"],
                job["lease_token"],
                run_at=run_at,
                # A deferral is normal operation, not a failure — the attempts
                # budget is R8's, and a daily ceiling would exhaust it in three
                # rungs of a ladder measured in minutes.
                restore_attempt=True,
            )
        logger.info(
            "send_email %s deferred: over the %s/day budget until %s",
            job.get("id"),
            BUDGET_LIMIT,
            run_at.isoformat(),
        )
        return None

    ref = await sender.send(to=to, subject=subject, body=body)
    # The ref, never the recipient: this line reaches logs.
    logger.info("send_email %s sent (provider ref %s)", job.get("id"), ref)
    return ref
