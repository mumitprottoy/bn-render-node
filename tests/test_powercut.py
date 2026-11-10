"""Power cut and crash: the worker runs as a real process and is killed mid-frame; started again, it resumes with no
frame lost or rendered twice."""

import json
import os
import signal
import subprocess
import sys
import unittest

from helpers import FAKE_BLENDER
from helpers import FFMPEG
from helpers import FFPROBE
from helpers import REPO
from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job
from helpers import wait_for

from bnrender.procs import is_running

FRAMES = 12


@unittest.skipIf(os.name == 'nt', 'uses POSIX process groups to simulate a power cut')
class PowerCutTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.render_log = self.temp / 'render_log.txt'
        self.processes: list[subprocess.Popen] = list()
        item = plate_item('plate_3d/test/one', frames=FRAMES, fake_frame_seconds=0.25)
        self.write_job(plate_job('plates-test', [item]))

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        super().tearDown()

    def start_worker(self) -> subprocess.Popen:
        environment = dict(os.environ)
        environment.update(
            BN_RENDER_ROOT=str(self.root), BN_APP_DIR=str(self.app), BN_BLENDER=str(FAKE_BLENDER), BN_FFMPEG=FFMPEG,
            BN_FFPROBE=FFPROBE, BN_GPU_DEVICE='METAL', FAKE_LOG=str(self.render_log), PYTHONPATH=str(REPO),
            BN_TUNING=json.dumps(dict(poll_seconds=0.05, heartbeat_seconds=0.5, self_update=False, min_free_gb=0,
                                      screen_enabled=False, kiosk_enabled=False, reboot_enabled=False,
                                      finish_threads=2, backoff_initial=0.1)),
        )
        process = subprocess.Popen([sys.executable, '-m', 'bnrender', 'worker'], cwd=str(REPO), env=environment,
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def frames_done(self) -> int:
        row = self.item_row('plate_3d/test/one') if self.settings.db_path.exists() else None
        return 0 if row is None else row['frames_done']

    def blender_pid(self) -> tuple[int | None, float | None]:
        row = self.item_row('plate_3d/test/one')
        return (row['pid'], row['pid_started']) if row is not None else (None, None)

    def check_result(self) -> None:
        self.assertTrue(wait_for(lambda: self.item_state('plate_3d/test/one') == 'done', timeout=90))
        rendered = [int(line) for line in self.render_log.read_text().split()]
        self.assertEqual(sorted(rendered), list(range(1, FRAMES + 1)), 'every frame rendered exactly once')
        video = self.api.storage['library/plate_3d/test/one/video.mp4']['body']
        path = self.temp / 'video.mp4'
        path.write_bytes(video)
        probe = subprocess.run([FFPROBE, '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                'stream=nb_read_frames', '-of', 'csv=p=0', str(path)], capture_output=True, text=True,
                               check=True)
        self.assertEqual(int(probe.stdout.strip()), FRAMES)

    def test_power_cut_mid_frame_resumes_without_lost_or_duplicate_frames(self) -> None:
        worker = self.start_worker()
        self.assertTrue(wait_for(lambda: self.frames_done() >= 4, timeout=60))
        pid, _ = self.blender_pid()
        # Everything stops at once, the render included, in the middle of a frame.
        os.killpg(worker.pid, signal.SIGKILL)
        os.killpg(pid, signal.SIGKILL)
        worker.wait()
        frames_dir = self.root / 'work' / 'plates-test' / 'test__one' / 'out' / 'frames'
        on_disk = sorted(path.name for path in frames_dir.glob('[0-9][0-9][0-9][0-9].png'))
        self.assertEqual(on_disk, [f'{index:04d}.png' for index in range(1, len(on_disk) + 1)])
        self.assertEqual(self.item_state('plate_3d/test/one'), 'running')

        self.start_worker()
        self.check_result()

    def test_crashed_runner_leaves_no_orphan_render_running_alongside(self) -> None:
        worker = self.start_worker()
        self.assertTrue(wait_for(lambda: self.frames_done() >= 3, timeout=60))
        pid, started = self.blender_pid()
        # Only the runner dies; its render is left running.
        os.killpg(worker.pid, signal.SIGKILL)
        worker.wait()
        self.start_worker()
        self.assertTrue(wait_for(lambda: not is_running(pid, started), timeout=30), 'the orphan render was ended')
        self.check_result()


if __name__ == '__main__':
    unittest.main()
