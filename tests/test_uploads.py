"""Uploading and registering (CONTRACT section 6): retries, refusals, and skipping what is already registered."""

import unittest

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job

from bnrender.jobs import item_fingerprint


class UploadTest(NodeTestCase):
    def test_upload_retries_on_server_errors_then_succeeds(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        self.api.fail('/uploads', 503, 502)
        self.api.fail('/r2/', 500)
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        self.assertEqual(len(self.api.requests_to('/uploads', 'POST')), 4)
        self.assertIn('plate_3d/test/one', self.api.records)
        self.assertFalse((self.root / 'work' / 'plates-test' / 'test__one').exists())

    def test_rendering_carries_on_while_uploads_wait(self) -> None:
        items = [plate_item('plate_3d/test/one', frames=3), plate_item('plate_3d/test/two', frames=3)]
        self.write_job(plate_job('plates-test', items))
        self.api.fail('/uploads', *([503] * 6))
        self.settings.backoff_initial = 0.4
        self.settings.backoff_max = 0.4
        worker = self.worker()
        # Both items get rendered and finished although the first upload attempts fail.
        self.run_until(worker, lambda: self.item_state('plate_3d/test/two') in ('uploading', 'done')
                       and self.item_state('plate_3d/test/one') in ('uploading', 'done'))
        self.run_until(self.worker(), lambda: self.all_items_in('done'))

    def test_register_refusal_fails_the_item_with_the_api_message(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        self.api.register_error = (400, 'Every studio resource needs a title and a group.')
        self.run_until(self.worker(), lambda: self.all_items_in('failed'))
        row = self.item_row('plate_3d/test/one')
        self.assertEqual(row['failure_kind'], 'api')
        self.assertIn('Every studio resource needs a title and a group.', row['last_error'])
        # Nothing is deleted until registration succeeds.
        self.assertTrue((self.root / 'work' / 'plates-test' / 'test__one' / 'final' / 'video.mp4').exists())
        self.assertEqual(self.job_row('plates-test')['state'], 'failed')

    def test_uploads_failing_for_the_whole_window_fail_then_retry_at_the_next_check(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        self.settings.upload_retry_window = 0.5
        self.api.fail('/uploads', *([503] * 1000))
        worker = self.worker()
        self.run_until(worker, lambda: self.all_items_in('failed'))
        row = self.item_row('plate_3d/test/one')
        self.assertEqual(row['failure_kind'], 'upload_transient')
        self.assertIn('Uploads kept failing', row['last_error'])
        self.api.failures.clear()
        worker = self.worker()
        worker.next_maintenance_at = 0.0
        self.run_until(worker, lambda: self.all_items_in('done'))
        self.assertIn('plate_3d/test/one', self.api.records)

    def test_registered_item_with_the_same_fingerprint_is_skipped(self) -> None:
        item = plate_item('plate_3d/test/one', frames=3)
        self.api.preregister(item['asset_id'], item_fingerprint(item, '1'), total_bytes=4567)
        self.write_job(plate_job('plates-test', [item]))
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        row = self.item_row(item['asset_id'])
        self.assertEqual(row['skipped'], 1)
        self.assertEqual(self.api.requests_to('/uploads', 'POST'), list())
        self.assertFalse((self.root / 'work' / 'plates-test').exists() and
                         any((self.root / 'work' / 'plates-test').iterdir()))

    def test_registered_item_with_another_fingerprint_is_made_again(self) -> None:
        item = plate_item('plate_3d/test/one', frames=3)
        self.api.preregister(item['asset_id'], 'f' * 64)
        self.write_job(plate_job('plates-test', [item]))
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        self.assertEqual(self.item_row(item['asset_id'])['skipped'], 0)
        self.assertEqual(self.api.records[item['asset_id']]['job_fingerprint'], item_fingerprint(item, '1'))

    def test_a_refused_key_holds_blender_jobs_and_says_why(self) -> None:
        (self.root / '.env').write_text(f'BN_API_KEY=wrong\nBN_API_BASE={self.api.api_base}\n')
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        worker = self.worker()
        worker.startup()
        worker.step()
        job = self.job_row('plates-test')
        self.assertEqual(job['state'], 'waiting')
        self.assertIn('Invalid or revoked API key.', job['reason'])
        self.assertEqual(self.item_state('plate_3d/test/one'), 'pending')
        self.assertIn('Invalid or revoked API key.', worker.db.last_error()['message'])


if __name__ == '__main__':
    unittest.main()
