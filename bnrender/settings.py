"""Where everything lives on the node, and the runner's tunable limits."""

import dataclasses
import json
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = 'https://backend.bowlersnetwork.com/api/studio/library'
WINDOWS_DEFAULT_ROOT = 'D:\\bn-render'
TASK_NAME = 'BN Render Node'
API_KEY_NAME = 'BN_API_KEY'
API_BASE_NAME = 'BN_API_BASE'

IS_WINDOWS = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'


class SettingsError(Exception):
    """The node's folders or tools could not be worked out."""


@dataclasses.dataclass
class Settings:
    root: Path
    app_dir: Path
    blender_command: list[str]
    python_command: list[str]
    ffmpeg: str
    ffprobe: str
    gpu_device: str
    samples: int = 32
    max_cpu_threads: int = 11
    max_ram_gb: float = 13.0
    gpu_ram_gb: float = 4.0
    finish_threads: int = 4
    finish_ram_gb: float = 2.0
    max_uploads: int = 2
    poll_seconds: float = 1.0
    heartbeat_seconds: float = 5.0
    maintenance_seconds: float = 900.0
    maintenance_overdue_seconds: float = 2700.0
    upload_retry_window: float = 1800.0
    backoff_initial: float = 5.0
    backoff_max: float = 300.0
    min_free_gb: float = 5.0
    oom_retries: int = 3
    retry_delay_seconds: float = 30.0
    input_retry_seconds: float = 120.0
    crash_retries: int = 1
    drain_timeout: float = 600.0
    selfcheck_timeout: float = 900.0
    self_update: bool = True
    git_branch: str = 'main'
    selfcheck_command: list[str] | None = None
    api_base_override: str | None = None
    screen_enabled: bool = True
    screen_port: int = 8765
    kiosk_enabled: bool = IS_WINDOWS
    kiosk_check_seconds: float = 60.0
    reboot_enabled: bool = IS_WINDOWS
    reboot_every_hours: float = 6.0
    restart_countdown_seconds: float = 60.0
    restart_retry_seconds: float = 3600.0
    startup_selfcheck: bool = IS_WINDOWS

    @property
    def work_dir(self) -> Path:
        return self.root / 'work'

    @property
    def state_dir(self) -> Path:
        return self.root / 'state'

    @property
    def db_path(self) -> Path:
        return self.state_dir / 'runner.db'

    @property
    def pause_file(self) -> Path:
        return self.state_dir / 'paused'

    @property
    def log_file(self) -> Path:
        return self.state_dir / 'log.txt'

    @property
    def env_file(self) -> Path:
        return self.root / '.env'

    @property
    def models_dir(self) -> Path:
        return self.root / 'models'

    @property
    def assets_dir(self) -> Path:
        return self.root / 'assets'

    @property
    def tools_dir(self) -> Path:
        return self.root / 'tools'

    @property
    def jobs_dir(self) -> Path:
        return self.app_dir / 'library' / 'jobs'

    @property
    def models_file(self) -> Path:
        return self.app_dir / 'library' / 'models.yaml'

    @property
    def taxonomy_file(self) -> Path:
        return self.app_dir / 'library' / 'taxonomy.yaml'

    @property
    def blender_scripts_dir(self) -> Path:
        return self.app_dir / 'blender'

    def ensure_folders(self) -> None:
        for folder in (self.work_dir, self.state_dir, self.models_dir, self.assets_dir):
            folder.mkdir(parents=True, exist_ok=True)

    def api_base(self) -> str:
        if self.api_base_override:
            return self.api_base_override.rstrip('/')
        values = read_env_file(self.env_file)
        return (values.get(API_BASE_NAME) or os.environ.get(API_BASE_NAME) or DEFAULT_API_BASE).rstrip('/')

    def api_key(self) -> str | None:
        """Read fresh every time, so removing the key at the end takes effect at once. Never logged or printed."""
        value = read_env_file(self.env_file).get(API_KEY_NAME)
        if value is None or not value.strip():
            return None
        return value.strip()


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = dict()
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return values
    except OSError as error:
        logger.warning(f'Could not read {path.name}: {error}')
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or '=' not in stripped:
            continue
        name, value = stripped.split('=', 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def remove_env_value(path: Path, name: str) -> bool:
    """Remove one NAME=... line, keeping the rest of the file. Returns True if a line was removed."""
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except FileNotFoundError:
        return False
    kept = [line for line in lines if line.strip().split('=', 1)[0].strip() != name]
    if len(kept) == len(lines):
        return False
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text('\n'.join(kept) + ('\n' if kept else ''), encoding='utf-8')
    os.replace(temporary, path)
    return True


def _default_root(package_repo: Path) -> Path:
    configured = os.environ.get('BN_RENDER_ROOT')
    if configured:
        return Path(configured)
    if package_repo.name == 'app':
        return package_repo.parent
    if IS_WINDOWS:
        return Path(WINDOWS_DEFAULT_ROOT)
    raise SettingsError('Set BN_RENDER_ROOT to the render node folder (on the PC: D:\\bn-render).')


def _default_app_dir(root: Path, package_repo: Path) -> Path:
    configured = os.environ.get('BN_APP_DIR')
    if configured:
        return Path(configured)
    if (root / 'app' / 'bnrender').is_dir():
        return root / 'app'
    return package_repo


def _command_for(path_text: str) -> list[str]:
    if path_text.endswith('.py'):
        return [sys.executable, path_text]
    return [path_text]


def _default_blender(root: Path) -> list[str]:
    configured = os.environ.get('BN_BLENDER')
    if configured:
        return _command_for(configured)
    if IS_WINDOWS:
        return [str(root / 'tools' / 'blender' / 'blender.exe')]
    candidates = [root / 'tools' / 'blender' / 'blender', Path('/Applications/Blender.app/Contents/MacOS/Blender')]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which('blender')
    return [found if found is not None else str(candidates[0])]


def _default_tool(root: Path, name: str) -> str:
    """BN_FFMPEG / BN_FFPROBE, then the copy setup.ps1 puts in tools/ffmpeg/bin (the same in every logon context),
    then PATH."""
    configured = os.environ.get(f'BN_{name.upper()}')
    if configured:
        return configured
    bundled = root / 'tools' / 'ffmpeg' / 'bin' / (f'{name}.exe' if IS_WINDOWS else name)
    if bundled.exists():
        return str(bundled)
    found = shutil.which(name)
    return found if found is not None else str(bundled)


def _python_command() -> list[str]:
    """The scheduled task starts pythonw.exe (no console window); children use python.exe from the same folder so
    their output can be read reliably. They are started without a window either way."""
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe' and (executable.parent / 'python.exe').exists():
        return [str(executable.parent / 'python.exe')]
    return [str(executable)]


def _default_gpu_device() -> str:
    configured = os.environ.get('BN_GPU_DEVICE')
    if configured:
        return configured.upper()
    if IS_WINDOWS:
        return 'HIP'
    if IS_MAC:
        return 'METAL'
    return 'CPU'


def load_settings(root: Path | None = None) -> Settings:
    package_repo = Path(__file__).resolve().parent.parent
    resolved_root = root if root is not None else _default_root(package_repo)
    settings = Settings(
        root=resolved_root,
        app_dir=_default_app_dir(resolved_root, package_repo),
        blender_command=_default_blender(resolved_root),
        python_command=_python_command(),
        ffmpeg=_default_tool(resolved_root, 'ffmpeg'),
        ffprobe=_default_tool(resolved_root, 'ffprobe'),
        gpu_device=_default_gpu_device(),
    )
    tuning = os.environ.get('BN_TUNING')
    if tuning:
        # Test and diagnosis overrides, for example {"poll_seconds": 0.2, "self_update": false}.
        for name, value in json.loads(tuning).items():
            if not hasattr(settings, name):
                raise SettingsError(f'Unknown setting in BN_TUNING: {name}')
            setattr(settings, name, value)
    return settings
