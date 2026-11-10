"""The full-screen page: / and /status.json respond on 127.0.0.1 and follow the work, the pause and the finish."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job
from helpers import wait_for

from bnrender.control import request_pause
from bnrender.db import Database
from bnrender.desktop import find_edge
from bnrender.desktop import keep_awake
from bnrender.desktop import kiosk_command
from bnrender.screen import PAGE_BYTES
from bnrender.screen import screen_state


class ScreenTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.settings.screen_enabled = True
        self.settings.screen_port = 0

    def get(self, worker: object, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(f'{worker.screen.url.rstrip("/")}{path}', timeout=10) as response:
            return response.status, response.read(), response.headers.get('Content-Type')

    def status(self, worker: object) -> dict:
        return json.loads(self.get(worker, '/status.json')[1])

    def test_page_and_status_respond_and_follow_the_work(self) -> None:
        item = plate_item('plate_3d/test/one', frames=30, fake_frame_seconds=0.1)
        item['title'] = 'Pocket strike, pin cam, slow motion'
        self.write_job(plate_job('plates-test', [item]))
        worker = self.worker()
        thread = threading.Thread(target=worker.run, kwargs=dict(timeout=120), daemon=True)
        thread.start()
        self.assertTrue(wait_for(lambda: worker.screen is not None, timeout=10))
        self.assertEqual(worker.screen.server.server_address[0], '127.0.0.1')

        status, body, content_type = self.get(worker, '/')
        page = body.decode('utf-8')
        self.assertEqual(status, 200)
        self.assertIn('text/html', content_type)
        self.assertIn('This PC is busy with a very important task.', page)
        self.assertIn('Please ask Mumit Boss before using it.', page)
        self.assertIn('Thank you for lending this PC to BowlersNetwork.', page)
        self.assertIn('Mumit Boss: Win+R, powershell, bn status', page)
        self.assertIn('prefers-reduced-motion', page)

        self.assertTrue(wait_for(lambda: (self.item_row(item['asset_id']) or dict()).get('frames_done', 0) >= 3))
        data = self.status(worker)
        self.assertEqual(data['state'], 'working')
        self.assertEqual(data['state_text'], 'Working')
        self.assertEqual(data['current']['title'], 'Pocket strike, pin cam, slow motion')
        self.assertRegex(data['current']['detail'], r'^frame \d+ of 30$')
        self.assertGreater(data['percent'], 0)
        self.assertRegex(data['time_left'], r'^(about |under )')

        request_pause(self.settings)
        self.assertTrue(wait_for(lambda: self.status(worker)['state_text'] == 'Paused by Mumit Boss'))

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(worker, '/anything-else')
        self.assertEqual(caught.exception.code, 404)
        worker.stop_event.set()
        thread.join(30)

    def test_states_for_restart_finish_and_counts(self) -> None:
        db = Database(self.settings.db_path)
        db.init_schema()
        self.addCleanup(db.close)
        now = time.time()
        db.upsert_upload('plate_3d/test/a', 'plates', state='confirmed', bytes=10, confirmed_at=now - 60)
        db.upsert_upload('plate_3d/test/b', 'plates', state='confirmed', bytes=10, confirmed_at=now - 3 * 86400)
        db.upsert_upload('catalogue/nightly/catalogue', 'catalogue-nightly', state='confirmed', bytes=10,
                         confirmed_at=now - 60)
        restarting = screen_state(self.settings, db, dict(restart_in=42.4), now=now)
        self.assertEqual(restarting['state_text'], 'Restarting in 42 s')
        self.assertEqual((restarting['finished_today'], restarting['finished_total']), (1, 2))
        waiting = screen_state(self.settings, db, dict(gpu_problem='no HIP device'), now=now)
        self.assertEqual(waiting['state_text'], 'Waiting for the graphics card')
        finished = screen_state(self.settings, db, dict(finished=True), now=now)
        self.assertEqual((finished['state'], finished['state_text'], finished['percent']), ('finished', 'Finished', 100.0))

    def test_page_is_plain_ascii_with_no_external_resources(self) -> None:
        page = PAGE_BYTES.decode('utf-8')
        self.assertTrue(page.isascii())
        self.assertNotIn('http://', page.replace('http://127.0.0.1', ''))
        self.assertNotIn('https://', page)

    def test_windows_helpers_do_nothing_here(self) -> None:
        self.assertFalse(keep_awake())
        self.assertIsNone(find_edge())
        from pathlib import Path
        command = kiosk_command(Path('msedge.exe'), 'http://127.0.0.1:8765/', Path('profile'))
        self.assertEqual(command[1:5], ['--kiosk', 'http://127.0.0.1:8765/', '--edge-kiosk-type=fullscreen',
                                        '--no-first-run'])


if __name__ == '__main__':
    unittest.main()
