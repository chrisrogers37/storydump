"""The vocabulary module: the closed sets are the migrations' CHECK lists, the
command port re-exports them, every reason has a sentence in the CLI's own
words, and the envelope checker refuses every malformed document.

The CHECK lists are parsed from the migration files rather than typed twice:
a value added to a migration without the module — or the reverse — fails
here instead of at a CLI that prints a state it has never heard of.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.services.target import commands, vocabulary

MIGRATIONS = Path(__file__).resolve().parents[4] / "scripts" / "migrations"


def _check_values(migration: str, constraint: str) -> tuple[str, ...]:
    text = (MIGRATIONS / migration).read_text()
    match = re.search(
        rf"CONSTRAINT {constraint}\s+CHECK\s*\(\s*\w+\s+IN\s*\(([^)]*)\)", text, re.S
    )
    assert match, f"{constraint} not found in {migration}"
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


class TestTheClosedSets:
    @pytest.mark.parametrize(
        "values, migration, constraint",
        [
            (
                vocabulary.INTENT_STATES,
                "055_intent_ledger_tables.sql",
                "ck_intent_state",
            ),
            (
                vocabulary.PUBLISH_STEPS,
                "055_intent_ledger_tables.sql",
                "ck_intent_step",
            ),
            (
                vocabulary.AUDIT_ACTOR_KINDS,
                "055_intent_ledger_tables.sql",
                "ck_audit_actor",
            ),
            (
                vocabulary.AUDIT_CHANNELS,
                "055_intent_ledger_tables.sql",
                "ck_audit_channel",
            ),
            (
                vocabulary.TOKEN_ROLES,
                "060_auth_plane_tables.sql",
                "ck_service_token_role",
            ),
        ],
    )
    def test_each_set_is_its_migrations_check_list_in_order(
        self, values, migration, constraint
    ):
        assert values == _check_values(migration, constraint)

    def test_the_command_port_re_exports_the_module(self):
        assert commands.VOCABULARY is vocabulary.COMMANDS
        assert commands.REASONS is vocabulary.REASONS

    def test_the_admission_channels_lead_the_audit_channels(self):
        from src.services.target import webhook_ingress

        assert vocabulary.AUDIT_CHANNELS[: len(webhook_ingress.CHANNELS)] == tuple(
            webhook_ingress.CHANNELS
        )

    def test_every_reason_and_refusal_has_a_sentence_in_the_clis_words(self):
        for reason in vocabulary.REASONS + vocabulary.TOKEN_REFUSALS:
            assert reason in vocabulary.REASON_SENTENCES, reason
        for sentence in list(vocabulary.REASON_SENTENCES.values()) + list(
            vocabulary.OUTCOME_SENTENCES.values()
        ):
            words = set(re.findall(r"[a-z]+", sentence.lower()))
            assert not words & set(vocabulary.TAP_WORDS), sentence


class TestExitCodes:
    @pytest.mark.parametrize(
        "status, reason, code",
        [
            (200, None, vocabulary.EXIT_OK),
            (201, None, vocabulary.EXIT_OK),
            (202, None, vocabulary.EXIT_OK),
            (401, None, vocabulary.EXIT_NOT_AUTHORIZED),
            (403, "readonly_token", vocabulary.EXIT_NOT_AUTHORIZED),
            (404, "not_found", vocabulary.EXIT_NOT_FOUND),
            (404, None, vocabulary.EXIT_NOT_AUTHORIZED),
            (409, "manual_mode", vocabulary.EXIT_REFUSED),
            (400, "invalid_args", vocabulary.EXIT_REFUSED),
            (422, None, vocabulary.EXIT_REFUSED),
            (500, None, vocabulary.EXIT_API_UNREACHABLE),
            (503, None, vocabulary.EXIT_API_UNREACHABLE),
            (0, None, vocabulary.EXIT_API_UNREACHABLE),
        ],
    )
    def test_the_answer_maps_to_the_documented_code(self, status, reason, code):
        assert vocabulary.exit_code_for(status, reason) == code

    def test_the_documented_table_is_the_constants(self):
        assert vocabulary.EXIT_CODES == {
            "ok": 0,
            "not_found": 1,
            "refused": 2,
            "not_authorized": 3,
            "api_unreachable": 4,
            "railway_unreachable": 5,
            "watch_failed": 6,
            "usage": 64,
        }


class TestTheEnvelope:
    def test_the_two_well_formed_shapes_pass(self):
        vocabulary.check_envelope(vocabulary.envelope("whoami", {"kind": "token"}))
        vocabulary.check_envelope(
            vocabulary.error_envelope(
                "whoami", code=3, reason="not_authorized", detail="d", fix="f"
            )
        )

    @pytest.mark.parametrize(
        "document",
        [
            "not an object",
            {"v": 1, "kind": "x", "data": {}},  # a missing key
            {"v": 1, "kind": "x", "data": {}, "error": None, "extra": 1},
            {"v": 2, "kind": "x", "data": {}, "error": None},
            {"v": 1, "kind": "", "data": {}, "error": None},
            {"v": 1, "kind": "x", "data": None, "error": None},  # neither
            {  # both
                "v": 1,
                "kind": "x",
                "data": {},
                "error": {"code": 3, "reason": "r", "detail": "d", "fix": "f"},
            },
            {"v": 1, "kind": "x", "data": None, "error": {"code": 3}},
            {  # a success code on an error
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {"code": 0, "reason": "r", "detail": "d", "fix": "f"},
            },
            {  # an undocumented code
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {"code": 7, "reason": "r", "detail": "d", "fix": "f"},
            },
            {  # a non-string field
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {"code": 3, "reason": None, "detail": "d", "fix": "f"},
            },
        ],
    )
    def test_every_malformed_document_is_refused(self, document):
        with pytest.raises(ValueError):
            vocabulary.check_envelope(document)
