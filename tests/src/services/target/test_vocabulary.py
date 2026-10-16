"""The vocabulary module: the closed sets are the models' CHECK lists, the
command port re-exports them, every reason has a sentence in the CLI's own
words, and the envelope checker refuses every malformed document.

The CHECK lists are read from the models' constraints rather than typed twice:
a value added to a model's CHECK without the module — or the reverse — fails
here instead of at a CLI that prints a state it has never heard of.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint

from src.models.target import TargetBase
from src.services.target import commands, vocabulary


def _check_values(constraint: str) -> tuple[str, ...]:
    """The values the model CHECK named *constraint* allows, in its order. The
    constraint is one column `IN` a list of literals, and nothing else."""
    found = [
        c
        for table in TargetBase.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint) and c.name == constraint
    ]
    assert len(found) == 1, f"{len(found)} model CHECK constraints named {constraint}"
    text = str(found[0].sqltext)
    match = re.fullmatch(r"\s*\w+\s+IN\s*\(([^)]*)\)\s*", text)
    assert match, f"{constraint} is not one column IN a list: {text}"
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


class TestTheClosedSets:
    @pytest.mark.parametrize(
        "values, constraint",
        [
            (vocabulary.INTENT_ORIGINS, "ck_intent_origin"),
            (vocabulary.APPROVAL_POSITIONS, "ck_ws_approval_mode"),
            (vocabulary.APPROVAL_POSITIONS, "ck_source_approval_mode"),
            (vocabulary.APPROVAL_RULES, "ck_intent_approval_rule"),
            (vocabulary.MEDIA_STATES, "ck_media_state"),
            (vocabulary.REPEAT_UNITS, "ck_intent_repeat_unit"),
            (vocabulary.INTENT_STATES, "ck_intent_state"),
            (vocabulary.PUBLISH_STEPS, "ck_intent_step"),
            (vocabulary.AUDIT_ACTOR_KINDS, "ck_audit_actor"),
            (vocabulary.AUDIT_CHANNELS, "ck_audit_channel"),
            (vocabulary.TOKEN_ROLES, "ck_service_token_role"),
            (vocabulary.OUTBOX_FAILURE_CLASSES, "ck_outbox_failure_class"),
        ],
    )
    def test_each_set_is_its_check_list_in_order(self, values, constraint):
        assert values == _check_values(constraint)

    def test_every_rule_has_its_label(self):
        """#1477: the by-line names each rule, and nothing else."""
        assert set(vocabulary.RULE_LABELS) == set(vocabulary.APPROVAL_RULES)

    def test_a_rule_this_tier_does_not_know_shows_as_itself(self):
        """`rule_label`'s fallback: a rule a newer ledger adds is named as
        itself, never as nothing. The web's copy is pinned the same way
        (`landing/src/lib/intents.test.ts`)."""
        assert "weekend" not in vocabulary.RULE_LABELS
        assert vocabulary.rule_label("weekend") == "weekend"

    def test_the_lock_rule_splits_every_lock_kind_once(self):
        """#1413: a lock kind either blocks a planned story or warns of
        it, never both and never neither — a new kind in `ck_locks_kind` is a
        failing test until someone decides which."""
        kinds = set(_check_values("ck_locks_kind"))
        blocking, warning = (
            set(vocabulary.BLOCKING_LOCKS),
            set(vocabulary.WARNING_LOCKS),
        )
        assert blocking | warning == kinds and not blocking & warning

    def test_every_fact_a_planned_story_is_refused_with_has_the_clis_words(self):
        """The CLI says each fact `schedule_item` and `reschedule_item` send, and
        its tests are parametrized over these tables, so a key dropped here
        would drop its own case there. What is in the way: the item's media
        states other than `available` (spelled `item_<state>`), then every lock
        kind."""
        from src.services.target import workspaces

        assert set(vocabulary.IN_THE_WAY) == {
            *(f"item_{s}" for s in workspaces.MEDIA_STATES if s != "available"),
            *vocabulary.BLOCKING_LOCKS,
            *vocabulary.WARNING_LOCKS,
        }
        assert set(vocabulary.AT_RULE_SENTENCES) == {
            "shape",
            "not_a_date",
            "skipped",
            "past",
            "horizon",
        }
        assert set(vocabulary.MISSING_SENTENCES) == {"account", "item"}
        assert set(vocabulary.REPEAT_SENTENCES) == {"invalid", "off_day"}

    def test_the_provider_names_are_the_check_lists(self):
        """One spelling of `"ig_login"` and `"gdrive"`, pinned against the
        DDL (#1325)."""
        assert vocabulary.PROVIDER_IG_LOGIN == "ig_login"
        assert vocabulary.PROVIDER_GDRIVE == "gdrive"
        assert vocabulary.PROVIDER_GOOGLE == "google"
        assert vocabulary.PROVIDER_TELEGRAM == "telegram"
        credentials = _check_values("ck_credentials_provider")
        sources = _check_values("ck_sources_provider")
        oauth_states = _check_values("ck_oauth_state_provider")
        assert vocabulary.PROVIDER_IG_LOGIN in credentials
        assert vocabulary.PROVIDER_GDRIVE in credentials
        assert vocabulary.PROVIDER_GDRIVE in sources
        for name in (
            vocabulary.PROVIDER_IG_LOGIN,
            vocabulary.PROVIDER_GDRIVE,
            vocabulary.PROVIDER_GOOGLE,
            vocabulary.PROVIDER_TELEGRAM,
        ):
            assert name in oauth_states

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
            {  # both (a documented reason, so ONLY the exclusivity rule refuses it)
                "v": 1,
                "kind": "x",
                "data": {},
                "error": {
                    "code": 3,
                    "reason": "not_authorized",
                    "detail": "d",
                    "fix": "f",
                },
            },
            {"v": 1, "kind": "x", "data": None, "error": {"code": 3}},
            {  # a success code on an error
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {
                    "code": 0,
                    "reason": "not_authorized",
                    "detail": "d",
                    "fix": "f",
                },
            },
            {  # an undocumented code
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {
                    "code": 7,
                    "reason": "not_authorized",
                    "detail": "d",
                    "fix": "f",
                },
            },
            {  # a non-string field
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {"code": 3, "reason": None, "detail": "d", "fix": "f"},
            },
            {  # an undocumented reason
                "v": 1,
                "kind": "x",
                "data": None,
                "error": {"code": 3, "reason": "r", "detail": "d", "fix": "f"},
            },
        ],
    )
    def test_every_malformed_document_is_refused(self, document):
        with pytest.raises(ValueError):
            vocabulary.check_envelope(document)


