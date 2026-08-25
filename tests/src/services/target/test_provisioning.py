"""The two pure parsers in `provisioning` — unit gate, no database.

These exist as a fast tier beside `tests/scripts/test_provisioning_gate.py`,
which is `integration`+`slow` and is skipped wherever Postgres is not
available. What is covered here is only the part that is a pure function of its
argument; every claim about what actually LANDS in a row — idempotency,
tenancy, the seeded cursor, the clock picking it up — lives in the gate,
because none of it can be proven without the schema.

The parsers are not cosmetic. `uq_ig_account_live` and the source's
folder-match are both EXACT string comparisons, so two spellings of one
reference are two destinations posting to one Instagram feed, or two sources
ingesting one Drive folder twice. Normalising is the only place that is
prevented.
"""

from __future__ import annotations

import pytest

from src.services.target.provisioning import (
    ACCOUNT_REF_MAX,
    ProvisioningRefused,
    account_ref_from,
    folder_ref_from,
)


class TestAccountRefFrom:
    def test_a_plain_reference_passes_through(self):
        assert account_ref_from("17841400000000000") == "17841400000000000"

    def test_surrounding_whitespace_is_stripped(self):
        """`"123 "` and `"123"` would be two rows under an exact unique index —
        two schedules against one real feed."""
        assert account_ref_from("  17841400000000000\n") == "17841400000000000"

    @pytest.mark.parametrize(
        "bad", ["", "   ", "\n\t ", None, 17841, 3.5, {"a": 1}, []]
    )
    def test_anything_without_a_value_is_refused_by_name(self, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            account_ref_from(bad)
        assert exc.value.reason == "account_ref_required"

    def test_the_length_cap_refuses_by_its_own_name(self):
        with pytest.raises(ProvisioningRefused) as exc:
            account_ref_from("1" * (ACCOUNT_REF_MAX + 1))
        assert exc.value.reason == "account_ref_too_long"

    def test_the_cap_is_inclusive(self):
        """A boundary asserted in both directions, so the cap cannot drift by
        one without a test noticing."""
        assert account_ref_from("1" * ACCOUNT_REF_MAX) == "1" * ACCOUNT_REF_MAX

    def test_a_non_numeric_reference_is_accepted(self):
        """`provider_account_ref` carries NO format CHECK (054) and the column
        comment calls it opaque. Rejecting a shape Meta has not shipped yet
        would be this module inventing a constraint the schema declined to
        make."""
        assert account_ref_from("ig_abc-123") == "ig_abc-123"


class TestFolderRefFrom:
    def test_a_bare_id_passes_through(self):
        assert folder_ref_from("1AbCdEfGhIjK") == "1AbCdEfGhIjK"

    @pytest.mark.parametrize(
        "pasted",
        [
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK?usp=sharing",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK/",
            "https://drive.google.com/drive/folders/1AbCdEfGhIjK#anchor",
            "  https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjK  ",
        ],
    )
    def test_every_shape_of_pasted_url_yields_the_bare_id(self, pasted):
        """ "Paste the folder" means the address bar to everyone who has not
        read the Drive API docs. Storing the URL would fail much later, at the
        first list call, as something that reads like a Drive fault."""
        assert folder_ref_from(pasted) == "1AbCdEfGhIjK"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            None,
            12,
            [],
            "https://drive.google.com/drive/folders/",
            "https://drive.google.com/drive/folders/?usp=sharing",
        ],
    )
    def test_anything_without_a_folder_is_refused_by_name(self, bad):
        with pytest.raises(ProvisioningRefused) as exc:
            folder_ref_from(bad)
        assert exc.value.reason == "folder_required"

    @pytest.mark.parametrize(
        "markerless",
        [
            "https://example.com/x",
            "https://drive.google.com/open?id=ABC123",
            "drive.google.com/folderview?id=ABC123",
            "https://drive.google.com/",
            "https://drive.google.com",
        ],
    )
    def test_a_url_without_the_marker_is_refused_not_salvaged(self, markerless):
        """A link with no `/folders/` marker has no id to extract.

        The delimiter cut runs regardless of whether the marker was found, so
        these used to chew down to `https:` / `drive.google.com` and be RETURNED
        as folder ids. An earlier version of this test asserted exactly that,
        which turned the defect into the specification.
        """
        with pytest.raises(ProvisioningRefused) as exc:
            folder_ref_from(markerless)
        assert exc.value.reason == "folder_not_a_drive_folder"

    def test_two_different_markerless_urls_cannot_collide_on_one_ref(self):
        """The bite was collision, not just a nonsense id.

        Every markerless URL reduced to the same handful of tokens, so two
        UNRELATED folders produced one ref, the idempotency key matched, and the
        second paste silently returned the FIRST source with ``created=False``.
        One person, two links, one source, no signal.

        Asserted as a property over the pairs rather than as two raises: what
        must never come back is a shared value, so the test names sameness.
        """
        a = "https://drive.google.com/open?id=AAA"
        b = "https://drive.google.com/open?id=BBB"

        refs = []
        for url in (a, b):
            try:
                refs.append(folder_ref_from(url))
            except ProvisioningRefused:
                pass  # refused is the correct outcome; it yields no ref to collide

        assert refs == [], (
            "distinct folder links produced storable refs "
            f"{refs!r} — if these are ever equal, two folders become one source"
        )

    def test_a_bare_id_survives_the_url_shape_guard(self):
        """The positive control on the guard.

        A denylist that refused too much would make the whole writer unusable,
        and every other passing test here would still pass — they run through
        `/folders/` URLs, which strip to the same id.
        """
        assert folder_ref_from("1AbCdEfGhIjK-_9") == "1AbCdEfGhIjK-_9"


class TestTheRefusalCarriesItsReason:
    def test_reason_is_an_attribute_not_only_a_message(self):
        """`app.py` maps on `.reason`; a refusal that only carried prose would
        fall through to the unmapped 500 handler."""
        exc = ProvisioningRefused("folder_required", "detail here")
        assert exc.reason == "folder_required"
        assert "folder_required" in str(exc)
        assert "detail here" in str(exc)
