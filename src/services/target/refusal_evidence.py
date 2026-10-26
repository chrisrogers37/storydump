"""The evidence a refusal keeps, and what it never keeps: a past post's refused
fetch, and a container Instagram would not process.

A refusal's evidence is one JSON object, `{"v": 1, "kind": <kind>, ...}`: the
facts in scope where the fetch was refused, so a parked story says why without
anyone reading the provider again. It is attached to the raised error as its
`evidence` attribute and lands wherever the refusal is recorded: the asset's
`validation_capture -> 'refusal'`, the story's `last_error -> 'error' ->
'refusal'` and the park's audit detail. An error's message stays constant; the
evidence never reaches a log line or a traceback. A dead container's evidence,
a `container_dead` refusal, is written by the publish pipeline straight into
the story's `last_error -> 'error' -> 'refusal'`.

A route into review that refuses nothing records its own reason in the same
place (`review_error`, one of `REVIEW_KINDS`), so a story's review card says
why it went into review and never an earlier attempt's refusal. The card says
such a reason as "Why", a refusal as "Refused" (`label`).

Redacted where it is captured, never later:

- the access token is never copied. A builder takes named values, never a
  request, a response or their headers, and `scrub_text` removes from provider
  text the token as given or percent-encoded, the value of any token parameter
  however it is written, and a bearer credential;
- a signed media or CDN link keeps only its host and its expiry parameter
  (`oe`); its path, its signature and every other parameter are dropped. In
  provider text, `scrub_text` says a link as its host alone, with or without
  its scheme, and masks a delivery signature (`s--…--`) that stands outside
  one;
- a Graph request keeps its path, never its query.

Media bytes and raw tool output are never stored.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Optional
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

VERSION = 1

#: Provider text is kept for the private record, bounded.
TEXT_LIMIT = 500
#: A short field: a type, a trace id, a content type, a media type.
FIELD_LIMIT = 100

#: A token parameter's value, however the parameter is written: as a query
#: parameter (`=`, percent-encoded or JSON-escaped), with spaces around its
#: sign, or as a quoted key. The value ends at a separator, a quote or a
#: masked link; a value that runs into a path (a link written up against it)
#: runs on to the next space, so no part of that link is left behind.
_TOKEN_VALUE = r"""(?:(?!%26|\\u0026)[^&\s"',;}<])"""
_TOKEN_PARAM = re.compile(
    r"""access_token(?P<sep>["']?\s*(?:=|:|%3D|\\u003d)\s*["']?)"""
    rf"""(?:{_TOKEN_VALUE}*/\S*|{_TOKEN_VALUE}+)""",
    re.I,
)
#: A bearer credential, as an Authorization header's text says it.
_BEARER = re.compile(r"\b(?P<scheme>bearer\s+)[A-Za-z0-9._~+/-]{8,}=*", re.I)
#: What can split a secret without being seen: NUL (jsonb refuses it too) and
#: the zero-width characters.
_INVISIBLE = re.compile("[\x00\u200b\u200c\u200d\u2060\ufeff]")
#: A provider object id, as a message names it: masked in a chat line.
_LONG_ID = re.compile(r"\d{15,}")
#: How many measured values the generic sentence names, and how many items of
#: a list value.
_SAID_VALUES = 6
_SAID_ITEMS = 8
_LINK = re.compile(r"https?://\S+", re.I)
#: A link written without its scheme: a host name, then a path.
_BARE_LINK = re.compile(
    r"(?<![\w.@/-])(?://)?(?P<host>(?:[a-z0-9-]+\.)+[a-z]{2,})/\S*", re.I
)
#: A delivery signature standing outside a link: the signed delivery url it
#: comes from does not expire.
_SIGNATURE = re.compile(r"s--[A-Za-z0-9_-]+--")


def evidence(kind: str, **facts) -> dict:
    """One refusal's evidence: its kind and the facts in scope."""
    return {"v": VERSION, "kind": kind, **facts}


def review_error(error_type: str, message: str, kind: str, **facts) -> dict:
    """The `last_error -> 'error'` a route into review records when no
    provider error says why: a type and a message for the operator, and the
    reason, one of `REVIEW_KINDS`, as the refusal the review card reads."""
    return {
        "type": error_type,
        "code": None,
        "message": message,
        "refusal": evidence(kind, **facts),
    }


def label(refusal) -> str:
    """The word a reason line opens with: "Why" for a review kind, since
    nothing refused the story, else "Refused"."""
    kind = refusal.get("kind") if isinstance(refusal, dict) else None
    return "Why" if isinstance(kind, str) and kind in REVIEW_KINDS else "Refused"


def attach(exc: BaseException, facts: dict) -> BaseException:
    """`exc` carrying `facts` as its evidence; for `raise attach(...)`."""
    exc.evidence = facts
    return exc


def of(exc: BaseException) -> Optional[dict]:
    """The evidence `exc` carries, or None."""
    value = getattr(exc, "evidence", None)
    return value if isinstance(value, dict) else None


def graph_path(url: str) -> str:
    """A request URL's path. Its query, and so any parameter, is never read."""
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def link_evidence(url) -> dict:
    """A signed link's host and its expiry parameter; nothing else of it."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        expiry = parse_qs(parsed.query).get("oe", [None])[0]
    except (AttributeError, TypeError, ValueError):
        return {"host": None, "expiry": None}
    return {
        "host": host[:FIELD_LIMIT] if host else None,
        "expiry": expiry[:32] if isinstance(expiry, str) else None,
    }


def _secret_forms(secret: str) -> list:
    """*secret* as given and percent-encoded, in either letter case of its
    escapes, longest first, so no form is masked in part by a shorter one."""
    forms = {secret, quote(secret, safe=""), quote_plus(secret, safe="")}
    forms |= {re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), f) for f in forms}
    return sorted(forms, key=len, reverse=True)


def _masked_link(host) -> str:
    """How provider text says a link: its host alone."""
    return f"<link {host or 'unknown'}>"


def scrub_text(value, *, secrets=()) -> Optional[str]:
    """Provider text without the token, any token parameter, any bearer
    credential, any link or any delivery signature.

    A link becomes `<link host>`, so the text keeps where it pointed and loses
    what would open it, and a signature outside a link becomes `s--<SIG>--`.
    A link with its scheme is masked before parameters, so a parameter's value
    written up against it cannot leave its tail behind; one without its scheme
    after them, so its host cannot take in a value written up against it. NUL
    and the zero-width characters are dropped first, so none can split what
    the scrub matches, and the result is bounded.
    """
    if not isinstance(value, str):
        return None
    text = _INVISIBLE.sub("", value)
    for secret in secrets:
        if secret:
            for form in _secret_forms(secret):
                text = text.replace(form, "<token>")
    text = _LINK.sub(lambda m: _masked_link(link_evidence(m.group(0))["host"]), text)
    text = _TOKEN_PARAM.sub(lambda m: f"access_token{m.group('sep')}<token>", text)
    text = _BARE_LINK.sub(
        lambda m: _masked_link(m.group("host").lower()[:FIELD_LIMIT]), text
    )
    text = _BEARER.sub(lambda m: f"{m.group('scheme')}<token>", text)
    text = _SIGNATURE.sub("s--<SIG>--", text)
    return text[:TEXT_LIMIT]


def short(value) -> Optional[str]:
    """A short provider field as bounded text, or None."""
    if value is None:
        return None
    return str(value).replace("\x00", "")[:FIELD_LIMIT]


def whole(value) -> Optional[int]:
    """A provider number kept only when it is a whole number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flat(value) -> str:
    """A measured value as short text: a list as its distinct items, the first
    few comma-joined, and how many more there are."""
    if isinstance(value, (list, tuple)):
        items = list(dict.fromkeys(short(item) or "" for item in value))
        said = ", ".join(items[:_SAID_ITEMS])
        more = len(items) - _SAID_ITEMS
        return f"{said} and {more} more" if more > 0 else said
    return short(value) or ""


def _with_words(sentence: str, words) -> str:
    """*sentence*, then Instagram's own *words* after a colon when it gave any.
    They may name the object they refer to: the record keeps it, a chat line
    does not."""
    return f"{sentence}: {_LONG_ID.sub('<id>', str(words))}" if words else sentence


def _count(value) -> str:
    n = whole(value)
    return f"{n:,}" if n is not None else "?"


def _counted(value, one: str, many: str) -> str:
    """A count and its noun: *one* for one, *many* otherwise."""
    return f"{_count(value)} {one if whole(value) == 1 else many}"


def _seconds(value) -> str:
    """A measured Fraction's text as a short decimal, or the text as it is."""
    try:
        return f"{float(Fraction(str(value))):.2f}".rstrip("0").rstrip(".")
    except (OverflowError, ValueError, ZeroDivisionError):
        return str(value)[:FIELD_LIMIT]


def _held_to(measured, low: str, high: str, unit: str = "") -> str:
    """The bounds a check recorded, as what stories take; nothing for a record
    that kept none, since the bounds of its day are not read again."""
    if measured.get(low) is None or measured.get(high) is None:
        return ""
    return (
        f"; stories take {_seconds(measured[low])} to {_seconds(measured[high])}{unit}"
    )


#: What each local-validation check means in plain words, given what it measured.
_CHECKS = {
    "stream_topology": lambda m: "this file holds no video stream",
    "duration": lambda m: (
        f"this video is {_seconds(m.get('duration_seconds'))} s long"
        + _held_to(m, "min_seconds", "max_seconds", " s")
    ),
    "fps": lambda m: (
        f"this video runs at {_seconds(m.get('fps'))} fps"
        + _held_to(m, "min_fps", "max_fps")
    ),
    "video_codec": lambda m: (
        f"this video carries {_flat(m.get('refused_codecs')) or 'a stream'}, which"
        " stories do not take: they take H.264 or HEVC video and AAC audio"
    ),
    "image_format": lambda m: (
        f"this image is {m.get('format') or 'another format'}, not a JPEG"
    ),
    "malformed_image": lambda m: "this image is damaged or not an image",
    "malformed_container": lambda m: "this video file is damaged",
    "malformed_video": lambda m: "this video's stream is damaged",
    "external_reference": lambda m: "this video refers to media outside its own file",
    "byte_limit": lambda m: (
        f"this file is {_count(m.get('bytes'))} bytes; the limit is {_count(m.get('cap'))}"
    ),
    # The story derivation, the file Instagram fetches (`judge_derivation`).
    "derivation_bytes": lambda m: (
        f"the file Instagram fetches is {_count(m.get('bytes'))} bytes;"
        f" stories take up to {_count(m.get('limit'))}"
    ),
    "derivation_moov_position": lambda m: (
        "the video Instagram fetches has its index after its media;"
        " stories need it at the front"
    ),
    "derivation_edit_list": lambda m: (
        "the video Instagram fetches has an edit list; stories take none"
    ),
    "derivation_audio_codec": lambda m: (
        f"the video Instagram fetches carries {_flat(m.get('codec')) or 'unnamed'}"
        " audio; stories take AAC"
    ),
    "derivation_audio_channels": lambda m: (
        f"the video Instagram fetches has {_count(m.get('channels'))} audio"
        f" channels; stories take up to {_count(m.get('limit'))}"
    ),
    "derivation_audio_rate": lambda m: (
        f"the video Instagram fetches has audio at {_count(m.get('hz'))} Hz;"
        f" stories take up to {_count(m.get('limit'))}"
    ),
}

#: Why a story went into review by a route that refused nothing, given what
#: the route counted: its container never reported ready, Instagram's answer
#: to preparing its media was lost, its job's budget is spent, its publish was
#: never confirmed, or it waited approved until the reaper parked it.
_REVIEW = {
    "container_pending": lambda r: (
        "Instagram did not report this story's media as ready on the last of"
        f" {_counted(r.get('tries'), 'try', 'tries')}"
    ),
    "answer_lost": lambda r: (
        "Instagram's answer was lost on the last of"
        f" {_counted(r.get('tries'), 'try', 'tries')} to prepare this story's media"
    ),
    "budget_spent": lambda r: (
        f"we hit a fault on our side {_counted(r.get('tries'), 'time', 'times')}"
        " trying to post this story"
    ),
    "publish_unconfirmed": lambda r: (
        "Instagram's answer to the post was lost, and it could not be confirmed"
        f" as posted ({_counted(r.get('checks'), 'check', 'checks')})"
    ),
    "approved_stale": lambda r: (
        f"we lost track of this story for {_counted(r.get('days'), 'day', 'days')}"
    ),
}
#: The kinds `review_error` records, which a card says as "Why" (`label`).
REVIEW_KINDS = frozenset(_REVIEW)


def plain(refusal) -> Optional[str]:
    """One sentence saying why a story was refused, from its evidence.

    For the story's review card and its notice, which are private chat text:
    Instagram's code and message, its status for a container it would not
    process, or the check that failed and its value.
    None when the refusal is not a record. The sentence carries no closing
    period; the caller ends it.
    """
    try:
        sentence = _plain(refusal)
    except (
        AttributeError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
        ZeroDivisionError,
    ):
        # A record the writers cannot produce today; a reason line is never
        # worth failing the park or the card it would sit in.
        return None
    return sentence.rstrip(" .")[:TEXT_LIMIT] if sentence else None


def _plain(refusal) -> Optional[str]:
    if not isinstance(refusal, dict):
        return None
    kind = refusal.get("kind")
    if kind == "graph_error":
        parts = [f"HTTP {refusal.get('http_status')}"]
        if refusal.get("code") is not None:
            code = f"code {refusal['code']}"
            if refusal.get("error_subcode") is not None:
                code += f"/{refusal['error_subcode']}"
            parts.append(code)
        if refusal.get("type"):
            parts.append(str(refusal["type"]))
        return _with_words(
            f"Instagram refused the read ({', '.join(parts)})", refusal.get("message")
        )
    if kind == "ownership_unproven":
        return "Instagram did not show this post as the connected account's"
    if kind == "identity_mismatch":
        return "Instagram returned a different post than the one indexed"
    if kind == "slide_gone":
        indexed = refusal.get("slide_count")
        of = f" of the {indexed} indexed" if indexed is not None else ""
        return (
            "the chosen slide is no longer in the post: Instagram listed"
            f" {_count(refusal.get('children'))} slide(s){of}"
        )
    if kind == "no_media_link":
        media = str(refusal.get("media_type") or "media").lower()
        return f"Instagram gave no file link for this {media}"
    if kind == "untrusted_host":
        return f"Instagram's file link points to an untrusted host ({refusal.get('host') or 'unknown'})"
    if kind == "over_byte_cap":
        return (
            f"this file is over the {_count(refusal.get('cap'))}-byte limit"
            f" (stopped after {_count(refusal.get('received'))} bytes)"
        )
    if kind in ("cdn_refused", "cdn_error"):
        statuses = "/".join(str(s) for s in refusal.get("statuses") or []) or "?"
        verb = "refused the link twice" if kind == "cdn_refused" else "failed"
        return f"Instagram's file server {verb} (HTTP {statuses})"
    if kind == "wrong_bytes":
        return (
            f"Instagram served {refusal.get('content_type') or 'no content type'}"
            f" ({_count(refusal.get('length'))} bytes) instead of a story's image or video"
        )
    if kind == "slide_changed":
        return (
            "Instagram's slide list was incomplete"
            f" ({_count(refusal.get('children'))} listed), so the chosen slide could"
            " not be confirmed"
        )
    if kind == "container_dead":
        code = short(refusal.get("status_code"))
        what = (
            "Instagram let this story's media expire before it was posted"
            if code == "EXPIRED"
            else "Instagram could not process this story's media"
        )
        if code:
            what += f" ({code})"
        return _with_words(what, refusal.get("status"))
    if isinstance(kind, str) and kind in _REVIEW:
        return _REVIEW[kind](refusal)
    if kind == "local_validation":
        check = short(refusal.get("check"))
        measured = refusal.get("measured") or {}
        if not check:
            return "this file failed a check that was not named"
        if refusal.get("state") == "validation_pending":
            # A pending result is not a failure: the check could not finish.
            return f"this file could not be fully checked ({check})"
        render = _CHECKS.get(check)
        if render is not None:
            return render(measured)
        values = "; ".join(
            f"{short(key)} {_flat(value)}"
            for key, value in list(measured.items())[:_SAID_VALUES]
        )
        return f"this file failed the {check} check" + (
            f" ({values})" if values else ""
        )
    # A record with nothing specific still says what it holds: its error's
    # class for a refusal raised without evidence, else its kind.
    held = short(refusal.get("error")) if kind == "unrecorded" else short(kind)
    return f"the fetch was refused ({held or 'unknown'}) and no detail was recorded"
