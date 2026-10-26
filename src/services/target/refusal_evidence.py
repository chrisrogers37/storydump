"""The evidence a refused past-post fetch keeps, and what it never keeps.

A refusal's evidence is one JSON object, `{"v": 1, "kind": <kind>, ...}`: the
facts in scope where the fetch was refused, so a parked story says why without
anyone reading the provider again. It is attached to the raised error as its
`evidence` attribute and lands wherever the refusal is recorded: the asset's
`validation_capture -> 'refusal'`, the story's `last_error -> 'error' ->
'refusal'` and the park's audit detail. An error's message stays constant; the
evidence never reaches a log line or a traceback.

Redacted where it is captured, never later:

- the access token is never copied. A builder takes named values, never a
  request, a response or their headers, and `scrub_text` removes the token and
  any `access_token=` parameter from provider text;
- a signed media or CDN link keeps only its host and its expiry parameter
  (`oe`); its path, its signature and every other parameter are dropped;
- a Graph request keeps its path, never its query.

Media bytes and raw tool output are never stored.
"""

from __future__ import annotations

import re
from fractions import Fraction
from typing import Optional
from urllib.parse import parse_qs, urlsplit

VERSION = 1

#: Provider text is kept for the private record, bounded.
TEXT_LIMIT = 500
#: A short field: a type, a trace id, a content type, a media type.
FIELD_LIMIT = 100

_TOKEN_PARAM = re.compile(r"access_token=[^&\s]+", re.I)
#: A provider object id, as a message names it: masked in a chat line.
_LONG_ID = re.compile(r"\d{15,}")
#: How many measured values the generic sentence names, and how many items of
#: a list value.
_SAID_VALUES = 6
_SAID_ITEMS = 8
_LINK = re.compile(r"https?://\S+", re.I)


def evidence(kind: str, **facts) -> dict:
    """One refusal's evidence: its kind and the facts in scope."""
    return {"v": VERSION, "kind": kind, **facts}


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


def scrub_text(value, *, secrets=()) -> Optional[str]:
    """Provider text without the token, any token parameter or any link.

    A link becomes `<link host>`, so the text keeps where it pointed and loses
    what would open it. NUL is dropped (jsonb refuses it) and the result is
    bounded.
    """
    if not isinstance(value, str):
        return None
    text = value
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<token>")
    text = _TOKEN_PARAM.sub("access_token=<token>", text)
    text = _LINK.sub(
        lambda m: f"<link {link_evidence(m.group(0))['host'] or 'unknown'}>", text
    )
    return text.replace("\x00", "")[:TEXT_LIMIT]


def short(value) -> Optional[str]:
    """A short provider field as bounded text, or None."""
    if value is None:
        return None
    return str(value).replace("\x00", "")[:FIELD_LIMIT]


def whole(value) -> Optional[int]:
    """A provider number kept only when it is a whole number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flat(value) -> str:
    """A measured value as short text: a list as its first items, comma-joined."""
    if isinstance(value, (list, tuple)):
        return ", ".join(short(item) or "" for item in value[:_SAID_ITEMS])
    return short(value) or ""


def _count(value) -> str:
    n = whole(value)
    return f"{n:,}" if n is not None else "?"


def _seconds(value) -> str:
    """A measured Fraction's text as a short decimal, or the text as it is."""
    try:
        return f"{float(Fraction(str(value))):.2f}".rstrip("0").rstrip(".")
    except (OverflowError, ValueError, ZeroDivisionError):
        return str(value)[:FIELD_LIMIT]


#: What each local-validation check means in plain words, given what it measured.
_CHECKS = {
    "stream_topology": lambda m: "this file holds no video stream",
    "duration": lambda m: (
        f"this video is {_seconds(m.get('duration_seconds'))} s long;"
        " stories take 3 to 60 s"
    ),
    "fps": lambda m: (
        f"this video runs at {_seconds(m.get('fps'))} fps; stories take 23 to 60"
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


def plain(refusal) -> Optional[str]:
    """One sentence saying why a past post was refused, from its evidence.

    For the story's review card and its notice, which are private chat text:
    Instagram's code and message, or the check that failed and its value.
    None when the evidence says nothing more than the generic notice. The
    sentence carries no closing period; the caller ends it.
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
        sentence = f"Instagram refused the read ({', '.join(parts)})"
        if refusal.get("message"):
            # The stored message may name the object it refused: the record
            # keeps it, a chat line does not.
            sentence += f": {_LONG_ID.sub('<id>', str(refusal['message']))}"
        return sentence
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
