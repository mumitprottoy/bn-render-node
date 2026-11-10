"""Upload one finished item and register it (CONTRACT section 6). The caller deletes the work folder only after this
returns `done`, which means the register call answered 200."""

import dataclasses
import logging
import time
from pathlib import Path

from . import VERSION
from .api import ApiClient
from .api import ApiError
from .api import TransientError
from .finishing import read_manifest

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class UploadOutcome:
    status: str  # done | retry | failed | missing
    message: str = str()
    bytes: int = 0


def file_entries(manifest: dict) -> list[dict]:
    return [dict(role=entry['role'], filename=entry['filename'], size=int(entry['size']), sha256=entry['sha256'])
            for entry in manifest['files']]


def register_payload(job: dict, item: dict, manifest: dict, fingerprint: str, taxonomy_version: str,
                     template_version: str) -> dict:
    measured = dict(manifest.get('measured') or dict())
    measured.setdefault('made', dict(date=time.strftime('%Y-%m-%d'), template_version=template_version,
                                     runner=VERSION))
    return dict(
        asset_id=item['asset_id'],
        kind=job['kind'],
        title=str(item.get('title') or str()),
        description=str(item.get('description') or str()),
        group=str(item.get('group') or str()),
        labels=item.get('labels') or dict(),
        measured=measured,
        files=file_entries(manifest),
        job_fingerprint=fingerprint,
        taxonomy_version=taxonomy_version,
    )


def already_registered(api: ApiClient, asset_id: str, fingerprint: str) -> dict | None:
    """The library record when this exact item (same fingerprint) is already registered."""
    record = api.get_asset(asset_id)
    if record is not None and record.get('job_fingerprint') == fingerprint:
        return record
    return None


def upload_item(api: ApiClient, job: dict, item: dict, fingerprint: str, work_dir: Path, taxonomy_version: str,
                template_version: str) -> UploadOutcome:
    asset_id = item['asset_id']
    try:
        manifest = read_manifest(work_dir)
        if manifest is None:
            # The files are gone (for example a power cut right after an earlier, successful registration).
            record = already_registered(api, asset_id, fingerprint)
            if record is not None:
                return UploadOutcome(status='done', bytes=int(record.get('total_bytes') or 0))
            return UploadOutcome(status='missing', message='The finished files are missing; the item is made again.')
        for entry in manifest['files']:
            path = work_dir / entry['path']
            if not path.is_file() or path.stat().st_size != int(entry['size']):
                return UploadOutcome(status='missing', message=f'The {entry["role"]} file changed or is missing; '
                                                               'the item is made again.')
        links = api.prepare_uploads(asset_id, job['kind'], file_entries(manifest))
        by_role = {str(link.get('role')): link for link in links if isinstance(link, dict)}
        for entry in manifest['files']:
            link = by_role.get(entry['role'])
            if link is None or not link.get('url'):
                return UploadOutcome(status='failed', message=f'The API gave no upload link for the {entry["role"]} file.')
            api.put_file(str(link['url']), link.get('headers') or dict(), work_dir / entry['path'])
        api.register(register_payload(job, item, manifest, fingerprint, taxonomy_version, template_version))
    except ApiError as error:
        return UploadOutcome(status='failed', message=f'The library refused it: {error.message}')
    except TransientError as error:
        return UploadOutcome(status='retry', message=str(error))
    return UploadOutcome(status='done', bytes=sum(int(entry['size']) for entry in manifest['files']))
