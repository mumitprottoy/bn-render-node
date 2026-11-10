"""Render one library item (CONTRACT.md section 3, blender_item).

  blender -b --factory-startup -P blender/render_item.py -- --item <work>/item.json --out <work>/out
      --device HIP|METAL|CPU --samples 32 --assets <root>/assets
  blender -b --factory-startup -P blender/render_item.py -- --selftest --device HIP --assets <root>/assets

The first run builds the scene, bakes any simulation to keyframes and saves the prepared scene to out/scene.blend with
the plate's plan (out/plan.json). A resumed run opens that file instead, so a pause or a power cut never rebuilds or
re-simulates. Frames already in out/frames/ are skipped; each frame is written under a temporary name and renamed when
complete. Prints PROGRESS <done> <total> after each frame, writes out/meta.json at the end and prints DONE.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def parse_args() -> dict:
    args = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else list()
    options = dict(selftest='--selftest' in args)
    for index, arg in enumerate(args):
        if arg.startswith('--') and arg != '--selftest' and index + 1 < len(args):
            options[arg[2:]] = args[index + 1]
    return options


OPTIONS = parse_args()
ITEM = json.loads(Path(OPTIONS['item']).read_text()) if 'item' in OPTIONS else dict(params=dict(template='lane_push'))
PARAMS = ITEM.get('params', dict())
OUT = Path(OPTIONS.get('out', str(HERE / 'selftest-out')))
os.environ['BN_DEVICE'] = OPTIONS.get('device', os.environ.get('BN_DEVICE', 'METAL'))
os.environ['BN_ASSETS'] = OPTIONS.get('assets', os.environ.get('BN_ASSETS', str(HERE)))
os.environ['BN_OUT'] = str(OUT / 'scratch')
# iq_shots reads its arguments at import: <shot> <samples> [--pocket=] [--speed=]
sys.argv = ['blender', '--', PARAMS.get('template', 'item'), str(OPTIONS.get('samples', 32)),
            f"--pocket={PARAMS.get('pocket', 17.5)}", f"--speed={PARAMS.get('ball_speed', 7.3)}"]
sys.path.insert(0, str(HERE))

import bpy  # noqa: E402

import library_shots as lib  # noqa: E402
import lane_scene as ls  # noqa: E402


def apply_device() -> str:
    """Device preferences live outside the .blend, so they are set on every start. Returns the device names used."""
    prefs = bpy.context.preferences.addons['cycles'].preferences
    device_type = os.environ['BN_DEVICE']
    prefs.compute_device_type = 'NONE' if device_type == 'CPU' else device_type
    prefs.get_devices()
    used = list()
    for device in prefs.devices:
        device.use = (device.type == 'CPU') if device_type == 'CPU' else (device.type != 'CPU')
        if device.use:
            used.append(device.name)
    bpy.context.scene.cycles.device = 'CPU' if device_type == 'CPU' or not used else 'GPU'
    return ', '.join(used) or 'CPU'


def selftest() -> None:
    """Build the scene and render one small frame on the requested device; used by `bnrender selfcheck`."""
    started = time.time()
    parts = lib.setup()
    devices = apply_device()
    plate = lib.t_lane_push(parts, dict(duration_s=0.2))
    scene = bpy.context.scene
    scene.camera = plate.camera
    scene.render.resolution_percentage = 25
    scene.cycles.samples = 8
    OUT.mkdir(parents=True, exist_ok=True)
    scene.render.filepath = str(OUT / 'selftest.png')
    render_started = time.time()
    bpy.ops.render.render(write_still=True)
    print(f'SELFTEST device={os.environ["BN_DEVICE"]} devices="{devices}" setup_s={render_started - started:.1f} '
          f'frame_s={time.time() - render_started:.2f} ok={int((OUT / "selftest.png").exists())}', flush=True)


def prepare() -> lib.Plate | dict:
    """Build the plate's scene, or reopen the one saved by an earlier run of this item."""
    blend, plan_path = OUT / 'scene.blend', OUT / 'plan.json'
    if blend.exists() and plan_path.exists():
        bpy.ops.wm.open_mainfile(filepath=str(blend))
        return json.loads(plan_path.read_text())
    parts = lib.setup()
    plate = lib.TEMPLATES[PARAMS['template']](parts, PARAMS)
    data = lib.frame_data(plate)
    plan = dict(camera=plate.camera.name, scene_frames=plate.scene_frames, shutter=plate.shutter, markers=plate.markers,
                required=plate.required, notes=plate.notes, frames=data, template_version=lib.TEMPLATE_VERSION)
    OUT.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(blend))
    temporary = plan_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(plan))
    os.replace(temporary, plan_path)
    return plan


