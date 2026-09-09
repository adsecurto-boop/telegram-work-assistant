"""Download owner-supplied Telegram evidence into private local storage."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path


def safe_name(value: str | None, default='evidence.bin') -> str:
    name = Path(value or default).name
    name = re.sub(r'[^A-Za-z0-9._ -]+', '_', name).strip(' .') or default
    return name[:120]


async def voice_bytes(message):
    telegram_file = await message.voice.get_file()
    data = await telegram_file.download_as_bytearray()
    return bytes(data), message.voice.mime_type or 'audio/ogg', message.voice.file_id


def save_voice_data(data, base_dir, mime_type='audio/ogg'):
    folder = Path(base_dir) / datetime.now().strftime('%Y') / datetime.now().strftime('%m')
    folder.mkdir(parents=True, exist_ok=True)
    extension = mimetypes.guess_extension(mime_type) or '.ogg'
    path = folder / f'{uuid.uuid4().hex[:12]}-voice{extension}'
    path.write_bytes(data)
    return str(path.resolve()), hashlib.sha256(data).hexdigest()


async def save_evidence(message, base_dir):
    if message.photo:
        media = message.photo[-1]
        kind, original_name, mime = 'photo', 'photo.jpg', 'image/jpeg'
    elif message.document:
        media = message.document
        original_name = message.document.file_name or 'document.bin'
        mime = message.document.mime_type or mimetypes.guess_type(original_name)[0] or 'application/octet-stream'
        kind = 'document'
    elif message.video:
        media = message.video
        original_name = message.video.file_name or 'video.mp4'
        mime = message.video.mime_type or 'video/mp4'
        kind = 'video'
    else:
        raise ValueError('Supported evidence: photo, document, or video.')
    folder = Path(base_dir) / datetime.now().strftime('%Y') / datetime.now().strftime('%m')
    folder.mkdir(parents=True, exist_ok=True)
    name = f'{uuid.uuid4().hex[:12]}-{safe_name(original_name)}'
    path = folder / name
    telegram_file = await media.get_file()
    await telegram_file.download_to_drive(custom_path=path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        'kind': kind, 'path': str(path.resolve()), 'telegram_file_id': media.file_id,
        'caption': message.caption or '', 'sha256': digest, 'mime_type': mime,
    }
