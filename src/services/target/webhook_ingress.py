"""L.8 — webhook ingress and idempotent admission (`02` §6, issue #865).

Every other Phase L increment controls its own entry. This one does not: a
webhook arrives when the provider decides, as often as it decides, possibly
twice for the same event, possibly out of order, and possibly while the
previous copy is still in flight. **Idempotent admission is the whole
increment** — the same delivery arriving twice must produce one effect, and
that has to hold under CONCURRENCY, not merely under retry-after-failure.

Those are different properties and only the second is easy. A retry arrives
after the first attempt finished, so any "have I seen this?" check works. Two
simultaneous deliveries both run that check before either has written anything,
so a check-then-act is exactly wrong. Admission is therefore a single
`INSERT … ON CONFLICT`, which is atomic, rather than a SELECT followed by an
INSERT, which is not.

## The three outcomes, and why the middle one is not an absence

- **Admitted** — this key was not present; the caller may execute the command.
- **Replay** — the key was present with the SAME fingerprint. Acknowledged
  WITHOUT re-execution, which is what makes 200 replayed callbacks one command.
  This is raised as a NAMED refusal rather than returned as a false, because
  `rowcount` cannot discriminate a winner from a loser (#883 showed both
  getting `rows = 1` on a different rail) and "the second one did not insert"
  has to be an identifiable event, not the absence of one.
- **Conflict** — the key was present with a DIFFERENT fingerprint. Rejected as
  a conflict, never silently swallowed as a replay: a reused web/cli
  idempotency token carrying a new command body is a caller bug or an attack,
  and treating it as a replay would silently drop the second command.

## What a concurrent loser actually sees, stated because it decides the design

Under `READ COMMITTED`, `ON CONFLICT DO NOTHING` makes the second inserter WAIT
on the first one's tuple rather than fail immediately. Two consequences, both
load-bearing and both tested rather than assumed:

1. If the winner COMMITS, the loser returns zero rows, and its follow-up read —
   a new statement, therefore a new snapshot — sees the committed row. So the
   loser can always name why it lost.
2. If the winner ABORTS, Postgres lets the loser's insert PROCEED. The delivery
   is admitted exactly once overall, and an abandoned attempt does not poison
   the key. No retry loop is needed for this; the database already does it.

## Relationship to #903 (LeaseHeartbeat), asked and answered rather than assumed

**#903 does not affect admission correctness here, and the reason is
structural rather than reassuring.** Idempotency lives in `command_dedup` at
ADMISSION, which is upstream of any job row. If a live sender is reaped at the
lease boundary its job is re-claimed and the *effect* is re-attempted — that is
governed by L.4's `uq_jobs_serialized_lease` and its CAS, not by this table. A
missed heartbeat therefore cannot turn one admission into two.

**Where it does touch this increment is the ack SLO.** The gate measures ack
latency under slow-worker injection, and a slow worker is precisely the
condition in which the 60-120s lease boundary bites. So the SLO number is
adjacent to #903 even though the idempotency guarantee is not, and #903 stays
open: no composition root exists yet, and this increment does not create one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from sqlalchemy import text

from src.exceptions import StorydumpError

logger = logging.getLogger(__name__)

#: `ck` on the column's own comment: the three admission channels.
CHANNELS = ("telegram", "web", "cli")

#: Telegram update ids are issued bot-globally and are already unique, so the
#: principal slot is empty for that channel. Web uses the session id and CLI
#: the service-token id, which is what makes a key collision across tenants
#: structurally impossible rather than merely unlikely.
TELEGRAM_PRINCIPAL = ""


class DeliveryReplayed(StorydumpError):
    """The same delivery, already admitted, with an identical fingerprint.

    A NAMED outcome rather than a returned false: the caller must acknowledge
    it (so the provider stops redelivering) and must NOT execute the command.
    Those are two different obligations and a boolean carries neither.
    """


class AdmissionConflict(StorydumpError):
    """The same key with a DIFFERENT fingerprint.

    Never treated as a replay. A reused idempotency token carrying new content
    is a caller bug or an attack, and swallowing it would silently drop a real
    command — the failure mode that is invisible precisely because it looks
    like successful deduplication.
    """


def fingerprint(payload: Any) -> str:
    """SHA256 over the NORMALIZED payload.

    Normalized means sorted keys and no incidental whitespace, so two
    byte-different encodings of the same command hash the same. Without that,
    a provider that reorders JSON keys between deliveries would make every
    replay look like a conflict — which fails closed, but fails closed on every
    single redelivery, which is its own outage.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def verify_secret_token(presented: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison of Telegram's `X-Telegram-Bot-Api-Secret-Token`.

    `hmac.compare_digest`, not `==`: a short-circuiting comparison leaks the
    length of the matching prefix through timing. An absent expected value is
    a misconfiguration and refuses everything rather than accepting everything
    — the direction matters, and the legacy adapter already gets this right.
    """
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


async def admit(
    conn,
    *,
    channel: str,
    external_ref: str,
    payload: Any,
    principal: str = TELEGRAM_PRINCIPAL,
) -> dict[str, Any]:
    """Admit one delivery, exactly once. Returns the admitted row.

    Raises :class:`DeliveryReplayed` for a true replay and
    :class:`AdmissionConflict` for a key reused with different content.

    The insert is a SINGLE atomic statement on purpose. A SELECT-then-INSERT
    would pass every sequential test and admit both copies of a simultaneous
    pair, which is the one case this function exists for.
    """
    if channel not in CHANNELS:
        raise ValueError(f"not an admission channel: {channel!r}")

    fp = fingerprint(payload)
    result = await conn.execute(
        text(
            "INSERT INTO command_dedup (channel, principal, external_ref, fingerprint)"
            " VALUES (:channel, :principal, :ref, :fp)"
            " ON CONFLICT (channel, principal, external_ref) DO NOTHING"
            " RETURNING channel, principal, external_ref, fingerprint, created_at"
        ),
        {"channel": channel, "principal": principal, "ref": external_ref, "fp": fp},
    )
    row = result.mappings().first()
    if row is not None:
        return dict(row)

    # Lost the race, or a plain redelivery. Either way the winner has committed
    # by the time DO NOTHING returns, so this read sees it.
    existing = await conn.execute(
        text(
            "SELECT fingerprint FROM command_dedup"
            " WHERE channel = :channel AND principal = :principal"
            "   AND external_ref = :ref"
        ),
        {"channel": channel, "principal": principal, "ref": external_ref},
    )
    found = existing.first()
    if found is None:
        # The competing transaction aborted after we began waiting on its
        # tuple. Postgres normally lets our insert proceed in that case, so
        # reaching here means something stranger happened; refuse rather than
        # invent an admission.
        raise AdmissionConflict(
            f"{channel}:{external_ref} neither inserted nor found — the "
            "competing transaction left no row; refusing to guess"
        )

    if found[0] == fp:
        raise DeliveryReplayed(
            f"{channel}:{external_ref} already admitted with an identical "
            "fingerprint: acknowledge it, do not execute it"
        )
    raise AdmissionConflict(
        f"{channel}:{external_ref} was already admitted with a DIFFERENT "
        "fingerprint: the key was reused for different content, which is not a "
        "replay and must not be acknowledged as one"
    )
