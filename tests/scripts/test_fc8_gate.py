"""The FC-8 zero-row gate, against the real replayed legacy schema (#790 M.1).

Runs on `replayed_db` — the actual legacy lineage replayed from empty — rather
than a hand-built fixture table, so the gate is exercised against the column
types, defaults and constraints production really has.

The property that matters most here is the one this repo has already paid for
once (#758): **a gate that cannot answer must not report CLEAR.** A missing
table returns ERROR, not zero.
"""

import psycopg2
import pytest

from scripts.fc8_gate import CLEAR, ERROR, HALT, Census, census, main, render
from tests.scripts.conftest import execute

pytestmark = [pytest.mark.integration]


def _media(dsn, source_type, is_active=True, path=None):
    """Insert one legacy media_items row, returning its id."""
    path = path or f"/tmp/{source_type}-{is_active}-{id(dsn)}-{_media.n}"
    _media.n += 1
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO media_items"
            " (file_path, file_name, file_size, file_hash, source_type, is_active)"
            " VALUES (%s, 'f.jpg', 1, %s, %s, %s) RETURNING id",
            (path, f"hash-{_media.n}", source_type, is_active),
        )
        mid = cur.fetchone()[0]
    conn.close()
    return mid


_media.n = 0


def _history(dsn, media_id):
    execute(
        dsn,
        # queue_deleted_at is NOT NULL in the replayed schema (the row records a
        # queue item that has been consumed), so it is supplied here rather than
        # defaulted — caught by running against the real lineage, not by reading
        # the model.
        "INSERT INTO posting_history"
        " (media_item_id, queue_created_at, queue_deleted_at, scheduled_for,"
        "  posted_at, status, success)"
        " VALUES (%s, now(), now(), now(), now(), 'posted', true)",
        (media_id,),
    )


def _census(dsn):
    conn = psycopg2.connect(dsn)
    try:
        return census(conn)
    finally:
        conn.close()


class TestTheTwoNamedCounts:
    def test_an_empty_corpus_is_clear(self, replayed_db):
        c = _census(replayed_db)
        assert (c.live_named, c.history_named) == (0, 0)
        assert c.halts is False

    @pytest.mark.parametrize("origin", ["local", "upload"])
    def test_one_live_row_of_a_named_origin_halts(self, replayed_db, origin):
        _media(replayed_db, origin)
        c = _census(replayed_db)
        assert c.live_named == 1
        assert c.halts is True

    def test_a_google_drive_row_does_not_halt(self, replayed_db):
        """The positive control. Without it, a gate that halted on everything
        would pass every test above."""
        _media(replayed_db, "google_drive")
        c = _census(replayed_db)
        assert (c.live_named, c.history_named) == (0, 0)
        assert c.halts is False

    def test_an_inactive_local_row_is_not_live_but_its_history_still_counts(
        self, replayed_db
    ):
        """The two counts are deliberately not the same predicate. A posted row
        whose media was since deactivated still has no target destination for
        that media, so count 2 does not filter on is_active — and this is the
        test that would go red if someone 'tidied' the two queries into one."""
        mid = _media(replayed_db, "local", is_active=False)
        _history(replayed_db, mid)
        c = _census(replayed_db)
        assert c.live_named == 0, "deactivated row is not live"
        assert c.history_named == 1, "but its history row still resolves to it"
        assert c.halts is True

    def test_history_resolving_through_the_fk_is_what_count_two_means(
        self, replayed_db
    ):
        """`posting_history` carries no origin column — 'resolving to' can only
        be answered through the join, so a drive-origin history row must not
        count while a local-origin one must."""
        _history(replayed_db, _media(replayed_db, "google_drive"))
        assert _census(replayed_db).history_named == 0

        _history(replayed_db, _media(replayed_db, "local"))
        assert _census(replayed_db).history_named == 1


class TestItDisclosesWhatItCannotRule:
    """No invented policy: the gate halts on FC-8's two counts and reports
    everything else rather than bucketing it silently."""

    def test_instagram_backfill_is_reported_unclassified_and_does_not_halt(
        self, replayed_db
    ):
        """The real gap. FC-8 names local/upload; the corpus also carries
        `instagram_backfill`; and the target's media-source CHECK is closed at
        'gdrive' (`02` §2), so such a row plausibly has no destination either.
        Deciding that is an owner ruling — so the gate surfaces it and does NOT
        invent a third halt condition."""
        _media(replayed_db, "instagram_backfill")
        c = _census(replayed_db)
        assert c.halts is False, "it must not invent a halt it has no authority for"
        assert "instagram_backfill" in c.unclassified
        assert c.by_source["instagram_backfill"]["live"] == 1

    def test_a_null_is_active_row_is_its_own_bucket_not_folded_either_way(
        self, replayed_db
    ):
        """`is_active` is nullable, so liveness has three states. `IS NOT FALSE`
        would quietly call NULL live; `IS TRUE` alone would quietly call it
        dead. It is excluded from the live count AND counted separately."""
        _media(replayed_db, "local", is_active=None)
        c = _census(replayed_db)
        assert c.live_named == 0
        assert c.null_active == 1

    def test_a_ruled_origin_is_never_listed_as_unclassified(self, replayed_db):
        for origin in ("local", "upload", "google_drive"):
            _media(replayed_db, origin)
        assert _census(replayed_db).unclassified == []


