"""Shared test scaffolding: a temporary node root with its own app/ (job files, blender script stub), a fake API,
and Settings pointing the runner at the fake Blender."""

import json
import logging
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / 'fakes'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(FAKES) not in sys.path:
    sys.path.insert(0, str(FAKES))

from bnrender.settings import Settings  # noqa: E402
from fake_api import KEY  # noqa: E402
from fake_api import FakeApi  # noqa: E402

FAKE_BLENDER = FAKES / 'fake_blender.py'
# The runner logs through the root logger; keep test output clean.
logging.getLogger().addHandler(logging.NullHandler())
FFMPEG = shutil.which('ffmpeg') or 'ffmpeg'
FFPROBE = shutil.which('ffprobe') or 'ffprobe'


def plate_item(asset_id: str, frames: int = 6, **params: object) -> dict:
    return dict(
        asset_id=asset_id,
        title=f'Test plate {asset_id.rsplit("/", 1)[-1]}',
        group='Test plates',
        description='A test plate.',
        labels=dict(subjects=['ball'], mood='league_night_warm'),
        params=dict(template='test', fake_frames=frames, **params),
        estimate=dict(frames=frames),
    )


def plate_job(job_id: str, items: list[dict], **fields: object) -> dict:
    job = dict(id=job_id, title=f'Job {job_id}', kind='plate_3d', device='gpu', priority=10, executor='blender_item',
               entry='render_item.py', items=items)
    job.update(fields)
    return job


class NodeTestCase(unittest.TestCase):
    """A fresh node per test: root/, root/app/ and a running fake API."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix='bnrender-test-'))
        self.root = self.temp / 'node'
        self.app = self.root / 'app'
        (self.app / 'library' / 'jobs').mkdir(parents=True)
        (self.app / 'blender').mkdir(parents=True)
        (self.app / 'blender' / 'render_item.py').write_text('# stand-in; tests run fake_blender.py instead\n')
        self.api = FakeApi().start()
        self.root.mkdir(exist_ok=True)
        (self.root / '.env').write_text(f'BN_API_KEY={KEY}\nBN_API_BASE={self.api.api_base}\n')
        self.settings = self.make_settings()

    def tearDown(self) -> None:
        self.api.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def make_settings(self, **overrides: object) -> Settings:
        settings = Settings(
            root=self.root,
            app_dir=self.app,
            blender_command=[sys.executable, str(FAKE_BLENDER)],
            python_command=[sys.executable],
            ffmpeg=FFMPEG,
            ffprobe=FFPROBE,
            gpu_device='METAL',
            poll_seconds=0.05,
            heartbeat_seconds=0.5,
            maintenance_seconds=3600,
            backoff_initial=0.1,
            backoff_max=0.3,
            upload_retry_window=60,
            min_free_gb=0,
            finish_threads=2,
            self_update=False,
            drain_timeout=10,
            retry_delay_seconds=0.1,
            screen_enabled=False,
            kiosk_enabled=False,
            reboot_enabled=False,
            startup_selfcheck=False,
            input_retry_seconds=0.2,
        )
        for name, value in overrides.items():
            setattr(settings, name, value)
        return settings

    def write_job(self, job: dict) -> None:
        # JSON is valid YAML, so this works with or without PyYAML.
        (self.app / 'library' / 'jobs' / f'{job["id"]}.yaml').write_text(json.dumps(job, indent=2))

    def worker(self, **kwargs: object) -> 'object':
        from bnrender.runner import Worker
        worker = Worker(self.settings, **kwargs)
        self.addCleanup(worker.db.close)
        return worker

    def run_until(self, worker: object, condition: object, timeout: float = 60) -> None:
        worker.run(until=condition, timeout=timeout)

    def item_state(self, asset_id: str) -> str | None:
        from bnrender.db import Database
        db = Database(self.settings.db_path)
        try:
            row = db.item(asset_id)
            return None if row is None else row['state']
        finally:
            db.close()

    def item_row(self, asset_id: str) -> dict | None:
        from bnrender.db import Database
        db = Database(self.settings.db_path)
        try:
            row = db.item(asset_id)
            return None if row is None else dict(row)
        finally:
            db.close()

    def job_row(self, job_id: str) -> dict | None:
        from bnrender.db import Database
        db = Database(self.settings.db_path)
        try:
            row = db.job(job_id)
            return None if row is None else dict(row)
        finally:
            db.close()

    def all_items_in(self, *states: str) -> bool:
        from bnrender.db import Database
        db = Database(self.settings.db_path)
        try:
            rows = db.query('SELECT state FROM items')
            return bool(rows) and all(row['state'] in states for row in rows)
        finally:
            db.close()


def wait_for(condition: object, timeout: float = 30, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False
