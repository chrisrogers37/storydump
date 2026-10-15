"""The `EmailSender` port and the `send_email` executor.

The out-of-band channel for a **web-born** workspace. Notifications route to
the workspace's bindings (#1092): `push_bindings` selects `channel_bindings`
rows, and a workspace that has never bound a Telegram group has none — so
every notification we produce for it lands nowhere. Email is the other
channel, and the address already exists — `users.primary_email` is filled
from the **verified** Google claim at every sign-in, so we hold a deliverable
address for every account that has ever signed in.

(`bindings.bind` writes that table, reached from the `/start` door's `bind-`
lane (#1172): a binding is an explicit act a web-born workspace need never
perform, so the default shape has no push channel.)

## What this module is, and what it is not

It is the transport and the job that drains it. It is **not** a producer: nothing
here decides that an email should be sent. An invitation is sent through the port
directly, after the invitation commits (`invitations.InvitationEmail`): a
queued job would hold the raw token in `jobs.payload`, and the
token reaches the database only as its SHA-256. `send_email` jobs serve
producers whose payload carries no credential: the activation nudge (#1481),
and admissions from the waitlist (#1723). An admission's email carries a
one-time claim link, and its payload carries only the link's base: the
executor draws the token as it sends, and stores its hash only once the
provider has taken the email (:func:`execute_send_email`).

**It does not know about jobs, deliberately.** :class:`ResendSender` is a plain
callable, so a caller that must not depend on the jobs table — operator alerting
for an outage that has broken the jobs table is the live case (#1099) — can call
`send()` directly and synchronously. The executor below is one caller of the
port, not the port itself. That split is the reason this is two objects rather
than one.

## The provider: Resend

Resend is the default provider, and email is the transport for invitations.
It ships **inert without credentials**: :func:`sender_from_env` returns
None when they are absent, the API answers an invitation's delivery as
`not_configured`, and the worker's registry parks `send_email` with a reason
naming exactly what is missing. Turning it on is a config change, not a build.

**Where that reason is visible is narrower than it looks.**
The worker's `live_kinds` line lists kinds with
NO parked reason (`worker.py`: `if not hasattr(e, "reason")`), so a parked kind is
ABSENT from it rather than named in it, and the reason string itself first reaches
a log only when a job of the kind is claimed — which, for a kind with no producer,
is never. What an operator can read is the absence.

It is inert, never a silent no-op. A sender that accepted sends and dropped them
would report success for mail nobody received, which is worse than not having a
channel at all: it converts a visible gap into an invisible one.

## Egress: a narrower policy, not a wider default

`egress.DEFAULT_ALLOWED_HOSTS` is a closed set whose own comment calls it "the
load-bearing control". This module therefore declares its own single-host
`allowed_hosts` on its policy rather than widening the shared default: the
module that talks to the provider is the only thing that gains reach to it, and
swapping providers touches one file. Widening the global set would hand every
other caller the same reach for no reason. (The validated address is pinned to
the connection (#871). The quotation tracks `egress`'s comment, which is
the one that must be edited first — #1325.)

## Budget

Provider-wide **90/day** on the `email_global` scope, key `''` — headroom
under the free tier's 100/day quota, past which Resend refuses sends (429
`daily_quota_exceeded`), which would strand invitation delivery exactly during
a cohort-onboarding burst. :func:`debit_budget` is the budget's one owner; an
invitation also spends its sender's own day there, so no one person can spend
the deployment's. Over budget the job **defers** on
its retry schedule rather than failing, and the deferral **restores the
attempt** the claim consumed, because `jobs.reschedule_job` records the rule: a
deferral is normal operation, not a failure, and the attempts budget is the
retryable-failure budget.

**The debit commits BEFORE the provider call, and that is forced rather than
chosen.** `egress.request` refuses to run inside an open transaction — the
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
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Optional

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.exceptions.base import RefusalError
from src.services.target import (
    egress,
    jobs,
    rate_counters,
    sessions,
    signup_admissions,
)
from src.services.target.egress import EgressPolicy
from src.services.target.unit_of_work import apply_gucs, transaction_discipline
from src.utils.datetime_utils import utcnow

logger = logging.getLogger(__name__)

#: The default provider. One host, declared here rather
#: than in the shared allowlist — see the module docstring.
PROVIDER_HOST = "api.resend.com"
SEND_URL = f"https://{PROVIDER_HOST}/emails"

#: This module's own reach, not the shared default's — see the module docstring.
DEFAULT_POLICY = EgressPolicy(
    timeout_class="standard", allowed_hosts=frozenset({PROVIDER_HOST})
)

#: The API's policy, for a send a person is waiting on (an invitation, after
#: it commits): one attempt on the fast class. A POST that timed out may still
#: have been accepted, so a retry could send twice; one attempt cannot, and the
#: inviter still holds the copy link if it fails.
INTERACTIVE_POLICY = EgressPolicy(
    timeout_class="fast", allowed_hosts=frozenset({PROVIDER_HOST}), max_attempts=1
)

#: The counter coordinates and the ceiling. The key is `''` because the
#: budget is provider-wide: one bucket for the whole deployment, not per tenant.
BUDGET_SCOPE = "email_global"
BUDGET_KEY = ""
BUDGET_LIMIT = 90
BUDGET_WINDOW_SECONDS = 24 * 3600

#: 3 attempts, backoff 1/5/15 min. Indexed by the attempt already
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
#: wrong thing. The registry starts at the invitation's template;
#: producers that need another add it here with their producer.
#: The invitation's subject, fixed. The workspace's name and the inviter's
#: Google name are the inviter's to choose, and this email goes to any address
#: under our domain's signature, so neither rides the line a mail client shows
#: first (#1692).
INVITATION_SUBJECT = "You're invited to a workspace on Storydump"


def _shown(text: str) -> str:
    """Text someone else chose, as one quoted name: control and format
    characters dropped (line breaks, tabs, bidirectional overrides),
    whitespace collapsed, and a double quote made single so it cannot close
    the quotes around it."""
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    return '"' + " ".join(kept.split()).replace('"', "'") + '"'


def _invitation(params: Mapping[str, Any]) -> tuple[str, str]:
    workspace = _shown(_required(params, "workspace_name"))
    accept_url = _required(params, "accept_url")
    address = _required(params, "invited_email")
    expires = _required(params, "expires_on")
    inviter = params.get("inviter_name")
    opener = (
        f"{_shown(inviter)} has invited you"
        if isinstance(inviter, str) and inviter.strip()
        else "You have been invited"
    )
    return (
        INVITATION_SUBJECT,
        f"{opener} to join the workspace {workspace} on Storydump.\n\n"
        f"Accept the invitation:\n{accept_url}\n\n"
        f"Sign in with Google as {address} — the invitation works only for that"
        f" address. The link works once, and it expires on {expires}.\n\n"
        "If you were not expecting this, you can ignore this message — "
        "nothing happens until you accept.\n",
    )


#: The activation nudge's copy (#1481), one entry per step a person can stop
#: at (`activation_nudge.STEPS`): `(subject, body)`, the body carrying
#: `{link}`. The folder step's Google line mirrors the Drive card's
#: (`landing/src/lib/drive.ts`).
_NUDGE_COPY = {
    "instagram": (
        "Your Storydump workspace is ready for Instagram",
        "You created a Storydump workspace but have not connected Instagram"
        " yet. Connecting it is the step that lets Storydump post your"
        " Stories.\n\nConnect Instagram:\n{link}\n\n"
        "Instagram asks for a professional account (Business or Creator)."
        " Switching takes a minute in the Instagram app.\n",
    ),
    "folder": (
        "Choose the folder Storydump posts from",
        "Your workspace is connected to Instagram. The last setup step is"
        " choosing the Google Drive folder your Stories come from.\n\n"
        "Add a folder:\n{link}\n\n"
        "If Google says it hasn't verified this app, choose Advanced, then"
        ' "Go to storydump (unsafe)".\n',
    ),
    "approval": (
        "Your first Story is one tap away",
        "Storydump lines up a Story for you to approve at each posting time."
        " Approve the next one from your queue and it goes out on"
        " schedule.\n\nOpen your queue:\n{link}\n",
    ),
}

_NUDGE_FOOTER = (
    "\nThis is the only reminder we will send. If you have decided Storydump"
    " is not for you, you can ignore it.\n"
)


def _activation_nudge(params: Mapping[str, Any]) -> tuple[str, str]:
    step = _required(params, "step")
    link = _required(params, "link")
    if step not in _NUDGE_COPY:
        raise EmailRefused("template_params_missing", "step")
    subject, body = _NUDGE_COPY[step]
    return subject, body.format(link=link) + _NUDGE_FOOTER


#: The admission email's subject, fixed (#1723).
ADMISSION_SUBJECT = "Your spot on Storydump is ready"


def _admission(params: Mapping[str, Any]) -> tuple[str, str]:
    """An admitted person's one email (#1723), sent to their waitlist
    address. The link works for any Google account, once; and the owner may
    admit an address that never joined, so the email says to ignore it if it
    was not expected."""
    address = _required(params, "address")
    claim_url = _required(params, "claim_url")
    return (
        ADMISSION_SUBJECT,
        "Your spot on Storydump is ready.\n\n"
        f"Claim it here:\n{claim_url}\n\n"
        "Continue with any Google account. The one you choose becomes your"
        " Storydump sign-in. The link works once, and signing in with Google"
        f" as {address} works without it.\n\n"
        "If you weren't expecting this, you can ignore this message — "
        "nothing happens unless you sign in.\n",
    )


TEMPLATES = {
    "invitation": _invitation,
    "activation_nudge": _activation_nudge,
    signup_admissions.TEMPLATE: _admission,
}


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
    #: send, which is right at the provider-wide 90 a day, from the worker's
    #: job and the API's invitation alike.
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


def sender_from_env(
    env: Mapping[str, str], *, policy: Optional[EgressPolicy] = None
) -> Optional[ResendSender]:
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
    return ResendSender(api_key=api_key, sender=sender, policy=policy)


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


@asynccontextmanager
async def system_session(engine):
    """One committed transaction for a TENANT-LESS job.

    Not `unit_of_work(...)`: that factory REFUSES an empty tenant id on purpose
    — RLS reads `app.tenant_id`, so a tenant-less unit would be a
    widened query, and the refusal makes that unrepresentable, not discouraged.
    A `send_email` row genuinely has no tenant: it is a system kind under the
    classing rule (payload-complete, zero tenant reads at execution) and
    `ck_jobs_system_kinds` is a biconditional, so `workspace_id` is NULL by
    constraint.

    So this applies the GUCs directly with the empty tenant id — the same
    convention `worker.make_session_for` uses for exactly these rows, and it is
    fail-closed: under any tenant policy an empty `app.tenant_id` matches
    nothing rather than everything.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        # Armed like a unit of work's, so the egress floor refuses a provider
        # call made before this transaction closes.
        with transaction_discipline():
            async with session.begin():
                await apply_gucs(session, tenant_id="", actor_kind="system")
                yield session


