"""The settings the API and the worker must share: the list against the
deployment guide's tables, and each value read as its services read it."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from storydump_cli.railway import Railway, RailwayUnavailable
from storydump_cli.shared_settings import SHARED, fingerprint

GUIDE = (
    Path(__file__).resolve().parents[2]
    / "documentation"
    / "guides"
    / "cloud-deployment.md"
)


def table(heading: str) -> set[str]:
    """The variables the first column of the guide's `### <heading>` table
    names."""
    section = GUIDE.read_text().split(f"### {heading}\n", 1)[1].split("\n### ", 1)[0]
    names: set[str] = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            names.update(re.findall(r"`([A-Z][A-Z0-9_]*)`", line.split("|")[1]))
    return names


def test_the_list_is_the_one_the_guide_sets_on_both_services():
    """Every variable the guide sets on both services, and every one both
    services' own tables name, is compared; the rotation list rides
    ENCRYPTION_KEY's row. A variable added to either kind of row joins the
    list, or this fails."""
    both = table("Both services")
    own = table("The API (`storydump`)") & table("The worker")
    assert own == {"OPS_USER_IDS", "RESEND_API_KEY", "EMAIL_FROM"}
    assert set(SHARED) == both | own | {"ENCRYPTION_KEYS"}


def test_only_a_shared_setting_with_a_value_has_a_fingerprint():
    assert fingerprint("CLOUDINARY_CLOUD_NAME", "placeholder-cloud") is None
    assert fingerprint("OPS_USER_IDS", " \t ") is None, "blank reads as unset"
    assert fingerprint("WEB_APP_URL", "https://app.example.com") is not None


def test_a_value_is_compared_exactly_unless_its_services_read_it_otherwise():
    url = "https://app.example.com"
    assert fingerprint("WEB_APP_URL", url) != fingerprint("WEB_APP_URL", url + "/")
    # a database URL that names no host is compared whole
    assert fingerprint("TARGET_DATABASE_URL", "not a url") != fingerprint(
        "TARGET_DATABASE_URL", "not a url either"
    )


def _raising(exc):
    def run(args):
        raise exc

    return run


@pytest.mark.parametrize(
    "run",
    [
        # a truncated document: the decode error holds all of it
        lambda args: (0, '{"GOOGLE_CLIENT_SECRET": "placeholder-leak", ', ""),
        # a timeout carries the output so far
        _raising(
            subprocess.TimeoutExpired(
                ["railway", "variables"],
                60,
                output=b'{"GOOGLE_CLIENT_SECRET": "placeholder-leak"}',
            )
        ),
        # an answer that is not UTF-8 carries the whole answer
        _raising(
            UnicodeDecodeError(
                "utf-8",
                b'{"GOOGLE_CLIENT_SECRET": "placeholder-leak\xff"}',
                42,
                43,
                "invalid start byte",
            )
        ),
    ],
    ids=["truncated", "timed-out", "not-utf-8"],
)
def test_a_refused_read_keeps_nothing_the_binary_printed(run):
    """The refusal holds no value, in its message or on ``__context__``."""
    with pytest.raises(RailwayUnavailable) as caught:
        Railway(run).variables("worker", fingerprint)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert "placeholder-leak" not in f"{caught.value.detail} {caught.value!r}"


def test_the_seam_keeps_only_the_fingerprints_it_is_asked_for():
    """A variable outside the list, and a value that is not a string, never
    reach the caller."""
    ids = "11111111-1111-4111-8111-111111111111"
    body = {
        "OPS_USER_IDS": ids,
        "CLOUDINARY_CLOUD_NAME": "placeholder",
        "WEB_APP_URL": 8080,
    }
    rail = Railway(lambda args: (0, json.dumps(body), ""))
    assert rail.variables("worker", fingerprint) == {
        "OPS_USER_IDS": fingerprint("OPS_USER_IDS", ids)
    }