class TestTheWindowGrammar:
    NOW = dt.datetime(2026, 9, 16, 12, 0, 30, 123456, tzinfo=dt.timezone.utc)

    @pytest.mark.parametrize(
        "value, seconds",
        [("15m", 900), ("3h", 10800), ("2d", 172800), ("30d", 30 * 86400)],
    )
    def test_a_span_is_measured_from_now_without_microseconds(self, value, seconds):
        start = vocabulary.window_start(value, self.NOW)
        assert start == self.NOW.replace(microsecond=0) - dt.timedelta(seconds=seconds)

    def test_a_timestamp_with_any_zone_or_none_is_utc(self):
        want = dt.datetime(2026, 9, 15, 14, 50, tzinfo=dt.timezone.utc)
        for value in (
            "2026-09-15T14:50:00Z",
            "2026-09-15T14:50:00+00:00",
            "2026-09-15T10:50:00-04:00",
            "2026-09-15T14:50:00",
        ):
            assert vocabulary.window_start(value, self.NOW) == want, value
        assert vocabulary.window_start("2026-09-15", self.NOW) == dt.datetime(
            2026, 9, 15, tzinfo=dt.timezone.utc
        ), "a bare date is its midnight UTC"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "x",
            "3",
            "-3h",
            "3w",
            "999999d",
            "9999999d",
            "31d",
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59-05:00",
            "2026-09-17T00:00:00Z",
        ],
    )
    def test_everything_else_is_refused_with_a_sentence(self, value):
        with pytest.raises(ValueError) as caught:
            vocabulary.window_start(value, self.NOW)
        assert str(caught.value), value


