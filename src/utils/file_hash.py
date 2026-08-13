"""File hashing utilities (MD5 of content).

Uses MD5 to match Google Drive's md5Checksum field, ensuring consistent
hash comparisons across local and cloud media sources.

Every ``media_items.file_hash`` value must come from this module. The hash is
only useful because rows from different sources are comparable, so a caller
that picks its own algorithm produces a value that cannot match anything —
silently, since a non-matching hash is indistinguishable from novel content.
"""

import hashlib
from pathlib import Path

#: The one algorithm decision. Both entry points below build from this, so
#: changing it is a single edit rather than a search for literal call sites.
_HASH = hashlib.md5


def calculate_bytes_hash(data: bytes) -> str:
    """
    Calculate MD5 hash of an in-memory payload.

    For callers that already hold the content (an upload body, a downloaded
    response). Equivalent to :func:`calculate_file_hash` on the same bytes.

    Args:
        data: File content

    Returns:
        Hex string of MD5 hash (32 characters)
    """
    return _HASH(data).hexdigest()


def calculate_file_hash(file_path: Path) -> str:
    """
    Calculate MD5 hash of file content.

    Uses MD5 to match Google Drive's native md5Checksum, enabling
    cross-source deduplication and hash-aware selection.

    Note: Hash is based ONLY on file content, not filename.
    This means:
    - Same image with different names = same hash
    - Different images with same name = different hash

    Args:
        file_path: Path to file

    Returns:
        Hex string of MD5 hash (32 characters)

    Example:
        >>> calculate_file_hash(Path("/path/to/image.jpg"))
        'abc123def456...'
    """
    md5_hash = _HASH()

    with open(file_path, "rb") as f:
        # Read in chunks for memory efficiency
        for byte_block in iter(lambda: f.read(4096), b""):
            md5_hash.update(byte_block)

    return md5_hash.hexdigest()
