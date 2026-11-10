"""The worker loop: out-of-memory retries, crash retries, and `bn pause` / `bn resume` through the flag file."""

import threading
import time
import unittest

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job
from helpers import wait_for

from bnrender.control import clear_pause
from bnrender.control import request_pause
from bnrender.status import render_status


class WorkerTest(NodeTestCase):
    def test_out_of_graphics_memory_is_retried_then_succeeds(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3, fake_oom_launches=2)]))
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        self.assertEqual(self.item_row('plate_3d/test/one')['oom_retries'], 2)

    def test_out_of_graphics_memory_four_times_fails_the_item(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3, fake_oom_launches=9)]))
        self.run_until(self.worker(), lambda: self.all_items_in('failed'))
        row = self.item_row('plate_3d/test/one')
        self.assertEqual(row['failure_kind'], 'oom')
        self.assertEqual(row['oom_retries'], 4)
        launches = (self.root / 'work' / 'plates-test' / 'test__one' / 'out' / 'launches.txt').read_text()
        self.assertEqual(launches, '4')

    def test_a_crash_is_tried_once_more_then_fails(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', fake_crash=True)]))
        self.run_until(self.worker(), lambda: self.all_items_in('failed'))
        row = self.item_row('plate_3d/test/one')
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(row['last_error'], 'KeyError: template')

    def _run_in_background(self, worker: object) -> threading.Thread:
        thread = threading.Thread(target=worker.run, kwargs=dict(timeout=120), daemon=True)
        thread.start()
        return thread

    def test_pause_once_stays_paused_with_frames_kept_then_resume_finishes(self) -> None:
        item = plate_item('plate_3d/test/one', frames=20, fake_frame_seconds=0.1)
        self.write_job(plate_job('plates-test', [item]))
        worker = self.worker()
        thread = self._run_in_background(worker)
        self.assertTrue(wait_for(lambda: (self.item_row(item['asset_id']) or dict()).get('frames_done', 0) >= 3))
        request_pause(self.settings)
        self.assertTrue(wait_for(lambda: self.item_row(item['asset_id'])['active_phase'] == ''))
        row = self.item_row(item['asset_id'])
        self.assertEqual(row['state'], 'running')
        frames_at_pause = row['frames_done']
        self.assertLess(frames_at_pause, 20)
        time.sleep(0.5)
        self.assertEqual(self.item_row(item['asset_id'])['frames_done'], frames_at_pause)
        status = render_status(self.settings)
        self.assertIn('PAUSED since', status)
        self.assertIn(f'paused at: plate_3d/test/one, frame {frames_at_pause} of 20 done', status)
        clear_pause(self.settings)
        self.assertTrue(wait_for(lambda: self.item_state(item['asset_id']) == 'done', timeout=60))
        worker.stop_event.set()
        thread.join(30)

    def test_pause_twice_stops_the_unit_at_once(self) -> None:
        item = plate_item('plate_3d/test/one', frames=6, fake_frame_seconds=2.0)
        self.write_job(plate_job('plates-test', [item]))
        worker = self.worker()
        thread = self._run_in_background(worker)
        self.assertTrue(wait_for(lambda: (self.item_row(item['asset_id']) or dict()).get('frames_done', 0) >= 1,
                                 timeout=30))
        request_pause(self.settings)
        time.sleep(0.2)
        self.assertIn('PAUSING', render_status(self.settings))
        started = time.monotonic()
        request_pause(self.settings)
        self.assertTrue(wait_for(lambda: self.item_row(item['asset_id'])['active_phase'] == '', timeout=10))
        self.assertLess(time.monotonic() - started, 1.9)
        self.assertEqual(self.item_row(item['asset_id'])['frames_done'], 1)
        worker.stop_event.set()
        thread.join(30)
        self.assertTrue((self.root / 'work' / 'plates-test' / 'test__one' / 'out' / 'frames' / '0001.png').exists())
        self.assertFalse((self.root / 'work' / 'plates-test' / 'test__one' / 'out' / 'frames' / '0002.png').exists())


if __name__ == '__main__':
    unittest.main()
