"""`refusal_evidence` — what a refused fetch keeps of the provider's answer.

The module reduces a refusal to a small JSON object a private record may hold:
provider text without the token or any link, a signed link as its host and
expiry, a Graph request as its path. These tests pin each function's redaction
and bounds on its own; `test_instagram_past_posts_adapter.py` pins what each
kind of refusal records through the adapter.

`plain` is the other direction: the one sentence a story's review card and its
park notice say of a refusal, from the facts the record holds. Its sentences are
pinned whole, one per kind of fetch refusal and per local-validation check, with
the park notice built from them.
"""

from __future__ import annotations

import json
import traceback

import pytest

from src.services.target import instagram_publish, refusal_evidence

#: A signed CDN link as Graph hands one out, with everything around the host
#: that must not outlive `link_evidence`: a port, a path, signature and routing
#: parameters, a fragment. Only the host and the `oe` value survive.
SIGNED = (
    "https://scontent-test.fbcdn.net:8443"
    "/v/t51.2885-15/signed-example.jpg"
    "?stp=dst-jpg_e35&_nc_cat=100&_nc_sid=sid-value-1&ccb=1-7"
    "&oh=00_signature-value&oe=6A1B2C3D#frag"
)
#: The pieces of SIGNED that are neither its host nor its expiry.
SIGNED_PARTS = (
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


# --- plain ---------------------------------------------------------------------

#: What the turn records beside every refusal's own facts. `plain` never reads it.
COUNTERS = {
    "graph_attempts": 2,
    "observed_parents": 1,
    "source_fence_drop": 0,
    "listed_parents": 0,
    "cdn_attempts": 1,
}
#: What a container check measures: the boxes read and the sample codecs found.
CONTAINER = {
    "boxes": ["ftyp", "moov", "mdat"],
    "box_count": 3,
    "sample_codecs": ["avc1", "mp4a"],
}
ASKED = "17890000000000003"
HOST = "scontent-test.fbcdn.net"
EXPIRY = "6A1B2C3D"


def fetch_refusal(kind, **facts):
    """A refused fetch's evidence as the adapter and the preparation record it:
    the kind, its facts, and the turn's counters."""
    return refusal_evidence.evidence(kind, counters=dict(COUNTERS), **facts)


def validation_refusal(check, **measured):
    """A failed local check's evidence as the preparation records it: the check,
    what it measured, the bytes judged, the matrix and the turn's counters."""
    return refusal_evidence.evidence(
        "local_validation",
        state="unsupported",
        check=check,
        measured=measured,
        bytes=1_048_576,
        matrix_version="matrix-test",
        counters=dict(COUNTERS),
    )


#: One refusal of each kind of fetch, and the sentence it reads as. The record's
#: other fields (host, expiry, path, trace id, counters) sit beside the facts and
#: are not said.
FETCH_SENTENCES = [
    pytest.param(
        fetch_refusal(
            "graph_error",
            http_status=400,
            code=100,
            error_subcode=33,
            type="GraphMethodException",
            message="Unsupported get request.",
            fbtrace_id="AbC123fakeTrace",
            path=f"/v26.0/{ASKED}",
        ),
        (
            "Instagram refused the read (HTTP 400, code 100/33,"
            " GraphMethodException): Unsupported get request"
        ),
        id="graph-error",
    ),
    pytest.param(
        fetch_refusal(
            "graph_error",
            http_status=403,
            code=24,
            error_subcode=None,
            type="OAuthException",
            message="Media not found",
            fbtrace_id=None,
            path=f"/v26.0/{ASKED}/children",
        ),
        (
            "Instagram refused the read (HTTP 403, code 24, OAuthException):"
            " Media not found"
        ),
        id="graph-error-without-a-subcode",
    ),
    pytest.param(
        fetch_refusal(
            "graph_error",
            http_status=404,
            code=None,
            error_subcode=None,
            type=None,
            message=None,
            fbtrace_id=None,
            path=f"/v26.0/{ASKED}",
        ),
        "Instagram refused the read (HTTP 404)",
        id="graph-error-status-only",
    ),
    pytest.param(
        fetch_refusal(
            "ownership_unproven",
            test="app_scoped_id",
            match={"account_matches": False, "app_scoped_id_matches": False},
        ),
        "Instagram did not show this post as the connected account's",
        id="ownership-unproven",
    ),
    pytest.param(
        fetch_refusal("identity_mismatch", id_matches=False),
        "Instagram returned a different post than the one indexed",
        id="identity-mismatch",
    ),
    pytest.param(
        fetch_refusal("slide_gone", children=2, complete=True, slide_count=4),
        (
            "the chosen slide is no longer in the post: Instagram listed 2 slide(s)"
            " of the 4 indexed"
        ),
        id="slide-gone",
    ),
    pytest.param(
        fetch_refusal("slide_gone", children=0, complete=True, slide_count=3),
        (
            "the chosen slide is no longer in the post: Instagram listed 0 slide(s)"
            " of the 3 indexed"
        ),
        id="slide-gone-none-left",
    ),
    pytest.param(
        fetch_refusal("slide_gone", children=1, complete=True, slide_count=None),
        "the chosen slide is no longer in the post: Instagram listed 1 slide(s)",
        id="slide-gone-without-an-indexed-count",
    ),
    pytest.param(
        fetch_refusal("no_media_link", media_type="VIDEO", has_link=False),
        "Instagram gave no file link for this video",
        id="no-media-link",
    ),
    pytest.param(
        fetch_refusal("no_media_link", media_type=None, has_link=False),
        "Instagram gave no file link for this media",
        id="no-media-link-without-a-type",
    ),
    pytest.param(
        fetch_refusal("untrusted_host", host="cdn.untrusted.example", expiry=None),
        "Instagram's file link points to an untrusted host (cdn.untrusted.example)",
        id="untrusted-host",
    ),
    pytest.param(
        fetch_refusal("untrusted_host", host=None, expiry=None),
        "Instagram's file link points to an untrusted host (unknown)",
        id="untrusted-host-unparsed",
    ),
    pytest.param(
        fetch_refusal("over_byte_cap", cap=40000000, received=40000003, host=HOST),
        (
            "this file is over the 40,000,000-byte limit"
            " (stopped after 40,000,003 bytes)"
        ),
        id="over-byte-cap",
    ),
    pytest.param(
        fetch_refusal("cdn_refused", statuses=[403, 404], host=HOST, expiry=EXPIRY),
        "Instagram's file server refused the link twice (HTTP 403/404)",
        id="cdn-refused",
    ),
    pytest.param(
        fetch_refusal("cdn_error", statuses=[403, 502], host=HOST, expiry=EXPIRY),
        "Instagram's file server failed (HTTP 403/502)",
        id="cdn-error-after-a-refusal",
    ),
    pytest.param(
        fetch_refusal("cdn_error", statuses=[500], host=HOST, expiry=None),
        "Instagram's file server failed (HTTP 500)",
        id="cdn-error",
    ),
    pytest.param(
        fetch_refusal("cdn_error", statuses=[], host=HOST, expiry=None),
        "Instagram's file server failed (HTTP ?)",
        id="cdn-error-with-no-status-recorded",
    ),
    pytest.param(
        fetch_refusal(
            "wrong_bytes",
            content_type="text/html",
            length=1532,
            http_status=200,
            host=HOST,
        ),
        (
            "Instagram served text/html (1,532 bytes)"
            " instead of a story's image or video"
        ),
        id="wrong-bytes",
    ),
    pytest.param(
        fetch_refusal(
            "wrong_bytes", content_type="", length=0, http_status=200, host=HOST
        ),
        (
            "Instagram served no content type (0 bytes)"
            " instead of a story's image or video"
        ),
        id="wrong-bytes-empty",
    ),
]

#: One local-validation refusal per `_CHECKS` entry, and a second where an entry
#: reads its measurements differently: (check, what it measured, the sentence).
CHECK_CASES = [
    pytest.param("edit_list", CONTAINER, "this video has an edit list", id="edit_list"),
    pytest.param(
        "moov_not_front",
        CONTAINER,
        "this video's index comes after its media",
        id="moov_not_front",
    ),
    pytest.param(
        "duration",
        {"duration_seconds": "145/2", "fps": "30/1", "video_frames": 2175},
        "this video is 72.5 s long; stories take 3 to 60 s",
        id="duration",
    ),
    pytest.param(
        "duration",
        {"duration_seconds": "2", "fps": "30/1", "video_frames": 60},
        "this video is 2 s long; stories take 3 to 60 s",
        id="duration-whole-seconds",
    ),
    pytest.param(
        "duration",
        {"duration_seconds": "7/3", "fps": "30/1", "video_frames": 70},
        "this video is 2.33 s long; stories take 3 to 60 s",
        id="duration-to-a-hundredth",
    ),
    pytest.param(
        "fps",
        {"duration_seconds": "10", "fps": "20/1", "video_frames": 200},
        "this video runs at 20 fps; stories take 23 to 60",
        id="fps",
    ),
    pytest.param(
        "fps",
        {"duration_seconds": "10", "fps": "121/2", "video_frames": 605},
        "this video runs at 60.5 fps; stories take 23 to 60",
        id="fps-fractional",
    ),
    pytest.param(
        "fps",
        {"duration_seconds": "10", "fps": "15000/1001", "video_frames": 150},
        "this video runs at 14.99 fps; stories take 23 to 60",
        id="fps-ntsc",
    ),
    pytest.param(
        "width",
        {"width": 2160, "height": 3840},
        "this video is 2160 px wide; stories take up to 1920",
        id="width",
    ),
    pytest.param(
        "aspect",
        {"width": 100, "height": 1500, "sample_aspect_ratio": "1", "aspect": "1/15"},
        "this video's aspect ratio (1/15) is outside what stories take",
        id="aspect",
    ),
    pytest.param(
        "interlaced",
        {"width": 1080, "height": 1920},
        "this video is interlaced",
        id="interlaced",
    ),
    pytest.param(
        "chroma_subsampling",
        {"pix_fmt": "yuv444p"},
        "this video's pixel format (yuv444p) is not 4:2:0",
        id="chroma_subsampling",
    ),
    pytest.param(
        "video_codec",
        {"video_codec": "vp9"},
        "this video's codec (vp9) is not H.264 or HEVC",
        id="video_codec",
    ),
    pytest.param(
        "video_codec",
        {**CONTAINER, "sample_codecs": ["vp09", "mp4a"]},
        "this video's codec (vp09, mp4a) is not H.264 or HEVC",
        id="video_codec-from-the-container",
    ),
    pytest.param(
        "video_codec",
        {"video_codec": None},
        "this video's codec (unknown) is not H.264 or HEVC",
        id="video_codec-unnamed",
    ),
    pytest.param(
        "audio_codec",
        {"audio_codec": "mp3"},
        "this video's audio (mp3) is not AAC",
        id="audio_codec",
    ),
    pytest.param(
        "audio_sample_rate",
        {"sample_rate": 96000},
        "this video's audio runs at 96,000 Hz; stories take up to 48,000",
        id="audio_sample_rate",
    ),
    pytest.param(
        "audio_channels",
        {"channels": 6},
        "this video's audio has 6 channels; stories take 1 or 2",
        id="audio_channels",
    ),
    pytest.param(
        "image_format",
        {"format": "PNG"},
        "this image is PNG, not a JPEG",
        id="image_format",
    ),
    pytest.param(
        "image_format",
        {"format": None},
        "this image is another format, not a JPEG",
        id="image_format-unnamed",
    ),
    pytest.param(
        "malformed_image",
        {},
        "this image is damaged or not an image",
        id="malformed_image",
    ),
    pytest.param(
        "malformed_container",
        CONTAINER,
        "this video file is damaged",
        id="malformed_container",
    ),
    pytest.param(
        "byte_limit",
        {"bytes": 52428801, "cap": 52428800},
        "this file is 52,428,801 bytes; the limit is 52,428,800",
        id="byte_limit",
    ),
]

#: What carries no sentence: no record, a record that is not one, a refusal
#: recorded without evidence, and a kind `plain` has no words for.
NO_SENTENCE = [
    pytest.param(None, id="none"),
    pytest.param("local_validation", id="text"),
    pytest.param(["kind", "local_validation"], id="list"),
    pytest.param(7, id="number"),
    pytest.param({}, id="no-kind"),
    pytest.param({"v": 1, "kind": None}, id="null-kind"),
    pytest.param(
        fetch_refusal("unrecorded", error="MediaTerminalError"), id="unrecorded"
    ),
    pytest.param(fetch_refusal("no_such_kind", reason="unmapped"), id="unknown-kind"),
]

#: Every kind `plain` speaks of: those with a case above, and local validation.
KINDS = sorted(
    {case.values[0]["kind"] for case in FETCH_SENTENCES} | {"local_validation"}
)


@pytest.mark.unit
@pytest.mark.parametrize("refusal,sentence", FETCH_SENTENCES)
def test_plain_says_why_each_kind_of_fetch_was_refused(refusal, sentence):
    """One sentence per refusal, from its facts alone: the record's host, expiry,
    path, trace id and counters are not in it."""
    assert refusal_evidence.plain(refusal) == sentence


@pytest.mark.unit
@pytest.mark.parametrize("check,measured,sentence", CHECK_CASES)
def test_plain_says_what_each_local_validation_check_found(check, measured, sentence):
    """The check that failed and the value it measured, in one sentence. The rest
    of what was measured (boxes, dimensions, frame counts) and the record's
    bookkeeping are not in it."""
    assert refusal_evidence.plain(validation_refusal(check, **measured)) == sentence


@pytest.mark.unit
def test_every_check_the_table_has_words_for_has_a_case_above():
    """A check added to `_CHECKS` without a case in `CHECK_CASES` fails here, so
    no entry of the table goes unpinned."""
    assert {case.values[0] for case in CHECK_CASES} == set(refusal_evidence._CHECKS)


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["N/A", "0/0"], ids=["not-a-number", "zero-denominator"]
)
def test_plain_keeps_a_timing_it_cannot_read_as_a_number_as_the_text_it_is(value):
    """A duration or a frame rate that is not a fraction (ffprobe's `N/A` and
    `0/0`) is said as it was recorded: not dropped, and not an error."""
    duration = validation_refusal("duration", duration_seconds=value)
    fps = validation_refusal("fps", fps=value)
    assert refusal_evidence.plain(duration) == (
        f"this video is {value} s long; stories take 3 to 60 s"
    )
    assert refusal_evidence.plain(fps) == (
        f"this video runs at {value} fps; stories take 23 to 60"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [None, "40000000", 40000000.0, True, [40000000]],
    ids=["none", "text", "float", "bool", "list"],
)
def test_plain_says_a_question_mark_for_a_count_that_is_not_a_whole_number(value):
    """A count held as anything but a whole number reads as ?, so a field that is
    not a number never reaches the sentence as itself."""
    refusal = fetch_refusal("over_byte_cap", cap=value, received=value, host=HOST)
    assert refusal_evidence.plain(refusal) == (
        "this file is over the ?-byte limit (stopped after ? bytes)"
    )


