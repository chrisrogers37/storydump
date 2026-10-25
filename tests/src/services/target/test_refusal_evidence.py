"""`refusal_evidence` — what a refused fetch keeps of the provider's answer.

The module reduces a refusal to a small JSON object a private record may hold:
provider text without the token or any link, a signed link as its host and
expiry, a Graph request as its path. These tests pin each function's redaction
and bounds on its own; `test_instagram_past_posts_adapter.py` pins what each
kind of refusal records through the adapter.
"""

from __future__ import annotations

import json
import traceback

import pytest

from src.services.target import refusal_evidence

#: A signed CDN link as Graph hands one out, with everything around the host
#: that must not outlive `link_evidence`: credentials, a port, a path, signature
#: and routing parameters, a fragment. Only the host and the `oe` value survive.
SIGNED = (
    "https://fake-user:fake-pass@scontent-test.fbcdn.net:8443"
    "/v/t51.2885-15/signed-example.jpg"
    "?stp=dst-jpg_e35&_nc_cat=100&_nc_sid=sid-value-1&ccb=1-7"
    "&oh=00_signature-value&oe=6A1B2C3D#frag"
)
#: The pieces of SIGNED that are neither its host nor its expiry.
SIGNED_PARTS = (
    "fake-user",
    "fake-pass",
    "8443",
    "signed-example",
    "/v/t51",
    "stp=",
    "_nc_",
    "sid-value-1",
    "ccb=",
    "oh=",
    "00_signature-value",
    "frag",
    "?",
)
SECRET = "EAAB-fake-secret-value"


# --- the record's shape --------------------------------------------------------


@pytest.mark.unit
def test_evidence_is_the_version_the_kind_and_the_facts():
    """A refusal's evidence is the version, the kind and the facts, and survives a
    JSON round trip unchanged."""
    built = refusal_evidence.evidence("cdn_refused", statuses=[403, 404], host="h")
    assert built == {
        "v": 1,
        "kind": "cdn_refused",
        "statuses": [403, 404],
        "host": "h",
    }
    assert refusal_evidence.VERSION == 1
    assert json.loads(json.dumps(built)) == built


# --- attach and of -------------------------------------------------------------


@pytest.mark.unit
def test_attach_returns_the_error_it_was_given_with_the_evidence_on_it():
    """`raise attach(error, facts)` raises the very error, evidence readable by `of`."""
    facts = refusal_evidence.evidence("identity_mismatch", id_matches=False)
    error = RuntimeError("constant message")
    assert refusal_evidence.attach(error, facts) is error
    assert refusal_evidence.of(error) is facts


@pytest.mark.unit
def test_attached_evidence_rides_a_raise_and_leaves_the_message_alone():
    """The evidence survives the raise; the error's message and args stay constant."""
    facts = refusal_evidence.evidence("graph_error", message="provider text")
    with pytest.raises(RuntimeError) as caught:
        raise refusal_evidence.attach(RuntimeError("constant message"), facts)
    assert refusal_evidence.of(caught.value) is facts
    assert str(caught.value) == "constant message"
    assert caught.value.args == ("constant message",)


@pytest.mark.unit
def test_attached_evidence_never_reaches_a_log_line_or_traceback():
    """What an error renders as (str, repr, a formatted traceback) omits its evidence."""
    facts = refusal_evidence.evidence("graph_error", message="PROVIDER-TEXT")
    with pytest.raises(RuntimeError) as caught:
        raise refusal_evidence.attach(RuntimeError("constant message"), facts)
    error = caught.value
    rendered = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert "constant message" in rendered  # the traceback did render
    for shown in (str(error), repr(error), rendered):
        assert "PROVIDER-TEXT" not in shown


@pytest.mark.unit
def test_of_an_error_without_evidence_is_none():
    """A plain exception carries no evidence, and `of` says so with None."""
    assert refusal_evidence.of(ValueError("plain")) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [None, "text", ["kind"], ("v", 1), 7],
    ids=["none", "text", "list", "tuple", "number"],
)
def test_of_ignores_an_evidence_attribute_that_is_not_a_dict(value):
    """Only a dict is evidence: any other `evidence` attribute reads as none."""
    error = RuntimeError("plain")
    error.evidence = value
    assert refusal_evidence.of(error) is None


@pytest.mark.unit
def test_of_hands_back_an_empty_dict_rather_than_none():
    """An empty dict is still evidence: `of` checks the type, not the truth."""
    error = refusal_evidence.attach(RuntimeError("plain"), {})
    assert refusal_evidence.of(error) == {}


