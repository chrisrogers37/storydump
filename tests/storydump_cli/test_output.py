"""What reaches the terminal: one well-formed envelope, and never a secret.

``check_envelope`` is the vocabulary's own checker and runs over everything
these tests emit; the redaction rules cover the three secret shapes the spec
names (a token, a database URL, a webhook secret). The CLI's words are its
own — the Telegram adapter's tap words never appear in a sentence.
"""

from __future__ import annotations

import json
import re

import pytest

from src.services.target.vocabulary import (
    EXIT_NOT_AUTHORIZED,
    OUTCOME_SENTENCES,
    REASON_SENTENCES,
    TAP_WORDS,
    check_envelope,
    envelope,
    error_envelope,
)
from storydump_cli.output import emit, redact

SECRET = "sdt_" + "a" * 43
WS = "11111111-1111-4111-8111-111111111111"

PRINCIPAL = {
    "kind": "token",
    "user_id": "22222222-2222-4222-8222-222222222222",
    "token": {
        "id": "33333333-3333-4333-8333-333333333333",
        "name": f"leaked {SECRET}",
        "role": "operator",
        "workspace_id": None,
        "expires_at": "2026-12-14T00:00:00Z",
    },
    "workspaces": [{"id": WS, "name": f"studio {SECRET}", "role": "owner"}],
}

TOKENS = {
    "tokens": [
        {
            "id": "33333333-3333-4333-8333-333333333333",
            "name": f"chris-mbp {SECRET}",
            "role": "operator",
            "expires_at": "2026-12-14T00:00:00Z",
            "revoked_at": None,
            "last_used_at": "2026-09-15T10:00:00Z",
            "created_at": "2026-09-15T09:00:00Z",
        }
    ]
}

DOCUMENTS = [
    envelope("login", PRINCIPAL),
    envelope("whoami", PRINCIPAL),
    envelope("tokens", TOKENS),
    envelope("tokens", {"revoked": {"id": WS, "name": f"old {SECRET}"}}),
    envelope("logout", {"signed_out": True, "note": SECRET}),
    envelope(
        "unknown-kind",
        {"anything": SECRET, "nested": [{"url": f"postgres://u:{SECRET}@h/db"}]},
    ),
    error_envelope(
        "whoami",
        code=EXIT_NOT_AUTHORIZED,
        reason="not_authorized",
        detail=f"refused {SECRET}",
        fix=f"try postgresql://user:pw@host/db or secret_token={SECRET}",
    ),
]


def test_redacts_a_token():
    assert redact(f"token {SECRET} here") == "token sdt_… here"


def test_leaves_the_bare_prefix_alone():
    assert redact("a token starts with sdt_") == "a token starts with sdt_"


def test_redacts_database_urls():
    assert redact("db postgres://u:p@h:5432/db now") == "db postgres://<redacted> now"
    assert redact("db postgresql://u:p@h/db") == "db postgres://<redacted>"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://user:pw@host:5432/db",
        "postgresql+psycopg2://user:pw@host/db",
        "postgres://user:pw@host/db?sslmode=require",
    ],
)
def test_redacts_every_dialect_of_a_database_url(url):
    """The one URL shape this deployment uses carries a driver suffix; the
    backstop must strike it too."""
    assert "pw" not in redact(f"dsn {url} end")
    assert "<redacted>" in redact(f"dsn {url} end")


def test_the_tokens_renderer_survives_a_row_that_is_not_an_object(capsys):
    emit(envelope("tokens", {"tokens": ["garbage", None, 7]}), json_mode=False)
    out = capsys.readouterr().out
    assert "Traceback" not in out


def test_redacts_webhook_secrets():
    assert redact("secret_token=abc123 ok") == "secret_token=<redacted> ok"
    assert redact("?token=abc123&x=1") == "?token=<redacted>"


def test_json_mode_prints_one_valid_envelope_per_document(capsys):
    emit(envelope("whoami", PRINCIPAL), json_mode=True)
    out, err = capsys.readouterr()
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 1
    document = json.loads(lines[0])
    check_envelope(document)
    assert document["kind"] == "whoami"


def test_json_mode_redacts_inside_the_document_and_stays_valid_json(capsys):
    emit(DOCUMENTS[5], json_mode=True)
    out, _ = capsys.readouterr()
    document = json.loads(out)
    check_envelope(document)
    assert SECRET not in out
    assert document["data"]["anything"] == "sdt_…"
    assert document["data"]["nested"][0]["url"] == "postgres://<redacted>"


def test_json_mode_error_goes_to_stdout_as_an_envelope(capsys):
    emit(DOCUMENTS[6], json_mode=True)
    out, err = capsys.readouterr()
    assert err == ""
    document = json.loads(out)
    check_envelope(document)
    assert document["error"]["code"] == EXIT_NOT_AUTHORIZED
    assert SECRET not in out


@pytest.mark.parametrize("document", DOCUMENTS, ids=[d["kind"] for d in DOCUMENTS])
def test_human_mode_never_prints_a_secret(document, capsys):
    check_envelope(document)
    emit(document, json_mode=False)
    out, err = capsys.readouterr()
    assert SECRET not in out + err
    assert "postgres://u:" not in out + err
    assert out + err != ""


def test_human_mode_error_is_two_lines_on_stderr(capsys):
    emit(DOCUMENTS[6], json_mode=False)
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines()[0] == "error: refused sdt_…"
    assert err.splitlines()[1].startswith("fix: ")


def test_human_mode_renders_the_principal(capsys):
    emit(envelope("whoami", PRINCIPAL), json_mode=False)
    out, _ = capsys.readouterr()
    assert "operator" in out
    assert "owner" in out
    assert WS in out


def test_the_cli_never_uses_the_tap_words():
    tap = re.compile(r"\b(" + "|".join(TAP_WORDS) + r")s?\b", re.IGNORECASE)
    for sentence in [*REASON_SENTENCES.values(), *OUTCOME_SENTENCES.values()]:
        assert not tap.search(sentence), sentence


class TestTheBotTokenShape:
    """A Telegram bot token (`<bot id>:<35 chars>`) is struck wherever it
    appears — in a Bot API URL, bare, or after `token=` — but a uuid's digit
    run inside a sync key is not a token (phase 03 review)."""

    def test_a_bot_api_url_and_a_bare_token_are_struck(self):
        from storydump_cli.output import redact

        token = "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"
        assert token not in redact(
            f"https://api.telegram.org/bot{token}/getMe answered 401"
        )
        assert token not in redact(f"{token} was refused")
        assert token not in redact(f"token={token}")

    def test_a_sync_key_survives(self):
        from storydump_cli.output import redact

        key = (
            "sync_now:33d1ccca-353c-4236-8d39-0d8fd9165054"
            ":5a5a5a5a-5a5a-4a5a-8a5a-5a5a5a5a5a5a:9d9d9d9d"
        )
        assert redact(key) == key
        assert redact("idempotency_key=" + key) == "idempotency_key=" + key
