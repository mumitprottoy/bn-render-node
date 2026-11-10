"""The studio library API (CONTRACT section 6) and plain downloads.

Every request, including the PUT to R2 and model downloads, sends `User-Agent: bn-render-node/1.0`: Cloudflare in
front of the backend blocks Python's default agent (error 1010). API requests also send `X-API-Key`, which is never
logged.
"""

import hashlib
import http.client
import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import USER_AGENT
from .settings import Settings

logger = logging.getLogger(__name__)

API_TIMEOUT = 60
TRANSFER_TIMEOUT = 300
CHUNK_BYTES = 1024 * 1024
TRANSIENT_STATUSES = (408, 425, 429)


class ApiError(Exception):
    """A refusal (4xx). The item fails with this message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class TransientError(Exception):
    """A network error, a 5xx, or no key yet: worth retrying later."""


def _error_message(status: int, body: bytes) -> str:
    text = body.decode('utf-8', errors='replace').strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        errors = parsed.get('errors')
        if isinstance(errors, list) and errors:
            return '; '.join(str(entry) for entry in errors)
        if parsed.get('detail'):
            return str(parsed['detail'])
    if text.startswith('<'):
        # R2 answers in XML: keep the code and message only.
        code = text.split('<Code>', 1)[1].split('</Code>', 1)[0] if '<Code>' in text else str()
        message = text.split('<Message>', 1)[1].split('</Message>', 1)[0] if '<Message>' in text else str()
        return f'{code} {message}'.strip() or f'HTTP {status}'
    return text[:300] or f'HTTP {status}'


class ApiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # transport

    def _send(self, request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        request.add_header('User-Agent', USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            body = error.read() if error.fp is not None else bytes()
            message = _error_message(error.code, body)
            if error.code >= 500 or error.code in TRANSIENT_STATUSES:
                raise TransientError(f'The server answered {error.code}: {message}') from error
            raise ApiError(error.code, message) from error
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException,
                OSError) as error:
            reason = getattr(error, 'reason', None) or error
            raise TransientError(f'The network is not reachable ({reason}).') from error

    def _api(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        key = self.settings.api_key()
        if key is None:
            raise TransientError('There is no API key in .env yet.')
        url = f'{self.settings.api_base()}{path}'
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header('X-API-Key', key)
        request.add_header('Accept', 'application/json')
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        status, body = self._send(request, API_TIMEOUT)
        try:
            parsed = json.loads(body.decode('utf-8')) if body else dict()
        except ValueError as error:
            raise TransientError(f'The server sent something that is not JSON (HTTP {status}).') from error
        return status, parsed if isinstance(parsed, dict) else dict(value=parsed)

    # library API

    def get_asset(self, asset_id: str) -> dict | None:
        """The registered record, or None when the asset is not in the library."""
        try:
            _, record = self._api('GET', f'/assets/{urllib.parse.quote(asset_id, safe="/")}')
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        return record

    def list_sources(self, prefix: str) -> list[dict]:
        """Every file under sources/<prefix>, following `next` pages. Each entry: key, size, url."""
        relative = prefix[len('sources/'):] if prefix.startswith('sources/') else prefix
        entries: list[dict] = list()
        continuation: str | None = None
        while True:
            query = dict(prefix=relative)
            if continuation:
                query['next'] = continuation
            _, page = self._api('GET', f'/sources?{urllib.parse.urlencode(query)}')
            entries.extend(entry for entry in page.get('sources') or list() if isinstance(entry, dict))
            continuation = page.get('next')
            if not continuation:
                return entries

    def prepare_uploads(self, asset_id: str, kind: str, files: list[dict]) -> list[dict]:
        _, response = self._api('POST', '/uploads', dict(asset_id=asset_id, kind=kind, files=files))
        links = response.get('files')
        if not isinstance(links, list):
            raise TransientError('The upload links were missing from the answer.')
        return links

    def register(self, payload: dict) -> dict:
        _, record = self._api('POST', '/register', payload)
        return record

    def put_file(self, url: str, headers: dict, path: Path) -> None:
        """PUT the file to its signed link with exactly the given headers (plus our User-Agent and its length)."""
        size = path.stat().st_size
        with open(path, 'rb') as handle:
            request = urllib.request.Request(url, data=handle, method='PUT')
            for name, value in (headers or dict()).items():
                request.add_header(name, str(value))
            request.add_header('Content-Length', str(size))
            self._send(request, TRANSFER_TIMEOUT)

    def download(self, url: str, destination: Path, expected_sha256: str | None = None) -> tuple[int, str]:
        """Stream a file to destination through a .part file; check its SHA-256 when given. Returns (size, sha256)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.part')
        digest = hashlib.sha256()
        size = 0
        request = urllib.request.Request(url, method='GET')
        request.add_header('User-Agent', USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=TRANSFER_TIMEOUT) as response, open(partial, 'wb') as handle:
                while True:
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
        except urllib.error.HTTPError as error:
            partial.unlink(missing_ok=True)
            if error.code >= 500 or error.code in TRANSIENT_STATUSES:
                raise TransientError(f'The download server answered {error.code}.') from error
            raise ApiError(error.code, f'The download was refused (HTTP {error.code}).') from error
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException,
                OSError) as error:
            partial.unlink(missing_ok=True)
            raise TransientError(f'The download stopped ({getattr(error, "reason", None) or error}).') from error
        sha256 = digest.hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256.lower():
            partial.unlink(missing_ok=True)
            raise ApiError(0, f'The downloaded file does not match its SHA-256 (got {sha256[:12]}...).')
        os.replace(partial, destination)
        return size, sha256


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
