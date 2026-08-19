"""The FC-2 ratchet's red-test demos (`04` F.6 gate, epic #843).

`04` F.6's gate is verbatim *"Ratchet proven by a red-test demo; baseline
committed; Phase X later burns it down."* — so the load-bearing tests here are
the ones that make the gate FAIL. A ratchet nobody has seen go red is
indistinguishable from one that cannot.

Two classes of test, and the second is the one F.1 needed:

1. **Falsifiability** — each of the four axes is driven red by a planted
   violation, on a synthetic tree so the real baseline is untouched.
2. **Discrimination** — the gate counts the THING, not the STRING. A module
   that merely says "telegram" in prose must NOT count, and a Telegram module
   that never spells the word MUST. Those two are the F.1 `SYSTEM_SCOPE`
   defect and its mirror image, pinned so a future substring shortcut fails
   here rather than in production.
"""

from __future__ import annotations

import json

import pytest

from scripts.telegram_ratchet import (
    AXES,
    compare,
    main,
    measure,
    unparsable,
)


def _tree(root, files):
    """Build a synthetic repo: {relative path: source}."""
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return root


def _empty_baseline():
    return {axis: [] for axis in AXES}


class TestItCountsTheThingNotTheString:
    """The F.1 defect, pinned in both directions."""

    def test_prose_mentioning_telegram_is_not_counted(self, tmp_path):
        """A raw substring ratchet counts this. It must not.

        This is the F.1 `SYSTEM_SCOPE` failure exactly: 24 of its 88 entries
        were imports and prose, leaving headroom a real violation could land
        in. Measured on the real tree at install, the substring predicate
        returns 72 where this one returns 27 — 46 phantom entries.
        """
        _tree(
            tmp_path, {"src/a.py": '# we no longer use telegram here\nX = "telegram"\n'}
        )
        assert measure(tmp_path)["telegram_modules"] == []

    def test_a_telegram_module_that_never_says_telegram_IS_counted(self, tmp_path):
        """The mirror image, and the more dangerous direction.

        `src/services/core/telegram_operation_state.py` is a real module on the
        tree whose text contains no literal "telegram" — a substring predicate
        misses it entirely. Over-counting reads as protection; under-counting
        is a hole.
        """
        _tree(tmp_path, {"src/services/core/telegram_state.py": "STATES = {'a'}\n"})
        assert measure(tmp_path)["telegram_modules"] == [
            "src/services/core/telegram_state.py"
        ]

    def test_a_telegram_named_module_counts_even_with_no_other_mention(self, tmp_path):
        """Adapter-hood is declared by the module's own name, so a
        ``telegram_*.py`` that imports nothing and never spells the word in its
        body still counts. Before #868 this spot asserted the mirror claim —
        that importing the SDK was itself enough — which is the exemption bug:
        eleven non-adapter modules imported a Telegram name for unrelated
        reasons and vanished from the chat-id axis. See
        ``test_a_chat_id_function_is_counted_even_when_its_module_imports_
        telegram_for_an_unrelated_reason``."""
        _tree(tmp_path, {"src/telegram_z.py": "VALUE = 1\n"})
        assert measure(tmp_path)["telegram_modules"] == ["src/telegram_z.py"]

    def test_a_non_adapter_importing_the_sdk_does_NOT_count(self, tmp_path):
        """The direct inverse of what this spot used to assert (#868)."""
        _tree(tmp_path, {"src/z.py": "from telegram.ext import Application\n"})
        assert measure(tmp_path)["telegram_modules"] == []

    def test_telegram_must_be_the_stem_not_a_substring_of_it(self, tmp_path):
        """The docstring claims a stem check closes a collision class a raw
        path-substring would admit. On the present tree the two forms agree, so
        that claim has no natural witness — this supplies one rather than
        leaving it unpinned prose."""
        _tree(tmp_path, {"src/nontelegram_helper.py": "def f(chat_id):\n    pass\n"})
        out = measure(tmp_path)
        assert out["telegram_modules"] == []
        assert out["chat_id_functions_outside_adapters"] == [
            "src/nontelegram_helper.py::f"
        ]

    def test_a_word_containing_telegram_in_a_docstring_is_not_an_import(self, tmp_path):
        _tree(
            tmp_path, {"src/z.py": '"""Sends via telegram eventually."""\nimport os\n'}
        )
        assert measure(tmp_path)["telegram_modules"] == []