class TestTheWindowSlack:
    """The CLI computes a span's start on ITS clock and the API judges it on
    ITS OWN: a `30d` sent by a client one second behind, or a timestamp from
    a clock one second ahead, is inside the grammar's slack — clamped to the
    bound, never refused. Beyond the slack the bound holds."""

    NOW = dt.datetime(2026, 9, 16, 12, 0, 30, 123456, tzinfo=dt.timezone.utc)

    def test_a_widest_span_from_a_clock_slightly_behind_is_clamped(self):
        anchor = self.NOW.replace(microsecond=0)
        behind = (anchor - dt.timedelta(days=30, seconds=1)).isoformat()
        assert vocabulary.window_start(behind, self.NOW) == anchor - dt.timedelta(
            days=30
        )

    def test_a_timestamp_slightly_ahead_is_now(self):
        anchor = self.NOW.replace(microsecond=0)
        ahead = (anchor + dt.timedelta(seconds=2)).isoformat()
        assert vocabulary.window_start(ahead, self.NOW) == anchor
        same_second = (anchor + dt.timedelta(microseconds=500_000)).isoformat()
        assert vocabulary.window_start(same_second, self.NOW) == anchor

    def test_the_slack_is_bounded(self):
        anchor = self.NOW.replace(microsecond=0)
        with pytest.raises(ValueError):
            vocabulary.window_start(
                (anchor + dt.timedelta(minutes=10)).isoformat(), self.NOW
            )
        with pytest.raises(ValueError):
            vocabulary.window_start(
                (anchor - dt.timedelta(days=30, minutes=10)).isoformat(), self.NOW
            )
        with pytest.raises(ValueError):
            vocabulary.window_start("31d", self.NOW)


class TestTheClosedSetOfReasons:
    """Every reason an envelope can carry is a member of one closed set — the
    port's refusals and the CLI's own answers — so an agent can switch on it
    and `check_envelope` refuses a reason nobody documented."""

    def test_check_envelope_refuses_an_undocumented_reason(self):
        document = vocabulary.error_envelope(
            "whoami", code=vocabulary.EXIT_USAGE, reason="bogus", detail="x", fix="y"
        )
        with pytest.raises(ValueError):
            vocabulary.check_envelope(document)

    def test_every_sentence_and_fix_names_a_documented_reason(self):
        from storydump_cli.main import FIXES

        assert set(vocabulary.REASON_SENTENCES) <= set(vocabulary.CLI_REASONS)
        assert set(FIXES) <= set(vocabulary.CLI_REASONS)

    def test_the_clis_own_reasons_are_documented(self):
        import re
        from pathlib import Path

        package = Path(__file__).resolve().parents[4] / "storydump_cli"
        raised = set()
        for path in package.rglob("*.py"):
            raised |= set(re.findall(r'reason="([a-z_]+)"', path.read_text()))
        assert raised <= set(vocabulary.CLI_REASONS), sorted(
            raised - set(vocabulary.CLI_REASONS)
        )


