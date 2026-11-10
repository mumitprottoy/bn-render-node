"""`python -m bnrender run`: the process started at boot. It holds the single-instance lock and keeps a worker
process alive: it starts it again after a self-update (exit code 75) or a crash (with a growing pause), and exits when
the worker reports the run finished or stopped."""

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from .procs import ABOVE_NORMAL_PRIORITY_CLASS
from .procs import CREATE_NO_WINDOW
from .procs import set_own_priority
from .runner import EXIT_DONE
from .runner import EXIT_RESTART
from .settings import IS_WINDOWS
from .settings import Settings

logger = logging.getLogger(__name__)

CRASH_BACKOFF_FIRST = 10.0
CRASH_BACKOFF_MAX = 300.0
LOG_BYTES = 5 * 1024 * 1024
LOG_FILES = 5


def setup_logging(settings: Settings, to_console: bool) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter('%(asctime)s %(levelname)s [%(process)d %(threadName)s] %(name)s: %(message)s')
    file_handler = logging.handlers.RotatingFileHandler(settings.log_file, maxBytes=LOG_BYTES, backupCount=LOG_FILES,
                                                        encoding='utf-8', delay=True)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if to_console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)


class SingleInstance:
    """An exclusive lock on a file, released automatically if the process dies (even in a power cut)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: TextIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+', encoding='utf-8')
        try:
            if IS_WINDOWS:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def run_supervisor(settings: Settings) -> int:
    lock = SingleInstance(settings.state_dir / 'runner.lock')
    if not lock.acquire():
        print('The render node is already running.')
        return 0
    logger.info(set_own_priority())
    started_at = time.time()
    environment = dict(os.environ)
    environment['BN_RENDER_ROOT'] = str(settings.root)
    environment['BN_RUN_STARTED_AT'] = str(started_at)
    package_parent = str(Path(__file__).resolve().parent.parent)
    environment['PYTHONPATH'] = os.pathsep.join([package_parent, environment.get('PYTHONPATH', str())]).rstrip(os.pathsep)
    stopping = False
    worker: subprocess.Popen | None = None

    def stop(signal_number: int, _: object) -> None:
        nonlocal stopping
        stopping = True
        if worker is not None and worker.poll() is None:
            worker.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    backoff = CRASH_BACKOFF_FIRST
    try:
        while not stopping:
            command = [*settings.python_command, '-m', 'bnrender', 'worker', '--parent', str(os.getpid())]
            options: dict = dict(creationflags=ABOVE_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW) if IS_WINDOWS else dict()
            worker = subprocess.Popen(command, cwd=str(settings.app_dir), env=environment, stdin=subprocess.DEVNULL,
                                      **options)
            code = worker.wait()
            if stopping:
                break
            if code == EXIT_RESTART:
                logger.info('Worker restarting for new code.')
                backoff = CRASH_BACKOFF_FIRST
                continue
            if code == EXIT_DONE:
                logger.info('Worker finished.')
                return 0
            logger.error(f'Worker stopped unexpectedly (exit code {code}); starting it again in {backoff:.0f} s.')
            time.sleep(backoff)
            backoff = min(CRASH_BACKOFF_MAX, backoff * 2)
    finally:
        lock.release()
    return 0