@pytest.mark.unit
@pytest.mark.parametrize("refusal", NO_SENTENCE)
def test_plain_is_none_when_the_record_says_nothing_more(refusal):
    """The generic notice is all there is to say of no record, of a record that
    is not one, of a refusal recorded without evidence (`unrecorded`) and of a
    kind `plain` has no words for."""
    assert refusal_evidence.plain(refusal) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "refusal,sentence",
    [
        pytest.param(
            validation_refusal("packet_outside_media", packet_pos=12, packet_size=34),
            (
                "this file failed the packet_outside_media check"
                " (packet_pos 12, packet_size 34)"
            ),
            id="with-measurements",
        ),
        pytest.param(
            validation_refusal("packet_outside_media"),
            "this file failed the packet_outside_media check",
            id="nothing-measured",
        ),
        pytest.param(
            {"v": 1, "kind": "local_validation", "check": "packet_outside_media"},
            "this file failed the packet_outside_media check",
            id="no-measured-field",
        ),
    ],
)
def test_plain_names_a_check_it_has_no_words_for_and_what_it_measured(
    refusal, sentence
):
    """A check outside the table is named with each value it measured, so a new
    check's refusal is still said before anyone writes its sentence."""
    assert "packet_outside_media" not in refusal_evidence._CHECKS, (
        "this case needs a check the table lacks"
    )
    assert refusal_evidence.plain(refusal) == sentence


