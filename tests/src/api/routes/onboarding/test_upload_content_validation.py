"""Upload content validation — the guard must not fail open (#761).

`_validate_upload_content` compares the client's declared MIME against the MIME
detected from the file's magic bytes. The declared value is attacker-controlled
(a header, or the filename extension), so the detected value is the only real
check. When detection did not recognise the content, the guard used to skip the
comparison entirely — an unrecognised format was therefore *more* trusted than a
recognised one.

That is the shape #758 had one layer down: absence of a signal read as absence
of a problem. These tests pin the inversion.
"""

import io

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from src.api.routes.onboarding.dashboard import _validate_upload_content

# Real magic bytes. JPEG/PNG/GIF are in the recognised set; the rest are formats
# a client can send that the table does not name -- which is the whole point.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF87A = b"GIF87a" + b"\x00" * 64
GIF89A = b"GIF89a" + b"\x00" * 64
# ISO base media: 4-byte size, "ftyp", then the 4-byte major brand. The brand is
# what separates QuickTime from MP4 -- both carry an ftyp box.
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64
EPS = b"%!PS-Adobe-3.0 EPSF-3.0\n" + b"\x00" * 64
JP2 = b"\x00\x00\x00\x0cjP  \r\n\x87\n" + b"\x00" * 64
TGA = b"\x00\x00\x02\x00" + b"\x00" * 64
BMP = b"BM" + b"\x00" * 64
TIFF = b"II*\x00" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


class _Request:
    """Minimal stand-in carrying only what the function reads: Content-Length.

    Not a mock of the code under test -- the real `Request` needs an ASGI scope
    to construct, and the function touches exactly one attribute of it.
    """

    def __init__(self, content_length: int):
        self.headers = {"content-length": str(content_length)}


def _upload(content: bytes, claimed_mime: str, filename: str = "photo.jpg"):
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": claimed_mime}),
    )


async def _validate(content: bytes, claimed_mime: str, filename: str = "photo.jpg"):
    return await _validate_upload_content(
        _Request(len(content)), _upload(content, claimed_mime, filename)
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestUnrecognisedContentIsRejected:
    """The fail-open case: content the table does not recognise must not pass."""

    @pytest.mark.parametrize(
        "name,content",
        [
            ("EPS", EPS),
            ("JPEG2000", JP2),
            ("TGA", TGA),
            ("BMP", BMP),
            ("TIFF", TIFF),
            ("WebP", WEBP),
        ],
    )
    async def test_unrecognised_content_claiming_jpeg_is_rejected(self, name, content):
        """Each of these reaches a distinct Pillow decoder if it lands on disk.

        `Image.open()` dispatches on content, and the indexing path gates on the
        file *extension*, so `photo.jpg` carrying EPS bytes reaches the EPS
        decoder regardless of what was declared.
        """
        with pytest.raises(HTTPException) as exc:
            await _validate(content, "image/jpeg")
        assert exc.value.status_code == 400

    async def test_the_rejection_names_the_reason(self):
        """A caller must be able to tell this apart from a type mismatch."""
        with pytest.raises(HTTPException) as exc:
            await _validate(EPS, "image/jpeg")
        assert "not recognized" in str(exc.value.detail).lower()


@pytest.mark.unit
@pytest.mark.asyncio
class TestAllowedFormatsStillUpload:
    """Rejecting the unrecognised is only safe if the recognised set is complete.

    Every entry in ALLOWED_MIME_TYPES must be *detectable*, or the fix for #761
    trades a security hole for broken uploads. `video/quicktime` was the gap: it
    is allowlisted, but every `ftyp` box was mapped to `video/mp4` without
    reading the brand, so a genuine `.mov` was rejected as a mismatch.
    """

    @pytest.mark.parametrize(
        "name,content,claimed",
        [
            ("JPEG", JPEG, "image/jpeg"),
            ("PNG", PNG, "image/png"),
            ("GIF87a", GIF87A, "image/gif"),
            ("GIF89a", GIF89A, "image/gif"),
            ("MP4", MP4, "video/mp4"),
            ("QuickTime", MOV, "video/quicktime"),
        ],
    )
    async def test_every_allowed_type_is_accepted(self, name, content, claimed):
        returned, mime = await _validate(content, claimed)
        assert returned == content
        assert mime == claimed

    async def test_the_allowlist_and_the_detector_agree(self):
        """No entry may be allowlisted that detection cannot produce.

        Guards the failure mode directly: allowlisting a type the detector
        cannot emit means every genuine upload of it is rejected as a mismatch.
        """
        from src.api.routes.onboarding.dashboard import (
            ALLOWED_MIME_TYPES,
            _detectable_mime_types,
        )

        assert ALLOWED_MIME_TYPES <= _detectable_mime_types()


@pytest.mark.unit
@pytest.mark.asyncio
class TestRecognisedMismatchStillRejected:
    """Control: the case that already worked must keep working."""

    async def test_png_content_declared_as_jpeg_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await _validate(PNG, "image/jpeg")
        assert exc.value.status_code == 400
        assert "does not match" in str(exc.value.detail).lower()

    async def test_a_type_outside_the_allowlist_is_rejected_before_detection(self):
        with pytest.raises(HTTPException) as exc:
            await _validate(JPEG, "application/pdf")
        assert exc.value.status_code == 400
        assert "unsupported file type" in str(exc.value.detail).lower()
