"""The client half of the harness and the report (S.1, `02` step 5).

The client offers synthetic `callback_query` updates to the real route and
delivers them the way Telegram does: at most `max_connections` at once (the
registration constant), the rest queued as Telegram's `pending_update_count`
would be, and every non-2xx redelivered on a documented schedule (Telegram's
is undocumented; ours is `REDELIVERY_SCHEDULE_S`). Every delivery attempt is
timed; the fake Telegram supplies the other end — when the answer arrived,
when the strip and the outcome edit landed on each card.

**Which latency is which** (the F1 evidence names it): `answer` is tap →
`answerCallbackQuery` received at the fake, end to end from the FIRST
delivery attempt, redelivery included — the SLO. `route` is the 200's
round trip. `strip` and `outcome` are edit-landed times for the card the
tap named (the route's immediate strip; the sender's paced outcome line).
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

import httpx

from src.channels.telegram_webhook_registration import DEFAULT_MAX_CONNECTIONS
from src.services.target.callback_tokens import token as callback_token

from .fake_telegram import FakeTelegram
from .seed import Card, Workspace

#: How the harness redelivers a non-2xx: seconds after each failed attempt.
#: Telegram's schedule is undocumented; this one is stated in the report.
REDELIVERY_SCHEDULE_S = (0.5, 1.0, 2.0, 4.0)


@dataclass
class Tap:
    update_id: int
    callback_query_id: str
    chat: str
    message_id: str
    action: str
    intent_id: str
    tapper_tg: str
    workspace_id: str

    def payload(self) -> dict:
        return {
            "update_id": self.update_id,
            "callback_query": {
                "id": self.callback_query_id,
                "from": {"id": int(self.tapper_tg), "first_name": "Tapper"},
                "message": {
                    "message_id": int(self.message_id),
                    "chat": {"id": int(self.chat), "type": "supergroup"},
                },
                "data": callback_token(self.action, self.intent_id),
            },
        }


@dataclass
class Delivery:
    tap: Tap
    first_attempt_at: float = 0.0
    attempts: list[tuple[float, int, float]] = field(
        default_factory=list
    )  # (t, status, dt)
    outcome: Optional[str] = None
    status_final: Optional[int] = None

    @property
    def redeliveries(self) -> int:
        return max(0, len(self.attempts) - 1)


_ids = iter(range(10_000_000, 90_000_000))


def tap_for(
    ws: Workspace, card: Card, *, action: str = "skip", chat: Optional[str] = None
) -> Tap:
    chat = chat or ws.bindings[0].chat
    n = next(_ids)
    return Tap(
        update_id=n,
        callback_query_id=f"{chat}:{n}",
        chat=chat,
        message_id=card.refs[chat],
        action=action,
        intent_id=card.intent,
        tapper_tg=ws.tapper_tg,
        workspace_id=ws.id,
    )


class Client:
    """Delivers taps at Telegram's concurrency, redelivering non-2xx."""

    def __init__(
        self, base: str, secret: str, *, max_connections: int = DEFAULT_MAX_CONNECTIONS
    ):
        self.base = base
        self.secret = secret
        self.max_connections = max_connections
        self.queued_peak = 0
        self._queued = 0

    async def deliver(
        self, taps: Iterable[Tap], *, offer_within_s: float = 0.0
    ) -> list[Delivery]:
        taps = list(taps)
        sem = asyncio.Semaphore(self.max_connections)
        limits = httpx.Limits(max_connections=self.max_connections + 2)
        async with httpx.AsyncClient(
            base_url=self.base, limits=limits, timeout=30.0
        ) as http:
            gap = offer_within_s / max(1, len(taps))

            async def one(i: int, tap: Tap) -> Delivery:
                if gap:
                    await asyncio.sleep(i * gap)
                d = Delivery(tap=tap)
                self._queued += 1
                self.queued_peak = max(self.queued_peak, self._queued)
                async with sem:
                    self._queued -= 1
                    d.first_attempt_at = time.monotonic()
                    for attempt, delay in enumerate((0.0,) + REDELIVERY_SCHEDULE_S):
                        if delay:
                            await asyncio.sleep(delay)
                        t0 = time.monotonic()
                        try:
                            r = await http.post(
                                "/webhooks/telegram",
                                json=tap.payload(),
                                headers={
                                    "X-Telegram-Bot-Api-Secret-Token": self.secret
                                },
                            )
                            status = r.status_code
                            body = r.json() if status == 200 else {}
                        except httpx.HTTPError:
                            status, body = 599, {}
                        d.attempts.append((t0, status, time.monotonic() - t0))
                        if 200 <= status < 300:
                            d.status_final = status
                            d.outcome = body.get("outcome") or body.get("status")
                            break
                    else:
                        d.status_final = d.attempts[-1][1]
                return d

            return await asyncio.gather(*(one(i, t) for i, t in enumerate(taps)))


