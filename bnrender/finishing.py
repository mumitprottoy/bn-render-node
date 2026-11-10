"""Finishing (CONTRACT section 5): turn a rendered item into the files that are uploaded, after the checks pass.

A blender item's frames become video.mp4, preview.mp4, sheet.jpg and frames.json. A python item's files are taken as
its out/meta.json lists them. Either way the result is final/manifest.json, which the upload step reads.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy
from PIL import Image
from PIL import ImageDraw

from .api import file_digest
from .executors import PAUSE_NOW
from .executors import UnitControl
from .procs import child_popen_options
from .settings import Settings

logger = logging.getLogger(__name__)

FRAME_NAME_REGEX = re.compile(r'^(\d+)\.png$')
ROLE_REGEX = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')
# The backend's STUDIO_FILE_EXTENSIONS and per-item limits.
ALLOWED_EXTENSIONS = ('.mp4', '.mkv', '.mov', '.webm', '.png', '.jpg', '.jpeg', '.webp', '.wav', '.flac', '.mp3',
                      '.json', '.npy', '.txt')
MAX_FILES = 40
MAX_FILE_BYTES = 4 * 1024 ** 3
EXPOSURE_MIN = 0.06
EXPOSURE_MAX = 0.85
CLIPPED_MAX = 0.02
# A pixel counts as a clipped highlight when even its darkest channel is blown (near pure white).
CLIP_LEVEL = 250 / 255
EXPOSURE_EVERY = 10
PREVIEW_WIDTH, PREVIEW_HEIGHT = 540, 960
SHEET_THUMB_WIDTH = 216
SHEET_COLUMNS = 6
VIDEO_CRF = 12
PREVIEW_CRF = 28
MANIFEST = 'manifest.json'


class FinishingError(Exception):
    """A check failed or a file could not be made; the item fails with this message and nothing is uploaded."""


class FinishingInterrupted(Exception):
    """Stopped by a second `bn pause`; the item is finished again on resume."""


def list_frames(frames_dir: Path) -> list[tuple[int, Path]]:
    if not frames_dir.is_dir():
        return list()
    frames = list()
    for path in frames_dir.iterdir():
        match = FRAME_NAME_REGEX.match(path.name)
        if match is not None:
            frames.append((int(match.group(1)), path))
    return sorted(frames)


def safe_crop_of(meta: dict) -> tuple[int, int, int, int]:
    width, height = int(meta.get('width') or 0), int(meta.get('height') or 0)
    crop = meta.get('safe_crop') or dict()
    try:
        left, top, crop_width, crop_height = int(crop['x']), int(crop['y']), int(crop['w']), int(crop['h'])
    except (KeyError, TypeError, ValueError) as error:
        raise FinishingError('meta.json has no usable safe_crop.') from error
    if left < 0 or top < 0 or crop_width <= 0 or crop_height <= 0 or left + crop_width > width or top + crop_height > height:
        raise FinishingError('The safe crop in meta.json does not fit inside the frame.')
    return left, top, crop_width, crop_height


def check_frames(frames_dir: Path, meta: dict) -> list[Path]:
    """Frame count equals frame_count, numbered 1 to N with no gaps, and every PNG decodes."""
    try:
        frame_count = int(meta['frame_count'])
    except (KeyError, TypeError, ValueError) as error:
        raise FinishingError('meta.json has no frame_count.') from error
    frames = list_frames(frames_dir)
    if len(frames) != frame_count:
        raise FinishingError(f'{len(frames)} frames on disk, but meta.json says {frame_count}.')
    if [number for number, _ in frames] != list(range(1, frame_count + 1)):
        raise FinishingError(f'The frames are not numbered 1 to {frame_count} without gaps.')
    for number, path in frames:
        try:
            with Image.open(path) as image:
                image.load()
        except Exception as error:
            raise FinishingError(f'Frame {number} does not decode: {error}') from error
    return [path for _, path in frames]


def _luminance(path: Path, crop: tuple[int, int, int, int]) -> numpy.ndarray:
    left, top, crop_width, crop_height = crop
    with Image.open(path) as image:
        pixels = numpy.asarray(image.convert('RGB').crop((left, top, left + crop_width, top + crop_height)),
                               dtype=numpy.float32) / 255.0
    return pixels


def check_exposure(frames: list[Path], crop: tuple[int, int, int, int]) -> dict:
    """On every 10th frame: mean luminance of the safe crop between 0.06 and 0.85, clipped highlights under 2%."""
    means: list[float] = list()
    clipped_shares: list[float] = list()
    for index in range(0, len(frames), EXPOSURE_EVERY):
        pixels = _luminance(frames[index], crop)
        luminance = 0.2126 * pixels[..., 0] + 0.7152 * pixels[..., 1] + 0.0722 * pixels[..., 2]
        mean = float(luminance.mean())
        clipped = float((pixels.min(axis=2) >= CLIP_LEVEL).mean())
        frame_number = index + 1
        if mean < EXPOSURE_MIN:
            raise FinishingError(f'Frame {frame_number} is too dark (mean luminance {mean:.3f}, at least {EXPOSURE_MIN}).')
        if mean > EXPOSURE_MAX:
            raise FinishingError(f'Frame {frame_number} is too bright (mean luminance {mean:.3f}, at most {EXPOSURE_MAX}).')
        if clipped >= CLIPPED_MAX:
            raise FinishingError(f'Frame {frame_number} has {clipped * 100:.1f}% clipped highlights (under 2% allowed).')
        means.append(mean)
        clipped_shares.append(clipped)
    return dict(frames_checked=len(means), mean_min=round(min(means), 4), mean_max=round(max(means), 4),
                clipped_max=round(max(clipped_shares), 5))


def _box_bounds(box: object) -> tuple[float, float, float, float] | None:
    if not isinstance(box, (list, tuple)) or not box:
        return None
    if all(isinstance(value, (int, float)) for value in box) and len(box) == 4:
        return float(box[0]), float(box[1]), float(box[2]), float(box[3])
    if all(isinstance(point, (list, tuple)) and len(point) == 2 for point in box):
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        return min(xs), min(ys), max(xs), max(ys)
    return None


def check_subjects(meta: dict, crop: tuple[int, int, int, int]) -> list[str]:
    """Every required subject's box overlaps the safe crop on every frame (a null box means not visible)."""
    required = meta.get('subjects_required') or list()
    if not required:
        return list()
    frames = meta.get('frames')
    frame_count = int(meta.get('frame_count') or 0)
    if not isinstance(frames, list) or len(frames) != frame_count:
        raise FinishingError(f'meta.json has frame data for {len(frames) if isinstance(frames, list) else 0} frames, '
                             f'expected {frame_count}.')
    left, top, crop_width, crop_height = crop
    for index, frame in enumerate(frames, 1):
        boxes = frame.get('boxes') if isinstance(frame, dict) else None
        for subject in required:
            bounds = _box_bounds((boxes or dict()).get(subject))
            if bounds is None:
                raise FinishingError(f'The {subject} is not visible on frame {index} (no box).')
            x0, y0, x1, y1 = bounds
            overlaps = x0 < left + crop_width and x1 > left and y0 < top + crop_height and y1 > top
            if not overlaps:
                raise FinishingError(f'The {subject} is outside the safe crop on frame {index}.')
    return list(required)


