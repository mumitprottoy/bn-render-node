"""Self-update between units: fast-forward app/ to origin/main, self-check the new version, and go back to the
previous commit if the check fails (the failure shows in `bn status`). A version that failed is not tried again until
main moves on."""

import dataclasses
import logging
import os
import subprocess
import sys

from .db import Database
from .settings import Settings

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 180
# Changes under these folders mean the runner itself (or the label check it loads) changed: it restarts to use them.
RESTART_PREFIXES = ('bnrender/', 'catalogue/')


@dataclasses.dataclass
class UpdateResult:
    changed: bool = False
    old_head: str = str()
    new_head: str = str()
    rolled_back: bool = False
    needs_restart: bool = False
    error: str = str()


class GitError(Exception):
    pass


def git(settings: Settings, *arguments: str, timeout: float = GIT_TIMEOUT) -> str:
    environment = dict(os.environ)
    environment['GIT_TERMINAL_PROMPT'] = '0'
    try:
        completed = subprocess.run(['git', '-C', str(settings.app_dir), *arguments], capture_output=True, text=True,
                                   timeout=timeout, env=environment, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitError(f'git {arguments[0]} did not finish ({error}).') from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise GitError(f'git {arguments[0]} failed: {detail[-1] if detail else completed.returncode}')
    return completed.stdout.strip()


def selfcheck_command(settings: Settings) -> list[str]:
    if settings.selfcheck_command:
        return list(settings.selfcheck_command)
    return [*settings.python_command, '-m', 'bnrender', 'selfcheck', '--after-update']


def run_selfcheck(settings: Settings) -> tuple[bool, str]:
    """Run the new version's self-check in a fresh process (the running runner still has the old code loaded)."""
    environment = dict(os.environ)
    environment['BN_RENDER_ROOT'] = str(settings.root)
    environment['PYTHONPATH'] = str(settings.app_dir)
    try:
        completed = subprocess.run(selfcheck_command(settings), cwd=str(settings.app_dir), capture_output=True,
                                   text=True, timeout=settings.selfcheck_timeout, env=environment,
                                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f'the self-check did not finish ({error})'
    output = (completed.stdout or str()) + (completed.stderr or str())
    if completed.returncode == 0:
        return True, str()
    failures = [line.strip() for line in output.splitlines() if line.strip().startswith('FAIL')]
    detail = failures[0] if failures else (output.strip().splitlines() or ['no output'])[-1]
    return False, detail[:400]


def update_app(settings: Settings, db: Database) -> UpdateResult:
    result = UpdateResult()
    if not (settings.app_dir / '.git').exists():
        return result
    try:
        result.old_head = git(settings, 'rev-parse', 'HEAD')
        git(settings, 'fetch', '--quiet', 'origin', settings.git_branch)
        remote_head = git(settings, 'rev-parse', 'FETCH_HEAD')
    except GitError as error:
        logger.warning(f'Update check skipped: {error}')
        return result
    if remote_head == result.old_head:
        return result
    if remote_head == db.get_meta('bad_head'):
        return result
    try:
        git(settings, 'merge', '--ff-only', '--quiet', 'FETCH_HEAD')
        result.new_head = git(settings, 'rev-parse', 'HEAD')
    except GitError as error:
        result.error = f'The update could not be applied: {error}'
        db.set_meta('update_error', result.error)
        db.add_event('error', result.error)
        return result
    if result.new_head == result.old_head:
        return result
    result.changed = True
    changed_files = git(settings, 'diff', '--name-only', result.old_head, result.new_head).splitlines()
    passed, detail = run_selfcheck(settings)
    short = result.new_head[:7]
    if not passed:
        try:
            git(settings, 'reset', '--hard', '--quiet', result.old_head)
        except GitError as error:
            logger.error(f'Rolling back to {result.old_head[:7]} failed: {error}')
        result.rolled_back = True
        result.changed = False
        result.error = f'Update {short} failed its self-check and was undone: {detail}'
        db.set_meta('bad_head', remote_head)
        db.set_meta('update_error', result.error)
        db.add_event('error', result.error)
        logger.error(result.error)
        return result
    result.needs_restart = any(name.startswith(RESTART_PREFIXES) and name.endswith('.py') for name in changed_files)
    db.delete_meta('update_error')
    db.set_meta('app_head', result.new_head)
    db.add_event('info', f'Updated to {short} ({len(changed_files)} files changed).')
    logger.info(f'Updated app to {short}; restart needed: {result.needs_restart}')
    return result


def current_head(settings: Settings) -> str:
    try:
        return git(settings, 'rev-parse', 'HEAD', timeout=30)
    except GitError:
        return str()


def python_is_supported() -> bool:
    return sys.version_info >= (3, 12)
