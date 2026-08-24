"""The #1024 predicate: media_type from file_name, never file_path."""

import pytest
from unittest.mock import Mock

from src.utils.media_kind import instagram_media_type


@pytest.mark.unit
class TestInstagramMediaType:
    def test_provider_shaped_video_is_video(self):
        """THE #1024 pin: provider-sourced media carry a synthetic
        extensionless file_path (provider://id) — the extension lives only on
        file_name. A file_path predicate is always false for these, which is
        how 140 of 140 Drive videos posted as IMAGE. This fixture is
        deliberately provider-shaped; a local-style path passes with the bug
        present."""
        drive_video = Mock(
            file_path="google_drive://fake-identifier", file_name="clip.mp4"
        )
        assert instagram_media_type(drive_video) == "VIDEO"

    def test_local_shaped_video_is_video(self):
        local_video = Mock(file_path="/media/clip.mov", file_name="clip.mov")
        assert instagram_media_type(local_video) == "VIDEO"

    def test_case_insensitive(self):
        assert instagram_media_type(Mock(file_name="CLIP.MOV")) == "VIDEO"

    def test_image_is_image(self):
        drive_image = Mock(
            file_path="google_drive://fake-identifier", file_name="pic.jpg"
        )
        assert instagram_media_type(drive_image) == "IMAGE"

    def test_missing_file_name_falls_to_image(self):
        assert instagram_media_type(Mock(file_name=None)) == "IMAGE"
