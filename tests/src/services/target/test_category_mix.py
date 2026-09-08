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


S1, S2, S3 = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


def _row(source_id, ratio):
    return {"source_id": source_id, "ratio": ratio}


class TestValidation:
    """Owner ruling 2026-09-08: the mix is keyed on the CONNECTED FOLDER
    (`media_sources.id`), never a name. Refused by name; a 0 is Off."""

    @pytest.mark.parametrize(
        "mix, reason",
        [
            ("nope", "not_a_list"),
            ([_row("", 1.0)], "empty_source"),
            ([_row(None, 1.0)], "empty_source"),
            ([_row(S1, 0.5), _row(S1, 0.5)], "duplicate_source"),
            ([_row(S1, 1.5)], "bad_ratio"),
            ([_row(S1, -0.1)], "bad_ratio"),
            ([_row(S1, float("nan"))], "bad_ratio"),
            ([_row(S1, True)], "bad_ratio"),
            ([_row(S1, 0.7), _row(S2, 0.2)], "sum_not_one"),
        ],
    )
    def test_refuses_by_name(self, mix, reason):
        with pytest.raises(category_mix.MixInvalid) as exc:
            category_mix.normalize(mix)
        assert exc.value.reason == reason

    def test_normalizes_ratios_to_four_places(self):
        assert category_mix.normalize([_row(S1, 0.70004), _row(S2, 0.29996)]) == [
            (S1, 0.7),
            (S2, 0.3),
        ]

    def test_a_zero_is_kept_as_off_and_the_rest_must_sum_to_one(self):
        assert category_mix.normalize([_row(S1, 1.0), _row(S2, 0)]) == [
            (S1, 1.0),
            (S2, 0.0),
        ]

    def test_a_tiny_rounding_gap_is_tolerated(self):
        rows = category_mix.normalize(
            [_row(S1, 0.3333), _row(S2, 0.3333), _row(S3, 0.3333)]
        )
        assert len(rows) == 3

    def test_an_empty_mix_is_legal_and_means_every_folder_is_automatic(self):
        assert category_mix.normalize([]) == []

    def test_too_many_rows_is_refused(self):
        rows = [_row(f"{i:032x}", 1 / 600) for i in range(600)]
        with pytest.raises(category_mix.MixInvalid) as exc:
            category_mix.normalize(rows)
        assert exc.value.reason == "too_many_sources"