class TestCannotAnswerIsNotClear:
    """#758's lesson, applied before it can be re-learned: the failure mode
    that matters is a gate reporting the safe answer when it ran nothing."""

    def test_a_missing_table_is_error_not_clear(self, replayed_db):
        execute(replayed_db, "ALTER TABLE media_items RENAME TO media_items_hidden")
        try:
            rc = main(["--dsn", replayed_db])
        finally:
            execute(replayed_db, "ALTER TABLE media_items_hidden RENAME TO media_items")
        assert rc == ERROR, "a gate that could not answer must not exit CLEAR"

    def test_an_unreachable_database_is_error_not_clear(self):
        rc = main(["--dsn", "postgresql://nobody@127.0.0.1:1/nope"])
        assert rc == ERROR


class TestExitCodes:
    def test_clear_exits_zero(self, replayed_db):
        assert main(["--dsn", replayed_db]) == CLEAR

    def test_halt_exits_one(self, replayed_db):
        _media(replayed_db, "local")
        assert main(["--dsn", replayed_db]) == HALT

    def test_json_mode_carries_the_disclosure_not_only_the_verdict(
        self, replayed_db, capsys
    ):
        import json

        _media(replayed_db, "instagram_backfill")
        main(["--dsn", replayed_db, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "CLEAR"
        assert payload["unclassified_source_types"] == ["instagram_backfill"]


class TestTheDisclosureReachesTheOutputAnOperatorReads:
    """The two disclosure buckets, covered where they are actually read.

    `census()` computing them and `--json` carrying them are both covered
    above — and neither would notice if the **text** blocks were deleted from
    `render()`. Text is the DEFAULT path (`--json` is opt-in), so that deletion
    would leave every other test green, CI green, both halt counts still
    correct, and the unknowns simply gone from what a human sees.

    That matters more than coverage arithmetic because these two buckets *are*
    the mechanism keeping unruled things visible: they are what stopped
    `instagram_backfill` being swept to the safe side by a `NOT IN` that would
    have read as deliberate, and what stopped the nullable `is_active` third
    state being silently assigned. A mechanism whose failure produces silence
    rather than an error has to be pinned where it is consumed.

    These are pure-function tests over `render()` — no database — so they are
    fast and cannot contend for the host. One end-to-end case below proves the
    wiring, which pure tests cannot.
    """

    UNCLASSIFIED_CENSUS = Census(
        live_named=0,
        history_named=0,
        by_source={
            "google_drive": {"live": 3, "total": 4},
            "instagram_backfill": {"live": 2, "total": 2},
        },
        unclassified=["instagram_backfill"],
        null_active=0,
    )

    def test_an_unclassified_origin_is_named_and_marked_in_the_text(self):
        out = render(self.UNCLASSIFIED_CENSUS)
        assert "instagram_backfill" in out
        assert "UNCLASSIFIED" in out
        # The inline marker and the explanatory block are two separate
        # disclosures; either can be deleted alone, so both are asserted.
        marked = [ln for ln in out.splitlines() if "instagram_backfill" in ln]
        assert any("UNCLASSIFIED" in ln for ln in marked), (
            "the row itself must carry the mark — a reader scanning the census"
            f" table must not have to correlate it with a block below: {marked}"
        )

    def test_the_unclassified_block_keeps_the_reason_not_just_the_label(self):
        """A bare marker degrades to noise. What makes it actionable is that it
        says why it did not halt and whose call it is — so that survives too."""
        out = render(self.UNCLASSIFIED_CENSUS)
        assert "does NOT halt" in out
        assert "owner ruling" in out
        assert "gdrive" in out, "the closed target CHECK is why this matters"

    def test_a_null_is_active_count_is_stated_with_its_number(self):
        out = render(
            Census(by_source={"local": {"live": 0, "total": 1}}, null_active=7)
        )
        assert "7 media_items have is_active IS NULL" in out
        assert "neither live nor dead" in out

    def test_a_clean_census_prints_neither_disclosure(self):
        """The negative control. Without it, a `render` that unconditionally
        printed both blocks would satisfy every assertion above while telling
        an operator nothing."""
        out = render(Census(by_source={"google_drive": {"live": 5, "total": 5}}))
        assert "UNCLASSIFIED" not in out
        assert "is_active IS NULL" not in out
        assert "VERDICT: CLEAR" in out

    def test_every_origin_present_is_listed_with_its_counts(self):
        """The census table is itself a disclosure — a classified origin going
        missing would hide the corpus's shape just as effectively."""
        out = render(self.UNCLASSIFIED_CENSUS)
        assert "google_drive" in out
        assert "live=3" in out and "total=4" in out

    def test_the_default_path_prints_the_text_disclosure_to_stdout(
        self, replayed_db, capsys
    ):
        """The wiring, which no pure test can reach: text is what `main`
        actually emits without `--json`. If the default ever flipped, every
        assertion above would still pass against a `render` nobody calls."""
        _media(replayed_db, "instagram_backfill")
        _media(replayed_db, "local", is_active=None)

        assert main(["--dsn", replayed_db]) == CLEAR
        out = capsys.readouterr().out
        assert "UNCLASSIFIED" in out
        assert "is_active IS NULL" in out
        assert not out.lstrip().startswith("{"), "default must be text, not JSON"
