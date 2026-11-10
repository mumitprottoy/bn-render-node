"""Requirement gating (after, inputs, models), scene asset sync, the label check hook, python jobs and generators."""

import hashlib
import json
import textwrap
import unittest

from helpers import NodeTestCase
from helpers import plate_item
from helpers import plate_job


def python_job(job_id: str, items: list[dict] | None = None, **fields: object) -> dict:
    job = dict(id=job_id, title=f'Job {job_id}', kind='sfx', device='cpu', priority=20, executor='python',
               entry='sample_jobs:make_sound')
    if items is not None:
        job['items'] = items
    job.update(fields)
    return job


def sound_item(slug: str, **extra: object) -> dict:
    item = dict(asset_id=f'sfx/test/{slug}', title=f'Sound {slug}', group='Test sounds', labels=dict(),
                params=dict(), estimate=dict(seconds=2))
    item.update(extra)
    return item


SAMPLE_JOBS = textwrap.dedent('''
    import json
    from pathlib import Path


    def make_sound(item, out):
        print('PROGRESS 1 2', flush=True)
        (out / 'clip.wav').write_bytes(b'RIFF' + bytes(64))
        sources = item.get('input_files') or dict()
        (out / 'info.json').write_text(json.dumps(dict(inputs=sorted(sources))))
        print('PROGRESS 2 2', flush=True)
        return dict(files=[dict(role='audio', path='clip.wav'), dict(role='info', path='info.json')],
                    measured=dict(length_s=1.0, inputs=len(sources)))


    def list_items(job, sources):
        items = list()
        for entry in sources.get('sources/sfx/', list()):
            name = Path(entry['key']).stem
            items.append(dict(asset_id=f'sfx/raw/{name}', title=name, group='Raw sounds', labels=dict(),
                              sources=[entry['key']]))
        return items
''')


