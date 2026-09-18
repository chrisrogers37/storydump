"""The legacy inventory literal is one fact, and it is the owner's F4 ruling.

`LEGACY_TABLES` names what production's `legacy` schema holds (measured by
the tear-out's read-only probe on 2026-09-17: exactly these sixteen). The
lane compares the replayed schema to it; phase 03's snapshot file copies it.
`HAND_MADE` is the subset no migration creates — exactly the tables the
by-hand fixture the lane seeds declares, so the two cannot drift apart.
"""

from __future__ import annotations

import re

from tests.scripts.conftest import BY_HAND_SQL
from tests.scripts.legacy_inventory import HAND_MADE, LEGACY_TABLES


def test_the_sixteen_tables_are_sixteen_distinct_names():
    assert len(LEGACY_TABLES) == 16
    assert len(set(LEGACY_TABLES)) == 16


def test_f4s_two_dispositions_are_in_the_inventory():
    """The fifteenth and sixteenth tables the owner ruled snapshotted."""
    assert "posting_history_dedup_archive" in LEGACY_TABLES
    assert "schema_version" in LEGACY_TABLES


def test_the_hand_made_subset_is_exactly_what_the_by_hand_fixture_creates():
    created = re.findall(
        r"CREATE TABLE (?:IF NOT EXISTS )?(?:legacy\.|public\.)?(\w+)",
        BY_HAND_SQL.read_text(),
    )
    assert sorted(created) == sorted(HAND_MADE)
    assert set(HAND_MADE) <= set(LEGACY_TABLES)
