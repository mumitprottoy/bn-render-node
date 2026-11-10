"""A fake studio library API plus a fake R2, in a thread, matching bn_v2's studio_library endpoints:
POST /uploads, PUT <signed url>, POST /register, GET /assets/<id>, GET /sources?prefix=&next=. Every request is logged
with its headers so tests can check the User-Agent and the key."""

import hashlib
import json
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

API_PREFIX = '/api/studio/library'
KEY = 'test-key'
SOURCES_PAGE_SIZE = 2


class FakeApi:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = list()
        self.storage: dict[str, dict] = dict()
        self.records: dict[str, dict] = dict()
        self.sources: dict[str, bytes] = dict()
        self.downloads: dict[str, bytes] = dict()
        # path prefix (for example "/uploads") -> list of statuses to answer before behaving normally
        self.failures: dict[str, list[int]] = dict()
        self.register_error: tuple[int, str] | None = None
        handler = self._handler_class()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    @property
    def api_base(self) -> str:
        return f'{self.base}{API_PREFIX}'

    def start(self) -> 'FakeApi':
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_source(self, key: str, content: bytes) -> None:
        with self.lock:
            self.sources[key] = content

    def preregister(self, asset_id: str, fingerprint: str, total_bytes: int = 123) -> None:
        with self.lock:
            self.records[asset_id] = dict(asset_id=asset_id, job_fingerprint=fingerprint, total_bytes=total_bytes,
                                          files=list())

    def fail(self, prefix: str, *statuses: int) -> None:
        with self.lock:
            self.failures.setdefault(prefix, list()).extend(statuses)

    def requests_to(self, fragment: str, method: str | None = None) -> list[dict]:
        with self.lock:
            return [entry for entry in self.requests if fragment in entry['path']
                    and (method is None or entry['method'] == method)]

    def _handler_class(self) -> type:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *arguments: object) -> None:
                return

            def _send(self, status: int, payload: object = None, raw: bytes | None = None) -> None:
                body = raw if raw is not None else json.dumps(payload if payload is not None else dict()).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json' if raw is None else 'application/octet-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self) -> bytes:
                length = int(self.headers.get('Content-Length') or 0)
                return self.rfile.read(length) if length else bytes()

            def _log(self, body: bytes) -> None:
                with api.lock:
                    api.requests.append(dict(method=self.command, path=self.path,
                                             headers={name.lower(): value for name, value in self.headers.items()},
                                             size=len(body)))

            def _injected(self, relative: str) -> int | None:
                with api.lock:
                    for prefix, statuses in api.failures.items():
                        if relative.startswith(prefix) and statuses:
                            return statuses.pop(0)
                return None

            def _authorised(self) -> bool:
                if self.headers.get('X-API-Key') != KEY:
                    self._send(401, dict(detail='Invalid or revoked API key.'))
                    return False
                return True

            def do_GET(self) -> None:
                self._log(bytes())
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.startswith('/src/'):
                    key = urllib.parse.unquote(parsed.path[len('/src/'):])
                    content = api.sources.get(key, api.downloads.get(key))
                    if content is None:
                        self._send(404, dict(errors=['missing']))
                        return
                    self._send(200, raw=content)
                    return
                relative = parsed.path[len(API_PREFIX):]
                if not self._authorised():
                    return
                injected = self._injected(relative)
                if injected is not None:
                    self._send(injected, dict(errors=[f'injected {injected}']))
                    return
                if relative.startswith('/assets/'):
                    asset_id = urllib.parse.unquote(relative[len('/assets/'):])
                    record = api.records.get(asset_id)
                    if record is None:
                        self._send(404, dict(errors=['That studio resource is not in the library.']))
                    else:
                        self._send(200, record)
                    return
                if relative == '/sources':
                    query = urllib.parse.parse_qs(parsed.query)
                    prefix = 'sources/' + (query.get('prefix') or [''])[0]
                    start = int((query.get('next') or ['0'])[0])
                    keys = sorted(key for key in api.sources if key.startswith(prefix))
                    page = keys[start:start + SOURCES_PAGE_SIZE]
                    following = start + SOURCES_PAGE_SIZE
                    self._send(200, dict(
                        sources=[dict(key=key, size=len(api.sources[key]),
                                      url=f'{api.base}/src/{urllib.parse.quote(key)}') for key in page],
                        next=str(following) if following < len(keys) else None,
                    ))
                    return
                self._send(404, dict(errors=['unknown path']))

            def do_POST(self) -> None:
                body = self._body()
                self._log(body)
                relative = urllib.parse.urlparse(self.path).path[len(API_PREFIX):]
                if not self._authorised():
                    return
                injected = self._injected(relative)
                if injected is not None:
                    self._send(injected, dict(errors=[f'injected {injected}']))
                    return
                payload = json.loads(body or b'{}')
                if relative == '/uploads':
                    links = list()
                    for entry in payload['files']:
                        extension = Path(entry['filename']).suffix
                        key = f'library/{payload["asset_id"]}/{entry["role"]}{extension}'
                        mime = mimetypes.guess_type(f'file{extension}')[0] or 'application/octet-stream'
                        links.append(dict(role=entry['role'], key=key, size=entry['size'],
                                          url=f'{api.base}/r2/{urllib.parse.quote(key)}?X-Amz-Signature=fake',
                                          headers={'Content-Type': mime, 'x-amz-meta-sha256': entry['sha256']}))
                    self._send(200, dict(files=links))
                    return
                if relative == '/register':
                    if api.register_error is not None:
                        status, message = api.register_error
                        self._send(status, dict(errors=[message]))
                        return
                    files = list()
                    for entry in payload['files']:
                        key = f'library/{payload["asset_id"]}/{entry["role"]}{Path(entry["filename"]).suffix}'
                        stored = api.storage.get(key)
                        if stored is None:
                            self._send(400, dict(errors=['A file was not found in storage.']))
                            return
                        if len(stored['body']) != entry['size'] or stored['sha256'] != entry['sha256']:
                            self._send(400, dict(errors=['A file in storage does not match what was registered.']))
                            return
                        files.append(dict(role=entry['role'], key=key, size=entry['size'], sha256=entry['sha256']))
                    record = dict(asset_id=payload['asset_id'], kind=payload['kind'], title=payload['title'],
                                  description=payload['description'], group=payload['group'], labels=payload['labels'],
                                  measured=payload['measured'], files=files,
                                  total_bytes=sum(entry['size'] for entry in files),
                                  job_fingerprint=payload['job_fingerprint'],
                                  taxonomy_version=payload['taxonomy_version'])
                    with api.lock:
                        api.records[payload['asset_id']] = record
                    self._send(200, record)
                    return
                self._send(404, dict(errors=['unknown path']))

            def do_PUT(self) -> None:
                body = self._body()
                self._log(body)
                parsed = urllib.parse.urlparse(self.path)
                injected = self._injected(parsed.path)
                if injected is not None:
                    self._send(injected, dict(errors=[f'injected {injected}']))
                    return
                if not parsed.path.startswith('/r2/'):
                    self._send(404, dict(errors=['unknown path']))
                    return
                key = urllib.parse.unquote(parsed.path[len('/r2/'):])
                claimed = self.headers.get('x-amz-meta-sha256')
                if claimed != hashlib.sha256(body).hexdigest():
                    self._send(400, raw=b'<Error><Code>BadDigest</Code><Message>sha</Message></Error>')
                    return
                with api.lock:
                    api.storage[key] = dict(body=body, sha256=claimed, content_type=self.headers.get('Content-Type'))
                self._send(200, dict())

        return Handler
