"""Child processes: launched at "above normal" priority on Windows (never high or realtime), killed as a whole tree,
and recognised again after a crash so an orphaned render never runs alongside its replacement."""

import logging
import os
import signal
import subprocess
import time

from .settings import IS_WINDOWS

logger = logging.getLogger(__name__)

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

try:
    import psutil
except ImportError:
    psutil = None


def child_popen_options() -> dict:
    if IS_WINDOWS:
        return dict(creationflags=ABOVE_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    return dict(start_new_session=True)


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment['PYTHONUNBUFFERED'] = '1'
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment.pop('BN_API_KEY', None)
    if extra:
        environment.update(extra)
    return environment


def set_own_priority() -> str:
    """Raise this process to "above normal" on Windows. Elsewhere nothing changes."""
    if not IS_WINDOWS:
        return 'Process priority left as it is (not Windows).'
    if psutil is not None:
        try:
            psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
            return 'Process priority set to above normal.'
        except (psutil.Error, OSError) as error:
            logger.warning(f'psutil could not set the priority: {error}')
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS):
            return 'Process priority set to above normal.'
    except (OSError, AttributeError) as error:
        logger.warning(f'SetPriorityClass failed: {error}')
    return 'Process priority could not be raised; work continues at normal priority.'


def process_started_at(pid: int) -> float | None:
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


def is_running(pid: int, started_at: float | None) -> bool:
    """True when pid is alive and, if known, is the same process that was started (not a reused pid)."""
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            process = psutil.Process(pid)
            if process.status() == psutil.STATUS_ZOMBIE:
                return False
            return started_at is None or abs(process.create_time() - started_at) < 1.0
        except (psutil.Error, OSError):
            return False
    if IS_WINDOWS:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_tree(pid: int) -> None:
    """End a process and everything it started, at once."""
    if pid <= 0:
        return
    if psutil is not None:
        try:
            parent = psutil.Process(pid)
            processes = parent.children(recursive=True) + [parent]
        except (psutil.Error, OSError):
            processes = list()
        for process in processes:
            try:
                process.kill()
            except (psutil.Error, OSError):
                pass
        if processes:
            psutil.wait_procs(processes, timeout=10)
            return
    if IS_WINDOWS:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, check=False)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def kill_orphan(pid: int | None, started_at: float | None) -> bool:
    """Kill a render left over from a crashed runner. Returns True if one was found."""
    if pid is None or not is_running(pid, started_at):
        return False
    if psutil is None and started_at is not None:
        # Without psutil the pid cannot be told apart from a reused one, so it is left alone.
        return False
    logger.warning(f'Ending a leftover process {pid} from before the restart.')
    kill_tree(pid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and is_running(pid, started_at):
        time.sleep(0.2)
    return True


def parent_alive(pid: int) -> bool:
    if psutil is not None:
        return psutil.pid_exists(pid)
    if IS_WINDOWS:
        return True
    return os.getppid() == pid
