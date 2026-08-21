"""The Fork E cost measurement's one pure guard (#943).

Only `assert_code_default_matches_source` is covered. The rest is a read-only
production query whose value is the numbers it returns.

That guard earns a test because it is **load-bearing for the finding itself**:
both tenants have `repost_ttl_days` NULL, so the whole "is a NULL setting
bounded or unbounded?" answer rests on the code fallback being 30. The script
prints that constant, and a printed constant is one that can go stale against
the source it claims to quote. The guard reads it back; this proves the guard
can fail.
"""

import pytest

from scripts.fork_e_lock_cost import (
    CODE_DEFAULT_REPOST_TTL_DAYS,
    assert_code_default_matches_source,
)


class TestThePrintedFallbackCannotGoStale:
    def test_it_agrees_with_the_source_today(self):
        assert_code_default_matches_source()  # does not raise

    def test_the_constant_is_the_one_the_app_would_use(self):
        """Independent read of the same fact — if this and the guard both
        derived from the same place, neither would be checking anything."""
        from src.config import defaults

        assert defaults.DEFAULT_REPOST_TTL_DAYS == CODE_DEFAULT_REPOST_TTL_DAYS

    def test_the_guard_fails_when_the_source_moves(self, tmp_path, monkeypatch):
        """A guard that cannot fail proves nothing. Drift is simulated by
        pointing the guard at a defaults.py that disagrees."""
        from scripts import fork_e_lock_cost as mod

        fake_root = tmp_path / "scripts"
        fake_root.mkdir()
        cfg = tmp_path / "src" / "config"
        cfg.mkdir(parents=True)
        (cfg / "defaults.py").write_text("DEFAULT_REPOST_TTL_DAYS = 999\n")
        monkeypatch.setattr(mod, "__file__", str(fake_root / "fork_e_lock_cost.py"))

        with pytest.raises(RuntimeError, match="drifted"):
            mod.assert_code_default_matches_source()

    def test_the_guard_fails_when_the_constant_is_gone_entirely(
        self, tmp_path, monkeypatch
    ):
        """Deletion is a different failure from disagreement, and silently
        passing on a missing constant would be the worse of the two."""
        from scripts import fork_e_lock_cost as mod

        fake_root = tmp_path / "scripts"
        fake_root.mkdir()
        cfg = tmp_path / "src" / "config"
        cfg.mkdir(parents=True)
        (cfg / "defaults.py").write_text("SOMETHING_ELSE = 1\n")
        monkeypatch.setattr(mod, "__file__", str(fake_root / "fork_e_lock_cost.py"))

        with pytest.raises(RuntimeError, match="not found"):
            mod.assert_code_default_matches_source()
