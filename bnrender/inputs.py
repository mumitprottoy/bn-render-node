"""Inputs from R2's sources/ (through the API's short-lived links) and pinned models.

- Scene assets: `sources/assets/` is mirrored into `<root>/assets/` (sub-paths kept) before any blender job runs.
  Files already present with the same size are skipped. They stay until the node finishes and are never uploaded.
- Item sources: an item may list `sources: [sources/...]`; those files are fetched into its work folder and deleted
  with it once the item is registered.
- Models: `library/models.yaml` lists id, url, sha256, licence and path; a model is downloaded into models/<path>,
  checked by SHA-256, and only then used.
"""

import dataclasses
import logging
import os
import time
from pathlib import Path, PurePosixPath

from .api import ApiClient
from .api import ApiError
from .api import TransientError
from .api import file_digest
from .db import Database
from .settings import Settings
from .yamlio import YamlReadError
from .yamlio import load_yaml_file

logger = logging.getLogger(__name__)

ASSETS_PREFIX = 'sources/assets/'


class InputError(Exception):
    """An input could not be fetched or checked; the message is plain language."""


def _safe_relative(key: str, prefix: str) -> PurePosixPath | None:
    relative = PurePosixPath(key[len(prefix):])
    if not key.startswith(prefix) or not relative.parts or '..' in relative.parts or relative.is_absolute():
        return None
    return relative


def sync_assets(settings: Settings, api: ApiClient) -> tuple[int, int]:
    """Mirror sources/assets/ into <root>/assets/. Returns (downloaded, total). Raises TransientError or ApiError."""
    entries = api.list_sources(ASSETS_PREFIX)
    downloaded = 0
    total = 0
    for entry in entries:
        key = str(entry.get('key') or str())
        if key.endswith('/'):
            continue
        relative = _safe_relative(key, ASSETS_PREFIX)
        if relative is None:
            continue
        total += 1
        destination = settings.assets_dir.joinpath(*relative.parts)
        size = int(entry.get('size') or 0)
        if destination.is_file() and destination.stat().st_size == size:
            continue
        api.download(str(entry['url']), destination)
        downloaded += 1
    return downloaded, total


def folder_has_files(entries: list[dict]) -> bool:
    return any(not str(entry.get('key') or str()).endswith('/') and int(entry.get('size') or 0) > 0
               for entry in entries)


def waiting_reason_for(folder: str) -> str:
    """"sources/photos/" reads as "waiting for photos in sources/"."""
    parts = [part for part in folder.strip('/').split('/') if part]
    if len(parts) == 2 and parts[0] == 'sources':
        return f'waiting for {parts[1]} in sources/'
    return f'waiting for files in {folder}'


def fetch_item_sources(settings: Settings, api: ApiClient, keys: list[str], work_dir: Path,
                       listings: dict[str, list[dict]]) -> dict[str, str]:
    """Download the item's own source files into <work>/inputs/. Returns key -> local path."""
    local: dict[str, str] = dict()
    for key in keys:
        relative = _safe_relative(key, 'sources/')
        if relative is None:
            raise InputError(f'{key} is not a file under sources/.')
        destination = work_dir / 'inputs' / Path(*relative.parts)
        folder = str(PurePosixPath(key).parent) + '/'
        entries = listings.get(folder)
        if entries is None:
            entries = api.list_sources(folder)
            listings[folder] = entries
        match = next((entry for entry in entries if entry.get('key') == key), None)
        if match is None:
            raise InputError(f'{key} is not in sources/ any more.')
        if not (destination.is_file() and destination.stat().st_size == int(match.get('size') or 0)):
            api.download(str(match['url']), destination)
        local[key] = str(destination)
    return local


@dataclasses.dataclass
class ModelSpec:
    id: str
    url: str
    sha256: str
    licence: str
    path: str


def load_models(settings: Settings) -> tuple[dict[str, ModelSpec], list[str]]:
    """The approved models list. Returns (models by id, problems)."""
    if not settings.models_file.exists():
        return dict(), list()
    try:
        raw = load_yaml_file(settings.models_file)
    except YamlReadError as error:
        return dict(), [str(error)]
    entries = raw.get('models') if isinstance(raw, dict) else raw
    models: dict[str, ModelSpec] = dict()
    problems: list[str] = list()
    for entry in entries or list():
        if not isinstance(entry, dict):
            continue
        spec = ModelSpec(id=str(entry.get('id') or str()), url=str(entry.get('url') or str()),
                         sha256=str(entry.get('sha256') or str()).lower(), licence=str(entry.get('licence') or str()),
                         path=str(entry.get('path') or str()))
        relative = PurePosixPath(spec.path)
        # The SHA-256 pins the content, so the transport does not have to be trusted.
        if not spec.id or not spec.url.startswith(('https://', 'http://')) or len(spec.sha256) != 64:
            problems.append(f'Model {spec.id or "(no id)"} needs a download url and a SHA-256.')
            continue
        if not spec.licence.strip():
            problems.append(f'Model {spec.id} has no confirmed licence, so it is not used.')
            continue
        if not relative.parts or relative.is_absolute() or '..' in relative.parts:
            problems.append(f'Model {spec.id} has an unusable path.')
            continue
        models[spec.id] = spec
    return models, problems


def model_file(settings: Settings, spec: ModelSpec) -> Path:
    return settings.models_dir.joinpath(*PurePosixPath(spec.path).parts)


def ensure_model(settings: Settings, db: Database, api: ApiClient, spec: ModelSpec) -> None:
    """Install and verify one model. A file already verified (same size and time) is not hashed again."""
    path = model_file(settings, spec)
    row = db.one('SELECT * FROM models WHERE id = ?', (spec.id,))
    if path.is_file():
        stat = path.stat()
        if (row is not None and row['sha256'] == spec.sha256 and row['size'] == stat.st_size
                and abs(row['mtime'] - stat.st_mtime) < 1 and row['verified_at'] is not None):
            return
        _, sha256 = file_digest(path)
        if sha256 == spec.sha256:
            _record_model(db, spec, path, error=str())
            return
        logger.warning(f'Model {spec.id} on disk does not match its SHA-256; downloading it again.')
        path.unlink()
    try:
        api.download(spec.url, path, expected_sha256=spec.sha256)
    except ApiError as error:
        _record_model(db, spec, path, error=error.message)
        raise InputError(f'model {spec.id}: {error.message}') from error
    except TransientError as error:
        _record_model(db, spec, path, error=str(error))
        raise InputError(f'model {spec.id} is still downloading ({error})') from error
    _record_model(db, spec, path, error=str())


def _record_model(db: Database, spec: ModelSpec, path: Path, error: str) -> None:
    stat = path.stat() if path.is_file() else None
    db.execute(
        'INSERT INTO models (id, sha256, path, size, mtime, verified_at, error) VALUES (?, ?, ?, ?, ?, ?, ?) '
        'ON CONFLICT(id) DO UPDATE SET sha256 = excluded.sha256, path = excluded.path, size = excluded.size, '
        'mtime = excluded.mtime, verified_at = excluded.verified_at, error = excluded.error',
        (spec.id, spec.sha256, str(path), stat.st_size if stat else 0, stat.st_mtime if stat else 0,
         time.time() if not error and stat else None, error),
    )


def clean_partial_downloads(folder: Path) -> None:
    if not folder.exists():
        return
    for path in folder.rglob('*.part'):
        try:
            os.remove(path)
        except OSError:
            continue