class TestOneSpellingOfTheDeployment:
    """The names the API, the worker and the CLI read from the environment,
    and the deployment's identities, are spelled ONCE — in this module — and
    read from it by reference. A literal elsewhere is a second spelling that
    drifts (the API read `TARGET_TELEGRAM_*` as literals in seven places while
    the vocabulary's constants had no API reader at all)."""

    ROOT = __import__("pathlib").Path(__file__).resolve().parents[4]
    #: Variables spelled in their own module by design: not the webhook's.
    OWN_SPELLINGS = {
        "src/channels/telegram_webhook_registration.py": {
            "TARGET_TELEGRAM_WEBHOOK_AUTOREGISTER"
        },
        "src/channels/telegram_transport.py": {"TARGET_TELEGRAM_API_BASE"},
    }

    def test_no_telegram_variable_is_read_by_a_literal_outside_the_vocabulary(self):
        import re

        offenders = []
        for path in (self.ROOT / "src").rglob("*.py"):
            rel = str(path.relative_to(self.ROOT))
            if rel == "src/services/target/vocabulary.py":
                continue
            for match in re.finditer(r'"(TARGET_TELEGRAM_[A-Z_]+)"', path.read_text()):
                if match.group(1) not in self.OWN_SPELLINGS.get(rel, set()):
                    offenders.append(f"{rel}: {match.group(1)}")
        assert offenders == [], offenders

    def test_the_settings_fields_are_the_vocabularys_names(self):
        from src.config.settings import Settings

        assert vocabulary.TELEGRAM_SECRET_VAR in Settings.model_fields
        assert vocabulary.TELEGRAM_BOT_VAR in Settings.model_fields

    def test_the_railway_project_and_the_api_host_are_spelled_once(self):
        from storydump_cli import config, railway

        assert railway.PROJECT_ID_ENV is vocabulary.RAILWAY_PROJECT_ID_VAR
        assert railway.PROJECT_NAME is vocabulary.RAILWAY_PROJECT_NAME
        assert config.DEFAULT_API_URL is vocabulary.API_URL
        assert vocabulary.DEFAULT_WEBHOOK_URL.startswith(vocabulary.API_URL)
        twice = [
            str(p.relative_to(self.ROOT))
            for p in list((self.ROOT / "src").rglob("*.py"))
            + list((self.ROOT / "scripts").rglob("*.py"))
            + list((self.ROOT / "storydump_cli").rglob("*.py"))
            if p.name != "vocabulary.py"
            and vocabulary.RAILWAY_PROJECT_ID_VAR in p.read_text()
        ]
        assert twice == [], twice


class TestTheBoundsEveryAdapterEnforces:
    def test_the_token_bounds_are_read_by_reference(self):
        """`is` cannot tell a copied small integer from a reference (CPython
        interns them), so the pin reads the SOURCE: the assignment names the
        vocabulary's constant, and no literal number stands in for it."""
        from src.services.target import service_tokens

        source = Path(service_tokens.__file__).read_text()
        assert "NAME_MAX = vocabulary.TOKEN_NAME_MAX" in source
        assert "MAX_EXPIRY_DAYS = vocabulary.TOKEN_EXPIRY_DAYS_MAX" in source
        assert "DEFAULT_EXPIRY_DAYS = vocabulary.TOKEN_EXPIRY_DAYS_DEFAULT" in source
        assert service_tokens.NAME_MAX == vocabulary.TOKEN_NAME_MAX
        assert vocabulary.TOKEN_EXPIRY_DAYS_MIN == 1

    def test_the_floating_limits_are_read_by_reference(self):
        from src.services.target import ops_views

        source = Path(ops_views.__file__).read_text()
        assert "import FLOATING_LIMIT, FLOATING_LIMIT_MAX" in source
        assert not re.search(r"^FLOATING_LIMIT(?:_MAX)? = ", source, re.M), (
            "a literal limit beside the vocabulary's"
        )
        assert ops_views.FLOATING_LIMIT == vocabulary.FLOATING_LIMIT

    def test_the_plan_horizon_is_a_year(self):
        assert vocabulary.PLAN_HORIZON_DAYS == 365

    def test_the_waitlist_refs_width_is_read_by_reference(self):
        """The ref `signup_admissions` cuts and the one an Admit button's
        token checks are one width (#1723); `callback_tokens` reads it here
        because it must not import the service."""
        from src.services.target import callback_tokens, signup_admissions

        cut = Path(signup_admissions.__file__).read_text()
        check = Path(callback_tokens.__file__).read_text()
        assert "REF_HEX = vocabulary.WAITLIST_REF_HEX" in cut
        # The pattern's own width: the module's docstrings name the constant
        # too, so its bare name would pass beside a literal 32.
        assert "[0-9a-f]{{{vocabulary.WAITLIST_REF_HEX}}}" in check
        assert signup_admissions.REF_HEX == vocabulary.WAITLIST_REF_HEX == 32


