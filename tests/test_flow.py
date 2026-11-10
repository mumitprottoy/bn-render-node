"""End to end with the fake Blender and the fake API: render, finish, upload, register, delete locally."""

import json
import subprocess
import unittest

from helpers import FFPROBE
from helpers import KEY
from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job

from bnrender import USER_AGENT
from bnrender.jobs import item_fingerprint


class FlowTest(NodeTestCase):
    def test_items_are_rendered_uploaded_registered_and_deleted(self) -> None:
        items = [plate_item('plate_3d/test/one', frames=7), plate_item('plate_3d/test/two', frames=5)]
        self.write_job(plate_job('plates-test', items))
        worker = self.worker()
        self.run_until(worker, lambda: self.all_items_in('done'))

        for item in items:
            record = self.api.records[item['asset_id']]
            self.assertEqual(record['job_fingerprint'], item_fingerprint(item, '1'))
            self.assertEqual({entry['role'] for entry in record['files']}, {'video', 'frames', 'preview', 'sheet'})
            self.assertEqual(record['group'], 'Test plates')
            self.assertIn('checks', record['measured'])
        # Internal resume files never leave the PC.
        self.assertFalse(any('scene' in key or 'plan' in key for key in self.api.storage))
        # Work folders are deleted after registration.
        self.assertFalse(any((self.root / 'work' / 'plates-test').glob('*')))
        # The uploaded video holds every frame.
        video = self.api.storage['library/plate_3d/test/one/video.mp4']['body']
        path = self.temp / 'check.mp4'
        path.write_bytes(video)
        probe = subprocess.run([FFPROBE, '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                                'stream=nb_read_frames,pix_fmt,color_primaries', '-of', 'json', str(path)],
                               capture_output=True, text=True, check=True)
        stream = json.loads(probe.stdout)['streams'][0]
        self.assertEqual(int(stream['nb_read_frames']), 7)
        self.assertEqual(stream['pix_fmt'], 'yuv420p')
        self.assertEqual(stream['color_primaries'], 'bt709')

    def test_every_request_sends_the_user_agent_and_only_the_api_gets_the_key(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        self.assertTrue(self.api.requests)
        for request in self.api.requests:
            self.assertEqual(request['headers'].get('user-agent'), USER_AGENT, request['path'])
        puts = self.api.requests_to('/r2/', 'PUT')
        self.assertEqual(len(puts), 4)
        for request in puts:
            self.assertNotIn('x-api-key', request['headers'])
        for request in self.api.requests_to('/api/studio/library'):
            self.assertEqual(request['headers'].get('x-api-key'), KEY)


if __name__ == '__main__':
    unittest.main()
