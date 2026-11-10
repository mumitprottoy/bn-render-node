"""`bn status`: the numbers and the ETA, from a state database set up by hand."""

import json
import time
import unittest

from helpers import NodeTestCase

from bnrender.db import Database
from bnrender.status import format_duration
from bnrender.status import job_progress
from bnrender.status import render_status

NOW = time.mktime((2026, 11, 12, 22, 0, 0, 0, 0, -1))


class StatusTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.settings.db_path)
        self.db.init_schema()

    def tearDown(self) -> None:
        self.db.close()
        super().tearDown()

    def add_job(self, job_id: str, position: int, device: str, executor: str, state: str, reason: str = '',
                definition: dict | None = None, inputs_ok: int = 1, items_known: int = 1, kind: str = 'plate_3d') -> None:
        self.db.execute(
            'INSERT INTO jobs (id, title, kind, device, executor, priority, position, definition, state, reason, '
            'inputs_ok, models_ok, items_known) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)',
            (job_id, f'Title {job_id}', kind, device, executor, position, position, json.dumps(definition or dict()),
             state, reason, inputs_ok, items_known))

    def add_item(self, job_id: str, slug: str, position: int, state: str, frames_done: int = 0, frames_total: int = 0,
                 estimate: dict | None = None, active_phase: str = '', kind: str = 'plate_3d') -> None:
        asset_id = f'{kind}/test/{slug}'
        self.db.execute(
            'INSERT INTO items (asset_id, job_id, position, slug, data, fingerprint, state, frames_done, frames_total, '
            'active_phase) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (asset_id, job_id, position, slug, json.dumps(dict(asset_id=asset_id, estimate=estimate or dict())), 'f',
             state, frames_done, frames_total, active_phase))

    def build(self) -> None:
        # Job 1: GPU plates. 2 done (240 frames each), one at frame 113 of 240, one not started (estimate 240).
        self.add_job('plates', 1, 'gpu', 'blender_item', 'running')
        self.add_item('plates', 'a', 0, 'done', 240, 240)
        self.add_item('plates', 'b', 1, 'done', 240, 240)
        self.add_item('plates', 'c', 2, 'running', 113, 240, active_phase='render')
        self.add_item('plates', 'd', 3, 'pending', estimate=dict(frames=240))
        for _ in range(10):
            self.db.add_speed_sample('plates', 'frame', 6.0)
        # Job 2: CPU beds, 4 of 10 done, 110 s each measured, 1 thread each: 11 run side by side.
        self.add_job('beds', 2, 'cpu', 'python', 'running', definition=dict(threads=1, ram_gb=1.0), kind='music_bed')
        for index in range(10):
            self.add_item('beds', f'bed{index}', index, 'done' if index < 4 else 'pending', kind='music_bed')
        for _ in range(5):
            self.db.add_speed_sample('beds', 'item', 110.0)
        # Job 3: waiting for photos; nothing known yet.
        self.add_job('photos', 3, 'gpu', 'python', 'waiting', 'waiting for photos in sources/', inputs_ok=0,
                     items_known=0, kind='photo_motion')
        # Job 4: GPU, after job 1, never measured: the estimate (default 6 s a frame) is used.
        self.add_job('moods', 4, 'gpu', 'blender_item', 'waiting', 'after job 1')
        self.add_item('moods', 'm1', 0, 'pending', estimate=dict(frames=100))
        self.add_item('moods', 'm2', 1, 'pending', estimate=dict(frames=100))
        self.db.upsert_upload('plate_3d/test/a', 'plates', state='confirmed', bytes=1024 ** 3)
        self.db.upsert_upload('plate_3d/test/b', 'plates', state='confirmed', bytes=1024 ** 3)
        self.db.set_meta('heartbeat', str(NOW - 2))
        self.db.set_meta('worker_started_at', str(NOW - 3 * 3600 - 12 * 60))
        self.db.set_meta('gpu_name', 'AMD Radeon RX 6600')
        self.db.set_meta('gpu_backend', 'HIP')
        self.db.set_meta('checkpoint', str(NOW - 22))

    def test_numbers_and_eta(self) -> None:
        self.build()
        overview = job_progress(self.db, self.settings, paused=False)
        plates, beds, photos, moods = overview.jobs
        self.assertEqual(plates.units_total, 960)
        self.assertEqual(plates.units_done, 593)
        self.assertAlmostEqual(plates.eta_seconds, (127 + 240) * 6.0)
        self.assertTrue(plates.measured)
        self.assertAlmostEqual(beds.eta_seconds, 6 * 110.0 / 11)
        self.assertIsNone(photos.eta_seconds)
        self.assertFalse(moods.measured)
        self.assertAlmostEqual(moods.eta_seconds, 200 * 6.0)
        # GPU jobs run one after another; CPU jobs share the pool. The later of the two queues wins.
        self.assertAlmostEqual(overview.gpu_seconds, (127 + 240) * 6.0 + 200 * 6.0)
        self.assertAlmostEqual(overview.cpu_seconds, 60.0)
        self.assertAlmostEqual(overview.eta_seconds, 3402.0)
        self.assertEqual(overview.units_total, 960 + 10 + 200)
        self.assertEqual(overview.units_done, 593 + 4)

    def test_rendered_layout(self) -> None:
        self.build()
        text = render_status(self.settings, now=NOW)
        lines = text.splitlines()
        self.assertRegex(lines[0], r'^BowlersNetwork render node\s+RUNNING since 18:48 \(3 h 12 min\)$')
        self.assertIn('Overall   597 of 1,170 units done (51.0%)   about 57 min left   done around Thu 22:57', text)
        self.assertIn('1  Title plates                  2 / 4 plates        2       6.0 s a frame    37 min', text)
        self.assertIn('   now: plate_3d/test/c, frame 114 of 240', text)
        self.assertIn('2  Title beds                    4 / 10 beds         6       1.8 min a bed    1 min', text)
        self.assertIn('3  Title photos                  waiting for photos in sources/', text)
        self.assertIn('4  Title moods                   0 / 2 plates        2       (after job 1)    20 min', text)
        self.assertIn('Library   2 items confirmed in R2 (2.0 GB); 0 uploads in progress; 0 waiting', text)
        self.assertIn('GPU       AMD Radeon RX 6600 (HIP)    Last error: none    Last checkpoint 22 s ago', text)
        self.assertIn('This PC   0.0 GB of work in progress', text)

    def test_last_error_and_not_running(self) -> None:
        self.build()
        self.db.add_event('error', 'plate_3d/test/c: Out of graphics memory; trying again (1 of 3).')
        self.db.set_meta('heartbeat', str(NOW - 600))
        text = render_status(self.settings, now=NOW)
        self.assertIn('NOT RUNNING (last seen Thu 21:50)', text)
        self.assertIn('Error     plate_3d/test/c: Out of graphics memory; trying again (1 of 3).', text)

    def test_durations(self) -> None:
        self.assertEqual(format_duration(30), 'under 1 min')
        self.assertEqual(format_duration(41 * 60), '41 min')
        self.assertEqual(format_duration(7 * 3600 + 10 * 60), '7 h 10 min')
        self.assertEqual(format_duration(3 * 3600), '3 h 00 min')
        self.assertEqual(format_duration(41 * 3600 + 20 * 60), '41 h 20 min')


if __name__ == '__main__':
    unittest.main()
