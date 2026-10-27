"""Approval card presentation: text, labels, keyboards and payloads.

Database reads and prompt orchestration stay in :mod:`prompts`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.services.target import refusal_evidence, vocabulary
from src.services.target.callback_tokens import ACTIONS, token as _token
from src.utils.datetime_utils import ensure_utc, utcnow

# Stamp warnings log under the `prompts` module's logger name.
logger = logging.getLogger("src.services.target.prompts")

INSTAGRAM_DEEPLINK_URL = "https://www.instagram.com/"

_ACTIONS_API = ("post", "posted", "skip", "reject")
_ACTIONS_MANUAL = ("posted", "skip", "reject")
#: The review card's buttons: a `review_required` card is the
#: workspace's to resolve — it posted, not there (post again), give up. The
#: retry button's label is the answer to the review's own question ("is it
#: on your story?"), so a tap on it carries the member's verdict.
REVIEW_ACTIONS = ("itposted", "notposted", "giveup")
#: ⚙️ Configure and its menu: on every card for the card's whole life,
#: one button opening a menu one level down — the item's Auto-approve
#: flag, the web's approval settings, Back. Navigation runs no card command.
MENU_ACTIONS = ("cfg", "back", "aa1", "aa0")
assert set(_ACTIONS_API) | set(REVIEW_ACTIONS) | set(MENU_ACTIONS) == set(ACTIONS)

#: The word a card shows for each state a tap can find it in — the outcome
#: line and the answer a repeat tap gets both read from here, so the two
#: never disagree.
OUTCOME_WORDS = {
    "scheduled": "🗓 Scheduled",
    "prompt_pending": "⏳ Awaiting approval",
    "awaiting_approval": "⏳ Awaiting approval",
    "approved": "✅ Approved",
    "publishing": "🚀 Posting…",
    "publishing_ambiguous": "🚀 Posting… (confirming)",
    "review_required": "👀 Needs review",
    "posted": "✅ Posted",
    "dry_run": "🧪 Dry run — not published",
    "skipped": "⏭️ Skipped",
    "rejected": "🚫 Rejected",
    "expired": "⌛ Expired — slot passed",
    "cancelled": "🚫 Cancelled",
    "failed": "⚠️ Failed",
    "account_disabled": "⏸ Account disabled",
}

#: A planned card rendered this long after its story's time says "served
#: late". The sweep serves a due row within its 5-second beat and the sender
#: claims a card within seconds, so a minute past means something held it up
#: (the worker down, the workspace paused, a backlog), and the person who
#: chose the time should see that it was not kept.
SERVED_LATE_AFTER = timedelta(minutes=1)

#: Why a planned story was not served, as its notice says it: the miss door's
#: reasons in `fn_planned_misses`'s precedence, and `account_removed`
#: from a destination's removal as well (`provisioning.disable_destination`).
MISS_REASONS = {
    "item_removed": "the item was removed from the library",
    "item_unsupported": "Instagram cannot post the item",
    "item_missing": "the item's file is no longer in its Drive folder",
    "item_locked": "the item is locked",
    "account_removed": "the account was removed",
    "paused": "the workspace was not taking posts until it was too late",
    "late": "it could not be served in time",
}

#: What a repeating story's miss notice adds: whether its next occurrence now
#: waits, read from the ledger after the miss (`_next_waits`).
SERIES_WORDS = {True: "It repeats as planned.", False: "Its repeat has stopped."}

#: What a settled card's outcome line adds when the act ended the story's
#: series: a cancel or give-up that stops it, or a Reject, which ends a
#: series by its nature. The card's header keeps the rule it was sent with.
SERIES_STOPPED = "the series stopped"
SERIES_ENDED = "the series ended"


#: A Drive file name has no bound of its own; a Telegram caption stops at 1024
#: characters. The card's caption and the "not served" notice both name a file
#: within this.
FILE_NAME_BOUND = 200


def account_label(handle: Optional[str]) -> str:
    """How a notice names a destination: its handle, or a phrase when the
    handle is unknown (it is nullable), never a blank. A multi-account
    workspace cannot act on "a slot"."""
    return f"@{handle}" if handle else "one of this workspace's accounts"


#: Zones already warned about — one line per zone, not one per card.
_WARNED_ZONES: set[str] = set()


def stamp(at: datetime, tz: str) -> str:
    """`%Y-%m-%d %H:%M <tz>` in the WORKSPACE's timezone — the slot line and
    the outcome line share this one spelling. A zone Postgres accepted
    (`fn_safe_tz`: `PST`, `UTC+5`) that the IANA database does not know
    degrades to UTC, as the door does — one workspace's zone must never fail
    a tap or a sweep."""
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        if tz not in _WARNED_ZONES:
            _WARNED_ZONES.add(tz)
            logger.warning(
                "workspace tz %r is unknown to the IANA database — card times"
                " render in UTC until it is changed in Settings",
                tz,
            )
        zone, tz = ZoneInfo("UTC"), "UTC"
    return f"{at.astimezone(zone).strftime('%Y-%m-%d %H:%M')} {tz}"


def outcome_word(state: str, published_via: Optional[str] = None) -> str:
    """The state's word — and a `posted` row that was a DRY RUN says so,
    everywhere a state becomes a word (the tap's answer, the settled-card
    sweep, the outcome line), so a rehearsal never reads as a post."""
    if state == "posted" and published_via == "dry_run":
        return OUTCOME_WORDS["dry_run"]
    return OUTCOME_WORDS.get(state, state)


#: A story a rule approved keeps its card: it is sent settled — the
#: card, the outcome line naming the mechanism, no buttons — while the story is
#: in one of these. Any other state outside `LIVE_FOR_A_CARD` retires the card
#: unsent.
SETTLED_CARD_STATES = ("approved", "publishing", "publishing_ambiguous", "posted")

#: The lines that name the rule that approved a story where no person's name
#: stands ("✅ Posted by YOLO"): the settled states' lines, and
#: `dry_run`, the pipeline's word for a posted dry run. A failed, review,
#: expired or cancelled line never names it — a rule caused none of those.
RULE_LINE_STATES = (*SETTLED_CARD_STATES, "dry_run")


def sends_settled(state: Optional[str], rule: Optional[str]) -> bool:
    """Whether a story's card is sent settled: a rule approved it, and it is
    in a settled state. The send-time render and the settled-card sweep read
    this one answer."""
    return bool(rule) and state in SETTLED_CARD_STATES


def outcome_line(
    state: str,
    *,
    by: Optional[str],
    at: datetime,
    tz: str,
    published_via: Optional[str] = None,
    rule: Optional[str] = None,
    series: Optional[str] = None,
    refusal: Optional[Mapping] = None,
) -> str:
    """What a settled card says under its header: the state's word, who, when.
    *rule* is the story's `approval_rule` (`_line_actor`). *series* is what
    became of the story's series when the act ended it (`SERIES_STOPPED`,
    `SERIES_ENDED`), since the header still says how it repeats. *refusal* is
    why a story in review was refused (`last_error -> 'error' -> 'refusal'`):
    a second line in its words (`refusal_evidence.plain`), when they say
    anything."""
    word = outcome_word(state, published_via)
    by = _line_actor(state, by=by, rule=rule)
    who = f" by {by}" if by else ""
    line = f"{word}{who} · {stamp(at, tz)}"
    if series:
        line = f"{line} · {series}"
    reason = refusal_evidence.plain(refusal)
    return f"{line}\nRefused: {reason}." if reason else line


def _line_actor(state: str, *, by: Optional[str], rule: Optional[str]) -> Optional[str]:
    """Who a line about *state* names: *by*, the person who moved the story;
    or, when the system did, the rule's label on a line the rule caused. So a
    person's "it posted" or retry keeps their name on every surface."""
    if rule and not by and state in RULE_LINE_STATES:
        return vocabulary.rule_label(rule)
    return by