# --- graph_path ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "url,path",
    [
        (
            "https://graph.instagram.com/v26.0/17890000000000003",
            "/v26.0/17890000000000003",
        ),
        (
            "https://graph.instagram.com/v26.0/17890000000000003"
            "?fields=id%2Cowner&access_token=FAKE-TOKEN",
            "/v26.0/17890000000000003",
        ),
        (
            "https://graph.instagram.com/v26.0/17890000000000003/children"
            "?limit=20#frag",
            "/v26.0/17890000000000003/children",
        ),
        ("https://graph.instagram.com", ""),
    ],
    ids=["bare", "query", "fragment", "no-path"],
)
def test_graph_path_is_the_path_and_never_the_query(url, path):
    """A request URL reduces to its path: no host, no query, no fragment."""
    assert refusal_evidence.graph_path(url) == path


@pytest.mark.unit
def test_graph_path_of_an_unparseable_url_is_empty_and_echoes_nothing():
    """A URL the parser refuses yields an empty path, not the text it was given."""
    url = "https://[unclosed/v26.0/1?access_token=FAKE-TOKEN"
    assert refusal_evidence.graph_path(url) == ""


# --- link_evidence -------------------------------------------------------------


@pytest.mark.unit
def test_link_evidence_keeps_the_host_and_the_expiry_only():
    """A signed link keeps its host and `oe` value: no credentials, port, path,
    signature or other parameter survives in the dict or its JSON."""
    kept = refusal_evidence.link_evidence(SIGNED)
    assert kept == {"host": "scontent-test.fbcdn.net", "expiry": "6A1B2C3D"}
    encoded = json.dumps(kept)
    for dropped in SIGNED_PARTS:
        assert dropped not in encoded


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://scontent-test.fbcdn.net/a.jpg?stp=dst-jpg",
        "https://scontent-test.fbcdn.net/a.jpg",
        "https://scontent-test.fbcdn.net/a.jpg?oe=",
    ],
    ids=["other-parameters", "no-query", "blank-expiry"],
)
def test_a_link_without_an_expiry_has_its_host_and_no_expiry(url):
    """No usable `oe` value is a None expiry, never a guessed one."""
    assert refusal_evidence.link_evidence(url) == {
        "host": "scontent-test.fbcdn.net",
        "expiry": None,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        None,
        12345,
        ["https://scontent-test.fbcdn.net/a.jpg?oe=1"],
        "",
        "not a link",
        "https://[unclosed/x?oe=1",
    ],
    ids=["none", "number", "list", "empty", "words", "unparseable"],
)
def test_link_evidence_of_garbage_is_empty(value):
    """Anything that is not a parseable link has no host and no expiry."""
    assert refusal_evidence.link_evidence(value) == {"host": None, "expiry": None}


@pytest.mark.unit
def test_link_evidence_is_bounded():
    """A long host and a long `oe` value are cut, so a link cannot smuggle a
    long string into the record through either field."""
    host = "a" * (refusal_evidence.FIELD_LIMIT + 50) + ".fbcdn.net"
    kept = refusal_evidence.link_evidence(f"https://{host}/x.jpg?oe={'9' * 500}")
    assert kept["host"] == host[: refusal_evidence.FIELD_LIMIT]
    assert 0 < len(kept["expiry"]) < 500


# --- scrub_text ----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,secrets,scrubbed",
    [
        (
            "Cannot parse access token FAKE-TOKEN-1",
            ("FAKE-TOKEN-1",),
            "Cannot parse access token <token>",
        ),
        ("FAKE-TOKEN-1 then FAKE-TOKEN-1", ("FAKE-TOKEN-1",), "<token> then <token>"),
        (
            "a FAKE-TOKEN-1 b FAKE-TOKEN-2",
            ("FAKE-TOKEN-1", "FAKE-TOKEN-2"),
            "a <token> b <token>",
        ),
        ("key EAAB.fake+secret(1) end", ("EAAB.fake+secret(1)",), "key <token> end"),
    ],
    ids=["once", "repeated", "several", "literal-not-a-pattern"],
)
def test_scrub_text_removes_the_given_secret_wherever_it_appears(
    text, secrets, scrubbed
):
    """Each given secret is replaced, every time, as literal text."""
    assert refusal_evidence.scrub_text(text, secrets=secrets) == scrubbed