def _run_ffmpeg(settings: Settings, arguments: list[str], control: UnitControl, what: str) -> None:
    command = [settings.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *arguments]
    try:
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                   **child_popen_options())
    except OSError as error:
        raise FinishingError(f'ffmpeg could not be started ({error}).') from error
    if not control.attach(process):
        process.wait()
        raise FinishingInterrupted()
    try:
        _, error_output = process.communicate()
    finally:
        control.detach()
    if control.level >= PAUSE_NOW:
        raise FinishingInterrupted()
    if process.returncode != 0:
        detail = error_output.decode('utf-8', errors='replace').strip().splitlines()[-1:] if error_output else list()
        raise FinishingError(f'ffmpeg could not make the {what}' + (f': {detail[0][:300]}' if detail else '.'))


COLOUR_TAGS = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv']
# Recent ffmpeg carries each frame's own colour properties to the encoder, overriding the flags above, so the frames
# are tagged bt709 in the filter chain as well.
TAG_FRAMES = 'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv'


def make_video(settings: Settings, frames_dir: Path, fps: int, out_path: Path, control: UnitControl) -> None:
    _run_ffmpeg(settings, [
        '-framerate', str(fps), '-start_number', '1', '-i', str(frames_dir / '%04d.png'),
        '-vf', f'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,{TAG_FRAMES}',
        '-c:v', 'libx264', '-preset', 'medium', '-crf', str(VIDEO_CRF), '-pix_fmt', 'yuv420p', *COLOUR_TAGS,
        '-r', str(fps), '-movflags', '+faststart', '-threads', str(settings.finish_threads), str(out_path),
    ], control, 'video')