def waiting_line(
    next_run_at: datetime,
    *,
    tz: str,
    now: datetime,
    day_spent: bool = False,
    rule: Optional[str] = None,
) -> str:
    """What an approved card says while its story waits for a slot that is
    hours away (the float: a wait the user should know about is said on the
    card; a wait of seconds says nothing): the state's word and when it
    posts, in the workspace's zone — "today 14:00", "tomorrow 09:00", or
    the weekday. *day_spent* is the local cap's case: the day's count is at
    its cap, so a slot later today cannot post and the line promises
    tomorrow without a time the clock has not chosen yet. *rule*
    names the rule that approved the story, as `outcome_line` does."""
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — an unknown zone reads as UTC, never a crash
        zone = ZoneInfo("UTC")
    at = next_run_at.astimezone(zone)
    today = now.astimezone(zone).date()
    days = (at.date() - today).days
    who = _line_actor("approved", by=None, rule=rule)
    approved = outcome_word("approved") + (f" by {who}" if who else "")
    if day_spent and days <= 0:
        return f"{approved} · posts tomorrow"
    if days <= 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    else:
        day = at.strftime("%a")
    return f"{approved} · posts {day} {at.strftime('%H:%M')}"


_LABELS = {
    "post": "🚀 Post now",
    "posted": "✅ Posted myself",
    "skip": "⏭️ Skip",
    "reject": "🚫 Reject",
    "itposted": "✅ It posted",
    "notposted": "🔁 Not there — post again",
    "giveup": "🚫 Give up",
    "cfg": "⚙️ Configure",
    "back": "‹ Back",
}