@pytest.mark.unit
@pytest.mark.parametrize("kind", KINDS)
def test_plain_does_not_raise_on_a_refusal_missing_its_fields(kind):
    """A record holding only its kind still answers, with a sentence or None and
    never an error: the card and the notice are built on the answer."""
    sentence = refusal_evidence.plain({"kind": kind})
    assert sentence is None or isinstance(sentence, str)


@pytest.mark.unit
@pytest.mark.parametrize("check", sorted(refusal_evidence._CHECKS))
def test_plain_does_not_raise_on_a_check_that_measured_nothing(check):
    """Each entry of the table copes with a measurement that is absent, null or
    empty."""
    for record in ({}, {"measured": None}, {"measured": {}}):
        refusal = {"v": 1, "kind": "local_validation", "check": check, **record}
        sentence = refusal_evidence.plain(refusal)
        assert sentence is None or isinstance(sentence, str), record


# --- the park notice -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "evidence,notice",
    [
        pytest.param(
            validation_refusal("edit_list", **CONTAINER),
            (
                "This story from your Instagram past posts could not be prepared."
                " Refused: this video has an edit list. Open the Queue to review it."
            ),
            id="local-validation",
        ),
        pytest.param(
            fetch_refusal("over_byte_cap", cap=40000000, received=40000003, host=HOST),
            (
                "This story from your Instagram past posts could not be prepared."
                " Refused: this file is over the 40,000,000-byte limit"
                " (stopped after 40,000,003 bytes). Open the Queue to review it."
            ),
            id="over-byte-cap",
        ),
        pytest.param(
            fetch_refusal(
                "ownership_unproven",
                test="app_scoped_id",
                match={"account_matches": False, "app_scoped_id_matches": False},
            ),
            (
                "This story from your Instagram past posts could not be prepared."
                " Refused: Instagram did not show this post as the connected"
                " account's. Open the Queue to review it."
            ),
            id="ownership-unproven",
        ),
    ],
)
def test_the_park_notice_says_why_when_the_refusal_does(evidence, notice):
    """A story parked for a refused fetch is told why, in the refusal's own
    sentence, between what happened and where to review it."""
    assert instagram_publish._park_notice(evidence) == notice


@pytest.mark.unit
@pytest.mark.parametrize("evidence", NO_SENTENCE)
def test_the_park_notice_is_the_generic_one_when_the_record_says_nothing_more(
    evidence,
):
    """No evidence, evidence that is not a record, an unrecorded refusal and a
    kind with no sentence all park with the generic notice."""
    assert instagram_publish._park_notice(evidence) == instagram_publish.PARK_NOTICE


@pytest.mark.unit
def test_the_generic_park_notice_keeps_its_words():
    """The notice a story parks with when nothing more is known: it claims no
    reason."""
    assert instagram_publish.PARK_NOTICE == (
        "This story from your Instagram past posts has selected content that is"
        " unavailable or changed. Open the Queue to review it."
    )