class TestTheRatchetGoesRed:
    """Falsifiability, one axis at a time."""

    def test_a_new_telegram_module_reddens_it(self, tmp_path):
        _tree(tmp_path, {"src/telegram_new_adapter.py": '"""An adapter."""\n'})
        added, removed = compare(measure(tmp_path), _empty_baseline())[
            "telegram_modules"
        ]
        assert added == ["src/telegram_new_adapter.py"] and removed == []

    def test_a_new_core_telegram_module_reddens_the_core_axis(self, tmp_path):
        _tree(tmp_path, {"src/services/core/telegram_thing.py": '"""Adapter."""\n'})
        added, _ = compare(measure(tmp_path), _empty_baseline())[
            "core_telegram_modules"
        ]
        assert added == ["src/services/core/telegram_thing.py"]

    def test_a_chat_id_parameter_outside_an_adapter_reddens_it(self, tmp_path):
        _tree(
            tmp_path,
            {"src/services/core/svc.py": "def notify(chat_id, body):\n    pass\n"},
        )
        added, _ = compare(measure(tmp_path), _empty_baseline())[
            "chat_id_functions_outside_adapters"
        ]
        assert added == ["src/services/core/svc.py::notify"]

    def test_provider_account_ref_in_a_log_call_reddens_the_hard_zero_rule(
        self, tmp_path
    ):
        """`07` §5. This axis is the only pass/fail one, because it is the only
        one already at zero on the real tree — so it needs a demo more than the
        others, not less: a rule whose baseline is empty passes vacuously
        against any tree it cannot see."""
        _tree(
            tmp_path,
            {
                "src/svc.py": (
                    "import logging\n"
                    "log = logging.getLogger(__name__)\n"
                    "def go(acct):\n"
                    "    log.info('linked %s', acct.provider_account_ref)\n"
                )
            },
        )
        added, _ = compare(measure(tmp_path), _empty_baseline())[
            "provider_account_ref_log_sites"
        ]
        assert added == ["src/svc.py::4"]


class TestEqualityNotCeiling:
    def test_a_SHRINK_is_drift_too(self, tmp_path):
        """The ruling this follows. Under a ceiling the retired module would
        pass silently and leave a slot of unreviewed headroom behind it; under
        equality it lands in the same reviewable diff as an addition."""
        _tree(tmp_path, {"src/telegram_keep.py": '"""Adapter."""\n'})
        baseline = {
            **_empty_baseline(),
            "telegram_modules": ["src/telegram_keep.py", "src/telegram_gone.py"],
        }
        added, removed = compare(measure(tmp_path), baseline)["telegram_modules"]
        assert added == [] and removed == ["src/telegram_gone.py"]

    def test_a_shrink_exits_nonzero_through_the_cli(self, tmp_path, capsys):
        _tree(tmp_path, {"src/telegram_keep.py": '"""Adapter."""\n'})
        bl = tmp_path / "bl.json"
        bl.write_text(
            json.dumps(
                {
                    **_empty_baseline(),
                    "telegram_modules": [
                        "src/telegram_keep.py",
                        "src/telegram_gone.py",
                    ],
                }
            )
        )
        rc = main(["--repo", str(tmp_path), "--baseline", str(bl)])
        assert rc == 1
        assert "- src/telegram_gone.py" in capsys.readouterr().out