class TestTheAnswersAddedByTheAudit:
    def test_a_rate_limited_answer_is_unreachable_not_refused(self):
        assert (
            vocabulary.exit_code_for(429, "pool_saturated")
            == vocabulary.EXIT_API_UNREACHABLE
        )
        assert vocabulary.exit_code_for(429) == vocabulary.EXIT_API_UNREACHABLE

    def test_the_role_floor_and_the_rate_limit_have_sentences(self):
        for reason in ("insufficient_role", "pool_saturated"):
            sentence = vocabulary.REASON_SENTENCES[reason]
            assert sentence and "login" not in sentence, reason


class TestTheWriteSentences:
    def test_every_write_sentence_names_a_port_command_and_an_outcome(self):
        for (command, outcome), sentence in vocabulary.WRITE_SENTENCES.items():
            assert command in vocabulary.COMMANDS, command
            assert outcome in vocabulary.OUTCOME_SENTENCES, outcome
            assert sentence and sentence[0].islower()
            for word in vocabulary.TAP_WORDS:
                assert word not in sentence.lower().split(), (command, sentence)

    def test_the_replay_sentence_is_the_outcomes_own(self):
        assert vocabulary.write_sentence("skip", "replayed") == "already done"
        assert vocabulary.write_sentence("approve", "enqueued") == (
            "approved — posting shortly"
        )
        assert vocabulary.write_sentence("no_such", "executed") == "done"


class TestTheWebhookSpellings:
    """One spelling of the deployment's names, shared by the API's startup
    self-registration and the CLI's `webhook` verb."""

    def test_the_registration_asks_for_taps(self):
        assert list(vocabulary.ALLOWED_UPDATES) == ["message", "callback_query"]

    def test_the_variables_are_the_deployments(self):
        assert vocabulary.TELEGRAM_TOKEN_VAR == "TARGET_TELEGRAM_BOT_TOKEN"
        assert vocabulary.TELEGRAM_SECRET_VAR == "TARGET_TELEGRAM_WEBHOOK_SECRET_TOKEN"
        assert vocabulary.TELEGRAM_BOT_VAR == "TARGET_TELEGRAM_BOT_USERNAME"
        assert vocabulary.WEBHOOK_URL_VAR == "TARGET_TELEGRAM_WEBHOOK_URL"
        assert (
            vocabulary.MAX_CONNECTIONS_VAR == "TARGET_TELEGRAM_WEBHOOK_MAX_CONNECTIONS"
        )
        assert vocabulary.WEBHOOK_SECRET_HEADER == "X-Telegram-Bot-Api-Secret-Token"
        assert (
            vocabulary.DEFAULT_WEBHOOK_URL
            == "https://api.storydump.app/webhooks/telegram"
        )
        assert vocabulary.RAILWAY_ENVIRONMENT_VAR == "RAILWAY_ENVIRONMENT_NAME"
        assert vocabulary.PRODUCTION_ENVIRONMENT == "production"
        assert vocabulary.TELEGRAM_BOT_API_BASE == "https://api.telegram.org"

    @pytest.mark.parametrize(
        "raw,expected", [(None, 10), ("", 10), (" 20 ", 20), ("1", 1), ("100", 100)]
    )
    def test_the_connection_cap_is_read_within_telegrams_range(self, raw, expected):
        assert vocabulary.max_connections_from(raw) == expected

    @pytest.mark.parametrize("raw", ["0", "101", "many", "-1"])
    def test_a_cap_outside_the_range_is_refused_naming_the_variable(self, raw):
        with pytest.raises(vocabulary.BadMaxConnections) as exc:
            vocabulary.max_connections_from(raw)
        assert vocabulary.MAX_CONNECTIONS_VAR in str(exc.value)

    def test_bot_matches_ignores_case_and_the_at_sign(self):
        assert vocabulary.bot_matches("Storydump_App_Bot", "@storydump_app_bot")
        assert vocabulary.bot_matches("x_bot", None), (
            "no configured bot: nothing to refuse"
        )
        assert not vocabulary.bot_matches("example_bot", "storydump_app_bot")

    def test_the_api_module_re_exports_them_unchanged(self):
        from src.channels import telegram_webhook_registration as reg

        assert reg.ALLOWED_UPDATES is vocabulary.ALLOWED_UPDATES
        assert reg.max_connections_from is vocabulary.max_connections_from
        assert reg.bot_matches is vocabulary.bot_matches
        assert reg.DEFAULT_WEBHOOK_URL == vocabulary.DEFAULT_WEBHOOK_URL


