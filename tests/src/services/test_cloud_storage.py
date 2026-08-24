"""Tests for CloudStorageService."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta
import tempfile
from pathlib import Path

from src.exceptions import MediaUploadError
from tests.src.services.conftest import mock_track_execution


@pytest.mark.unit
class TestCloudStorageService:
    """Test suite for CloudStorageService."""

    @pytest.fixture
    def cloud_service(self):
        """Create CloudStorageService with mocked dependencies."""
        with patch("src.services.integrations.cloud_storage.cloudinary"):
            with patch(
                "src.services.integrations.cloud_storage.settings"
            ) as mock_settings:
                with patch("src.services.base_service.ServiceRunRepository"):
                    mock_settings.CLOUDINARY_CLOUD_NAME = "test_cloud"
                    mock_settings.CLOUDINARY_API_KEY = "test_key"
                    mock_settings.CLOUDINARY_API_SECRET = "test_secret"
                    mock_settings.CLOUD_UPLOAD_RETENTION_HOURS = 24

                    from src.services.integrations.cloud_storage import (
                        CloudStorageService,
                    )

                    service = CloudStorageService()
                    service.track_execution = mock_track_execution
                    service.set_result_summary = Mock()
                    yield service

    @pytest.fixture
    def temp_image_file(self):
        """Create a temporary image file for testing."""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".jpg", delete=False) as f:
            # Write minimal JPEG header
            f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")
            temp_path = Path(f.name)

        yield temp_path

        # Cleanup
        if temp_path.exists():
            temp_path.unlink()

    @pytest.fixture
    def temp_video_file(self):
        """Create a temporary video file for testing."""
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".mp4", delete=False) as f:
            f.write(b"\x00\x00\x00\x1c\x66\x74\x79\x70")  # MP4 header bytes
            temp_path = Path(f.name)

        yield temp_path

        # Cleanup
        if temp_path.exists():
            temp_path.unlink()

    # ==================== is_configured Tests ====================

    @patch("src.services.base_service.ServiceRunRepository")
    @patch("src.services.integrations.cloud_storage.settings")
    def test_is_configured_all_credentials_present(self, mock_settings, mock_repo):
        """Test is_configured returns True when all credentials set."""
        mock_settings.CLOUDINARY_CLOUD_NAME = "cloud"
        mock_settings.CLOUDINARY_API_KEY = "key"
        mock_settings.CLOUDINARY_API_SECRET = "secret"

        with patch("src.services.integrations.cloud_storage.cloudinary"):
            from src.services.integrations.cloud_storage import CloudStorageService

            service = CloudStorageService()
            assert service.is_configured() is True

    @patch("src.services.base_service.ServiceRunRepository")
    @patch("src.services.integrations.cloud_storage.settings")
    def test_is_configured_missing_cloud_name(self, mock_settings, mock_repo):
        """Test is_configured returns False when cloud_name missing."""
        mock_settings.CLOUDINARY_CLOUD_NAME = None
        mock_settings.CLOUDINARY_API_KEY = "key"
        mock_settings.CLOUDINARY_API_SECRET = "secret"

        with patch("src.services.integrations.cloud_storage.cloudinary"):
            from src.services.integrations.cloud_storage import CloudStorageService

            service = CloudStorageService()
            assert service.is_configured() is False

    @patch("src.services.base_service.ServiceRunRepository")
    @patch("src.services.integrations.cloud_storage.settings")
    def test_is_configured_missing_api_key(self, mock_settings, mock_repo):
        """Test is_configured returns False when api_key missing."""
        mock_settings.CLOUDINARY_CLOUD_NAME = "cloud"
        mock_settings.CLOUDINARY_API_KEY = None
        mock_settings.CLOUDINARY_API_SECRET = "secret"

        with patch("src.services.integrations.cloud_storage.cloudinary"):
            from src.services.integrations.cloud_storage import CloudStorageService

            service = CloudStorageService()
            assert service.is_configured() is False

    @patch("src.services.base_service.ServiceRunRepository")
    @patch("src.services.integrations.cloud_storage.settings")
    def test_is_configured_missing_api_secret(self, mock_settings, mock_repo):
        """Test is_configured returns False when api_secret missing."""
        mock_settings.CLOUDINARY_CLOUD_NAME = "cloud"
        mock_settings.CLOUDINARY_API_KEY = "key"
        mock_settings.CLOUDINARY_API_SECRET = None

        with patch("src.services.integrations.cloud_storage.cloudinary"):
            from src.services.integrations.cloud_storage import CloudStorageService

            service = CloudStorageService()
            assert service.is_configured() is False

    # ==================== upload_media Tests ====================

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_success(
        self, mock_cloudinary, cloud_service, temp_image_file
    ):
        """Test successful media upload."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://res.cloudinary.com/test/image/upload/test_image.jpg",
            "public_id": "storydump/test_image",
            "bytes": 12345,
            "format": "jpg",
            "width": 1080,
            "height": 1920,
        }

        result = cloud_service.upload_media(str(temp_image_file))

        assert (
            result["url"]
            == "https://res.cloudinary.com/test/image/upload/test_image.jpg"
        )
        assert result["public_id"] == "storydump/test_image"
        assert result["size_bytes"] == 12345
        assert result["format"] == "jpg"
        assert "uploaded_at" in result
        assert "expires_at" in result
        assert result["expires_at"] > result["uploaded_at"]

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_custom_folder(
        self, mock_cloudinary, cloud_service, temp_image_file
    ):
        """Test upload with custom folder."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://res.cloudinary.com/test/image/upload/custom/image.jpg",
            "public_id": "custom/image",
            "bytes": 1000,
            "format": "jpg",
        }

        cloud_service.upload_media(str(temp_image_file), folder="custom")

        # Verify folder was passed to upload
        call_kwargs = mock_cloudinary.uploader.upload.call_args[1]
        assert call_kwargs["folder"] == "custom"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_custom_public_id(
        self, mock_cloudinary, cloud_service, temp_image_file
    ):
        """Test upload with custom public_id."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://example.com/image.jpg",
            "public_id": "my_custom_id",
            "bytes": 1000,
            "format": "jpg",
        }

        cloud_service.upload_media(str(temp_image_file), public_id="my_custom_id")

        call_kwargs = mock_cloudinary.uploader.upload.call_args[1]
        assert call_kwargs["public_id"] == "my_custom_id"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_video_resource_type(
        self, mock_cloudinary, cloud_service, temp_video_file
    ):
        """Test that video files use video resource type."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://example.com/video.mp4",
            "public_id": "test_video",
            "bytes": 50000,
            "format": "mp4",
        }

        cloud_service.upload_media(str(temp_video_file))

        call_kwargs = mock_cloudinary.uploader.upload.call_args[1]
        assert call_kwargs["resource_type"] == "video"

    def test_upload_media_file_not_found(self, cloud_service):
        """Test upload raises error for non-existent file."""
        with pytest.raises(MediaUploadError, match="File not found"):
            cloud_service.upload_media("/path/to/nonexistent/file.jpg")

    def test_upload_media_path_is_directory(self, cloud_service, tmp_path):
        """Test upload raises error when path is a directory."""
        with pytest.raises(MediaUploadError, match="not a file"):
            cloud_service.upload_media(str(tmp_path))

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_cloudinary_error(
        self, mock_cloudinary, cloud_service, temp_image_file
    ):
        """Test upload handles Cloudinary errors."""
        # Create a mock exception class
        mock_cloudinary.exceptions = MagicMock()
        mock_cloudinary.exceptions.Error = Exception
        mock_cloudinary.uploader.upload.side_effect = Exception("Upload failed")

        with pytest.raises(MediaUploadError, match="Cloudinary upload failed"):
            cloud_service.upload_media(str(temp_image_file))

    # ==================== upload_media with file_bytes Tests ====================

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_with_bytes_success(self, mock_cloudinary, cloud_service):
        """Test successful upload using file_bytes instead of file_path."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://res.cloudinary.com/test/image/upload/bytes_image.jpg",
            "public_id": "storydump/bytes_image",
            "bytes": 5000,
            "format": "jpg",
            "width": 1080,
            "height": 1920,
        }

        result = cloud_service.upload_media(
            file_bytes=b"fake jpeg content",
            filename="test.jpg",
        )

        assert (
            result["url"]
            == "https://res.cloudinary.com/test/image/upload/bytes_image.jpg"
        )
        assert result["public_id"] == "storydump/bytes_image"
        assert result["size_bytes"] == 5000
        assert "uploaded_at" in result
        assert "expires_at" in result

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_with_bytes_video_resource_type(
        self, mock_cloudinary, cloud_service
    ):
        """Test that video filename triggers video resource type for bytes upload."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://example.com/video.mp4",
            "public_id": "test_video",
            "bytes": 50000,
            "format": "mp4",
        }

        cloud_service.upload_media(file_bytes=b"fake video", filename="clip.mp4")

        call_kwargs = mock_cloudinary.uploader.upload.call_args[1]
        assert call_kwargs["resource_type"] == "video"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_with_bytes_custom_folder(
        self, mock_cloudinary, cloud_service
    ):
        """Test bytes upload with custom folder."""
        mock_cloudinary.uploader.upload.return_value = {
            "secure_url": "https://example.com/image.jpg",
            "public_id": "custom/image",
            "bytes": 1000,
            "format": "jpg",
        }

        cloud_service.upload_media(
            file_bytes=b"content", filename="img.jpg", folder="custom"
        )

        call_kwargs = mock_cloudinary.uploader.upload.call_args[1]
        assert call_kwargs["folder"] == "custom"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_upload_media_with_bytes_error_handling(
        self, mock_cloudinary, cloud_service
    ):
        """Test bytes upload handles Cloudinary errors."""
        mock_cloudinary.exceptions = MagicMock()
        mock_cloudinary.exceptions.Error = Exception
        mock_cloudinary.uploader.upload.side_effect = Exception("Upload failed")

        with pytest.raises(MediaUploadError, match="Cloudinary upload failed"):
            cloud_service.upload_media(file_bytes=b"content", filename="img.jpg")

    def test_upload_media_both_path_and_bytes_raises(self, cloud_service):
        """Test that providing both file_path and file_bytes raises ValueError."""
        with pytest.raises(ValueError, match="not both"):
            cloud_service.upload_media(
                file_path="/some/path.jpg",
                file_bytes=b"content",
                filename="img.jpg",
            )

    def test_upload_media_neither_path_nor_bytes_raises(self, cloud_service):
        """Test that providing neither file_path nor file_bytes raises ValueError."""
        with pytest.raises(ValueError, match="either file_path or file_bytes"):
            cloud_service.upload_media()

    def test_upload_media_bytes_without_filename_raises(self, cloud_service):
        """Test that file_bytes without filename raises ValueError."""
        with pytest.raises(ValueError, match="filename is required"):
            cloud_service.upload_media(file_bytes=b"content")

    # ==================== delete_media Tests ====================

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_delete_media_success(self, mock_cloudinary, cloud_service):
        """Test successful media deletion."""
        mock_cloudinary.uploader.destroy.return_value = {"result": "ok"}

        result = cloud_service.delete_media(
            "storydump/test_image", resource_type="image"
        )

        assert result is True
        mock_cloudinary.uploader.destroy.assert_called_once_with(
            "storydump/test_image", resource_type="image"
        )

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_delete_media_not_found(self, mock_cloudinary, cloud_service):
        """Test delete returns False when image not found."""
        mock_cloudinary.uploader.destroy.return_value = {"result": "not found"}

        result = cloud_service.delete_media("nonexistent", resource_type="image")

        assert result is False

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_delete_media_error(self, mock_cloudinary, cloud_service):
        """Test delete handles errors gracefully."""
        mock_cloudinary.exceptions = MagicMock()
        mock_cloudinary.exceptions.Error = Exception
        mock_cloudinary.uploader.destroy.side_effect = Exception("Delete failed")

        result = cloud_service.delete_media("test_id", resource_type="video")

        assert result is False

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_delete_video_destroy_carries_video_resource_type(
        self, mock_cloudinary, cloud_service
    ):
        """THE #1019 shape assert: a video's destroy must say resource_type=
        "video". The SDK defaults to image, so a destroy without it matches
        nothing, returns success, and the video lives forever. A test that
        only asserts destroy-was-called passes WITH the bug present — this one
        asserts the call shape and goes red if the fix is reverted."""
        mock_cloudinary.uploader.destroy.return_value = {"result": "ok"}

        result = cloud_service.delete_media("fake-video-abc123", resource_type="video")

        assert result is True
        mock_cloudinary.uploader.destroy.assert_called_once_with(
            "fake-video-abc123", resource_type="video"
        )

    def test_delete_media_requires_resource_type(self, cloud_service):
        """The parameter is deliberately required: an image default would
        silently re-arm the bug for the next forgetful caller."""
        with pytest.raises(TypeError):
            cloud_service.delete_media("fake-id-no-type")

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_delete_media_for_item_derives_from_file_name_not_file_path(
        self, mock_cloudinary, cloud_service
    ):
        """The Drive-shape regression pin (#1019 review finding 1):
        provider-sourced media carry a synthetic extensionless file_path
        (provider://id) while file_name holds the real extension. Deriving
        from file_path silently classifies every Drive video as image and
        re-creates the exact bug — this goes red if the derivation ever
        moves back to file_path."""
        from unittest.mock import Mock as _Mock

        mock_cloudinary.uploader.destroy.return_value = {"result": "ok"}
        drive_video = _Mock(
            cloud_public_id="fake-drive-vid-1",
            file_path="google_drive://fake-identifier",
            file_name="clip.mp4",
        )

        assert cloud_service.delete_media_for_item(drive_video) is True
        mock_cloudinary.uploader.destroy.assert_called_once_with(
            "fake-drive-vid-1", resource_type="video"
        )

    # ==================== get_url Tests ====================

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_get_url_success(self, mock_cloudinary, cloud_service):
        """Test getting URL for existing resource."""
        mock_cloudinary.api.resource.return_value = {
            "secure_url": "https://res.cloudinary.com/test/image.jpg"
        }

        url = cloud_service.get_url("test_image")

        assert url == "https://res.cloudinary.com/test/image.jpg"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_get_url_not_found(self, mock_cloudinary, cloud_service):
        """Test get_url returns None when resource not found."""
        mock_cloudinary.exceptions = MagicMock()
        mock_cloudinary.exceptions.NotFound = type("NotFound", (Exception,), {})
        mock_cloudinary.api.resource.side_effect = mock_cloudinary.exceptions.NotFound()

        url = cloud_service.get_url("nonexistent")

        assert url is None

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_get_url_error(self, mock_cloudinary, cloud_service):
        """Test get_url handles errors gracefully."""
        mock_cloudinary.exceptions = MagicMock()
        mock_cloudinary.exceptions.NotFound = type("NotFound", (Exception,), {})
        mock_cloudinary.exceptions.Error = Exception
        mock_cloudinary.api.resource.side_effect = Exception("API Error")

        url = cloud_service.get_url("test_id")

        assert url is None

    # ==================== cleanup_expired Tests ====================

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_cleanup_expired_deletes_old_resources(
        self, mock_cloudinary, cloud_service
    ):
        """Test cleanup deletes resources older than retention period."""
        # Old resource (48 hours ago)
        old_date = (datetime.utcnow() - timedelta(hours=48)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # New resource (1 hour ago)
        new_date = (datetime.utcnow() - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        def resources_side_effect(**kwargs):
            if kwargs.get("resource_type") == "image":
                return {
                    "resources": [
                        {"public_id": "storydump/old_image", "created_at": old_date},
                        {"public_id": "storydump/new_image", "created_at": new_date},
                    ]
                }
            return {"resources": []}

        mock_cloudinary.api.resources.side_effect = resources_side_effect
        mock_cloudinary.uploader.destroy.return_value = {"result": "ok"}

        deleted_count = cloud_service.cleanup_expired()

        # Only the old resource should be deleted
        assert deleted_count == 1
        mock_cloudinary.uploader.destroy.assert_called_once_with(
            "storydump/old_image", resource_type="image"
        )

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_cleanup_expired_no_old_resources(self, mock_cloudinary, cloud_service):
        """Test cleanup returns 0 when no old resources."""
        recent_date = (datetime.utcnow() - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        mock_cloudinary.api.resources.return_value = {
            "resources": [
                {"public_id": "storydump/recent", "created_at": recent_date},
            ]
        }

        deleted_count = cloud_service.cleanup_expired()

        assert deleted_count == 0
        mock_cloudinary.uploader.destroy.assert_not_called()

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_cleanup_expired_handles_api_error(self, mock_cloudinary, cloud_service):
        """Test cleanup handles API errors gracefully."""
        mock_cloudinary.exceptions = MagicMock()
        mock_cloudinary.exceptions.Error = Exception
        mock_cloudinary.api.resources.side_effect = Exception("API Error")

        deleted_count = cloud_service.cleanup_expired()

        assert deleted_count == 0

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_cleanup_expired_custom_folder(self, mock_cloudinary, cloud_service):
        """Test cleanup uses custom folder parameter."""
        mock_cloudinary.api.resources.return_value = {"resources": []}

        cloud_service.cleanup_expired(folder="custom_folder")

        call_kwargs = mock_cloudinary.api.resources.call_args[1]
        assert call_kwargs["prefix"] == "custom_folder"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_cleanup_expired_paginates_past_first_page(
        self, mock_cloudinary, cloud_service
    ):
        """Cleanup follows next_cursor so expired uploads beyond the 500-item
        page cap are not silently skipped (issue #499)."""
        old_date = (datetime.utcnow() - timedelta(hours=48)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Page 1 returns a next_cursor; page 2 (the final page) does not.
        page_one = {
            "resources": [
                {"public_id": "storydump/old_page1", "created_at": old_date},
            ],
            "next_cursor": "cursor_page_2",
        }
        page_two = {
            "resources": [
                {"public_id": "storydump/old_page2", "created_at": old_date},
            ],
        }
        empty_page = {"resources": []}

        def resources_side_effect(**kwargs):
            if kwargs.get("resource_type") != "image":
                return empty_page
            return page_two if kwargs.get("next_cursor") else page_one

        mock_cloudinary.api.resources.side_effect = resources_side_effect
        mock_cloudinary.uploader.destroy.return_value = {"result": "ok"}

        deleted_count = cloud_service.cleanup_expired()

        # Both pages' expired uploads are deleted, not just the first page.
        assert deleted_count == 2
        # 2 image pages + 1 empty video page
        assert mock_cloudinary.api.resources.call_count == 3
        # The second request must forward the cursor returned by the first.
        second_call_kwargs = mock_cloudinary.api.resources.call_args_list[1][1]
        assert second_call_kwargs["next_cursor"] == "cursor_page_2"
        destroyed = {
            call.args[0] for call in mock_cloudinary.uploader.destroy.call_args_list
        }
        assert destroyed == {"storydump/old_page1", "storydump/old_page2"}

    # ==================== resource_type_for Tests ====================

    def test_get_resource_type_image_extensions(self, cloud_service):
        """Test resource type detection for image files."""
        assert cloud_service.resource_type_for(Path("test.jpg")) == "image"
        assert cloud_service.resource_type_for(Path("test.jpeg")) == "image"
        assert cloud_service.resource_type_for(Path("test.png")) == "image"
        assert cloud_service.resource_type_for(Path("test.gif")) == "image"

    def test_get_resource_type_video_extensions(self, cloud_service):
        """Test resource type detection for video files."""
        assert cloud_service.resource_type_for(Path("test.mp4")) == "video"
        assert cloud_service.resource_type_for(Path("test.mov")) == "video"
        assert cloud_service.resource_type_for(Path("test.avi")) == "video"
        assert cloud_service.resource_type_for(Path("test.webm")) == "video"

    def test_get_resource_type_case_insensitive(self, cloud_service):
        """Test resource type detection is case insensitive."""
        assert cloud_service.resource_type_for(Path("test.MP4")) == "video"
        assert cloud_service.resource_type_for(Path("test.JPG")) == "image"

    @patch("src.services.integrations.cloud_storage.cloudinary")
    def test_cleanup_expired_sweeps_each_resource_type(
        self, mock_cloudinary, cloud_service
    ):
        """The #1019 sweep half: api.resources defaults to image, so a sweep
        that never says resource_type cannot even LIST videos. The sweep must
        run once per type the app uploads (image and video, the range of
        resource_type_for) and destroy each expired item with its own type."""
        old = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def resources_side_effect(**kwargs):
            rt = kwargs.get("resource_type")
            assert rt in ("image", "video"), (
                f"api.resources called without an explicit resource_type "
                f"(got {rt!r}) — the SDK default is image and videos are "
                f"never listed"
            )
            if rt == "image":
                return {
                    "resources": [
                        {"public_id": "fake-img-1", "created_at": old},
                    ]
                }
            return {
                "resources": [
                    {"public_id": "fake-vid-1", "created_at": old},
                ]
            }

        mock_cloudinary.api.resources.side_effect = resources_side_effect
        mock_cloudinary.uploader.destroy.return_value = {"result": "ok"}

        deleted = cloud_service.cleanup_expired(folder="storydump")

        assert deleted == 2
        listed_types = {
            c.kwargs.get("resource_type")
            for c in mock_cloudinary.api.resources.call_args_list
        }
        assert listed_types == {"image", "video"}
        destroy_calls = {
            (c.args[0], c.kwargs.get("resource_type"))
            for c in mock_cloudinary.uploader.destroy.call_args_list
        }
        assert destroy_calls == {
            ("fake-img-1", "image"),
            ("fake-vid-1", "video"),
        }