class TestTheAdapterExemptionIsRealAndBounded:
    def test_a_chat_id_function_is_counted_even_when_its_module_imports_telegram_for_an_unrelated_reason(
        self, tmp_path
    ):
        """#868: importing something Telegram-flavored is not the same fact as
        being an edge adapter — an SDK type used for outbound sending, or an
        internal exception type caught for error handling, are the real shape
        (scheduler.py, google_drive_oauth.py, 9 others). Fails under the old
        single-hop is_telegram_coupled(), which read any telegram-ish import as
        adapter-hood regardless of why the import exists."""
        _tree(
            tmp_path,
            {
                "src/services/core/not_an_adapter.py": (
                    "from telegram import Bot\ndef notify(chat_id):\n    pass\n"
                )
            },
        )
        out = measure(tmp_path)
        assert out["chat_id_functions_outside_adapters"] == [
            "src/services/core/not_an_adapter.py::notify"
        ]
        assert out["telegram_modules"] == []

    def test_a_chat_id_INSIDE_an_allowlisted_adapter_is_not_a_violation(self, tmp_path):
        """FC-2 puts adapters at the edges, so a Telegram adapter taking a chat
        id is the design working. Counting it would make the rule fire on
        correct code — 45 of the 160 chat-id functions on the real tree are
        exactly this."""
        _tree(
            tmp_path,
            {"src/services/core/telegram_send.py": "def send(chat_id):\n    pass\n"},
        )
        out = measure(tmp_path)
        assert out["chat_id_functions_outside_adapters"] == []
        assert out["telegram_modules"] == ["src/services/core/telegram_send.py"]

    @pytest.mark.parametrize(
        "sig",
        [
            "def f(chat_id):",
            "def f(*, chat_id):",
            "def f(chat_id, /):",
            "async def f(chat_id):",
            "def f(chatId):",
            "def f(telegram_chat_id):",
        ],
    )
    def test_the_parameter_rule_cannot_be_evaded_by_position_or_case(
        self, tmp_path, sig
    ):
        """Positional-only, keyword-only and async forms are all read. Reading
        only `args` would let the rule be dodged by moving the parameter across
        a `/` or a `*`."""
        _tree(tmp_path, {"src/s.py": f"{sig}\n    pass\n"})
        assert measure(tmp_path)["chat_id_functions_outside_adapters"] == [
            "src/s.py::f"
        ]


class TestItDisclosesWhatItCouldNotRead:
    def test_an_unparsable_module_is_reported_not_silently_skipped(self, tmp_path):
        """A module the gate cannot parse contributes nothing to every axis, so
        without disclosure a syntax error would look like a clean shrink."""
        _tree(tmp_path, {"src/broken.py": "def f(\n"})
        assert unparsable(tmp_path) == ["src/broken.py"]
        assert measure(tmp_path)["telegram_modules"] == []


class TestTheRealBaselineIsHonest:
    def test_the_committed_baseline_matches_the_tree_it_claims_to_describe(self):
        """The §4 requirement: baseline and gate cannot disagree, because the
        baseline is derived by the same functions the check runs."""
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        baseline = json.loads(
            (repo / "scripts" / "telegram_ratchet_baseline.json").read_text()
        )
        for axis, (added, removed) in compare(measure(repo), baseline).items():
            assert added == [] and removed == [], f"{axis}: +{added} -{removed}"

    def test_no_count_is_stored_beside_its_set(self):
        """Storing a count next to the set it summarises is a second source of
        truth that can drift from it — the failure §4 describes one level up.
        Every count this gate reports is `len()` of a stored list."""
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        baseline = json.loads(
            (repo / "scripts" / "telegram_ratchet_baseline.json").read_text()
        )
        for key, value in baseline.items():
            assert key == "predicate" or isinstance(value, list), (
                f"{key} is not a list — a stored scalar can disagree with its set"
            )

    def test_the_predicate_is_committed_with_the_numbers(self):
        """F.1 §4: a committed count without its predicate is not a baseline,
        it is a number."""
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        baseline = json.loads(
            (repo / "scripts" / "telegram_ratchet_baseline.json").read_text()
        )
        import scripts.telegram_ratchet as mod

        assert baseline.get("predicate") == mod.__doc__.strip(), (
            "the committed predicate has drifted from the module docstring it was"
            " taken from. That is the F.1 §4 failure in miniature: the numbers and"
            " the description of what was counted must travel together, so a"
            " docstring edit requires --write-baseline in the same PR."
        )

    def test_the_real_tree_has_modules_on_every_ratcheted_axis(self):
        """Positive control. Three of the four axes passing with an empty set
        would mean the gate found nothing, not that the tree is clean."""
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        out = measure(repo)
        # Floors, not equalities — the committed baseline is what pins exact
        # membership. They moved with #868 (27->16, 22->15, 89->114) because
        # eleven modules stopped being wrongly exempt; the slack below each is
        # deliberately kept wide enough that "the gate found nothing" still
        # trips it.
        assert len(out["telegram_modules"]) > 10
        assert len(out["core_telegram_modules"]) > 5
        assert len(out["chat_id_functions_outside_adapters"]) > 50
