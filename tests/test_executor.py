"""The blender_item executor: resume after a killed process, and the two levels of pause."""

import threading
import time
import unittest

from helpers import NodeTestCase
from helpers import plate_item

from bnrender.executors import PAUSE_AFTER_UNIT
from bnrender.executors import PAUSE_NOW
from bnrender.executors import UnitControl
from bnrender.executors import run_item
from bnrender.finishing import list_frames
from bnrender.procs import kill_tree

JOB = dict(id='plates-test', kind='plate_3d', device='gpu', executor='blender_item', entry='render_item.py')


class ExecutorTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.root / 'work' / 'plates-test' / 'test__one'
        self.progress: list[tuple[int, int, float | None]] = list()

    def launch(self, item: dict, control: UnitControl, on_progress: object = None) -> object:
        def record(done: int, total: int, seconds: float | None) -> None:
            self.progress.append((done, total, seconds))
            if on_progress is not None:
                on_progress(done, total)

        return run_item(self.settings, JOB, item, self.work, control, lambda pid, started: None, record)

    def frame_numbers(self) -> list[int]:
        return [number for number, _ in list_frames(self.work / 'out' / 'frames')]

    def render_log(self) -> list[int]:
        return [int(line) for line in (self.work / 'out' / 'render_log.txt').read_text().split()]

    def test_resume_after_killed_process_skips_finished_frames(self) -> None:
        item = plate_item('plate_3d/test/one', frames=10, fake_frame_seconds=0.05)
        control = UnitControl()

        def crash_after_three(done: int, total: int) -> None:
            if done == 3:
                kill_tree(control.process.pid)

        first = self.launch(item, control, crash_after_three)
        self.assertEqual(first.status, 'failed')
        self.assertEqual(self.frame_numbers(), [1, 2, 3])

        second = self.launch(item, UnitControl())
        self.assertEqual(second.status, 'completed')
        self.assertEqual(self.frame_numbers(), list(range(1, 11)))
        self.assertEqual(self.render_log(), list(range(1, 11)))
        self.assertTrue((self.work / 'out' / 'meta.json').exists())
        # The first PROGRESS line after each launch includes the scene load: it is not a speed sample.
        first_lines_after_launch = [entry for entry in self.progress if entry[0] in (1, 4)]
        self.assertTrue(all(seconds is None for _, _, seconds in first_lines_after_launch))

    def test_pause_once_finishes_the_frame_then_stops(self) -> None:
        item = plate_item('plate_3d/test/one', frames=12, fake_frame_seconds=0.1)
        control = UnitControl()

        def pause_after_two(done: int, total: int) -> None:
            if done == 2:
                # The pause arrives while frame 3 renders: frame 3 must finish, then the process ends.
                threading.Timer(0.03, control.request, args=(PAUSE_AFTER_UNIT, 'Paused.')).start()

        outcome = self.launch(item, control, pause_after_two)
        self.assertEqual(outcome.status, 'interrupted')
        self.assertEqual(outcome.frames_done, 3)
        self.assertEqual(self.frame_numbers(), [1, 2, 3])
        self.assertFalse((self.work / 'out' / 'meta.json').exists())

        resumed = self.launch(item, UnitControl())
        self.assertEqual(resumed.status, 'completed')
        self.assertEqual(self.render_log(), list(range(1, 13)))

    def test_pause_twice_stops_at_once_and_the_frame_is_redone(self) -> None:
        item = plate_item('plate_3d/test/one', frames=6, fake_frame_seconds=1.5)
        control = UnitControl()

        def stop_now_during_frame_two(done: int, total: int) -> None:
            if done == 1:
                threading.Timer(0.3, control.request, args=(PAUSE_NOW, 'Paused.')).start()

        started = time.monotonic()
        outcome = self.launch(item, control, stop_now_during_frame_two)
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.status, 'interrupted')
        # Frame 1 took 1.5 s; the stop came 0.3 s into frame 2, well before it could finish.
        self.assertLess(elapsed, 1.5 + 1.2)
        self.assertEqual(self.frame_numbers(), [1])
        self.assertTrue((self.work / 'out' / 'frames' / '0002.partial.png').exists())

        item['params']['fake_frame_seconds'] = 0.02
        resumed = self.launch(item, UnitControl())
        self.assertEqual(resumed.status, 'completed')
        self.assertEqual(self.frame_numbers(), list(range(1, 7)))
        self.assertEqual(self.render_log(), list(range(1, 7)))

    def test_a_crash_reports_the_scripts_error_line(self) -> None:
        outcome = self.launch(plate_item('plate_3d/test/one', fake_crash=True), UnitControl())
        self.assertEqual(outcome.status, 'failed')
        self.assertEqual(outcome.message, 'KeyError: template')

    def test_out_of_graphics_memory_is_recognised(self) -> None:
        outcome = self.launch(plate_item('plate_3d/test/one', fake_oom_launches=1), UnitControl())
        self.assertEqual(outcome.status, 'oom')


if __name__ == '__main__':
    unittest.main()