class TestTheAutomaticRule:
    """The draw's arithmetic, ONE function for the planner and the card
    (F4 as re-locked 2026-09-07, per source since 2026-09-08): explicit rows
    share by ratio; automatic rows (no row) share the rest by media; together
    automatic rows never take more than the smallest explicit weight on the
    final split; Off (ratio 0) is never drawn and shapes nothing."""

    def _w(self, rows):
        return category_mix.weights(rows)

    def test_explicit_rows_only_share_by_ratio(self):
        w = self._w(
            [
                {"source_id": S1, "ratio": 0.7, "n": 5},
                {"source_id": S2, "ratio": 0.3, "n": 5},
            ]
        )
        assert w == pytest.approx({S1: 0.7, S2: 0.3})

    def test_automatic_rows_only_share_by_media(self):
        w = self._w(
            [
                {"source_id": S1, "ratio": None, "n": 30},
                {"source_id": S2, "ratio": None, "n": 10},
            ]
        )
        assert w == pytest.approx({S1: 0.75, S2: 0.25})

    def test_a_small_automatic_folder_takes_its_media_share(self):
        rows = [
            {"source_id": S1, "ratio": 0.7, "n": 3474},
            {"source_id": S2, "ratio": 0.3, "n": 1077},
            {"source_id": S3, "ratio": None, "n": 10},
        ]
        w = self._w(rows)
        share = 10 / (3474 + 1077 + 10)
        assert w[S3] == pytest.approx(share)
        assert w[S1] == pytest.approx((1 - share) * 0.7) and w[S2] == pytest.approx(
            (1 - share) * 0.3
        )
        assert sum(w.values()) == pytest.approx(1.0)

    def test_a_huge_automatic_folder_is_capped_at_the_smallest_explicit_weight(self):
        rows = [
            {"source_id": S1, "ratio": 0.7, "n": 100},
            {"source_id": S2, "ratio": 0.3, "n": 100},
            {"source_id": S3, "ratio": None, "n": 50_000},
        ]
        w = self._w(rows)
        assert w[S3] == pytest.approx(0.3 / 1.3)
        assert w[S2] == pytest.approx(0.3 / 1.3), (
            "on the final split the auto pool equals the smallest explicit weight"
        )

    def test_one_explicit_folder_at_100_caps_automatic_at_half(self):
        w = self._w(
            [
                {"source_id": S1, "ratio": 1.0, "n": 100},
                {"source_id": S3, "ratio": None, "n": 50_000},
            ]
        )
        assert w[S3] == pytest.approx(0.5) and w[S1] == pytest.approx(0.5)

    def test_an_explicit_folder_with_no_media_is_dropped_before_the_cap(self):
        rows = [
            {"source_id": S1, "ratio": 0.9, "n": 10},
            {"source_id": S2, "ratio": 0.1, "n": 0},
            {"source_id": S3, "ratio": None, "n": 10_000},
        ]
        w = self._w(rows)
        assert w[S2] == 0
        assert w[S3] == pytest.approx(0.5), (
            "the cap uses the remaining explicit rows (100 → 50 %)"
        )

    def test_off_is_never_drawn_and_shapes_nothing(self):
        rows = [
            {"source_id": S1, "ratio": 1.0, "n": 10},
            {"source_id": S2, "ratio": 0.0, "n": 10},
            {"source_id": S3, "ratio": None, "n": 10},
        ]
        w = self._w(rows)
        assert (
            w[S2] == 0 and w[S1] == pytest.approx(0.5) and w[S3] == pytest.approx(0.5)
        )

    def test_nothing_eligible_is_an_empty_draw(self):
        assert self._w([{"source_id": S1, "ratio": 0.5, "n": 0}]) == {S1: 0}
        assert self._w([]) == {}


class TestSetMixIsOneSupersedeThenInserts:
    async def test_validates_the_ids_then_supersedes_then_inserts_with_the_label(self):
        ex = _Exec(rows=[[{"id": S1, "label": "memes"}, {"id": S2, "label": "merch"}]])
        stored = await category_mix.set_mix(
            ex,
            workspace_id="ws-1",
            mix=[_row(S1, 0.7), _row(S2, 0.3)],
            by_user_id="u-1",
        )
        assert stored == [
            {"source_id": S1, "ratio": 0.7},
            {"source_id": S2, "ratio": 0.3},
        ]
        sqls = [c[0] for c in ex.calls]
        assert "FROM media_sources" in sqls[0] and "s.workspace_id = :ws" in sqls[0]
        assert "pg_advisory_xact_lock" in sqls[1]
        assert "SET effective_to = now()" in sqls[2] and "workspace_id = :ws" in sqls[2]
        inserts = [c for c in ex.calls if "INSERT INTO category_post_case_mix" in c[0]]
        assert [i[1]["source_id"] for i in inserts] == [S1, S2]
        assert [i[1]["category"] for i in inserts] == ["memes", "merch"], (
            "the label rides along"
        )

    async def test_an_id_that_is_not_a_connected_folder_here_is_refused_before_any_write(
        self,
    ):
        ex = _Exec(rows=[[{"id": S1, "label": "memes"}]])
        with pytest.raises(category_mix.MixInvalid) as exc:
            await category_mix.set_mix(
                ex,
                workspace_id="ws-1",
                mix=[_row(S1, 0.5), _row(S2, 0.5)],
                by_user_id=None,
            )
        assert exc.value.reason == "unknown_source"
        assert not any("UPDATE" in c[0] or "INSERT" in c[0] for c in ex.calls)

    async def test_all_zero_rows_are_off_only_when_another_folder_stays_automatic(self):
        # One connected folder not named → it is automatic → the mix is legal.
        ex = _Exec(
            rows=[[{"id": S1, "label": "memes"}, {"id": S2, "label": "archive"}]]
        )
        stored = await category_mix.set_mix(
            ex, workspace_id="ws-1", mix=[_row(S2, 0.0)], by_user_id=None
        )
        assert stored == [{"source_id": S2, "ratio": 0.0}]
        # Every connected folder named at 0 → nothing would post → refused.
        ex = _Exec(
            rows=[[{"id": S1, "label": "memes"}, {"id": S2, "label": "archive"}]]
        )
        with pytest.raises(category_mix.MixInvalid) as exc:
            await category_mix.set_mix(
                ex,
                workspace_id="ws-1",
                mix=[_row(S1, 0.0), _row(S2, 0.0)],
                by_user_id=None,
            )
        assert exc.value.reason == "all_off"
        assert not any("INSERT" in c[0] for c in ex.calls)

    async def test_an_empty_mix_only_supersedes(self):
        ex = _Exec()
        assert (
            await category_mix.set_mix(ex, workspace_id="ws-1", mix=[], by_user_id=None)
            == []
        )
        assert any("SET effective_to = now()" in c[0] for c in ex.calls)
        assert not any("INSERT" in c[0] for c in ex.calls)

    async def test_an_invalid_mix_writes_nothing(self):
        ex = _Exec()
        with pytest.raises(category_mix.MixInvalid):
            await category_mix.set_mix(
                ex, workspace_id="ws-1", mix=[_row(S1, 0.4)], by_user_id=None
            )
        assert ex.calls == []