# --- the report --------------------------------------------------------------


def pct(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, math.ceil(p / 100 * len(xs)) - 1))
    return xs[k]


@dataclass
class Scenario:
    name: str
    spread: str
    deliveries: list[Delivery]
    fake: FakeTelegram
    settled_until: float  # monotonic time until which edits were awaited
    health_after: dict
    flips: int
    audit_rows: int
    statements: Optional[int] = None
    notes: list[str] = field(default_factory=list)

    def numbers(self) -> dict:
        answers = self.fake.answers()
        strips = self.fake.edits(("editMessageReplyMarkup",))
        outcomes = self.fake.edits(("editMessageCaption", "editMessageText"))
        answer_lat, route_lat, strip_lat, outcome_lat = [], [], [], []
        five_xx = busy = refused_503 = redeliveries = unanswered = 0
        by_outcome: dict[str, int] = {}
        for d in self.deliveries:
            redeliveries += d.redeliveries
            for _, status, dt in d.attempts:
                if status >= 500:
                    five_xx += 1
                    if status == 503:
                        refused_503 += 1
            ok = [dt for _, s, dt in d.attempts if 200 <= s < 300]
            if ok:
                route_lat.append(ok[0])
            if d.outcome:
                by_outcome[d.outcome] = by_outcome.get(d.outcome, 0) + 1
            if d.outcome == "busy":
                busy += 1
            at = answers.get(d.tap.callback_query_id)
            if at is None:
                unanswered += 1
            else:
                answer_lat.append(at - d.first_attempt_at)
            key = (d.tap.chat, d.tap.message_id)
            if key in strips:
                strip_lat.append(strips[key] - d.first_attempt_at)
            if key in outcomes:
                outcome_lat.append(outcomes[key] - d.first_attempt_at)
        return {
            "taps": len(self.deliveries),
            "five_xx": five_xx,
            "refused_503": refused_503,
            "busy": busy,
            "redeliveries": redeliveries,
            "unanswered": unanswered,
            "by_outcome": dict(sorted(by_outcome.items())),
            "answer_p50_s": pct(answer_lat, 50),
            "answer_p95_s": pct(answer_lat, 95),
            "answer_max_s": max(answer_lat) if answer_lat else None,
            "route_p95_s": pct(route_lat, 95),
            "strip_p95_s": pct(strip_lat, 95),
            "outcome_p95_s": pct(outcome_lat, 95),
            "outcome_landed": len(outcome_lat),
            "flips": self.flips,
            "audit_rows": self.audit_rows,
            "pending_peak": self.fake.pending_update_count,
            "pool_peak": (self.health_after.get("pool") or {}).get("checked_out_peak"),
            "statements": self.statements,
        }


def render_report(
    *,
    run_at: str,
    rtt_note: str,
    container: dict,
    api_health: dict,
    scenarios: list[Scenario],
) -> str:
    lines = [
        f"# Load harness report — {run_at}",
        "",
        "Phase 2 of the 2026-09-09 tap plan, step 6 (`02_api-under-load.md`). The client",
        f"delivers at most `max_connections` = {DEFAULT_MAX_CONNECTIONS} taps at once and",
        f"redelivers a non-2xx after {', '.join(f'{s:g}' for s in REDELIVERY_SCHEDULE_S)} s.",
        "",
        "**Which latency is which.** `answer` = tap → `answerCallbackQuery` at the fake,",
        "end to end from the FIRST delivery attempt, redelivery included — the SLO (p95 < 2 s).",
        "`route` = the 200's round trip. `strip` / `outcome` = the card's keyboard gone /",
        "its outcome line landed, from the first attempt (reported, not judged; `one_slow_chat`",
        "judges `outcome` against the sender's cadence).",
        "",
        "## Run",
        "",
        f"- RTT: {rtt_note}",
        f"- Database: {container.get('version', '?')}; `synchronous_commit` = {container.get('synchronous_commit', '?')};"
        f" `max_connections` = {container.get('max_connections', '?')}",
        f"- API: pool {json.dumps(api_health.get('pool'))}; ingress_workers = {api_health.get('ingress_workers')}",
        "",
        "## Scenarios",
        "",
    ]
    for s in scenarios:
        n = s.numbers()
        lines += [
            f"### `{s.name}` — {s.spread}",
            "",
            "| number | value |",
            "|---|---|",
        ]
        for k, v in n.items():
            if k == "by_outcome":
                v = ", ".join(f"{a}: {b}" for a, b in v.items()) or "—"
            elif isinstance(v, float):
                v = f"{v:.3f}"
            lines.append(f"| {k} | {v} |")
        for note in s.notes:
            lines.append(f"\n> {note}")
        lines.append("")
    return "\n".join(lines) + "\n"


def new_update_id() -> int:
    return next(_ids)


def uuid_str() -> str:
    return str(uuid.uuid4())
