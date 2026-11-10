"""The node's own Windows session: keep the PC awake and the screen on, no screen saver, the full-screen page in Edge,
and the scheduled restart. Everything here does nothing on other systems, so the runner works the same on a Mac."""

import ctypes
import getpass
import logging
import os
import subprocess
import time
from pathlib import Path

from .procs import psutil
from .settings import IS_WINDOWS

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
SPI_SETSCREENSAVEACTIVE = 0x0011
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
RESTART_MESSAGE = 'BowlersNetwork render node: scheduled restart'


def keep_awake() -> bool:
    """Ask Windows to keep the system and the display on for as long as this thread lives (the worker's main thread)."""
    if not IS_WINDOWS:
        return False
    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    except (OSError, AttributeError) as error:
        logger.warning(f'SetThreadExecutionState failed: {error}')
        return False
    return result != 0


def disable_screen_saver() -> bool:
    """No screen saver and no lock behind it, for this account (the runner runs as the node's own account)."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Control Panel\Desktop', 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, 'ScreenSaveActive', 0, winreg.REG_SZ, '0')
            winreg.SetValueEx(key, 'ScreenSaverIsSecure', 0, winreg.REG_SZ, '0')
        ctypes.windll.user32.SystemParametersInfoW(SPI_SETSCREENSAVEACTIVE, 0, None,
                                                   SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
    except (OSError, AttributeError) as error:
        logger.warning(f'Could not turn the screen saver off: {error}')
        return False
    return True


def find_edge() -> Path | None:
    if not IS_WINDOWS:
        return None
    for variable in ('ProgramFiles(x86)', 'ProgramFiles'):
        base = os.environ.get(variable)
        if base:
            candidate = Path(base) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe'
            if candidate.exists():
                return candidate
    return None


def kiosk_command(edge: Path, url: str, profile_dir: Path) -> list[str]:
    # Its own profile folder makes it a separate Edge instance, so it opens in kiosk mode even if Edge is already
    # open, and it can be recognised by its command line.
    return [str(edge), '--kiosk', url, '--edge-kiosk-type=fullscreen', '--no-first-run',
            f'--user-data-dir={profile_dir}']


def kiosk_running(url: str) -> bool | None:
    """True when an Edge kiosk window showing url runs; None when that cannot be told (no psutil)."""
    if psutil is None:
        return None
    for process in psutil.process_iter(['name', 'cmdline']):
        try:
            name = (process.info.get('name') or str()).lower()
            command_line = process.info.get('cmdline') or list()
        except (psutil.Error, OSError):
            continue
        if name == 'msedge.exe' and '--kiosk' in command_line and url in command_line:
            return True
    return False


def launch_kiosk(url: str, profile_dir: Path) -> bool:
    edge = find_edge()
    if edge is None:
        logger.warning('Microsoft Edge was not found, so the screen page is not shown.')
        return False
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(kiosk_command(edge, url, profile_dir), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
    except OSError as error:
        logger.warning(f'Edge could not be started: {error}')
        return False
    logger.info('Edge started in kiosk mode for the screen page.')
    return True


def system_uptime_seconds() -> float | None:
    if IS_WINDOWS:
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return kernel32.GetTickCount64() / 1000.0
        except (OSError, AttributeError):
            pass
    if psutil is not None:
        return time.time() - psutil.boot_time()
    return None


def other_users_signed_in() -> list[str]:
    """Other people signed in to this PC (for example Boss's wife after switching user): a restart would end their
    session, so the node does not restart while there are any."""
    if psutil is None:
        return list()
    me = getpass.getuser().lower()
    try:
        names = {entry.name for entry in psutil.users() if entry.name}
    except (psutil.Error, OSError):
        return list()
    return sorted(name for name in names if name.lower() != me)


def restart_computer() -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, 'Restarting is only done on Windows.'
    try:
        completed = subprocess.run(['shutdown', '/r', '/t', '0', '/c', RESTART_MESSAGE], capture_output=True,
                                   text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f'shutdown could not be run: {error}'
    if completed.returncode != 0:
        return False, (completed.stderr or completed.stdout).strip()[:300] or f'exit code {completed.returncode}'
    return True, str()
