"""python -m bnrender status | pause | resume | run | selfcheck"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .control import clear_pause
from .control import read_pause
from .control import request_pause
from .settings import IS_WINDOWS
from .settings import SettingsError
from .settings import TASK_NAME
from .settings import Settings
from .settings import load_settings

logger = logging.getLogger(__name__)


def runner_alive(settings: Settings) -> bool:
    if not settings.db_path.exists():
        return False
    from .db import Database
    from .status import ALIVE_SECONDS
    db = Database(settings.db_path)
    try:
        heartbeat = db.get_meta('heartbeat')
    finally:
        db.close()
    try:
        return heartbeat is not None and time.time() - float(heartbeat) <= ALIVE_SECONDS
    except ValueError:
        return False


def command_status(settings: Settings) -> int:
    from .status import render_status
    print(render_status(settings))
    return 0


def command_pause(settings: Settings) -> int:
    state = request_pause(settings)
    alive = runner_alive(settings)
    if state.count == 1:
        print('Pausing. The frame in progress finishes, then the node stays paused, even after a restart.')
        print('Run `bn pause` again to stop the unit in progress at once. `bn resume` carries on.')
    else:
        print('Stopping the unit in progress now. Finished frames are kept; the unfinished one is redone on resume.')
    if not alive:
        print('The runner is not running at the moment; it will stay paused when it starts.')
    return 0


def command_resume(settings: Settings) -> int:
    was_paused = clear_pause(settings)
    print('Resumed. Work continues from the first unit not done.' if was_paused else 'The node was not paused.')
    if not runner_alive(settings) and IS_WINDOWS:
        completed = subprocess.run(['schtasks', '/Run', '/TN', TASK_NAME], capture_output=True, text=True)
        if completed.returncode == 0:
            print('The runner was not running; it has been started.')
        else:
            print('The runner is not running and could not be started; it starts by itself at the next boot.')
    elif not runner_alive(settings):
        print('The runner is not running. Start it with: python -m bnrender run')
    return 0


def command_run(settings: Settings) -> int:
    from .service import run_supervisor
    from .service import setup_logging
    setup_logging(settings, to_console=sys.stderr is not None and sys.stderr.isatty())
    if read_pause(settings) is not None:
        logger.info('Starting while paused: the runner waits for `bn resume`.')
    return run_supervisor(settings)


def command_worker(settings: Settings, parent: int | None) -> int:
    from .runner import Worker
    from .service import SingleInstance
    from .service import setup_logging
    setup_logging(settings, to_console=False)
    lock = SingleInstance(settings.state_dir / 'worker.lock')
    if not lock.acquire():
        logger.error('Another worker holds the lock; this one exits.')
        return 3
    worker = Worker(settings, parent_pid=parent)
    run_started = os.environ.get('BN_RUN_STARTED_AT')
    if run_started:
        worker.db.set_meta('run_started_at', run_started)

    def stop(signal_number: int, _: object) -> None:
        worker.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return worker.run()
    except Exception:
        logger.exception('The worker crashed.')
        return 1
    finally:
        lock.release()


def command_selfcheck(settings: Settings, options: argparse.Namespace) -> int:
    from .selfcheck import SelfCheck
    check = SelfCheck(settings, offline=options.offline, quick=options.quick)
    passed = check.run(after_update=options.after_update)
    if options.report:
        check.write_report(Path(options.report))
    return 0 if passed else 1


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='bn', description='BowlersNetwork render node')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status', help='progress, ETA, uploads, disk, GPU, last error')
    commands.add_parser('pause', help='finish the unit in progress, then stay paused (twice: stop it now)')
    commands.add_parser('resume', help='end a pause')
    commands.add_parser('run', help='the long-lived runner started at boot')
    worker = commands.add_parser('worker', help=argparse.SUPPRESS)
    worker.add_argument('--parent', type=int)
    selfcheck = commands.add_parser('selfcheck', help='check everything the node needs')
    selfcheck.add_argument('--offline', action='store_true')
    selfcheck.add_argument('--quick', action='store_true')
    selfcheck.add_argument('--after-update', action='store_true')
    selfcheck.add_argument('--report')
    options = parser.parse_args(arguments)
    try:
        settings = load_settings()
    except SettingsError as error:
        print(str(error))
        return 2
    if options.command == 'status':
        return command_status(settings)
    if options.command == 'pause':
        return command_pause(settings)
    if options.command == 'resume':
        return command_resume(settings)
    if options.command == 'run':
        return command_run(settings)
    if options.command == 'worker':
        return command_worker(settings, options.parent)
    return command_selfcheck(settings, options)


if __name__ == '__main__':
    sys.exit(main())