async def debit_budget(
    session, *, person_key: Optional[str] = None, person_limit: int = 0, now=utcnow
) -> Optional[str]:
    """Spend one email from the day's budget, or name the ceiling that refused.

    The provider-wide ceiling always (`email_global`, key ``''``); first, when
    *person_key* is given, that person's own day under the same scope, at
    *person_limit*. Returns None when spent, else ``person_limit`` or
    ``daily_limit`` (`vocabulary.EMAIL_BUDGET_REASONS`).

    No undo: a refused hit spends nothing (`rate_counters.increment`), and the
    keys share one window, so a person's slot spent on a day the provider's
    ceiling already refuses is spent only for that day. Runs in the caller's
    transaction.
    """
    window = rate_counters.window_start(now(), BUDGET_WINDOW_SECONDS)
    ceilings = [(BUDGET_KEY, BUDGET_LIMIT, "daily_limit")]
    if person_key is not None:
        ceilings.insert(0, (person_key, person_limit, "person_limit"))
    for key, limit, reason in ceilings:
        counted = await rate_counters.increment(
            session, scope=BUDGET_SCOPE, key=key, window_start=window, limit=limit
        )
        if counted is None:
            return reason
    return None


def backoff_seconds(attempts: int) -> int:
    """The ladder rung for a job that has already consumed *attempts*.

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
    now=utcnow,
) -> Optional[str]:
    """Drain one `send_email` job. Returns the provider ref, or None if deferred.

    Takes **no session**. Every database touch here opens its own transaction
    off *engine* and commits it, because the provider call cannot
    legally happen inside one (`egress.request` raises on an open transaction)
    and the over-budget path rewrites the job row — which, done in the
    loop's session, would be rolled back by that session's own finalization
    failing on a row no longer leased.

    **An admission's email sends first and stores second** (#1723), and only
    the `admission` template takes this path:

    1. Its claim link's token is drawn here, and the email rendered with
       `claim_base + token`, so the raw token never rests in `jobs.payload`.
    2. The debit, as for any email. Over budget the job defers, and nothing is
       stored.
    3. The send, with no transaction open. A failure raises and the job retries
       with a fresh token, while a link already emailed keeps working, because
       nothing replaced it.
    4. Once the provider has taken the email, a fresh transaction stores the
       token's hash (`signup_admissions.store_link`). False means the spot was
       taken between the admission and the send: one log line, and the job
       still succeeds. A fault here retries the job, which emails a fresh
       link: a rare duplicate, never a lost link.
    """
    payload = job.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise EmailRefused("payload_malformed")
    to = payload.get("to")
    if not isinstance(to, str) or "@" not in to:
        raise EmailRefused("recipient_invalid")
    template, params = payload.get("template"), payload.get("params")
    token = None
    if template == signup_admissions.TEMPLATE:
        token = sessions.new_token()
        # A missing base is refused by name before anything is spent; params
        # that are not an object are left for `render` to refuse.
        if isinstance(params, Mapping):
            params = {**params, "claim_url": _required(params, "claim_base") + token}
    subject, body = render(template, params)

    async with system_session(engine) as budget_session:
        refused = await debit_budget(budget_session, now=now)
    if refused is not None:
        run_at = now() + timedelta(seconds=backoff_seconds(job.get("attempts")))
        async with system_session(engine) as deferral:
            await jobs.reschedule_job(
                deferral,
                job["id"],
                job["lease_token"],
                run_at=run_at,
                # A deferral is normal operation, not a failure — the attempts
                # budget is for failures; a daily ceiling would exhaust it in three
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
    if token is not None:
        async with system_session(engine) as session:
            stored = await signup_admissions.store_link(session, to=to, token=token)
        if not stored:
            logger.warning(
                "send_email %s: the admission email's link is dead: the spot was"
                " already used",
                job.get("id"),
            )
    return ref
