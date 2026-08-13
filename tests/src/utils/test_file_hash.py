"""Tests for file_hash utility."""

import hashlib
import re
import tempfile
from pathlib import Path

import pytest

from src.utils import file_hash as file_hash_module
from src.utils.file_hash import calculate_bytes_hash, calculate_file_hash

#: Modules that produce a value stored as ``media_items.file_hash``. A new one
#: belongs here; see TestHashAlgorithmIsSingleSourced for why the list exists.
_FILE_HASH_WRITERS = (
    "services/integrations/backfill_downloader.py",
    "services/media_sources/google_drive_provider.py",
    "api/routes/onboarding/dashboard.py",
)

_DIRECT_HASH_CALL = re.compile(r"hashlib\.(md5|sha1|sha256|sha384|sha512)\s*\(")

#: `src/`, located from the module under test rather than from this file's own
#: depth, so moving the test does not silently make the scan vacuous.
_SRC_ROOT = Path(file_hash_module.__file__).parent.parent


@pytest.mark.unit
class TestFileHash:
    """Test file hash calculation."""

    def test_calculate_file_hash(self):
        """Test hash calculation for a file."""
        # Create temporary file
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("test content")
            temp_path = Path(f.name)

        try:
            # Calculate hash
            file_hash = calculate_file_hash(temp_path)

            # Verify it's a valid MD5 hex string
            assert len(file_hash) == 32
            assert all(c in "0123456789abcdef" for c in file_hash)

        finally:
            temp_path.unlink()

    def test_same_content_same_hash(self):
        """Test that same content produces same hash."""
        content = "identical content"

        # Create two files with same content
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write(content)
            path1 = Path(f1.name)

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write(content)
            path2 = Path(f2.name)

        try:
            hash1 = calculate_file_hash(path1)
            hash2 = calculate_file_hash(path2)

            assert hash1 == hash2

        finally:
            path1.unlink()
            path2.unlink()

    def test_different_content_different_hash(self):
        """Test that different content produces different hash."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write("content 1")
            path1 = Path(f1.name)

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
            f2.write("content 2")
            path2 = Path(f2.name)

        try:
            hash1 = calculate_file_hash(path1)
            hash2 = calculate_file_hash(path2)

            assert hash1 != hash2

        finally:
            path1.unlink()
            path2.unlink()


@pytest.mark.unit
class TestHashAlgorithmIsSingleSourced:
    """#619: a `file_hash` is only useful because rows from different sources
    are comparable, so the algorithm is a property of the column, not of the
    caller. Backfill, the Mini App upload path and the Drive provider's
    no-md5Checksum fallback each picked SHA256 independently — every one of
    them produced a value that could never match a Drive `md5Checksum`, and a
    non-matching hash is indistinguishable from novel content, so the misses
    were silent."""

    def test_both_entry_points_agree_and_are_md5(self, tmp_path):
        """The streaming and in-memory doors must return the same digest, and
        it must be MD5 — that is what Drive's md5Checksum field holds.

        Expectation anchored to `hashlib.md5` directly rather than to either
        function under test: `calculate_bytes_hash(x) == calculate_bytes_hash(x)`
        would still pass with the algorithm swapped.
        """
        content = b"content reachable from more than one source"
        path = tmp_path / "media.jpg"
        path.write_bytes(content)

        expected = hashlib.md5(content).hexdigest()

        assert calculate_bytes_hash(content) == expected
        assert calculate_file_hash(path) == expected
        assert len(expected) == 32

    def test_writers_do_not_choose_their_own_algorithm(self):
        """Every module writing `media_items.file_hash` must delegate here.

        This is the regression guard proper: the defect was not one wrong
        constant, it was three call sites each free to pick. A new writer that
        reaches for `hashlib` directly re-opens it, and the failure is silent
        at runtime — so it is caught here instead.
        """
        offenders = [
            rel
            for rel in _FILE_HASH_WRITERS
            if _DIRECT_HASH_CALL.search((_SRC_ROOT / rel).read_text())
        ]

        assert offenders == [], (
            f"{offenders} compute a media hash directly. Use "
            "src.utils.file_hash so the value stays comparable across sources."
        )

    def test_the_writer_scan_can_actually_fail(self):
        """Positive control: a probe that has never fired is not evidence.

        Pins that the pattern matches the exact shape this PR removed, and that
        every path in _FILE_HASH_WRITERS resolves — a typo'd path would read as
        a clean scan forever.
        """
        assert _DIRECT_HASH_CALL.search("    x = hashlib.sha256(data).hexdigest()")

        missing = [rel for rel in _FILE_HASH_WRITERS if not (_SRC_ROOT / rel).is_file()]
        assert missing == [], f"stale writer paths, scan is vacuous for: {missing}"
