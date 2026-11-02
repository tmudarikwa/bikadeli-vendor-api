from pathlib import Path

from django.conf import settings


def public_media_url(request, path):
    if settings.VENDOR_PUBLIC_BASE_URL:
        return f"{settings.VENDOR_PUBLIC_BASE_URL}{path}"
    return request.build_absolute_uri(path) if request else path


def validate_image_upload(upload):
    allowed = {
        "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
        "PNG": ("image/png", {".png"}),
        "WEBP": ("image/webp", {".webp"}),
        "GIF": ("image/gif", {".gif"}),
    }
    if upload.size is None or upload.size > 5 * 1024 * 1024:
        return "File too large or size unavailable (maximum 5 MiB)."
    try:
        from PIL import Image

        upload.seek(0)
        with Image.open(upload) as image:
            image.verify()
            image_format = image.format
        upload.seek(0)
    except Exception:  # noqa: BLE001 - image decoders raise format-specific exceptions
        upload.seek(0)
        return "Invalid image content."
    expected = allowed.get(image_format)
    if (
        expected is None
        or upload.content_type != expected[0]
        or Path(upload.name).suffix.lower() not in expected[1]
    ):
        return "Unsupported image type. Upload a valid JPEG, PNG, WEBP, or GIF."
    return None
