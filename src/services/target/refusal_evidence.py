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
from typing import Optional
from urllib.parse import parse_qs, urlsplit

VERSION = 1

#: Provider text is kept for the private record, bounded.
TEXT_LIMIT = 500
#: A short field: a type, a trace id, a content type, a media type.
FIELD_LIMIT = 100

_TOKEN_PARAM = re.compile(r"access_token=[^&\s]+", re.I)
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
