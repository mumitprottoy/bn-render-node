"""Finishing checks (CONTRACT section 5): frame count and decoding, exposure, required subjects in the safe crop."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import FFMPEG
from helpers import FFPROBE

from PIL import Image
from PIL import ImageDraw

from bnrender.executors import UnitControl
from bnrender.finishing import FinishingError
from bnrender.finishing import finish_plate
from bnrender.finishing import finish_python
from bnrender.settings import Settings

WIDTH, HEIGHT = 72, 128
CROP = dict(x=6, y=11, w=60, h=106)


class FinishingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix='bnrender-finish-'))
        self.work = self.temp / 'item'
        self.frames = self.work / 'out' / 'frames'
        self.frames.mkdir(parents=True)
        self.settings = Settings(root=self.temp, app_dir=self.temp, blender_command=['blender'], python_command=['python'],
                                 ffmpeg=FFMPEG, ffprobe=FFPROBE, gpu_device='CPU', finish_threads=2)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def make_plate(self, count: int = 12, grey: int = 120, box: object = (30, 60, 38, 68), white_share: float = 0.0,
                   meta_changes: dict | None = None) -> None:
        for index in range(1, count + 1):
            image = Image.new('RGB', (WIDTH, HEIGHT), (grey, grey, grey))
            if white_share:
                rows = int(CROP['h'] * white_share) + 1
                ImageDraw.Draw(image).rectangle((CROP['x'], CROP['y'], CROP['x'] + CROP['w'] - 1, CROP['y'] + rows),
                                                fill=(255, 255, 255))
            image.save(self.frames / f'{index:04d}.png')
        meta = dict(fps=3, width=WIDTH, height=HEIGHT, safe_crop=CROP, frame_count=count, markers=dict(contact=2),
                    subjects_required=['ball'],
                    frames=[dict(boxes=dict(ball=list(box) if box is not None else None)) for _ in range(count)],
                    measured=dict(duration_s=count / 3))
        meta.update(meta_changes or dict())
        (self.work / 'out' / 'meta.json').write_text(json.dumps(meta))

    def finish(self) -> dict:
        return finish_plate(self.settings, self.work, UnitControl())

    def test_a_good_plate_makes_four_files(self) -> None:
        self.make_plate()
        manifest = self.finish()
        self.assertEqual([entry['role'] for entry in manifest['files']], ['video', 'frames', 'preview', 'sheet'])
        self.assertEqual(manifest['measured']['frame_count'], 12)
        self.assertEqual(manifest['measured']['checks']['sheet_frames'], 4)
        frames_json = json.loads((self.work / 'final' / 'frames.json').read_text())
        self.assertEqual(frames_json, json.loads((self.work / 'out' / 'meta.json').read_text()))
        with Image.open(self.work / 'final' / 'sheet.jpg') as sheet:
            self.assertGreater(sheet.width, 0)

    def test_too_dark_fails(self) -> None:
        self.make_plate(grey=5)
        with self.assertRaisesRegex(FinishingError, 'too dark'):
            self.finish()

    def test_too_bright_fails(self) -> None:
        self.make_plate(grey=240)
        with self.assertRaisesRegex(FinishingError, 'too bright'):
            self.finish()

    def test_clipped_highlights_fail(self) -> None:
        self.make_plate(white_share=0.05)
        with self.assertRaisesRegex(FinishingError, 'clipped highlights'):
            self.finish()

    def test_missing_frame_fails(self) -> None:
        self.make_plate()
        (self.frames / '0005.png').unlink()
        with self.assertRaisesRegex(FinishingError, '11 frames on disk'):
            self.finish()

    def test_gap_in_numbering_fails(self) -> None:
        self.make_plate()
        (self.frames / '0005.png').rename(self.frames / '0013.png')
        with self.assertRaisesRegex(FinishingError, 'without gaps'):
            self.finish()

    def test_frame_that_does_not_decode_fails(self) -> None:
        self.make_plate()
        (self.frames / '0007.png').write_bytes(b'\x89PNG\r\n\x1a\n broken')
        with self.assertRaisesRegex(FinishingError, 'Frame 7 does not decode'):
            self.finish()

    def test_partial_frames_are_not_counted(self) -> None:
        self.make_plate()
        (self.frames / '0013.partial.png').write_bytes(b'not finished')
        self.assertEqual(len(self.finish()['files']), 4)

    def test_subject_outside_the_safe_crop_fails(self) -> None:
        self.make_plate(box=(0, 0, 4, 8))
        with self.assertRaisesRegex(FinishingError, 'outside the safe crop on frame 1'):
            self.finish()

    def test_null_box_means_not_visible(self) -> None:
        self.make_plate(box=None)
        with self.assertRaisesRegex(FinishingError, 'not visible on frame 1'):
            self.finish()

    def test_box_extending_past_the_frame_but_overlapping_the_crop_passes(self) -> None:
        self.make_plate(box=(-1632, 50, 3604, 3891))
        self.assertEqual(len(self.finish()['files']), 4)

    def test_python_item_files_are_taken_as_listed(self) -> None:
        out = self.work / 'out'
        (out / 'bed.wav').write_bytes(b'RIFF' + bytes(100))
        (out / 'grid.json').write_text('{"beats": []}')
        (out / 'meta.json').write_text(json.dumps(dict(files=[dict(role='audio', path='bed.wav'),
                                                             dict(role='beats', path='grid.json')],
                                                       measured=dict(tempo=92))))
        manifest = finish_python(self.work)
        self.assertEqual([entry['role'] for entry in manifest['files']], ['audio', 'beats'])
        self.assertEqual(manifest['measured'], dict(tempo=92))

    def test_python_item_with_unaccepted_file_type_fails(self) -> None:
        out = self.work / 'out'
        (out / 'thing.exe').write_bytes(b'x')
        (out / 'meta.json').write_text(json.dumps(dict(files=[dict(role='thing', path='thing.exe')])))
        with self.assertRaisesRegex(FinishingError, 'not accepted'):
            finish_python(self.work)


if __name__ == '__main__':
    unittest.main()
