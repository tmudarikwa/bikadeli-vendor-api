import socket
import struct
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError

MAX_KYC_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_KYC_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.webp'}
IMAGE_FORMATS = {
    'JPEG': {'.jpg', '.jpeg'},
    'PNG': {'.png'},
    'WEBP': {'.webp'},
}


def _scan_with_clamav(data):
    with socket.create_connection(
        (settings.CLAMAV_HOST, settings.CLAMAV_PORT),
        timeout=settings.CLAMAV_TIMEOUT_SECONDS,
    ) as scanner:
        scanner.settimeout(settings.CLAMAV_TIMEOUT_SECONDS)
        scanner.sendall(b'zINSTREAM\x00')
        for offset in range(0, len(data), 1024 * 1024):
            chunk = data[offset : offset + 1024 * 1024]
            scanner.sendall(struct.pack('!I', len(chunk)))
            scanner.sendall(chunk)
        scanner.sendall(struct.pack('!I', 0))
        chunks = []
        while True:
            chunk = scanner.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b'\x00' in chunk:
                break
        result = b''.join(chunks)
    if result.endswith(b'OK\x00'):
        return
    if b'FOUND' in result:
        raise ValidationError('The uploaded document failed the malware scan.')
    raise ValidationError('The malware scanner returned an invalid response.')


def validate_kyc_upload(upload):
    extension = Path(upload.name).suffix.lower()
    if extension not in ALLOWED_KYC_EXTENSIONS:
        raise ValidationError('Accepted formats are PDF, JPG, JPEG, PNG, and WEBP.')
    if not upload.size or upload.size > MAX_KYC_FILE_SIZE:
        raise ValidationError('Choose a non-empty file up to 10 MB.')

    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    if extension == '.pdf':
        if not data.startswith(b'%PDF-'):
            raise ValidationError('The uploaded file is not a valid PDF.')
    else:
        try:
            from PIL import Image

            with Image.open(upload) as image:
                image.verify()
                image_format = image.format
        except Exception as exc:
            raise ValidationError('The uploaded file is not a valid image.') from exc
        finally:
            upload.seek(0)
        if extension not in IMAGE_FORMATS.get(image_format, set()):
            raise ValidationError('The file content does not match its extension.')

    if settings.CLAMAV_ENABLED:
        try:
            _scan_with_clamav(data)
        except (OSError, TimeoutError) as exc:
            raise ValidationError('The malware scanner is unavailable; the document was not accepted.') from exc
    elif not settings.DEBUG:
        raise ValidationError('The malware scanner is required; the document was not accepted.')
    return upload
