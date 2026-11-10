"""Repeating jobs (repeat: daily): never skipped as already registered, waiting with their last result, run again
after 06:00 when something new was registered, and once more at the end so the final catalogue is current."""

import datetime
import json
import textwrap
import time
import unittest
from pathlib import Path

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job

from bnrender.jobs import JobFileError
from bnrender.jobs import item_fingerprint
from bnrender.jobs import parse_job
from bnrender.runner import Worker
from bnrender.status import render_status

CATALOGUE_STUB = textwrap.dedent('''
    import json
    import os
    import sqlite3
    from pathlib import Path


    def build(item, out):
        db = Path(os.environ['BN_RENDER_ROOT']) / 'state' / 'runner.db'
        connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        registered = connection.execute(
            "SELECT COUNT(*) FROM uploads WHERE state = 'confirmed' AND job_id != 'catalogue-nightly'").fetchone()[0]
        connection.close()
        (out / 'catalogue.json').write_text(json.dumps(dict(items=registered)))
        (out / 'report.txt').write_text(f'{registered} registered')
        return dict(files=[dict(role='catalogue', path='catalogue.json'), dict(role='report_text', path='report.txt')],
                    measured=dict(registered=registered, published=registered, held_back=0, not_registered_yet=0,
                                  pass_rate=1.0))
''')


def catalogue_job() -> dict:
    return dict(id='catalogue-nightly', title='Catalogue and findability', kind='catalogue', device='cpu',
                priority=90, repeat='daily', executor='python', entry='catalogue_stub:build',
                items=[dict(asset_id='catalogue/nightly/catalogue', title='The library catalogue', group='Catalogue',
                            labels=dict(), params=dict(), estimate=dict(seconds=5))])


def local_time(day_offset: int, hour: int, minute: int = 0) -> float:
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (today + datetime.timedelta(days=day_offset, hours=hour, minutes=minute)).timestamp()


class RepeatTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.app / 'catalogue_stub.py').write_text(CATALOGUE_STUB)

    def test_repeat_must_be_daily(self) -> None:
        self.assertEqual(parse_job(catalogue_job(), Path('catalogue-nightly.yaml')).repeat, 'daily')
        weekly = catalogue_job()
        weekly['repeat'] = 'weekly'
        with self.assertRaisesRegex(JobFileError, 'repeat'):
            parse_job(weekly, Path('catalogue-nightly.yaml'))

    def test_a_repeating_item_is_never_skipped_as_registered(self) -> None:
        job = catalogue_job()
        item = job['items'][0]
        self.api.preregister(item['asset_id'], item_fingerprint(item, '1'))
        self.write_job(job)
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        self.assertEqual(self.item_row(item['asset_id'])['skipped'], 0)
        self.assertEqual(len(self.api.requests_to('/register', 'POST')), 1)
        job_row = self.job_row('catalogue-nightly')
        self.assertIsNotNone(job_row['last_run_at'])
        self.assertEqual(json.loads(job_row['last_result'])['registered'], 0)

    def test_final_run_after_every_other_job_then_finish(self) -> None:
        self.write_job(catalogue_job())
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3),
                                                 plate_item('plate_3d/test/two', frames=3)]))
        worker = self.worker()
        self.assertEqual(worker.run(timeout=120), 0)
        record = self.api.records['catalogue/nightly/catalogue']
        # The catalogue registered last saw both plates.
        self.assertEqual(record['measured']['registered'], 2)
        text = render_status(self.settings)
        self.assertIn('FINISHED', text)
        when = time.strftime('%H:%M', time.localtime(self.job_row('catalogue-nightly')['last_run_at']))
        self.assertIn(f'last run {when}: 2 items, 0 held back; next run after 06:00', text)

    def test_daily_run_after_six_only_when_something_new_was_registered(self) -> None:
        self.write_job(catalogue_job())
        # Another job still has work, so only the daily rule can start the catalogue.
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        worker = self.worker()
        worker.startup()
        worker.maintenance()
        db = worker.db
        db.execute("UPDATE items SET state = 'done' WHERE job_id = 'catalogue-nightly'")
        db.update_job('catalogue-nightly', last_run_at=local_time(-1, 7), last_result=json.dumps(dict(registered=5)))
        worker.refresh_job_states()
        self.assertEqual(self.job_row('catalogue-nightly')['state'], 'waiting')
        self.assertIn('items, 0 held back; next run after 06:00', self.job_row('catalogue-nightly')['reason'])

        # Nothing registered since the last run: no run, even after 06:00.
        worker.check_repeats(now=local_time(0, 6, 30))
        self.assertEqual(self.item_state('catalogue/nightly/catalogue'), 'done')

        # Something registered yesterday at 08:00 (after the last run).
        db.upsert_upload('plate_3d/test/old', 'plates-test', state='confirmed', bytes=10,
                         confirmed_at=local_time(-1, 8))
        worker.check_repeats(now=local_time(0, 5, 30))
        self.assertEqual(self.item_state('catalogue/nightly/catalogue'), 'done', 'not before 06:00')
        worker.check_repeats(now=local_time(0, 6, 30))
        self.assertEqual(self.item_state('catalogue/nightly/catalogue'), 'pending', 'runs after 06:00')

    def test_boundary_is_the_latest_six_oclock(self) -> None:
        self.assertEqual(Worker.last_daily_boundary(local_time(0, 6, 30)), local_time(0, 6))
        self.assertEqual(Worker.last_daily_boundary(local_time(0, 5, 59)), local_time(-1, 6))


if __name__ == '__main__':
    unittest.main()
