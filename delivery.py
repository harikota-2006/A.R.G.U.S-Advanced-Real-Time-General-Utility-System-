"""Persist workstation artifacts and issue narrowly scoped, expiring download links."""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import io
import re
import secrets
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlencode
from config import DATA_DIR, env
from database import db

PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
ARTIFACT_FORMATS = {'application/pdf': ('.pdf', None), PPTX_MIME: ('.pptx', 'ppt/presentation.xml'), DOCX_MIME: ('.docx', 'word/document.xml')}
MAX_ARTIFACT_BYTES = 12 * 1024 * 1024

def _signature(identifier, expires):
    secret = env('ARGUS_SESSION_SECRET')
    if len(secret) < 32:
        raise ValueError('A configured session secret is required for shared downloads')
    return hmac.new(secret.encode(), f'export:{identifier}:{expires}'.encode(), hashlib.sha256).hexdigest()

def valid_share(identifier: str, expires: str, signature: str) -> bool:
    try:
        deadline = int(expires)
        return time.time() < deadline <= time.time() + 86400 * 7 + 60 and hmac.compare_digest(_signature(identifier, deadline), signature)
    except (ValueError, TypeError):
        return False

def export_file(identifier: str):
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise FileNotFoundError('Unknown export')
    metadata = db.get_setting('export:' + identifier)
    if not metadata:
        raise FileNotFoundError('Unknown export')
    suffix, _ = ARTIFACT_FORMATS.get(metadata.get('media_type'), ('', ''))
    if not suffix:
        raise FileNotFoundError('Unknown export format')
    path = DATA_DIR / 'exports' / (identifier + suffix)
    if not path.is_file():
        raise FileNotFoundError('Export file is missing')
    return path, metadata

def _store(result: dict) -> dict:
    artifact = result.get('artifact')
    if not isinstance(artifact, dict):
        raise ValueError('The workstation did not return a downloadable artifact')
    encoded = artifact.get('data_base64', '')
    if not isinstance(encoded, str) or len(encoded) > (MAX_ARTIFACT_BYTES + 2) // 3 * 4:
        raise ValueError('Artifact exceeds the transfer limit')
    raw = base64.b64decode(encoded, validate=True)
    if not raw or len(raw) > MAX_ARTIFACT_BYTES or len(raw) != artifact.get('size'):
        raise ValueError('Invalid artifact size')
    digest = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(digest, str(artifact.get('sha256', ''))):
        raise ValueError('Artifact checksum does not match')
    media_type = artifact.get('media_type')
    if media_type not in ARTIFACT_FORMATS:
        raise ValueError('Unsupported artifact type')
    suffix, document_part = ARTIFACT_FORMATS[media_type]
    if media_type == 'application/pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or not len(reader.pages):
            raise ValueError('Export must be a readable PDF with at least one page')
    else:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            if not {'[Content_Types].xml', document_part} <= names:
                raise ValueError('Not the declared Office document format')
            if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
                raise ValueError('Expanded presentation exceeds the limit')
            if archive.testzip() is not None:
                raise ValueError('Corrupt PowerPoint package')
    public = env('ARGUS_PUBLIC_URL').rstrip('/')
    if public:
        parsed = urlsplit(public)
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('ARGUS_PUBLIC_URL must be an HTTP(S) origin/base URL without credentials or query')
        if len(env('ARGUS_SESSION_SECRET')) < 32:
            raise ValueError('A configured session secret is required for shared downloads')
    filename = re.sub(r'[^\w .()-]', '_', str(artifact.get('filename', 'document' + suffix)))[:140]
    if not filename.lower().endswith(suffix):
        filename += suffix
    identifier = secrets.token_hex(16)
    directory = DATA_DIR / 'exports'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / (identifier + '.tmp')
    temporary.write_bytes(raw)
    temporary.replace(directory / (identifier + suffix))
    path = '/api/exports/' + identifier
    metadata = {'id': identifier, 'filename': filename, 'media_type': media_type, 'size': len(raw), 'sha256': digest, 'created_at': time.time(), 'download_path': path}
    db.set_setting('export:' + identifier, metadata)
    download_url = ''
    if public:
        expires = int(time.time()) + 86400
        download_url = public + path + '?' + urlencode({'expires': expires, 'signature': _signature(identifier, expires)})
    return {**{k: v for k, v in result.items() if k != 'artifact'}, **metadata, 'download_url': download_url, 'url': path}

async def publish_export(result: dict) -> dict:
    return await asyncio.to_thread(_store, result)