def _button(action: str, intent_id: str) -> dict:
    return {"text": _LABELS[action], "callback_data": _token(action, intent_id)}


def card_keyboard(
    intent_id: str,
    *,
    state: str,
    api_publishing_enabled: bool,
    item_state: Optional[str] = None,
    cancelling: bool = False,
) -> Optional[dict]:
    """The keyboard of an approval card for its story as it is now — the one
    spelling: the sender renders every card's keyboard with it
    when it sends or edits the card (`card_keyboard_for`), and Configure's
    Back and its heal read it the same way (`keyboard_of`), so no caller picks
    one. ⚙️ Configure is on every card but a rejected story's or a removed
    item's, which carry none:

    - a waiting story: the decision buttons (🚀 Post now only where the
      workspace posts through the API), and Configure beside 📱 Open Instagram
      — unless its cancel is pending (*cancelling*): it is on its way to
      `cancelled`, so its card shows what a cancelled one does, Configure
      alone, and the cancel's edit never brings the decision buttons back;
    - `review_required`: the member's three resolutions, then Configure;
    - any other state: Configure alone, for the card's whole life.
    """
    if state == "rejected" or item_state == "removed":
        return None
    configure = _button("cfg", intent_id)
    if state in LIVE_FOR_A_CARD and not cancelling:
        actions = _ACTIONS_API if api_publishing_enabled else _ACTIONS_MANUAL
        rows = [
            [_button(a, intent_id) for a in actions[:2]],
            [_button(a, intent_id) for a in actions[2:]],
            [{"text": "📱 Open Instagram", "url": INSTAGRAM_DEEPLINK_URL}, configure],
        ]
    elif state == "review_required":
        rows = [
            [_button(a, intent_id) for a in REVIEW_ACTIONS[:2]],
            [_button(a, intent_id) for a in REVIEW_ACTIONS[2:]],
            [configure],
        ]
    else:
        rows = [[configure]]
    return {"inline_keyboard": rows}


def menu_keyboard(intent_id: str, *, on: bool, settings_url: Optional[str]) -> dict:
    """Configure's menu, one level down: the item's Auto-approve flag,
    which a tap turns the other way; the web's approval settings, only for an
    https link, which is all Telegram takes in a URL button (any other fails
    the whole edit); and Back to the card's own keyboard."""
    toggle = "aa0" if on else "aa1"
    rows = [
        [
            {
                "text": f"🔁 Auto-approve this item: {'on' if on else 'off'}",
                "callback_data": _token(toggle, intent_id),
            }
        ]
    ]
    if settings_url and settings_url.startswith("https://"):
        rows.append([{"text": "⚙️ Approval settings ↗", "url": settings_url}])
    rows.append([_button("back", intent_id)])
    return {"inline_keyboard": rows}


