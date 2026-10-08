"""The activation nudge (#1481): one email, once, to a person who stopped
part-way through setting up.

`fn_activation_stalled` (106) lists who is owed one: people whose first
missing stage is connecting Instagram, adding a folder or approving a first
story, idle for the stall window, and never nudged. ONE conditional UPDATE
then sets `users.activation_nudge_at` for them and returns their addresses,
and a `send_email` job is enqueued for each, in the same transaction. The
UPDATE's predicate repeats the door's (still never nudged, still active, still
has an email), so two sweeps that overlap nudge a person once, and a person
the door listed who no longer qualifies is dropped, never stamped: the
`alert_stranded_sources` shape (`media_sync.py`).

## Built off

The registry parks this kind unless `WorkerConfig.activation_nudge_enabled`,
an email provider and a web origin are all present, and `compose` puts it on
the clock only when it is live. So today nothing mints it and nothing here
runs. Switching it on waits on two decisions that are not this code's: email
delivery being enabled, and the owner approving the copy
(`email_sender._NUDGE_COPY`).

## What it does not do

It records no analytics and adds nothing client-side: whether a nudged person
went on is read from the funnel against `activation_nudge_at`. A failed send
is not followed by a second nudge: the latch is set as the job is enqueued,
and `send_email`'s own ladder is the only retry. One reminder, best effort,
never two.
"""

from __future__ import annotations

import logging

from src.services.target import jobs, readers

logger = logging.getLogger(__name__)

#: A stage in `fn_activation_stages`' numbering → the step's key in the email
#: copy (`email_sender._NUDGE_COPY`) and the dashboard path that does the step.
#: Stage 2 (no workspace yet) is counted by the funnel and never emailed.
STEPS = {
    3: ("instagram", "/dashboard/settings?tab=accounts"),
    4: ("folder", "/dashboard/settings?tab=integrations"),
    5: ("approval", "/dashboard/queue"),
}

TEMPLATE = "activation_nudge"


async def sweep_stalled(
    session,
    *,
    since_days: int,
    stall_seconds: int,
    limit: int,
    web_app_origin: str,
) -> int:
    """Nudge up to *limit* stalled people who signed up in the last
    *since_days*. Returns how many emails were queued. *web_app_origin* is
    `settings.web_app_origin`, already normalized (`invitations.join_url`)."""
    owed = await readers.rows(
        session,
        "SELECT o_user_id AS user_id, o_stage AS stage"
        "  FROM fn_activation_stalled("
        "    now() - make_interval(days => :days),"
        "    make_interval(secs => :stall), :lim)",
        days=int(since_days),
        stall=float(stall_seconds),
        lim=int(limit),
    )
    steps = {}
    for row in owed:
        step = STEPS.get(int(row["stage"]))
        if step is None:
            # The door lists stages 3-5 only. A stage this table does not know
            # is a mismatch between the two, named in the log and not mailed.
            logger.warning(
                "activation nudge: no step for stage %s; skipped", row["stage"]
            )
            continue
        steps[str(row["user_id"])] = step
    if not steps:
        return 0
    addresses = {
        str(row["id"]): row["primary_email"]
        for row in await readers.rows(
            session,
            "UPDATE users SET activation_nudge_at = now()"
            " WHERE id = ANY(CAST(:ids AS uuid[]))"
            "   AND activation_nudge_at IS NULL"
            "   AND state = 'active'"
            "   AND primary_email IS NOT NULL"
            " RETURNING id, primary_email",
            ids=list(steps),
        )
    }
    queued = 0
    for user_id, (key, path) in steps.items():  # the door's order
        email = addresses.get(user_id)
        if email is None:
            continue
        await jobs.enqueue(
            session,
            kind="send_email",
            # NULL, by constraint: `send_email` is a system kind.
            workspace_id=None,
            serialization_key=f"email:nudge:{user_id}",
            payload={
                "v": 1,
                "to": email,
                "template": TEMPLATE,
                "params": {"step": key, "link": f"{web_app_origin}{path}"},
            },
            # `bulk`: nobody is waiting on this one, unlike an invitation.
            lane="bulk",
        )
        queued += 1
    if queued:
        logger.info("activation nudge: queued %d email(s)", queued)
    return queued
