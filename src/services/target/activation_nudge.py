"""The activation nudge (#1481): one email, once, to a person who stopped
part-way through setting up.

`fn_activation_stalled` (106) lists who is owed one: people whose first
missing stage is connecting Instagram, adding a folder or approving a first
story, idle for the stall window, and never nudged. For each, ONE conditional
UPDATE sets `users.activation_nudge_at` and returns the address, and the
`send_email` job is enqueued in the same transaction. The UPDATE's predicate
repeats the door's (still never nudged, still active, still has an email), so
two sweeps that overlap nudge a person once, and a person the door listed who
no longer qualifies is dropped, never stamped: the `alert_stranded_sources`
shape (`media_sync.py`).

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

from sqlalchemy import text

from src.services.target import jobs

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
    *since_days*. Returns how many emails were queued."""
    candidates = (
        (
            await session.execute(
                text(
                    "SELECT o_user_id AS user_id, o_stage AS stage"
                    "  FROM fn_activation_stalled("
                    "    now() - make_interval(days => :days),"
                    "    make_interval(secs => :stall), :lim)"
                ),
                {
                    "days": int(since_days),
                    "stall": float(stall_seconds),
                    "lim": int(limit),
                },
            )
        )
        .mappings()
        .all()
    )
    origin = web_app_origin.rstrip("/")
    queued = 0
    for row in candidates:
        step = STEPS.get(int(row["stage"]))
        if step is None:
            # The door lists stages 3-5 only. A stage this table does not know
            # is a mismatch between the two, named in the log and not mailed.
            logger.warning(
                "activation nudge: no step for stage %s; skipped", row["stage"]
            )
            continue
        user_id = str(row["user_id"])
        email = (
            await session.execute(
                text(
                    "UPDATE users SET activation_nudge_at = now()"
                    " WHERE id = CAST(:u AS uuid)"
                    "   AND activation_nudge_at IS NULL"
                    "   AND state = 'active'"
                    "   AND primary_email IS NOT NULL"
                    " RETURNING primary_email"
                ),
                {"u": user_id},
            )
        ).scalar_one_or_none()
        if email is None:
            continue
        key, path = step
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
                "params": {"step": key, "link": origin + path},
            },
            # `bulk`: nobody is waiting on this one, unlike an invitation.
            lane="bulk",
        )
        queued += 1
    if queued:
        logger.info("activation nudge: queued %d email(s)", queued)
    return queued
