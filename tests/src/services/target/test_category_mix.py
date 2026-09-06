"""The category mix — `category_post_case_mix`, D23's Type 2 SCD table — as
the one service that writes it (owner ruling 2026-09-06: subfolders are
categories; the workspace weights how often each posts, e.g. memes 70 /
merch 30). Sum-to-one is service-enforced here, as D23 says."""

from __future__ import annotations

import pytest

from src.services.target import category_mix

WS = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"


class _Exec:
    def __init__(self, rows=None):
        self.rows, self.calls = list(rows or []), []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        rows = self.rows.pop(0) if self.rows else []

        class _M:
            def all(self_inner):
                return rows

            def __iter__(self_inner):
                return iter(rows)

        class _R:
            rowcount = len(rows)

            def mappings(self_inner):
                return _M()

        return _R()


class TestValidation:
    @pytest.mark.parametrize(
        "mix, reason",
        [
            ([{"category": "", "ratio": 1}], "empty_category"),
            (
                [{"category": "a", "ratio": 0.5}, {"category": "a", "ratio": 0.5}],
                "duplicate_category",
            ),
            (
                [{"category": "a", "ratio": -0.1}, {"category": "b", "ratio": 1.1}],
                "bad_ratio",
            ),
            (
                [{"category": "a", "ratio": 0.5}, {"category": "b", "ratio": 0.4}],
                "sum_not_one",
            ),
            ([{"category": "a" * 101, "ratio": 1}], "category_too_long"),
            ("nope", "not_a_list"),
            (
                [
                    {"category": "a", "ratio": float("nan")},
                    {"category": "b", "ratio": 0.5},
                ],
                "bad_ratio",
            ),
            (
                [{"category": f"c{i}", "ratio": 0.002} for i in range(501)],
                "too_many_categories",
            ),
            (
                [{"category": "a", "ratio": 0.5}, {"category": "b", "ratio": 0.49}],
                "sum_not_one",
            ),
        ],
    )
    def test_refuses_by_name(self, mix, reason):
        with pytest.raises(category_mix.MixInvalid) as info:
            category_mix.normalize(mix)
        assert info.value.reason == reason

    def test_normalizes_ratios_to_four_places_and_trims_names(self):
        assert category_mix.normalize(
            [{"category": " memes ", "ratio": 0.7}, {"category": "merch", "ratio": 0.3}]
        ) == [
            ("memes", 0.7),
            ("merch", 0.3),
        ]

    def test_a_tiny_rounding_gap_is_tolerated(self):
        rows = category_mix.normalize(
            [
                {"category": "a", "ratio": 0.3333},
                {"category": "b", "ratio": 0.3333},
                {"category": "c", "ratio": 0.3334},
            ]
        )
        assert [r for _, r in rows] == [0.3333, 0.3333, 0.3334]

    def test_a_thousandth_is_tolerated_on_both_sides(self):
        """The web accepts a total of 99.9 % (|Δ| ≤ 0.1); the server must not
        then refuse 0.999 for a float rounding hair (review of #1251)."""
        assert category_mix.normalize(
            [{"category": "a", "ratio": 0.5}, {"category": "b", "ratio": 0.499}]
        )

    def test_an_empty_mix_is_legal_and_means_no_weighting(self):
        assert category_mix.normalize([]) == []


class TestSetMixIsOneSupersedeThenInserts:
    async def test_supersedes_the_current_rows_then_inserts_the_new_ones(self):
        ex = _Exec(rows=[[], [], [], []])
        out = await category_mix.set_mix(
            ex,
            workspace_id=WS,
            mix=[
                {"category": "memes", "ratio": 0.7},
                {"category": "merch", "ratio": 0.3},
            ],
            by_user_id=USER,
        )
        assert out == [
            {"category": "memes", "ratio": 0.7},
            {"category": "merch", "ratio": 0.3},
        ]
        sqls = [s for s, _ in ex.calls]
        # One writer per workspace at a time, then the supersede, then the rows.
        assert "pg_advisory_xact_lock" in sqls[0] and ex.calls[0][1] == {
            "key": f"case_mix:{WS}"
        }
        assert "UPDATE category_post_case_mix SET effective_to = now()" in sqls[1]
        assert "effective_to IS NULL" in sqls[1] and ex.calls[1][1] == {"ws": WS}
        assert all("INSERT INTO category_post_case_mix" in s for s in sqls[2:])
        assert ex.calls[2][1]["category"] == "memes" and ex.calls[2][1]["ratio"] == 0.7
        assert ex.calls[2][1]["by"] == USER and ex.calls[2][1]["ws"] == WS

    async def test_an_empty_mix_only_supersedes(self):
        ex = _Exec(rows=[[]])
        assert (
            await category_mix.set_mix(ex, workspace_id=WS, mix=[], by_user_id=USER)
            == []
        )
        assert len(ex.calls) == 2 and "UPDATE category_post_case_mix" in ex.calls[1][0]

    async def test_an_invalid_mix_writes_nothing(self):
        ex = _Exec()
        with pytest.raises(category_mix.MixInvalid):
            await category_mix.set_mix(
                ex,
                workspace_id=WS,
                mix=[{"category": "a", "ratio": 2}],
                by_user_id=USER,
            )
        assert ex.calls == []


class TestReads:
    async def test_current_mix_reads_the_live_rows_only(self):
        ex = _Exec(
            rows=[
                [
                    {"category": "memes", "ratio": 0.7},
                    {"category": "merch", "ratio": 0.3},
                ]
            ]
        )
        assert await category_mix.current_mix(ex, workspace_id=WS) == [
            {"category": "memes", "ratio": 0.7},
            {"category": "merch", "ratio": 0.3},
        ]
        ((sql, params),) = ex.calls
        assert "effective_to IS NULL" in sql and params == {"ws": WS}

    async def test_discovered_categories_count_available_media_including_the_root(self):
        ex = _Exec(
            rows=[
                [
                    {"category": "memes", "media_count": 12},
                    {"category": None, "media_count": 2},
                ]
            ]
        )
        out = await category_mix.discovered_categories(ex, workspace_id=WS)
        assert out == [
            {"category": "memes", "media_count": 12},
            {"category": None, "media_count": 2},
        ]
        ((sql, params),) = ex.calls
        assert "state = 'available'" in sql and "GROUP BY" in sql
