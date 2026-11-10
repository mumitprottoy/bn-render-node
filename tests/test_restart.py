"""Scheduled restarts: the decision only (fake uptime and a fake restart; nothing is restarted here)."""

import time
import unittest

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job

from bnrender.control import request_pause
from bnrender.executors import UnitControl
from bnrender.runner import EXIT_DONE
from bnrender.runner import ActiveUnit
from bnrender.status import render_status
from bnrender.screen import screen_state

HOUR = 3600.0


class RestartTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings.reboot_enabled = True
        self.settings.reboot_every_hours = 6
        self.settings.restart_countdown_seconds = 0.3
        self.uptime_value = 7 * HOUR
        self.others: list[str] = list()
        self.restarts: list[float] = list()
        self.restart_result = (True, '')

    def make_worker(self) -> object:
        def restart() -> tuple[bool, str]:
            self.restarts.append(time.time())
            return self.restart_result

        worker = self.worker(uptime=lambda: self.uptime_value, restart=restart, other_users=lambda: self.others)
        worker.startup()
        return worker

    def fake_unit(self, lane: str, phase: str) -> ActiveUnit:
        return ActiveUnit(key=f'{phase}:x', asset_id='plate_3d/test/x', job_id='plates-test', phase=phase, lane=lane,
                          threads=0, ram_gb=0, control=UnitControl(), started_at=time.time())

    def test_no_restart_before_the_uptime_is_reached(self) -> None:
        self.uptime_value = 5 * HOUR
        worker = self.make_worker()
        worker.step()
        self.assertIsNone(worker.restart_deadline)

    def test_restart_after_the_countdown_at_a_clean_point(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        worker = self.make_worker()
        worker.maintenance()
        self.assertIsNone(worker.step())
        self.assertIsNotNone(worker.restart_deadline)
        # Counting down: the screen says so and no new unit starts.
        self.assertEqual(screen_state(self.settings, worker.db, worker.screen_info())['state'], 'restarting')
        self.assertEqual(self.item_state('plate_3d/test/one'), 'pending')
        self.assertIn('RESTARTING in', render_status(self.settings))
        time.sleep(0.35)
        self.assertEqual(worker.step(), EXIT_DONE)
        self.assertEqual(len(self.restarts), 1)
        self.assertIsNotNone(worker.db.get_meta('last_restart_at'))
        messages = [row['message'] for row in worker.db.query('SELECT message FROM events')]
        self.assertTrue(any(message.startswith('Restarting the PC (scheduled every 6 h)') for message in messages))
        self.assertIn('last restart', render_status(self.settings))

    def test_never_while_paused(self) -> None:
        request_pause(self.settings)
        worker = self.make_worker()
        worker.step()
        self.assertIsNone(worker.restart_deadline)

    def test_a_pause_during_the_countdown_cancels_it(self) -> None:
        worker = self.make_worker()
        worker.step()
        self.assertIsNotNone(worker.restart_deadline)
        request_pause(self.settings)
        time.sleep(0.35)
        worker.step()
        self.assertIsNone(worker.restart_deadline)
        self.assertEqual(self.restarts, list())

    def test_not_while_a_gpu_item_renders_or_an_upload_is_in_flight(self) -> None:
        worker = self.make_worker()
        worker.active['render:x'] = self.fake_unit('gpu', 'render')
        worker._restart_tick(None)
        self.assertIsNone(worker.restart_deadline)
        worker.active.clear()
        worker.active['upload:x'] = self.fake_unit('net', 'upload')
        worker._restart_tick(None)
        self.assertIsNone(worker.restart_deadline)
        worker.active.clear()
        # A CPU unit does not hold a restart back; it resumes after.
        worker.active['render:y'] = self.fake_unit('cpu', 'render')
        worker._restart_tick(None)
        self.assertIsNotNone(worker.restart_deadline)

    def test_not_while_someone_else_is_signed_in(self) -> None:
        self.others = ['wife']
        worker = self.make_worker()
        worker.step()
        self.assertIsNone(worker.restart_deadline)

    def test_a_failed_restart_is_recorded_and_tried_again_later(self) -> None:
        self.restart_result = (False, 'Access is denied.')
        worker = self.make_worker()
        worker.step()
        time.sleep(0.35)
        self.assertIsNone(worker.step())
        self.assertEqual(len(self.restarts), 1)
        self.assertIn('Access is denied.', worker.db.last_error()['message'])
        self.assertIsNone(worker.db.get_meta('last_restart_at'))
        worker.step()
        self.assertIsNone(worker.restart_deadline)

    def test_switched_off_does_nothing(self) -> None:
        self.settings.reboot_enabled = False
        worker = self.make_worker()
        worker.step()
        self.assertIsNone(worker.restart_deadline)

    def test_gpu_jobs_wait_while_the_graphics_card_failed_its_check(self) -> None:
        self.settings.reboot_enabled = False
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        worker = self.make_worker()
        worker.gpu_problem = 'Blender sees no HIP graphics device here'
        worker.step()
        job = self.job_row('plates-test')
        self.assertEqual(job['state'], 'waiting')
        self.assertIn('waiting for the graphics card', job['reason'])
        self.assertEqual(self.item_state('plate_3d/test/one'), 'pending')


if __name__ == '__main__':
    unittest.main()
