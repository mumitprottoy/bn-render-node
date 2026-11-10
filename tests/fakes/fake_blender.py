"""A stand-in for `blender -b --factory-startup -P render_item.py -- ...` that follows the contract:
frames to out/frames/0001.png (temporary name, then rename), skipping frames already present, `PROGRESS <done> <total>`
per frame, out/meta.json and `DONE` at the end, exit 0 only on success.

Behaviour is steered by the item's params (fake_* keys) and the FAKE_FRAMES environment variable (caps the frame
count). Every finished frame is appended to out/render_log.txt so tests can find duplicates.
"""

import json
import os
import sys
import time
from pathlib import Path

from PIL import Image
from PIL import ImageDraw

WIDTH, HEIGHT = 72, 128
CROP = dict(x=6, y=11, w=60, h=106)


def options() -> dict:
    arguments = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else list()
    parsed = dict(selftest='--selftest' in arguments)
    for index, argument in enumerate(arguments):
        if argument.startswith('--') and argument != '--selftest' and index + 1 < len(arguments):
            parsed[argument[2:]] = arguments[index + 1]
    return parsed


def main() -> int:
    parsed = options()
    if parsed['selftest']:
        print('SELFTEST device=METAL devices="Fake GPU" setup_s=0.1 frame_s=0.05 ok=1', flush=True)
        return 0
    item = json.loads(Path(parsed['item']).read_text())
    params = item.get('params') or dict()
    out = Path(parsed['out'])
    frames_dir = out / 'frames'
    frames_dir.mkdir(parents=True, exist_ok=True)
    total = int(params.get('fake_frames') or (item.get('estimate') or dict()).get('frames') or 12)
    if os.environ.get('FAKE_FRAMES'):
        total = min(total, int(os.environ['FAKE_FRAMES']))
    # Internal resume files, as render_item.py writes them; they must never be uploaded.
    (out / 'scene.blend').write_bytes(b'blend')
    (out / 'plan.json').write_text('{}')
    counter = out / 'launches.txt'
    launches = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(launches))
    if launches <= int(params.get('fake_oom_launches') or 0):
        print('Error: System is out of GPU memory', flush=True)
        print('ERROR RuntimeError: Out of GPU memory', flush=True)
        return 1
    if params.get('fake_crash'):
        print('ERROR KeyError: template', flush=True)
        return 1
    brightness = int(params.get('fake_brightness', 120))
    seconds = float(params.get('fake_frame_seconds', 0.02))
    ball_left = int(params.get('fake_ball_left', 30))
    for index in range(1, total + 1):
        final = frames_dir / f'{index:04d}.png'
        if final.exists():
            continue
        partial = frames_dir / f'{index:04d}.partial.png'
        # A half-written frame sits under the temporary name while the frame "renders".
        partial.write_bytes(b'not finished')
        time.sleep(seconds)
        image = Image.new('RGB', (WIDTH, HEIGHT), (brightness, brightness, brightness))
        ImageDraw.Draw(image).rectangle((ball_left, 60, ball_left + 8, 68), fill=(30, 140, 60))
        image.save(partial, format='PNG')
        os.replace(partial, final)
        with open(os.environ.get('FAKE_LOG') or out / 'render_log.txt', 'a') as log:
            log.write(f'{index}\n')
        done = sum(1 for _ in frames_dir.glob('[0-9][0-9][0-9][0-9].png'))
        print(f'PROGRESS {done} {total}', flush=True)
    box = params.get('fake_box', [ball_left, 60, ball_left + 8, 68])
    meta = dict(fps=int(params.get('fake_fps', 3)), width=WIDTH, height=HEIGHT, safe_crop=CROP, frame_count=total,
                markers=dict(contact=1), subjects_required=['ball'],
                frames=[dict(camera=dict(lens=40), boxes=dict(ball=box)) for _ in range(total)],
                measured=dict(duration_s=round(total / 3, 3)))
    temporary = out / 'meta.tmp'
    temporary.write_text(json.dumps(meta))
    os.replace(temporary, out / 'meta.json')
    print('DONE', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