#: The web's settings page, where the approval positions live.
SETTINGS_PATH = "/dashboard/settings"


def settings_url() -> Optional[str]:
    """The approval settings' address on the web, or None when the front end's
    origin is unset."""
    from src.config.settings import settings  # noqa: PLC0415 — read at use

    origin = settings.web_app_origin
    return f"{origin}{SETTINGS_PATH}" if origin else None


def _canonical_fraction(value: str) -> str:
    """Pad a 1-5 digit fractional-seconds field to the canonical 6 digits.

    Postgres strips trailing fractional zeros when rendering a timestamptz
    into text/jsonb, so a microsecond value can arrive at any width 1-6.
    Widths other than 3 and 6 are rejected by ``fromisoformat`` at the
    repository's 3.10 floor, so the string is canonicalized before parsing
    rather than teaching this module which widths which interpreter accepts.
    Width 0 (no fraction) and width 6 pass through untouched.
    """
    return re.sub(
        r"\.(\d{1,5})(?!\d)", lambda m: "." + m.group(1).ljust(6, "0"), value, count=1
    )


def _planned_line(
    intent: dict, *, slot: datetime, tz: str, now: Optional[datetime]
) -> Optional[str]:
    """The line a PLANNED card adds under its slot: who scheduled it and for
    when, its repeat rule when it has one, plus "served late" when the
    card is rendered `SERVED_LATE_AFTER` past that time. None for a cadence
    card. *intent* carries ``scheduled_by``, the display name, ``repeat``,
    the rule, and ``repeat_tz``, the account's own zone, which the rule's time
    is in (`_with_scheduler`). The slot is stamped in *tz*, the workspace's,
    so the rule names its zone where the two differ. A scheduler who is gone
    (`ON DELETE SET NULL`) leaves the line without a name."""
    if intent.get("origin") != "planned":
        return None
    line = outcome_line("scheduled", by=intent.get("scheduled_by"), at=slot, tz=tz)
    if intent.get("repeat"):
        line += " · repeats " + vocabulary.describe_repeat(intent["repeat"])
        zone = intent.get("repeat_tz")
        if zone and zone != tz:
            line += f" ({zone})"
    if (now or utcnow()) - slot >= SERVED_LATE_AFTER:
        line += " · served late"
    return line


#: The line a waiting card adds when a rule would have approved its story but
#: step 1 of the decision stopped it. It is derived at render from
#: the decision's own facts, seconds after the serve made the same decision.
_BLOCK_NOTES = {
    "link": "🔒 {label} is on, but this item has a link to add — post it by hand.",
    "no_login": "🔒 {label} is on, but Instagram isn't connected for {account}.",
    "api_off": "🔒 {label} is set, but this workspace posts by hand.",
}


def block_note(decision, *, handle: Optional[str]) -> Optional[str]:
    """The waiting card's line for an `auto_approval.Decision`, or None when
    no block stopped a rule (`decide` names a block only then)."""
    if not decision.blocked_by:
        return None
    return _BLOCK_NOTES[decision.blocked_by].format(
        label=vocabulary.rule_label(decision.would_be), account=account_label(handle)
    )