class RequirementTest(NodeTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.app / 'sample_jobs.py').write_text(SAMPLE_JOBS)

    def test_after_waits_for_the_other_job(self) -> None:
        self.write_job(plate_job('plates-a', [plate_item('plate_3d/test/a', frames=3)], priority=20))
        self.write_job(plate_job('plates-b', [plate_item('plate_3d/test/b', frames=3)], priority=10,
                                 after=['plates-a']))
        worker = self.worker()
        worker.startup()
        worker.step()
        job_b = self.job_row('plates-b')
        self.assertEqual(job_b['state'], 'waiting')
        self.assertEqual(job_b['reason'], 'after job 2')
        self.run_until(worker, lambda: self.all_items_in('done'))
        first_done = self.item_row('plate_3d/test/a')['done_at']
        second_started = self.item_row('plate_3d/test/b')['started_at']
        self.assertGreater(second_started, self.item_row('plate_3d/test/a')['rendered_at'])
        self.assertIsNotNone(first_done)

    def test_inputs_wait_until_sources_hold_files(self) -> None:
        self.write_job(python_job('sounds', [sound_item('one')], inputs=['sources/sfx/']))
        worker = self.worker()
        worker.startup()
        worker.step()
        job = self.job_row('sounds')
        self.assertEqual(job['state'], 'waiting')
        self.assertEqual(job['reason'], 'waiting for sfx in sources/')
        self.assertEqual(self.item_state('sfx/test/one'), 'pending')
        self.api.add_source('sources/sfx/crash.wav', b'RIFF' + bytes(10))
        worker.maintenance()
        worker.refresh_job_states()
        self.assertEqual(self.job_row('sounds')['state'], 'queued')
        self.run_until(worker, lambda: self.all_items_in('done'))
        record = self.api.records['sfx/test/one']
        self.assertEqual({entry['role'] for entry in record['files']}, {'audio', 'info'})
        self.assertEqual(record['measured']['length_s'], 1.0)

    def test_models_are_installed_and_checked_before_the_job_runs(self) -> None:
        content = b'model weights ' * 100
        self.api.downloads['models/tiny.onnx'] = content
        good = dict(id='tiny', url=f'{self.api.base}/src/models/tiny.onnx', sha256=hashlib.sha256(content).hexdigest(),
                    licence='Apache-2.0', path='tiny/tiny.onnx')
        (self.app / 'library' / 'models.yaml').write_text(json.dumps(dict(models=[good])))
        self.write_job(python_job('sounds', [sound_item('one')], models=['tiny']))
        worker = self.worker()
        worker.startup()
        worker.step()
        self.assertEqual(self.job_row('sounds')['models_ok'], 1)
        self.assertEqual((self.root / 'models' / 'tiny' / 'tiny.onnx').read_bytes(), content)
        self.run_until(worker, lambda: self.all_items_in('done'))

    def test_a_model_with_the_wrong_checksum_is_not_used(self) -> None:
        self.api.downloads['models/tiny.onnx'] = b'tampered'
        spec = dict(id='tiny', url=f'{self.api.base}/src/models/tiny.onnx', sha256='0' * 64, licence='MIT',
                    path='tiny.onnx')
        (self.app / 'library' / 'models.yaml').write_text(json.dumps([spec]))
        self.write_job(python_job('sounds', [sound_item('one')], models=['tiny']))
        worker = self.worker()
        worker.startup()
        worker.step()
        job = self.job_row('sounds')
        self.assertEqual(job['state'], 'waiting')
        self.assertIn('does not match its SHA-256', job['reason'])
        self.assertFalse((self.root / 'models' / 'tiny.onnx').exists())

    def test_a_model_without_a_licence_is_not_used(self) -> None:
        spec = dict(id='tiny', url=f'{self.api.base}/src/models/tiny.onnx', sha256='0' * 64, licence='', path='t.onnx')
        (self.app / 'library' / 'models.yaml').write_text(json.dumps([spec]))
        self.write_job(python_job('sounds', [sound_item('one')], models=['tiny']))
        worker = self.worker()
        worker.startup()
        worker.step()
        self.assertIn('confirmed licence', self.job_row('sounds')['reason'])

    def test_scene_assets_are_synced_before_blender_jobs(self) -> None:
        files = {'sources/assets/hdri/studio.hdr': b'h' * 50, 'sources/assets/textures/wood/color.jpg': b'c' * 30,
                 'sources/assets/textures/wood/rough.jpg': b'r' * 20}
        for key, content in files.items():
            self.api.add_source(key, content)
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)],
                                 inputs=['sources/assets/']))
        worker = self.worker()
        worker.startup()
        worker.step()
        for key, content in files.items():
            self.assertEqual((self.root / 'assets' / key[len('sources/assets/'):]).read_bytes(), content)
        downloads = len(self.api.requests_to('/src/', 'GET'))
        self.assertEqual(downloads, 3)
        worker.maintenance()
        self.assertEqual(len(self.api.requests_to('/src/', 'GET')), downloads)
        self.run_until(worker, lambda: self.all_items_in('done'))
        # Assets are inputs: kept after the item is done, never uploaded.
        self.assertTrue((self.root / 'assets' / 'hdri' / 'studio.hdr').exists())
        self.assertFalse(any('assets' in key for key in self.api.storage))

    def test_label_check_refuses_items_with_errors(self) -> None:
        catalogue = self.app / 'catalogue'
        catalogue.mkdir()
        (catalogue / '__init__.py').write_text('')
        (catalogue / 'taxonomy.py').write_text(textwrap.dedent('''
            def validate_item(kind, labels):
                if labels.get('mood') == 'wrong':
                    return ['"wrong" is not a value of "mood".']
                return []
        '''))
        good = plate_item('plate_3d/test/good', frames=3)
        bad = plate_item('plate_3d/test/bad', frames=3)
        bad['labels']['mood'] = 'wrong'
        self.write_job(plate_job('plates-test', [good, bad]))
        self.run_until(self.worker(), lambda: self.item_state('plate_3d/test/good') == 'done')
        row = self.item_row('plate_3d/test/bad')
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['last_error'], 'Labels: "wrong" is not a value of "mood".')
        self.assertNotIn('plate_3d/test/bad', self.api.records)

    def test_missing_label_check_is_skipped_and_logged(self) -> None:
        self.write_job(plate_job('plates-test', [plate_item('plate_3d/test/one', frames=3)]))
        worker = self.worker()
        worker.startup()
        worker.step()
        messages = [row['message'] for row in worker.db.query('SELECT message FROM events')]
        self.assertTrue(any('labels are not checked' in message for message in messages))
        self.assertEqual(self.item_state('plate_3d/test/one') in ('pending', 'running'), True)

    def test_generator_makes_one_item_per_source_file_and_fetches_it(self) -> None:
        self.api.add_source('sources/sfx/crash.wav', b'RIFF' + bytes(10))
        self.api.add_source('sources/sfx/roll.wav', b'RIFF' + bytes(20))
        self.api.add_source('sources/sfx/cheer.wav', b'RIFF' + bytes(30))
        self.write_job(python_job('raw-sounds', generator='sample_jobs:list_items', inputs=['sources/sfx/']))
        self.run_until(self.worker(), lambda: self.all_items_in('done'))
        self.assertEqual(sorted(asset for asset in self.api.records),
                         ['sfx/raw/cheer', 'sfx/raw/crash', 'sfx/raw/roll'])
        self.assertEqual(self.api.records['sfx/raw/roll']['measured']['inputs'], 1)
        # Fetched inputs go with the work folder once the item is registered.
        self.assertFalse(any((self.root / 'work' / 'raw-sounds').glob('*')))


if __name__ == '__main__':
    unittest.main()
