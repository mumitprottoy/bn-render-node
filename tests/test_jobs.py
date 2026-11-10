"""Job files: reading and refusing, fingerprints, template versions, item folders."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import FIXTURES
from helpers import plate_item
from helpers import plate_job

from bnrender.jobs import JobFileError
from bnrender.jobs import check_item_shape
from bnrender.jobs import item_fingerprint
from bnrender.jobs import item_slug
from bnrender.jobs import load_job_files
from bnrender.jobs import parse_job
from bnrender.jobs import resolve_template_version


class JobFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix='bnrender-jobs-'))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_fixture_job_file_reads(self) -> None:
        jobs, problems = load_job_files(FIXTURES / 'jobs')
        self.assertEqual(problems, list())
        self.assertEqual([job.id for job in jobs], ['selftest-plates'])
        job = jobs[0]
        self.assertEqual((job.device, job.executor, job.entry, job.after, job.inputs), (
            'gpu', 'blender_item', 'render_item.py', list(), ['sources/assets/']))
        self.assertEqual(len(job.items), 2)

    def test_bad_job_files_are_reported_not_fatal(self) -> None:
        folder = self.temp / 'jobs'
        folder.mkdir()
        (folder / 'good-one.yaml').write_text(json.dumps(plate_job('good-one', [plate_item('plate_3d/a/b')])))
        (folder / 'wrong-name.yaml').write_text(json.dumps(plate_job('other-name', [plate_item('plate_3d/a/c')])))
        (folder / 'no-items.yaml').write_text(json.dumps(dict(id='no-items', kind='plate_3d', device='gpu',
                                                              executor='blender_item', entry='render_item.py')))
        (folder / 'broken.yaml').write_text('{ not: [valid')
        jobs, problems = load_job_files(folder)
        self.assertEqual([job.id for job in jobs], ['good-one'])
        self.assertEqual(sorted(name for name, _ in problems), ['broken', 'no-items', 'wrong-name'])

    def test_parse_refuses_unknown_executor_and_device(self) -> None:
        with self.assertRaisesRegex(JobFileError, 'executor'):
            parse_job(plate_job('x', list(), executor='shell'), Path('x.yaml'))
        with self.assertRaisesRegex(JobFileError, 'device'):
            parse_job(plate_job('x', list(), device='tpu'), Path('x.yaml'))

    def test_fingerprint_follows_labels_and_params_not_the_estimate(self) -> None:
        item = plate_item('plate_3d/test/one')
        base = item_fingerprint(item, '1')
        self.assertEqual(base, item_fingerprint(json.loads(json.dumps(item)), '1'))
        changed_labels = json.loads(json.dumps(item))
        changed_labels['labels']['mood'] = 'cosmic'
        self.assertNotEqual(base, item_fingerprint(changed_labels, '1'))
        changed_params = json.loads(json.dumps(item))
        changed_params['params']['speed'] = 2
        self.assertNotEqual(base, item_fingerprint(changed_params, '1'))
        changed_estimate = json.loads(json.dumps(item))
        changed_estimate['estimate'] = dict(frames=999)
        self.assertEqual(base, item_fingerprint(changed_estimate, '1'))
        self.assertNotEqual(base, item_fingerprint(item, '2'))
        reordered = dict(reversed(list(item.items())))
        self.assertEqual(base, item_fingerprint(reordered, '1'))

    def test_template_version_comes_from_the_blender_scripts(self) -> None:
        app = self.temp / 'app'
        (app / 'blender').mkdir(parents=True)
        (app / 'blender' / 'render_item.py').write_text('import library_shots\n')
        (app / 'blender' / 'library_shots.py').write_text('X = 1\nTEMPLATE_VERSION = 4\n')
        job = parse_job(plate_job('x', [plate_item('plate_3d/a/b')]), Path('x.yaml'))
        self.assertEqual(resolve_template_version(job, app), '4')
        pinned = parse_job(plate_job('x', [plate_item('plate_3d/a/b')], template_version='7b'), Path('x.yaml'))
        self.assertEqual(resolve_template_version(pinned, app), '7b')

    def test_item_shape_and_slug(self) -> None:
        job = parse_job(plate_job('x', list()), Path('x.yaml'))
        self.assertEqual(check_item_shape(job, plate_item('plate_3d/strike/pin-cam-slow')), list())
        self.assertTrue(check_item_shape(job, plate_item('sfx/strike/pin-cam-slow')))
        self.assertTrue(check_item_shape(job, plate_item('plate_3d/Strike/Pin Cam')))
        self.assertEqual(item_slug('plate_3d/strike/pocket-strike-pin-cam'), 'strike__pocket-strike-pin-cam')


if __name__ == '__main__':
    unittest.main()