def make_preview(settings: Settings, frames_dir: Path, fps: int, crop: tuple[int, int, int, int], out_path: Path,
                 control: UnitControl) -> None:
    left, top, crop_width, crop_height = crop
    _run_ffmpeg(settings, [
        '-framerate', str(fps), '-start_number', '1', '-i', str(frames_dir / '%04d.png'),
        '-vf', (f'crop={crop_width}:{crop_height}:{left}:{top},'
                f'scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:flags=lanczos:out_color_matrix=bt709:out_range=tv,'
                f'format=yuv420p,{TAG_FRAMES}'),
        '-c:v', 'libx264', '-preset', 'medium', '-crf', str(PREVIEW_CRF), '-pix_fmt', 'yuv420p', *COLOUR_TAGS,
        '-r', str(fps), '-movflags', '+faststart', '-threads', str(settings.finish_threads), str(out_path),
    ], control, 'preview')


def make_sheet(frames: list[Path], fps: int, crop: tuple[int, int, int, int], out_path: Path) -> int:
    """One frame per second of the safe crop, in a grid, each labelled with its time."""
    left, top, crop_width, crop_height = crop
    picks = list(range(0, len(frames), max(1, fps)))
    thumb_height = round(SHEET_THUMB_WIDTH * crop_height / crop_width)
    label_height = 18
    columns = min(SHEET_COLUMNS, len(picks))
    rows = (len(picks) + columns - 1) // columns
    gap = 6
    sheet = Image.new('RGB', (columns * (SHEET_THUMB_WIDTH + gap) + gap,
                              rows * (thumb_height + label_height + gap) + gap), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for position, index in enumerate(picks):
        with Image.open(frames[index]) as image:
            thumb = image.convert('RGB').crop((left, top, left + crop_width, top + crop_height))
            thumb = thumb.resize((SHEET_THUMB_WIDTH, thumb_height), Image.Resampling.LANCZOS)
        column, row = position % columns, position // columns
        x = gap + column * (SHEET_THUMB_WIDTH + gap)
        y = gap + row * (thumb_height + label_height + gap)
        sheet.paste(thumb, (x, y))
        draw.text((x + 2, y + thumb_height + 3), f'{index / max(1, fps):.1f} s  (frame {index + 1})',
                  fill=(220, 220, 220))
    sheet.save(out_path, 'JPEG', quality=88)
    return len(picks)


def _manifest_entry(role: str, path: Path, work_dir: Path) -> dict:
    size, sha256 = file_digest(path)
    return dict(role=role, filename=path.name, path=str(path.relative_to(work_dir)).replace(os.sep, '/'), size=size,
                sha256=sha256)


def write_manifest(work_dir: Path, files: list[dict], measured: dict) -> dict:
    manifest = dict(files=files, measured=measured)
    final_dir = work_dir / 'final'
    final_dir.mkdir(parents=True, exist_ok=True)
    temporary = final_dir / (MANIFEST + '.tmp')
    temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    os.replace(temporary, final_dir / MANIFEST)
    return manifest


def read_manifest(work_dir: Path) -> dict | None:
    try:
        manifest = json.loads((work_dir / 'final' / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) and isinstance(manifest.get('files'), list) else None


def read_meta(out_dir: Path) -> dict:
    try:
        meta = json.loads((out_dir / 'meta.json').read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise FinishingError('out/meta.json is missing.') from error
    except (OSError, ValueError) as error:
        raise FinishingError(f'out/meta.json could not be read: {error}') from error
    if not isinstance(meta, dict):
        raise FinishingError('out/meta.json is not an object.')
    return meta


def finish_plate(settings: Settings, work_dir: Path, control: UnitControl) -> dict:
    out_dir = work_dir / 'out'
    meta = read_meta(out_dir)
    crop = safe_crop_of(meta)
    frames = check_frames(out_dir / 'frames', meta)
    exposure = check_exposure(frames, crop)
    subjects = check_subjects(meta, crop)
    fps = int(meta.get('fps') or 30)
    final_dir = work_dir / 'final'
    if final_dir.exists():
        shutil.rmtree(final_dir)
    final_dir.mkdir(parents=True)
    make_video(settings, out_dir / 'frames', fps, final_dir / 'video.mp4', control)
    shutil.copyfile(out_dir / 'meta.json', final_dir / 'frames.json')
    make_preview(settings, out_dir / 'frames', fps, crop, final_dir / 'preview.mp4', control)
    sheet_frames = make_sheet(frames, fps, crop, final_dir / 'sheet.jpg')
    measured = dict(meta.get('measured') or dict())
    for name in ('fps', 'width', 'height', 'frame_count', 'safe_crop', 'markers'):
        if name in meta:
            measured.setdefault(name, meta[name])
    measured['checks'] = dict(frames='passed', exposure=exposure, subjects=subjects, sheet_frames=sheet_frames)
    files = [_manifest_entry(role, final_dir / filename, work_dir) for role, filename in
             (('video', 'video.mp4'), ('frames', 'frames.json'), ('preview', 'preview.mp4'), ('sheet', 'sheet.jpg'))]
    return write_manifest(work_dir, files, measured)


def finish_python(work_dir: Path) -> dict:
    out_dir = work_dir / 'out'
    meta = read_meta(out_dir)
    entries = meta.get('files')
    if not isinstance(entries, list) or not entries:
        raise FinishingError('out/meta.json lists no files.')
    if len(entries) > MAX_FILES:
        raise FinishingError(f'The item has {len(entries)} files; the library takes at most {MAX_FILES}.')
    files: list[dict] = list()
    roles: set[str] = set()
    for entry in entries:
        role = str(entry.get('role') or str()) if isinstance(entry, dict) else str()
        raw_path = str(entry.get('path') or str()) if isinstance(entry, dict) else str()
        if not ROLE_REGEX.match(role) or role in roles:
            raise FinishingError(f'File role "{role}" is not usable (lowercase, unique, up to 40 characters).')
        path = Path(raw_path) if Path(raw_path).is_absolute() else out_dir / raw_path
        if not path.is_file():
            raise FinishingError(f'The {role} file is missing ({raw_path}).')
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            raise FinishingError(f'The {role} file type {path.suffix} is not accepted by the library.')
        size = path.stat().st_size
        if size <= 0 or size > MAX_FILE_BYTES:
            raise FinishingError(f'The {role} file is empty or larger than 4 GB.')
        roles.add(role)
        resolved = path.resolve()
        try:
            files.append(_manifest_entry(role, resolved, work_dir.resolve()))
        except ValueError:
            # A file outside the work folder: keep its absolute path.
            size_value, sha256 = file_digest(resolved)
            files.append(dict(role=role, filename=resolved.name, path=str(resolved), size=size_value, sha256=sha256))
    measured = meta.get('measured') or dict()
    if not isinstance(measured, dict):
        raise FinishingError('"measured" in out/meta.json must be an object.')
    return write_manifest(work_dir, files, measured)