@pytest.mark.unit
def test_scrub_text_ignores_an_empty_secret():
    """An unset token (empty or None) scrubs nothing, rather than every gap."""
    assert refusal_evidence.scrub_text("plain text", secrets=("", None)) == "plain text"


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,scrubbed",
    [
        (
            "GET /me?fields=id&access_token=IGQ-fake-value&limit=1 failed",
            "GET /me?fields=id&access_token=<token>&limit=1 failed",
        ),
        (
            "ACCESS_TOKEN=abc123 and Access_Token=def456",
            "access_token=<token> and access_token=<token>",
        ),
        ("access_token=IGQ-fake-value", "access_token=<token>"),
    ],
    ids=["in-a-query", "any-case", "alone"],
)
def test_scrub_text_removes_the_value_of_any_token_parameter(text, scrubbed):
    """A token parameter loses its value whether or not the token was given."""
    assert refusal_evidence.scrub_text(text) == scrubbed


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,scrubbed",
    [
        (
            "see https://scontent-test.cdninstagram.com/v/t51/x.jpg"
            "?oh=00_signature-value&oe=6A1B2C3D now",
            "see <link scontent-test.cdninstagram.com> now",
        ),
        (
            "a http://graph.instagram.com/v26.0/1?fields=id b "
            "HTTPS://Scontent-Test.fbcdn.net/y c",
            "a <link graph.instagram.com> b <link scontent-test.fbcdn.net> c",
        ),
        ("go to https://[unclosed/x?oe=1 now", "go to <link unknown> now"),
        (
            "https://graph.instagram.com/v26.0/1?fields=id&access_token=FAKE-TOKEN-1",
            "<link graph.instagram.com>",
        ),
    ],
    ids=["signed-link", "several-links", "no-host", "token-parameter-in-a-link"],
)
def test_scrub_text_replaces_every_link_with_its_host(text, scrubbed):
    """A link becomes `<link host>`: where it pointed, never what would open it."""
    assert refusal_evidence.scrub_text(text) == scrubbed


@pytest.mark.unit
def test_scrub_text_leaves_plain_text_alone():
    """Control for the cases above: text with nothing to scrub is returned as is."""
    text = "Unsupported get request. The media does not exist."
    assert refusal_evidence.scrub_text(text, secrets=(SECRET,)) == text
    assert refusal_evidence.scrub_text("") == ""


@pytest.mark.unit
def test_scrub_text_drops_nul():
    """NUL is dropped (jsonb refuses it)."""
    assert refusal_evidence.scrub_text("a\x00b\x00") == "ab"


@pytest.mark.unit
def test_scrub_text_is_bounded_at_the_text_limit():
    """Text at the limit is whole; anything longer is cut to the limit."""
    limit = refusal_evidence.TEXT_LIMIT
    exact = "x" * limit
    assert refusal_evidence.scrub_text(exact) == exact
    assert refusal_evidence.scrub_text(exact + "y") == exact
    assert refusal_evidence.scrub_text("x" * (limit * 3)) == exact


@pytest.mark.unit
def test_scrub_text_removes_a_secret_before_it_cuts_so_no_prefix_survives():
    """A secret that straddles the limit is replaced whole, then the text is
    cut: cutting first would leave the secret's first characters in the record."""
    limit = refusal_evidence.TEXT_LIMIT
    text = "x" * (limit - 4) + SECRET + " trailing"
    scrubbed = refusal_evidence.scrub_text(text, secrets=(SECRET,))
    assert len(scrubbed) == limit
    assert SECRET[:4] not in scrubbed


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [None, 190, 1.5, True, b"bytes", ["text"], {"message": "text"}],
    ids=["none", "int", "float", "bool", "bytes", "list", "dict"],
)
def test_scrub_text_of_a_non_string_is_none(value):
    """Only text is provider text: anything else is no message."""
    assert refusal_evidence.scrub_text(value) is None


# --- short and whole -----------------------------------------------------------


@pytest.mark.unit
def test_short_keeps_a_provider_field_as_bounded_text():
    """A short field is text without NUL, cut to the field limit, whatever type
    the provider sent."""
    limit = refusal_evidence.FIELD_LIMIT
    exact = "x" * limit
    assert refusal_evidence.short("OAuthException") == "OAuthException"
    assert refusal_evidence.short(190) == "190"
    assert refusal_evidence.short("") == ""
    assert refusal_evidence.short("a\x00b") == "ab"
    assert refusal_evidence.short(exact) == exact
    assert refusal_evidence.short(exact + "y") == exact


@pytest.mark.unit
def test_short_of_none_is_none():
    """An absent field stays absent; it does not become the text 'None'."""
    assert refusal_evidence.short(None) is None


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 1, 190, -1, 10**12])
def test_whole_keeps_a_whole_number(value):
    """A provider number that is an int is kept as it is, zero and negatives too."""
    assert refusal_evidence.whole(value) == value


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [True, False, "190", 190.0, 1.5, None, [], {}, b"1"],
    ids=[
        "true",
        "false",
        "text",
        "whole-float",
        "float",
        "none",
        "list",
        "dict",
        "bytes",
    ],
)
def test_whole_drops_anything_that_is_not_a_whole_number(value):
    """Text, floats and bools (an int subclass) are not whole numbers."""
    assert refusal_evidence.whole(value) is None