def render(plan: dict) -> None:
    scene = bpy.context.scene
    apply_device()
    scene.cycles.samples = int(OPTIONS.get('samples', 32))
    scene.camera = bpy.data.objects[plan['camera']]
    scene.render.motion_blur_shutter = plan['shutter']
    scene.render.resolution_x, scene.render.resolution_y = lib.PLATE_W, lib.PLATE_H
    scene.render.resolution_percentage = 100
    frames_dir = OUT / 'frames'
    frames_dir.mkdir(parents=True, exist_ok=True)
    total = len(plan['scene_frames'])
    for index, scene_frame in enumerate(plan['scene_frames'], 1):
        final = frames_dir / f'{index:04d}.png'
        if final.exists():
            continue
        partial = frames_dir / f'{index:04d}.partial.png'
        scene.frame_set(scene_frame)
        scene.render.filepath = str(partial)
        bpy.ops.render.render(write_still=True)
        os.replace(partial, final)
        done = sum(1 for _ in frames_dir.glob('[0-9][0-9][0-9][0-9].png'))
        print(f'PROGRESS {done} {total}', flush=True)


def write_meta(plan: dict) -> None:
    frame_count = len(plan['scene_frames'])
    meta = dict(
        fps=lib.FPS, width=lib.PLATE_W, height=lib.PLATE_H,
        safe_crop=dict(x=lib.CROP_X, y=lib.CROP_Y, w=lib.SAFE_W, h=lib.SAFE_H),
        frame_count=frame_count, markers=plan['markers'], subjects_required=plan['required'], frames=plan['frames'],
        measured=dict(duration_s=round(frame_count / lib.FPS, 3), template=PARAMS.get('template'),
                      template_version=plan['template_version'], **plan.get('notes', dict())),
    )
    temporary = OUT / 'meta.tmp'
    temporary.write_text(json.dumps(meta))
    os.replace(temporary, OUT / 'meta.json')


def render_preview(plan: dict) -> None:
    """The first, key and last frames at quarter size and 8 samples, for checking a composition before a full render."""
    scene = bpy.context.scene
    apply_device()
    scene.cycles.samples = 8
    scene.camera = bpy.data.objects[plan['camera']]
    scene.render.resolution_percentage = 25
    count = len(plan['scene_frames'])
    key = plan['markers'].get('contact', plan['markers'].get('touchdown', plan['markers'].get('landing', (count + 1) // 2)))
    folder = OUT / 'preview'
    folder.mkdir(parents=True, exist_ok=True)
    for index in sorted({1, key, count}):
        scene.frame_set(plan['scene_frames'][index - 1])
        scene.render.filepath = str(folder / f'{index:04d}.png')
        bpy.ops.render.render(write_still=True)
    print('PREVIEW', ' '.join(str(index) for index in sorted({1, key, count})), flush=True)


def main() -> None:
    if OPTIONS['selftest']:
        selftest()
        return
    if OPTIONS.get('preview') == '1':
        render_preview(prepare())
        return
    plan = prepare()
    render(plan)
    write_meta(plan)
    print('DONE', flush=True)


try:
    main()
except Exception as error:  # report clearly to the runner, then exit non-zero
    print(f'ERROR {type(error).__name__}: {error}', flush=True)
    import traceback
    traceback.print_exc()
    sys.stdout.flush()
    os._exit(1)
