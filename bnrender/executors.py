"""Running one item (CONTRACT section 3): `blender_item` or `python`, as a child process whose output is followed line
by line for `PROGRESS <done> <total>`.

Pausing: the first pause lets the frame in progress finish (a blender item is ended right after its next PROGRESS
line; a python item, whose unit is the whole item, runs to its end); the second ends the process at once.
"""

import dataclasses
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .procs import child_environment
from .procs import child_popen_options
from .procs import kill_tree
from .procs import process_started_at
from .settings import Settings

PROGRESS_REGEX = re.compile(r'^\s*PROGRESS\s+(\d+)\s+(\d+)\s*$')
OOM_REGEX = re.compile(
    r'out of (gpu |device |video |graphics )?memory|hipErrorOutOfMemory|CUDA_ERROR_OUT_OF_MEMORY|'
    r'cudaErrorMemoryAllocation|System is out of GPU',
    re.IGNORECASE,
)
# A PROGRESS line within this many seconds of the previous one is a skipped frame being reported, not a render.
MIN_SAMPLE_SECONDS = 0.01
PACKAGE_PARENT = Path(__file__).resolve().parent.parent

PAUSE_AFTER_UNIT = 1
PAUSE_NOW = 2


class UnitControl:
    """Shared between the scheduler and the thread running a unit: how far to stop, and the live process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.level = 0
        self.reason = str()
        self.process: subprocess.Popen | None = None

    def request(self, level: int, reason: str) -> None:
        with self._lock:
            if level <= self.level:
                return
            self.level = level
            self.reason = reason
            process = self.process
        if level >= PAUSE_NOW and process is not None and process.poll() is None:
            kill_tree(process.pid)

    def cancel_pause(self) -> None:
        """`bn resume` before the frame in progress finished: carry on instead of stopping."""
        with self._lock:
            if self.level == PAUSE_AFTER_UNIT:
                self.level = 0
                self.reason = str()

    def attach(self, process: subprocess.Popen) -> bool:
        """Remember the process; returns False (and ends it) if a stop-now came in while it was starting."""
        with self._lock:
            self.process = process
            stop_now = self.level >= PAUSE_NOW
        if stop_now:
            kill_tree(process.pid)
            return False
        return True

    def detach(self) -> None:
        with self._lock:
            self.process = None


@dataclasses.dataclass
class ExecOutcome:
    status: str  # completed | interrupted | oom | failed
    message: str = str()
    frames_done: int = 0
    frames_total: int = 0
    seconds: float = 0.0


def write_item_file(work_dir: Path, job_id: str, item: dict, input_files: dict[str, str]) -> Path:
    """item.json is the item from the job file plus job_id (and the local paths of any source files it needs)."""
    content = dict(item)
    content['job_id'] = job_id
    if input_files:
        content['input_files'] = input_files
    path = work_dir / 'item.json'
    temporary = work_dir / 'item.json.tmp'
    temporary.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)
    return path


def blender_device(settings: Settings, job: dict) -> str:
    return settings.gpu_device if job.get('device') == 'gpu' else 'CPU'


def blender_command(settings: Settings, job: dict, item_file: Path, out_dir: Path) -> list[str]:
    command = [*settings.blender_command, '-b', '--factory-startup']
    if job.get('device') == 'cpu':
        command += ['-t', str(job.get('threads') or 1)]
    command += [
        '-P', str(settings.blender_scripts_dir / str(job.get('entry'))),
        '--',
        '--item', str(item_file),
        '--out', str(out_dir),
        '--device', blender_device(settings, job),
        '--samples', str(job.get('samples') or settings.samples),
        '--assets', str(settings.assets_dir),
    ]
    return command


def python_command(settings: Settings, job: dict, item_file: Path, out_dir: Path) -> list[str]:
    return [*settings.python_command, '-m', 'bnrender.exec', str(job.get('entry')), '--item', str(item_file),
            '--out', str(out_dir)]


def python_path_for(settings: Settings) -> str:
    entries = [str(PACKAGE_PARENT), str(settings.app_dir)]
    existing = os.environ.get('PYTHONPATH')
    if existing:
        entries.append(existing)
    return os.pathsep.join(dict.fromkeys(entries))


def _failure_message(tail: deque[str], exit_code: int) -> str:
    # render_item.py prints `ERROR <type>: <message>` before its traceback; that line says it best.
    error_lines = [line.strip() for line in tail if line.startswith('ERROR ')]
    if error_lines:
        return error_lines[-1][len('ERROR '):][:400]
    lines = [line.strip() for line in tail if line.strip() and not PROGRESS_REGEX.match(line)]
    detail = ' | '.join(lines[-3:])[-400:]
    return f'The process stopped with exit code {exit_code}' + (f': {detail}' if detail else '.')


def run_process(
    command: list[str],
    cwd: Path,
    log_path: Path,
    control: UnitControl,
    frame_based: bool,
    on_launch: Callable[[int, float | None], None],
    on_progress: Callable[[int, int, float | None], None],
    extra_environment: dict[str, str] | None = None,
) -> ExecOutcome:
    if control.level >= PAUSE_AFTER_UNIT:
        return ExecOutcome(status='interrupted', message='Paused before it started.')
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tail: deque[str] = deque(maxlen=60)
    frames_done = 0
    frames_total = 0
    stopped_by_us = False
    with open(log_path, 'a', encoding='utf-8', errors='replace') as log:
        log.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} {" ".join(command)}\n')
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                env=child_environment(extra_environment),
                **child_popen_options(),
            )
        except OSError as error:
            return ExecOutcome(status='failed', message=f'The program could not be started: {error}')
        if not control.attach(process):
            process.wait()
            control.detach()
            return ExecOutcome(status='interrupted', message='Stopped as it started.')
        on_launch(process.pid, process_started_at(process.pid))
        last_progress_at: float | None = None
        try:
            assert process.stdout is not None
            for line in process.stdout:
                log.write(line)
                tail.append(line)
                match = PROGRESS_REGEX.match(line)
                if match is None:
                    continue
                log.flush()
                frames_done, frames_total = int(match.group(1)), int(match.group(2))
                now = time.monotonic()
                seconds = None if last_progress_at is None else now - last_progress_at
                last_progress_at = now
                if seconds is not None and seconds < MIN_SAMPLE_SECONDS:
                    seconds = None
                on_progress(frames_done, frames_total, seconds)
                if frame_based and control.level >= PAUSE_AFTER_UNIT and frames_done < frames_total:
                    # The frame just finished is safely on disk under its final name: end here.
                    stopped_by_us = True
                    kill_tree(process.pid)
                    break
            exit_code = process.wait()
        finally:
            if process.poll() is None:
                kill_tree(process.pid)
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            control.detach()
    seconds_taken = time.monotonic() - started
    if stopped_by_us or control.level >= PAUSE_NOW:
        return ExecOutcome(status='interrupted', message=control.reason or 'Stopped.', frames_done=frames_done,
                           frames_total=frames_total, seconds=seconds_taken)
    if exit_code == 0:
        return ExecOutcome(status='completed', frames_done=frames_done, frames_total=frames_total,
                           seconds=seconds_taken)
    text = ''.join(tail)
    status = 'oom' if OOM_REGEX.search(text) else 'failed'
    message = 'Out of graphics memory.' if status == 'oom' else _failure_message(tail, exit_code)
    return ExecOutcome(status=status, message=message, frames_done=frames_done, frames_total=frames_total,
                       seconds=seconds_taken)


def run_item(
    settings: Settings,
    job: dict,
    item: dict,
    work_dir: Path,
    control: UnitControl,
    on_launch: Callable[[int, float | None], None],
    on_progress: Callable[[int, int, float | None], None],
    input_files: dict[str, str] | None = None,
) -> ExecOutcome:
    out_dir = work_dir / 'out'
    out_dir.mkdir(parents=True, exist_ok=True)
    item_file = write_item_file(work_dir, str(job.get('id')), item, input_files or dict())
    is_blender = job.get('executor') == 'blender_item'
    if is_blender:
        script = settings.blender_scripts_dir / str(job.get('entry'))
        if not script.exists():
            return ExecOutcome(status='failed', message=f'blender/{job.get("entry")} is not in the repo.')
        command = blender_command(settings, job, item_file, out_dir)
    else:
        command = python_command(settings, job, item_file, out_dir)
    outcome = run_process(
        command=command,
        cwd=settings.app_dir,
        log_path=work_dir / 'render.log',
        control=control,
        frame_based=is_blender,
        on_launch=on_launch,
        on_progress=on_progress,
        extra_environment=dict(PYTHONPATH=python_path_for(settings), BN_RENDER_ROOT=str(settings.root)),
    )
    if outcome.status == 'completed' and not (out_dir / 'meta.json').exists():
        return ExecOutcome(status='failed', message='The item finished without writing out/meta.json.',
                           frames_done=outcome.frames_done, frames_total=outcome.frames_total,
                           seconds=outcome.seconds)
    return outcome