class TestTheIngressConflict:
    def test_the_ingress_conflict_has_a_sentence(self):
        assert "idempotency key" in vocabulary.REASON_SENTENCES["admission_conflict"]


class TestARepeatInWords:
    """`describe_repeat` is the CLI's and the card's one sentence for a rule
    (#485), read from the rule's one shape, and `WEEKDAYS` the names its days
    are written with."""

    @staticmethod
    def _rule(unit, every, days, at):
        return {"unit": unit, "every": every, "days": days, "at": at}

    @pytest.mark.parametrize(
        "rule, words",
        [
            (("day", 1, None, "09:00"), "every day at 09:00"),
            (("day", 3, None, "09:00"), "every 3 days at 09:00"),
            (("week", 1, [4, 1], "16:07"), "every week on Mon and Thu at 16:07"),
            (("week", 2, [2], "16:07"), "every 2 weeks on Tue at 16:07"),
            (
                ("week", 1, [5, 1, 3], "08:30"),
                "every week on Mon, Wed and Fri at 08:30",
            ),
        ],
    )
    def test_each_unit_and_interval(self, rule, words):
        assert vocabulary.describe_repeat(self._rule(*rule)) == words

    def test_the_words_are_the_webs_too(self):
        """`tests/fixtures/repeat_words.json` holds this sentence and the web's
        `repeatPeriod` to one wording; each surface keeps its own clock face."""
        import json

        fixture = Path(__file__).resolve().parents[3] / "fixtures" / "repeat_words.json"
        for case in json.loads(fixture.read_text())["cases"]:
            words = vocabulary.describe_repeat({**case["rule"], "at": "16:07"})
            assert words == f"{case['period']} at 16:07", case

    @pytest.mark.parametrize("unit", vocabulary.REPEAT_UNITS)
    def test_every_unit_the_ledger_admits_has_words(self, unit):
        """A unit added to `ck_intent_repeat_unit` (and so to `REPEAT_UNITS`)
        fails here until the card and the CLI can say it."""
        words = vocabulary.describe_repeat(self._rule(unit, 2, [1], "10:00"))
        assert words.startswith("every 2 ") and words.endswith(" at 10:00")
        assert not any(w in words for w in vocabulary.TAP_WORDS)

    def test_an_unknown_unit_is_a_bug_raised(self):
        with pytest.raises(ValueError):
            vocabulary.describe_repeat(self._rule("month", 1, None, "10:00"))

    def test_a_weekdays_place_plus_one_is_its_iso_weekday(self):
        monday = dt.date(2026, 10, 12)
        days = [monday + dt.timedelta(days=n) for n in range(7)]
        assert vocabulary.WEEKDAYS == tuple(d.strftime("%a").lower() for d in days)
