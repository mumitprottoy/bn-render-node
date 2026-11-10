"""When every job is done: confirm each item is registered with its fingerprint, then delete work/ (and the scene
assets), remove the API key from .env, and record FINISHED with the space freed."""

import logging
import time

from .api import ApiClient
from .api import ApiError
from .api import TransientError
from .db import Database
from .settings import API_KEY_NAME
from .settings import Settings
from .settings import remove_env_value
from .sysinfo import folder_bytes
from .sysinfo import remove_folder

logger = logging.getLogger(__name__)


def finish(settings: Settings, db: Database, api: ApiClient) -> tuple[str, str]:
    """Returns ('finished' | 'reset' | 'retry', message)."""
    rows = db.query("SELECT items.asset_id, items.fingerprint, items.job_id FROM items JOIN jobs ON jobs.id = items.job_id "
                    "WHERE jobs.removed = 0")
    missing: list[str] = list()
    for row in rows:
        try:
            record = api.get_asset(row['asset_id'])
        except TransientError as error:
            return 'retry', f'Could not confirm the library yet: {error}'
        except ApiError as error:
            return 'retry', f'The library check was refused: {error.message}'
        if record is None or record.get('job_fingerprint') != row['fingerprint']:
            missing.append(row['asset_id'])
    if missing:
        with db.transaction() as connection:
            for asset_id in missing:
                connection.execute(
                    "UPDATE items SET state = 'pending', registry_checked = 0, frames_done = 0, frames_total = 0, "
                    "attempts = 0, oom_retries = 0, failure_kind = '', last_error = '', updated_at = ? "
                    "WHERE asset_id = ?", (time.time(), asset_id))
        message = f'{len(missing)} items were not confirmed in the library; making them again.'
        db.add_event('error', message)
        return 'reset', message
    freed = folder_bytes(settings.work_dir) + folder_bytes(settings.assets_dir)
    remove_folder(settings.work_dir)
    remove_folder(settings.assets_dir)
    key_removed = remove_env_value(settings.env_file, API_KEY_NAME)
    db.set_meta('finished_at', str(time.time()))
    db.set_meta('space_freed', str(freed))
    message = (f'Finished: {len(rows)} items confirmed in the library; work folder deleted'
               f'{"; API key removed" if key_removed else ""}.')
    db.add_event('info', message)
    logger.info(message)
    return 'finished', message
