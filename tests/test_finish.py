"""When every job is done: confirm the library, delete work/ and the scene assets, remove the key, show FINISHED."""

import time
import unittest

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job

from bnrender.completion import finish
from bnrender.status import render_status


class FinishTest(NodeTestCase):
    def test_finish_cleans_up_and_removes_the_key(self) -> None:
        self.api.add_source('sources/assets/hdri/studio.hdr', b'h' * 5000)
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3),
                                                 plate_item('plate_3d/test/two', frames=3)]))
        (self.root / 'work' / 'leftover').mkdir(parents=True)
        (self.root / 'work' / 'leftover' / 'stale.bin').write_bytes(bytes(1000))
        worker = self.worker()
        code = worker.run(timeout=90)
        self.assertEqual(code, 0)
        self.assertFalse((self.root / 'work').exists())
        self.assertFalse((self.root / 'assets').exists())
        env = (self.root / '.env').read_text()
        self.assertNotIn('BN_API_KEY', env)
        self.assertIn('BN_API_BASE=', env)
        # Every item was confirmed with a GET before the clean-up.
        self.assertEqual(len(self.api.requests_to('/assets/plate_3d/test/one', 'GET')), 2)
        text = render_status(self.settings)
        self.assertIn('FINISHED at', text)
        self.assertIn('Every job is done and every item is confirmed in the library.', text)
        self.assertIn('The API key was removed from this PC.', text)
        self.assertGreaterEqual(float(worker.db.get_meta('space_freed')), 6000)
        # Started again (after a reboot), it has nothing to do.
        self.assertEqual(self.worker().run(timeout=5), 0)

    def test_an_item_missing_from_the_library_is_made_again(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        worker = self.worker()
        self.run_until(worker, lambda: self.all_items_in('done'))
        del self.api.records['plate_3d/test/one']
        outcome, message = finish(self.settings, worker.db, worker.api)
        self.assertEqual(outcome, 'reset')
        self.assertEqual(self.item_state('plate_3d/test/one'), 'pending')
        self.assertIn('BN_API_KEY', (self.root / '.env').read_text())

    def test_failed_jobs_block_finishing(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', fake_crash=True)]))
        worker = self.worker()
        self.run_until(worker, lambda: self.all_items_in('failed'))
        worker.step()
        time.sleep(0.1)
        worker.step()
        self.assertIsNone(worker.db.get_meta('finished_at'))
        self.assertIn('BN_API_KEY', (self.root / '.env').read_text())


if __name__ == '__main__':
    unittest.main()
