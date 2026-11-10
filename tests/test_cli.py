"""The three commands as Boss types them (`python -m bnrender status|pause|resume`), and the real first job file run
end to end against the fake API with the fake Blender."""

import os
import shutil
import subprocess
import sys
import unittest

from helpers import FAKE_BLENDER
from helpers import REPO
from helpers import NodeTestCase

from bnrender.control import read_pause
from bnrender.jobs import item_fingerprint
from bnrender.jobs import load_job_files
from bnrender.jobs import resolve_template_version
from bnrender.jobs import taxonomy_version

REAL_JOB = REPO / 'library' / 'jobs' / 'plates-league-night.yaml'
REAL_CATALOGUE_JOB = REPO / 'library' / 'jobs' / 'catalogue-nightly.yaml'


class CommandTest(NodeTestCase):
    def bn(self, *arguments: str) -> str:
        environment = dict(os.environ, BN_RENDER_ROOT=str(self.root), BN_APP_DIR=str(self.app), PYTHONPATH=str(REPO))
        completed = subprocess.run([sys.executable, '-m', 'bnrender', *arguments], cwd=str(REPO), env=environment,
                                   capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def test_pause_twice_then_resume(self) -> None:
        first = self.bn('pause')
        self.assertIn('the node stays paused, even after a restart', first)
        self.assertEqual(read_pause(self.settings).count, 1)
        second = self.bn('pause')
        self.assertIn('Stopping the unit in progress now', second)
        self.assertEqual(read_pause(self.settings).count, 2)
        resumed = self.bn('resume')
        self.assertIn('Resumed.', resumed)
        self.assertIsNone(read_pause(self.settings))
        self.assertIn('The node was not paused.', self.bn('resume'))

    def test_status_before_the_first_run(self) -> None:
        self.assertIn('NOT STARTED', self.bn('status'))


@unittest.skipUnless(REAL_JOB.exists(), 'library/jobs/plates-league-night.yaml is not in the repo')
class RealJobFileTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        shutil.copy(REAL_JOB, self.app / 'library' / 'jobs' / REAL_JOB.name)
        if REAL_CATALOGUE_JOB.exists():
            shutil.copy(REAL_CATALOGUE_JOB, self.app / 'library' / 'jobs' / REAL_CATALOGUE_JOB.name)
        shutil.copy(REPO / 'library' / 'taxonomy.yaml', self.app / 'library' / 'taxonomy.yaml')
        if (REPO / 'catalogue').is_dir():
            shutil.copytree(REPO / 'catalogue', self.app / 'catalogue', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy(REPO / 'blender' / 'library_shots.py', self.app / 'blender' / 'library_shots.py')
        self.api.add_source('sources/assets/hdri/studio_small_09_2k.hdr', b'h' * 300)
        self.api.add_source('sources/assets/textures/Wood050/Wood050_2K-JPG_Color.jpg', b'c' * 200)
        self.api.add_source('sources/assets/textures/Wood095/Wood095_2K-JPG_Color.jpg', b'c' * 100)
        os.environ['FAKE_FRAMES'] = '3'
        self.addCleanup(os.environ.pop, 'FAKE_FRAMES', None)

    def test_the_real_job_files_run_to_finished(self) -> None:
        jobs, problems = load_job_files(self.app / 'library' / 'jobs')
        self.assertEqual(problems, list())
        job = next(job for job in jobs if job.id == 'plates-league-night')
        worker = self.worker()
        self.assertEqual(worker.run(timeout=240), 0)
        template_version = resolve_template_version(job, self.app)
        plates = {asset_id: record for asset_id, record in self.api.records.items() if asset_id.startswith('plate_3d/')}
        self.assertEqual(len(plates), len(job.items))
        if REAL_CATALOGUE_JOB.exists():
            catalogue = self.api.records['catalogue/nightly/catalogue']
            self.assertEqual({entry['role'] for entry in catalogue['files']}, {'catalogue', 'report', 'report_text'})
            # The final catalogue run saw every plate registered.
            self.assertEqual(catalogue['measured']['registered'], len(job.items))
        for item in job.items:
            record = self.api.records[item['asset_id']]
            self.assertEqual(record['job_fingerprint'], item_fingerprint(item, template_version))
            self.assertEqual(record['labels'], item['labels'])
            self.assertEqual(record['taxonomy_version'], taxonomy_version(self.app))
            self.assertEqual(record['measured']['made']['template_version'], template_version)
        self.assertNotIn('BN_API_KEY', (self.root / '.env').read_text())
        self.assertFalse((self.root / 'work').exists())


if __name__ == '__main__':
    unittest.main()
