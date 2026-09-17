"""The legacy inventory literal is one fact, and it is the owner's F4 ruling.

`LEGACY_TABLES` names what production's `legacy` schema holds (measured by
the tear-out's read-only probe on 2026-09-17: exactly these sixteen);
`LINEAGE_TABLES` is what the lineage's replay creates. The lane compares the
second to the replayed schema; phase 03's snapshot file copies the first.
Nothing else reads `LEGACY_TABLES` until then, so its shape is pinned here.
"""

from __future__ import annotations

from tests.scripts.legacy_inventory import HAND_MADE, LEGACY_TABLES, LINEAGE_TABLES


def test_the_sixteen_tables_are_sixteen_distinct_names():
    assert len(LEGACY_TABLES) == 16
    assert len(set(LEGACY_TABLES)) == 16


def test_f4s_two_dispositions_are_in_the_inventory():
    """The fifteenth and sixteenth tables the owner ruled snapshotted."""
    assert "posting_history_dedup_archive" in LEGACY_TABLES
    assert "schema_version" in LEGACY_TABLES


def test_the_hand_made_table_is_the_only_difference_between_the_two_subsets():
    assert set(HAND_MADE) <= set(LEGACY_TABLES)
    assert set(LINEAGE_TABLES) == set(LEGACY_TABLES) - set(HAND_MADE)
    assert len(LINEAGE_TABLES) == 15
