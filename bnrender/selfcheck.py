"""`python -m bnrender selfcheck`: everything the node needs, each on one PASS / FAIL / SKIP line.

Exit code 0 when nothing failed. `--report <file>` also writes the results as JSON (setup.ps1 reads the `gpu` check
from the scheduled task's context). `--quick` leaves out the Blender render test; `--offline` the API and downloads.
"""

import dataclasses
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .api import ApiClient
from .api import ApiError
from .api import TransientError
from .db import Database
from .inputs import load_models
from .inputs import sync_assets
from .jobs import LabelValidator
from .jobs import load_job_files
from .procs import child_popen_options
from .procs import psutil
from .settings import IS_WINDOWS
from .settings import Settings
from .sysinfo import format_gb
from .sysinfo import free_bytes
from .sysinfo import gpu_name_from_system

SELFTEST_REGEX = re.compile(r'^SELFTEST\s+(.*)$')
SELFTEST_FIELD_REGEX = re.compile(r'(\w+)=("([^"]*)"|\S+)')
BLENDER_VERSION_PREFIX = 'Blender 5.2'
SELFTEST_TIMEOUT = 900
PROBE_ASSET_ID = 'plate_3d/selfcheck/probe'


@dataclasses.dataclass
class Check:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str


class SelfCheck:
    def __init__(self, settings: Settings, offline: bool = False, quick: bool = False) -> None:
        self.settings = settings
        self.offline = offline
        self.quick = quick
        self.checks: list[Check] = list()
        self.facts: dict[str, str] = dict()

    def record(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))
        print(f'{status:<5} {name:<10} {detail}', flush=True)

    @property
    def passed(self) -> bool:
        return all(check.status != 'FAIL' for check in self.checks)

    # the checks

    def check_python(self) -> None:
        version = '.'.join(str(part) for part in sys.version_info[:3])
        if sys.version_info >= (3, 12):
            self.record('python', 'PASS', f'Python {version}')
        else:
            self.record('python', 'FAIL', f'Python {version}; 3.12 or newer is needed.')
        missing = list()
        for module in ('yaml', 'PIL', 'numpy'):
            try:
                __import__(module)
            except ImportError:
                missing.append(module)
        if missing:
            self.record('packages', 'FAIL', f'Missing: {", ".join(missing)}. Run: pip install -r requirements.txt')
        else:
            self.record('packages', 'PASS', 'PyYAML, Pillow and numpy are installed.')
        if psutil is None:
            self.record('psutil', 'FAIL' if IS_WINDOWS else 'SKIP', 'psutil is not installed (needed on Windows to '
                                                                     'manage render processes).')
        else:
            self.record('psutil', 'PASS', f'psutil {psutil.__version__}')

    def check_folders(self) -> None:
        try:
            self.settings.ensure_folders()
            probe = self.settings.state_dir / 'write-test.tmp'
            probe.write_text('ok', encoding='utf-8')
            probe.unlink()
            database = Database(self.settings.db_path)
            database.init_schema()
            database.close()
        except OSError as error:
            self.record('folders', 'FAIL', f'{self.settings.root} is not writable: {error}')
            return
        self.record('folders', 'PASS', f'{self.settings.root} is writable; state database opens.')
        free = free_bytes(self.settings.root)
        if free is not None and free < self.settings.min_free_gb * 1024 ** 3:
            self.record('disk', 'FAIL', f'Only {format_gb(free)} free where the work goes.')
        else:
            self.record('disk', 'PASS', f'{format_gb(free)} free where the work goes.')

    def _run(self, command: list[str], timeout: float) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                  timeout=timeout, stdin=subprocess.DEVNULL, **child_popen_options())
        except (OSError, subprocess.TimeoutExpired):
            return None

    def check_tools(self) -> None:
        completed = self._run(['git', '--version'], 60)
        if completed is None or completed.returncode != 0:
            self.record('git', 'FAIL', 'git is not installed or not on PATH.')
        elif not (self.settings.app_dir / '.git').exists():
            self.record('git', 'FAIL' if IS_WINDOWS else 'SKIP', f'{self.settings.app_dir} is not a git clone, so '
                                                                 'it cannot update itself.')
        else:
            self.record('git', 'PASS', completed.stdout.strip())
        for name, path in (('ffmpeg', self.settings.ffmpeg), ('ffprobe', self.settings.ffprobe)):
            completed = self._run([path, '-hide_banner', '-version'], 60)
            if completed is None or completed.returncode != 0:
                self.record(name, 'FAIL', f'{name} does not run ({path}).')
                continue
            self.record(name, 'PASS', completed.stdout.splitlines()[0][:80] if completed.stdout else path)
        completed = self._run([self.settings.ffmpeg, '-hide_banner', '-encoders'], 60)
        if completed is not None and 'libx264' in completed.stdout:
            self.record('x264', 'PASS', 'ffmpeg can encode H.264 (libx264).')
        else:
            self.record('x264', 'FAIL', 'ffmpeg has no libx264 encoder; install the full ffmpeg build.')

    def check_jobs(self) -> None:
        jobs, problems = load_job_files(self.settings.jobs_dir)
        for name, problem in problems:
            self.record('jobs', 'FAIL', f'{name}: {problem}')
        if not problems:
            self.record('jobs', 'PASS', f'{len(jobs)} job files read.')
        validator = LabelValidator(self.settings.app_dir)
        if not validator.available:
            self.record('labels', 'SKIP', validator.unavailable_reason)
        else:
            bad = 0
            for job in jobs:
                for item in job.items or list():
                    if validator.validate(job.kind, item.get('labels') or dict()):
                        bad += 1
            self.record('labels', 'PASS' if bad == 0 else 'FAIL',
                        'Every label is in the taxonomy.' if bad == 0 else f'{bad} items have labels to fix.')
        _, model_problems = load_models(self.settings)
        for problem in model_problems:
            self.record('models', 'FAIL', problem)

    def check_api(self) -> ApiClient | None:
        if self.offline:
            self.record('api', 'SKIP', 'Offline check.')
            return None
        if self.settings.api_key() is None:
            self.record('api', 'FAIL', 'There is no BN_API_KEY in .env.')
            return None
        api = ApiClient(self.settings)
        try:
            api.get_asset(PROBE_ASSET_ID)
        except ApiError as error:
            if error.status in (401, 403):
                self.record('api', 'FAIL', f'The API key was refused: {error.message}')
            else:
                self.record('api', 'FAIL', f'The API answered {error.status}: {error.message}')
            return None
        except TransientError as error:
            self.record('api', 'FAIL', f'The API could not be reached: {error}')
            return None
        self.record('api', 'PASS', f'The key is accepted by {self.settings.api_base()}.')
        return api

    def check_assets(self, api: ApiClient | None) -> None:
        if api is None:
            present = any(path.is_file() for path in self.settings.assets_dir.rglob('*')) if \
                self.settings.assets_dir.exists() else False
            self.record('assets', 'SKIP', 'Not synced (no API); scene assets are '
                                          f'{"present" if present else "missing"}.')
            return
        try:
            downloaded, total = sync_assets(self.settings, api)
        except (ApiError, TransientError) as error:
            self.record('assets', 'FAIL', f'Scene assets could not be fetched: {error}')
            return
        self.record('assets', 'PASS', f'{total} scene assets present ({downloaded} downloaded now).')

    def check_blender(self) -> None:
        command = self.settings.blender_command
        completed = self._run([*command, '--version'], 120)
        if completed is None or completed.returncode != 0:
            self.record('blender', 'FAIL', f'Blender does not run ({" ".join(command)}).')
            self.record('gpu', 'FAIL', 'Not tested: Blender does not run.')
            return
        version_line = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else 'unknown version'
        if not version_line.startswith(BLENDER_VERSION_PREFIX):
            self.record('blender', 'FAIL', f'{version_line}; Blender 5.2.3 is needed.')
        else:
            self.record('blender', 'PASS', version_line)
        script = self.settings.blender_scripts_dir / 'render_item.py'
        if not script.exists():
            self.record('render', 'FAIL', 'blender/render_item.py is missing from the repo, so no test render.')
            self.record('gpu', 'FAIL', 'Not tested: blender/render_item.py is missing.')
            return
        if self.quick:
            self.record('render', 'SKIP', 'Quick check: no test render.')
            return
        out_dir = self.settings.state_dir / 'selftest'
        out_dir.mkdir(parents=True, exist_ok=True)
        device = self.settings.gpu_device
        started = time.monotonic()
        completed = self._run([*command, '-b', '--factory-startup', '-P', str(script), '--', '--selftest',
                               '--device', device, '--assets', str(self.settings.assets_dir), '--out', str(out_dir)],
                              SELFTEST_TIMEOUT)
        if completed is None:
            self.record('render', 'FAIL', f'The test render did not finish within {SELFTEST_TIMEOUT // 60} minutes.')
            self.record('gpu', 'FAIL', 'Not confirmed: the test render did not finish.')
            return
        fields: dict[str, str] = dict()
        for line in completed.stdout.splitlines():
            match = SELFTEST_REGEX.match(line.strip())
            if match is not None:
                fields = {name: quoted if quoted else raw for name, raw, quoted in
                          SELFTEST_FIELD_REGEX.findall(match.group(1))}
        if completed.returncode != 0 or fields.get('ok') != '1':
            error_lines = [line for line in completed.stdout.splitlines() if line.startswith('ERROR ')]
            detail = error_lines[-1] if error_lines else (completed.stdout.strip().splitlines() or ['no output'])[-1]
            self.record('render', 'FAIL', f'The test render failed: {detail[:300]}')
            self.record('gpu', 'FAIL', 'Not confirmed: the test render failed.')
            return
        self.record('render', 'PASS', f'Test render in {time.monotonic() - started:.0f} s '
                                      f'(scene {fields.get("setup_s", "?")} s, frame {fields.get("frame_s", "?")} s).')
        devices = fields.get('devices', str())
        if device != 'CPU' and devices in (str(), 'CPU'):
            self.record('gpu', 'FAIL', f'Blender sees no {device} graphics device here; it would render on the CPU.')
            return
        self.facts['gpu_name'] = devices
        self.facts['gpu_backend'] = device
        self.facts['selftest_frame_s'] = fields.get('frame_s', str())
        self.record('gpu', 'PASS', f'{devices} ({device})')

    def run(self, after_update: bool = False) -> bool:
        if after_update:
            # Judging a new version: a network blip must not undo a good update, so the API and downloads are left
            # out (the runner has already fetched the scene assets).
            self.offline = True
        self.check_python()
        self.check_folders()
        self.check_tools()
        self.check_jobs()
        api = self.check_api()
        self.check_assets(api)
        self.check_blender()
        if 'gpu_name' not in self.facts:
            system_name = gpu_name_from_system()
            if system_name:
                self.facts['gpu_name'] = system_name
        self._store()
        failures = [check for check in self.checks if check.status == 'FAIL']
        if failures:
            print(f'Self-check failed: {len(failures)} problem(s).', flush=True)
        else:
            print('Self-check passed.', flush=True)
        return not failures

    def _store(self) -> None:
        try:
            database = Database(self.settings.db_path)
            database.init_schema()
            for name, value in self.facts.items():
                database.set_meta(name, value)
            database.set_meta('selfcheck_at', str(time.time()))
            database.set_meta('selfcheck_ok', '1' if self.passed else '0')
            database.close()
        except Exception:
            pass

    def write_report(self, path: Path) -> None:
        report = dict(ok=self.passed, at=time.time(), checks=[dataclasses.asdict(check) for check in self.checks],
                      facts=self.facts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