class TestReads:
    async def test_the_view_carries_each_connected_folder_with_its_effective_share(
        self,
    ):
        ex = _Exec(
            rows=[
                [
                    {
                        "source_id": S1,
                        "provider": "gdrive",
                        "name": "memes",
                        "state": "active",
                        "media_count": 30,
                        "ratio": 0.7,
                    },
                    {
                        "source_id": S2,
                        "provider": "gdrive",
                        "name": "merch",
                        "state": "active",
                        "media_count": 10,
                        "ratio": 0.3,
                    },
                    {
                        "source_id": S3,
                        "provider": "gdrive",
                        "name": "events",
                        "state": "active",
                        "media_count": 0,
                        "ratio": None,
                    },
                ]
            ]
        )
        rows = await category_mix.mix_view(ex, workspace_id="ws-1")
        assert [r["source_id"] for r in rows] == [S1, S2, S3]
        assert rows[0]["effective"] == pytest.approx(70.0) and rows[1][
            "effective"
        ] == pytest.approx(30.0)
        assert rows[2]["effective"] == 0 and rows[2]["ratio"] is None
        assert "removed" in ex.calls[0][0] and "workspace_id = :ws" in ex.calls[0][0]

    def test_the_v1_shape_is_derived_from_the_view_for_the_old_card(self):
        rows = [
            {
                "source_id": S1,
                "provider": "gdrive",
                "name": "memes",
                "state": "active",
                "media_count": 30,
                "ratio": 0.7,
                "effective": 70.0,
            },
            {
                "source_id": S3,
                "provider": "gdrive",
                "name": "events",
                "state": "active",
                "media_count": 4,
                "ratio": None,
                "effective": 30.0,
            },
        ]
        assert category_mix.v1_shape(rows) == {
            "mix": [{"category": "memes", "ratio": 0.7}],
            "categories": [
                {"category": "memes", "media_count": 30},
                {"category": "events", "media_count": 4},
            ],
        }

    def test_a_v1_body_by_name_resolves_to_sources_or_is_refused(self):
        view = [
            {"source_id": S1, "name": "memes"},
            {"source_id": S2, "name": "merch"},
            {"source_id": S3, "name": "merch"},
        ]
        with pytest.raises(category_mix.MixInvalid) as exc:
            category_mix.resolve_names(view, [{"category": "merch", "ratio": 1.0}])
        assert exc.value.reason == "ambiguous_name"
        assert category_mix.resolve_names(
            view, [{"category": "memes", "ratio": 1.0}]
        ) == [{"source_id": S1, "ratio": 1.0}]
        with pytest.raises(category_mix.MixInvalid) as exc:
            category_mix.resolve_names(view, [{"category": "ghost", "ratio": 1.0}])
        assert exc.value.reason == "unknown_source"
