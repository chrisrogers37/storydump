"""The one Instagram media_type predicate (#1024).

Derives from ``file_name`` — the display filename, which carries a real
extension for every source — deliberately never ``file_path``, which is a
synthetic extensionless ``provider://id`` for provider-sourced media (Drive),
so a ``file_path`` predicate is always false there and every video posts as
IMAGE. Same two-column confusion the removal path fixed in #1025; this module
exists so the expression cannot be duplicated back into drift.

The suffix set is deliberately the historical {mp4, mov} — narrower than the
Cloudinary classifier's (``resource_type_for``). Converging the two changes
what gets POSTED as video for .avi/.webm/.mkv and is tracked as its own
follow-up, not smuggled into a column fix.
"""

INSTAGRAM_VIDEO_SUFFIXES = (".mp4", ".mov")


def instagram_media_type(media_item) -> str:
    """``"VIDEO"`` or ``"IMAGE"`` for the IG Graph call, from the filename."""
    name = media_item.file_name or ""
    return "VIDEO" if name.lower().endswith(INSTAGRAM_VIDEO_SUFFIXES) else "IMAGE"