def render_card(
    intent: dict,
    *,
    api_publishing_enabled: bool,
    now: Optional[datetime] = None,
    note: Optional[str] = None,
) -> dict:
    """The approval-prompt payload in the transport contract
    (``{"v": 2, "text", "reply_markup", "media"?, "caption"?}``).

    *intent* carries ``intent_id``, ``file_name``, ``media_kind``,
    ``schedule_slot_at`` (ISO string or datetime) and ``tz``; with
    ``source_id`` + ``provider_file_ref`` (and ``workspace_id``, ``mime_type``,
    ``handle``) the card carries the MEDIA: the card is sent as ``text``, and
    the edit that brings its keyboard fetches the bytes and puts the photo or
    video on that message with ``caption`` — the account and the slot;
    ``text`` stays the card when it
    cannot. The slot renders in the WORKSPACE's timezone — a solo user reads
    his own clock, never UTC. A planned story's card (``origin``,
    ``scheduled_by``) also says who scheduled it, and whether it is late
    against *now*, the render's own instant (the clock's, when omitted).
    *note* is a waiting card's block note (`block_note`), one line under the
    slot. ``trimmed_to_seconds``, set for a past-posts video longer than a
    story, adds the line saying its story is trimmed.
    """
    intent_id = str(intent["intent_id"])
    slot = intent.get("schedule_slot_at")
    if isinstance(slot, str):
        slot = ensure_utc(datetime.fromisoformat(_canonical_fraction(slot)))
    tz = intent.get("tz") or "UTC"
    slot_line = f"Slot: {stamp(slot, tz)}"
    planned = _planned_line(intent, slot=slot, tz=tz, now=now)
    if planned:
        slot_line += f"\n{planned}"
    if intent.get("trimmed_to_seconds"):
        slot_line += f"\n✂️ Trimmed to {intent['trimmed_to_seconds']} s"
    if note:
        slot_line += f"\n{note}"
    file_name = intent.get("file_name") or "media"
    card_text = f"📸 {file_name} ({intent.get('media_kind', '?')})\n{slot_line}"
    payload: dict = {"v": 2, "text": card_text}
    keyboard = card_keyboard(
        intent_id,
        state=intent.get("state") or "awaiting_approval",
        api_publishing_enabled=api_publishing_enabled,
    )
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    if intent.get("provider_file_ref") and intent.get("source_id"):
        # The card becomes the photo: it is sent as `text`, and the edit that
        # brings its keyboard fetches these bytes under the workspace grant and
        # puts them on that message with `caption`; `text` stays the card when
        # it cannot.
        handle = intent.get("handle")
        # Bounded: Telegram captions stop at 1024 characters, and a Drive
        # file name has no bound of its own.
        who = f"@{handle}" if handle else file_name[:FILE_NAME_BOUND]
        if handle and intent.get("source_provider") == "instagram_past_posts":
            # The picture hides the text fallback, so its caption must still
            # identify the selected slide when an account handle is present.
            who += f"\n{file_name[:FILE_NAME_BOUND]}"
        payload["media"] = {
            "workspace_id": str(intent["workspace_id"]),
            "source_id": str(intent["source_id"]),
            "ref": str(intent["provider_file_ref"]),
            "kind": intent.get("media_kind"),
            "mime": intent.get("mime_type"),
            "file_name": file_name,
        }
        payload["caption"] = f"📸 {who}\n{slot_line}"
    return payload


#: The states in which a card still waits for its person — the slot has not
#: moved on. (`scheduled` is included: a card is minted in the transaction
#: that leaves it, and a fixture may not.) The sender's after-send edit reads
#: the same set.
LIVE_FOR_A_CARD = ("scheduled", "prompt_pending", "awaiting_approval")


def keyboard_of(intent_id: str, facts: Mapping) -> Optional[dict]:
    """`card_keyboard` for a story as `card_facts` read it."""
    return card_keyboard(
        str(intent_id),
        state=facts["state"],
        api_publishing_enabled=bool(facts["api_publishing_enabled"]),
        item_state=facts["item_state"],
        cancelling=bool(facts["cancel_requested"]),
    )


def missed_notice(
    *,
    file_name: Optional[str],
    handle: Optional[str],
    slot: datetime,
    tz: str,
    by: Optional[str],
    reason: str,
    repeats: Optional[bool] = None,
) -> str:
    """What the bound chats are told when a planned story is not served, and,
    for one that repeats, what became of its series: *repeats* is whether
    its next occurrence now waits, None for a story with no rule."""
    what = (file_name or "an item")[:FILE_NAME_BOUND]
    whose = account_label(handle)
    who = f" by {by}" if by else ""
    series = "" if repeats is None else f" {SERIES_WORDS[repeats]}"
    return (
        f"🗓 Not served: {what} for {whose}, scheduled for {stamp(slot, tz)}{who}:"
        f" {MISS_REASONS[reason]}. Nothing was posted.{series}"
    )
