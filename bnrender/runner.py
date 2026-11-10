"""The worker: the long-lived loop that runs the library.

One GPU unit at a time; CPU units in a pool within the thread and memory budget; finishing and uploads alongside.
Every 15 minutes, between GPU units: update the code, read the job files, list sources/, install models, and start
the jobs whose requirements are met. Everything is recorded in the state database, so a crash, a reboot or a power
cut resumes where it stopped.
"""

import dataclasses
import datetime
import json
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .api import ApiClient
from .api import ApiError
from .api import TransientError
from .completion import finish
from .control import read_pause
from .db import Database
from .executors import PAUSE_AFTER_UNIT
from .executors import PAUSE_NOW
from .executors import UnitControl
from .executors import python_path_for
from .executors import run_item
from .finishing import FinishingError
from .finishing import FinishingInterrupted
from .finishing import finish_plate
from .finishing import finish_python
from .finishing import read_manifest
from .inputs import InputError
from .inputs import clean_partial_downloads
from .inputs import ensure_model
from .inputs import fetch_item_sources
from .inputs import folder_has_files
from .inputs import load_models
from .inputs import sync_assets
from .inputs import waiting_reason_for
from .jobs import JobDefinition
from .jobs import LabelValidator
from .jobs import check_item_shape
from .jobs import item_fingerprint
from .jobs import item_slug
from .jobs import load_job_files
from .jobs import resolve_template_version
from .jobs import taxonomy_version
from .procs import child_environment
from .procs import child_popen_options
from .procs import kill_orphan
from .procs import parent_alive
from .procs import psutil
from .procs import set_own_priority
from .selfupdate import update_app
from .settings import Settings
from .sysinfo import free_bytes
from .sysinfo import remove_folder
from .uploads import already_registered
from .uploads import upload_item

logger = logging.getLogger(__name__)

EXIT_DONE = 0
EXIT_RESTART = 75
FINISH_RETRY_SECONDS = 300.0
GENERATOR_TIMEOUT = 600
DISK_CHECK_SECONDS = 30.0
REPEAT_CHECK_SECONDS = 10.0
DAILY_HOUR = 6
MEMORY_HEADROOM_BYTES = 1024 ** 3


@dataclasses.dataclass
class ActiveUnit:
    key: str
    asset_id: str
    job_id: str
    phase: str  # render | finish | upload
    lane: str  # gpu | cpu | net
    threads: int
    ram_gb: float
    control: UnitControl
    started_at: float
    thread: threading.Thread | None = None


class Worker:
    def __init__(self, settings: Settings, api: ApiClient | None = None, parent_pid: int | None = None) -> None:
        self.settings = settings
        settings.ensure_folders()
        self.db = Database(settings.db_path)
        self.db.init_schema()
        self.api = api if api is not None else ApiClient(settings)
        self.parent_pid = parent_pid
        self.active: dict[str, ActiveUnit] = dict()
        self.stop_event = threading.Event()
        self.next_maintenance_at = 0.0
        self.last_maintenance_at = 0.0
        self.restart_requested = False
        self.drain_started_at: float | None = None
        self.retry_after: dict[str, float] = dict()
        self.source_listings: dict[str, list[dict]] = dict()
        self.assets_ready = False
        self.assets_error = str()
        self.taxonomy_version = str()
        self.job_definitions: dict[str, JobDefinition] = dict()
        self.last_heartbeat = 0.0
        self.paused = False
        self.finish_retry_at = 0.0
        self.disk_checked_at = 0.0
        self.disk_ok = True
        self.labels_notice_logged = False
        self.repeats_checked_at = 0.0

    # paths

    def item_dir(self, job_id: str, slug: str) -> Path:
        return self.settings.work_dir / job_id / slug

    # lifecycle

    def startup(self) -> None:
        logger.info(set_own_priority())
        for row in self.db.query('SELECT asset_id, pid, pid_started FROM items WHERE pid IS NOT NULL'):
            kill_orphan(row['pid'], row['pid_started'])
        with self.db.transaction() as connection:
            connection.execute("UPDATE items SET pid = NULL, pid_started = NULL, active_phase = ''")
            connection.execute("UPDATE uploads SET state = 'waiting' WHERE state = 'active'")
        for row in self.db.query("SELECT job_id, slug FROM items WHERE state = 'done'"):
            remove_folder(self.item_dir(row['job_id'], row['slug']))
        clean_partial_downloads(self.settings.assets_dir)
        clean_partial_downloads(self.settings.models_dir)
        self.db.set_meta('worker_started_at', str(time.time()))
        self.db.set_meta('heartbeat', str(time.time()))
        self.db.add_event('info', 'The runner started.')
        logger.info('Runner started.')

    def is_finished_without_key(self) -> bool:
        if self.db.get_meta('finished_at') is None:
            return False
        if self.settings.api_key() is not None:
            # A new key means new work was set up after finishing.
            self.db.delete_meta('finished_at')
            return False
        return True

    def run(self, until: Callable[[], bool] | None = None, timeout: float | None = None) -> int:
        if self.is_finished_without_key():
            logger.info('Every job is finished and the key is removed; nothing to do.')
            return EXIT_DONE
        self.startup()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                code = self.step()
                if code is not None:
                    return code
                if until is not None and until():
                    return EXIT_DONE
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError('The runner did not reach the expected state in time.')
                if self.stop_event.wait(self.settings.poll_seconds):
                    logger.info('Stop requested.')
                    return EXIT_DONE
        finally:
            self.stop_all(PAUSE_NOW, 'The runner is stopping.')
            self.db.close()

    def stop_all(self, level: int, reason: str, wait_seconds: float = 60.0) -> None:
        for unit in list(self.active.values()):
            if unit.phase != 'upload':
                unit.control.request(level, reason)
        deadline = time.monotonic() + wait_seconds
        for unit in list(self.active.values()):
            if unit.thread is not None:
                unit.thread.join(max(0.1, deadline - time.monotonic()))
        self._reap()

    def step(self) -> int | None:
        self._reap()
        if self.parent_pid is not None and not parent_alive(self.parent_pid):
            logger.warning('The supervisor is gone; stopping.')
            self.stop_event.set()
            return EXIT_DONE
        pause = read_pause(self.settings)
        self._apply_pause(pause)
        if self.restart_requested:
            if self.drain_started_at is None:
                self.drain_started_at = time.monotonic()
                self.stop_all(PAUSE_AFTER_UNIT, 'Restarting for an update.', wait_seconds=0)
            if not self.active:
                return EXIT_RESTART
            if time.monotonic() - self.drain_started_at > self.settings.drain_timeout:
                self.stop_all(PAUSE_NOW, 'Restarting for an update.')
                return EXIT_RESTART
        if pause is None and self._maintenance_due():
            gpu_busy = any(unit.lane == 'gpu' for unit in self.active.values())
            self.maintenance(allow_update=not gpu_busy)
        if pause is None and time.monotonic() - self.repeats_checked_at >= REPEAT_CHECK_SECONDS:
            self.repeats_checked_at = time.monotonic()
            self.check_repeats()
        self.refresh_job_states()
        if pause is None and not self.restart_requested:
            if self._maybe_finish():
                return EXIT_DONE
            self.schedule()
        self._heartbeat()
        return None

    def _apply_pause(self, pause: object) -> None:
        if pause is None:
            if self.paused:
                self.paused = False
                self.next_maintenance_at = 0.0
                self.db.add_event('info', 'Resumed.')
                if not self.restart_requested:
                    for unit in self.active.values():
                        unit.control.cancel_pause()
            return
        if not self.paused:
            self.paused = True
            self.db.add_event('info', 'Paused.')
        level = PAUSE_NOW if getattr(pause, 'count', 1) >= 2 else PAUSE_AFTER_UNIT
        for unit in list(self.active.values()):
            if unit.phase != 'upload':
                unit.control.request(level, 'Paused.')

    def _maintenance_due(self) -> bool:
        now = time.time()
        if now < self.next_maintenance_at:
            return False
        gpu_busy = any(unit.lane == 'gpu' for unit in self.active.values())
        # Between GPU units; if a GPU unit runs very long, maintenance goes ahead without the code update.
        return not gpu_busy or now - self.last_maintenance_at > self.settings.maintenance_overdue_seconds

    def _heartbeat(self) -> None:
        now = time.time()
        if now - self.last_heartbeat >= self.settings.heartbeat_seconds:
            self.last_heartbeat = now
            self.db.set_meta('heartbeat', str(now))

    def request_restart(self) -> None:
        if not self.restart_requested:
            self.restart_requested = True
            self.db.add_event('info', 'New runner code installed; restarting after the units in progress.')

    # maintenance

    def maintenance(self, allow_update: bool = True) -> None:
        now = time.time()
        self.last_maintenance_at = now
        self.next_maintenance_at = now + self.settings.maintenance_seconds
        if allow_update and self.settings.self_update:
            result = update_app(self.settings, self.db)
            if result.changed:
                self._reset_failed_items()
                if result.needs_restart:
                    self.request_restart()
        self.sync_jobs()
        self.check_inputs()
        self.check_models()
        self.run_generators()
        self.requeue_transient_uploads()
        self.db.set_meta('maintenance_at', str(now))

    def _reset_failed_items(self) -> None:
        """New code may fix what failed: failed items are made again from scratch."""
        rows = self.db.query("SELECT asset_id, job_id, slug FROM items WHERE state = 'failed' "
                             "AND failure_kind NOT IN ('upload_transient', 'labels', 'item')")
        for row in rows:
            remove_folder(self.item_dir(row['job_id'], row['slug']))
            self._reset_item(row['asset_id'])
        if rows:
            self.db.add_event('info', f'{len(rows)} failed items will be tried again with the new code.')

    def _reset_item(self, asset_id: str, **extra: object) -> None:
        fields = dict(state='pending', frames_done=0, frames_total=0, attempts=0, oom_retries=0, failure_kind='',
                      last_error='', registry_checked=0, skipped=0, started_at=None, rendered_at=None, done_at=None,
                      render_seconds=0)
        fields.update(extra)
        self.db.update_item(asset_id, **fields)
        self.retry_after.pop(asset_id, None)

    def sync_jobs(self) -> None:
        validator = LabelValidator(self.settings.app_dir)
        if not validator.available and not self.labels_notice_logged:
            self.labels_notice_logged = True
            self.db.add_event('info', validator.unavailable_reason)
        self.taxonomy_version = taxonomy_version(self.settings.app_dir)
        jobs, problems = load_job_files(self.settings.jobs_dir)
        ordered = sorted(jobs, key=lambda job: (job.priority, job.id))
        self.job_definitions = {job.id: job for job in ordered}
        known_ids = set()
        for position, job in enumerate(ordered, 1):
            known_ids.add(job.id)
            template_version = resolve_template_version(job, self.settings.app_dir)
            record = job.as_record()
            record['template_version'] = template_version
            existing = self.db.job(job.id)
            items_known = 1 if job.items is not None else (existing['items_known'] if existing is not None else 0)
            self.db.execute(
                'INSERT INTO jobs (id, title, kind, device, executor, priority, position, definition, load_error, '
                'removed, items_known, repeat, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET title = excluded.title, kind = excluded.kind, device = excluded.device, '
                'executor = excluded.executor, priority = excluded.priority, position = excluded.position, '
                "definition = excluded.definition, load_error = '', removed = 0, items_known = excluded.items_known, "
                'repeat = excluded.repeat, updated_at = excluded.updated_at',
                (job.id, job.title, job.kind, job.device, job.executor, job.priority, position, json.dumps(record), '',
                 items_known, job.repeat, time.time()),
            )
            if job.items is not None:
                self.sync_items(job, job.items, template_version, validator)
        for offset, (name, problem) in enumerate(problems, len(ordered) + 1):
            known_ids.add(name)
            self.db.execute(
                "INSERT INTO jobs (id, title, position, load_error, removed, items_known, updated_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?) ON CONFLICT(id) DO UPDATE SET position = excluded.position, "
                "load_error = excluded.load_error, removed = 0, updated_at = excluded.updated_at",
                (name, name, offset, problem, time.time()),
            )
            logger.error(f'Job file problem: {problem}')
        for row in self.db.query('SELECT id FROM jobs WHERE removed = 0'):
            if row['id'] not in known_ids:
                self.db.update_job(row['id'], removed=1)
                self._drop_items(row['id'], keep=set())
                self.db.add_event('info', f'Job {row["id"]} was removed from the repo.')

    def _drop_items(self, job_id: str, keep: set[str]) -> None:
        for row in self.db.query('SELECT asset_id, slug, active_phase FROM items WHERE job_id = ?', (job_id,)):
            if row['asset_id'] in keep or row['active_phase']:
                continue
            remove_folder(self.item_dir(job_id, row['slug']))
            self.db.execute('DELETE FROM items WHERE asset_id = ?', (row['asset_id'],))

    def sync_items(self, job: JobDefinition, items: list[dict], template_version: str,
                   validator: LabelValidator) -> None:
        existing = {row['asset_id']: row for row in self.db.query('SELECT * FROM items WHERE job_id = ?', (job.id,))}
        seen: set[str] = set()
        for position, item in enumerate(items):
            asset_id = str(item.get('asset_id') or str())
            if not asset_id:
                self.db.add_event('error', f'Job {job.id}: item {position + 1} has no asset_id and is skipped.',
                                  job_id=job.id)
                continue
            if asset_id in seen:
                self.db.add_event('error', f'Job {job.id}: {asset_id} is listed twice; the second is skipped.',
                                  job_id=job.id)
                continue
            owner = self.db.one('SELECT job_id FROM items WHERE asset_id = ?', (asset_id,))
            if owner is not None and owner['job_id'] != job.id:
                self.db.add_event('error', f'Job {job.id}: {asset_id} already belongs to job {owner["job_id"]}.',
                                  job_id=job.id, asset_id=asset_id)
                continue
            seen.add(asset_id)
            shape_problems = check_item_shape(job, item)
            label_problems = validator.validate(job.kind, item.get('labels') or dict()) if not shape_problems else list()
            problem = str()
            failure_kind = str()
            if shape_problems:
                problem, failure_kind = 'Item: ' + '; '.join(shape_problems), 'item'
            elif label_problems:
                problem, failure_kind = 'Labels: ' + '; '.join(label_problems[:4]), 'labels'
            fingerprint = item_fingerprint(item, template_version)
            data = json.dumps(item, ensure_ascii=False)
            row = existing.get(asset_id)
            if row is None:
                self.db.execute(
                    'INSERT INTO items (asset_id, job_id, position, slug, data, fingerprint, state, failure_kind, '
                    'last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (asset_id, job.id, position, item_slug(asset_id), data, fingerprint,
                     'failed' if problem else 'pending', failure_kind, problem, time.time()),
                )
                if problem:
                    self.db.add_event('error', f'{asset_id} refused. {problem}', job_id=job.id, asset_id=asset_id)
                continue
            if row['active_phase']:
                # A changed item is applied once the unit in progress has ended.
                continue
            if row['fingerprint'] != fingerprint:
                remove_folder(self.item_dir(job.id, row['slug']))
                self._reset_item(asset_id, data=data, fingerprint=fingerprint, position=position,
                                 state='failed' if problem else 'pending', failure_kind=failure_kind,
                                 last_error=problem)
                continue
            fields: dict[str, object] = dict(data=data, position=position)
            if problem and row['state'] in ('pending', 'failed') and row['failure_kind'] in ('', 'labels', 'item'):
                if row['state'] == 'pending':
                    self.db.add_event('error', f'{asset_id} refused. {problem}', job_id=job.id, asset_id=asset_id)
                fields.update(state='failed', failure_kind=failure_kind, last_error=problem)
            elif not problem and row['state'] == 'failed' and row['failure_kind'] in ('labels', 'item'):
                fields.update(state='pending', failure_kind='', last_error='')
            self.db.update_item(asset_id, **fields)
        removed = [asset_id for asset_id in existing if asset_id not in seen]
        if removed:
            self._drop_items(job.id, keep=seen)

    def check_inputs(self) -> None:
        jobs = [job for job in self.job_definitions.values()]
        folders = sorted({folder for job in jobs for folder in job.inputs})
        failed_folders: dict[str, str] = dict()
        for folder in folders:
            try:
                self.source_listings[folder] = self.api.list_sources(folder)
            except (TransientError, ApiError) as error:
                failed_folders[folder] = str(error)
                logger.warning(f'Could not list {folder}: {error}')
        needs_assets = any(job.executor == 'blender_item' and (self.db.job(job.id) or dict(state=''))['state'] != 'done'
                           for job in jobs)
        if needs_assets:
            try:
                downloaded, total = sync_assets(self.settings, self.api)
                self.assets_ready = True
                self.assets_error = str()
                if downloaded:
                    self.db.add_event('info', f'Scene assets: {downloaded} of {total} files downloaded.')
            except (TransientError, ApiError, OSError) as error:
                if str(error) != self.assets_error:
                    self.db.add_event('error', f'Scene assets could not be fetched: {error}')
                self.assets_error = str(error)
                logger.warning(f'Scene assets could not be synced: {error}')
        for job in jobs:
            reason = str()
            for folder in job.inputs:
                entries = self.source_listings.get(folder)
                if entries is None:
                    reason = f'waiting to list {folder} ({failed_folders.get(folder, "not listed yet")})'
                    break
                if not folder_has_files(entries):
                    reason = waiting_reason_for(folder)
                    break
            if not reason and job.executor == 'blender_item' and not self.assets_ready:
                reason = f'waiting for the scene assets ({self.assets_error or "not fetched yet"})'
            self.db.update_job(job.id, inputs_ok=0 if reason else 1, inputs_reason=reason)

    def check_models(self) -> None:
        specs, problems = load_models(self.settings)
        for problem in problems:
            logger.warning(problem)
        for job in self.job_definitions.values():
            row = self.db.job(job.id)
            if not job.models or (row is not None and row['state'] == 'done'):
                self.db.update_job(job.id, models_ok=1, models_reason='')
                continue
            reason = str()
            for model_id in job.models:
                spec = specs.get(model_id)
                if spec is None:
                    reason = f'waiting for model {model_id} (not in library/models.yaml with a confirmed licence)'
                    break
                try:
                    ensure_model(self.settings, self.db, self.api, spec)
                except InputError as error:
                    reason = f'waiting for {error}'
                    break
            self.db.update_job(job.id, models_ok=0 if reason else 1, models_reason=reason)

    def _after_met(self, job: JobDefinition) -> bool:
        for other in job.after:
            row = self.db.job(other)
            if row is None or row['removed'] or row['state'] != 'done':
                return False
        return True

    def run_generators(self) -> None:
        for job in self.job_definitions.values():
            if job.generator is None:
                continue
            row = self.db.job(job.id)
            if row is None or not row['inputs_ok'] or not row['models_ok'] or not self._after_met(job):
                continue
            job_file = self.settings.state_dir / 'generate' / f'{job.id}.json'
            items_file = self.settings.state_dir / 'generate' / f'{job.id}.items.json'
            job_file.parent.mkdir(parents=True, exist_ok=True)
            content = job.as_record()
            content['_sources'] = {folder: self.source_listings.get(folder, list()) for folder in job.inputs}
            job_file.write_text(json.dumps(content), encoding='utf-8')
            items_file.unlink(missing_ok=True)
            command = [*self.settings.python_command, '-m', 'bnrender.exec', '--generate', job.generator, '--job',
                       str(job_file), '--out', str(items_file)]
            try:
                completed = subprocess.run(command, cwd=str(self.settings.app_dir), capture_output=True, text=True,
                                           encoding='utf-8', errors='replace', timeout=GENERATOR_TIMEOUT,
                                           stdin=subprocess.DEVNULL,
                                           env=child_environment(dict(PYTHONPATH=python_path_for(self.settings))),
                                           **child_popen_options())
                items = json.loads(items_file.read_text(encoding='utf-8')) if completed.returncode == 0 else None
                detail = str() if items is not None else (
                    [line for line in completed.stdout.splitlines() if line.startswith('ERROR ')] or ['no output'])[-1]
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                items, detail = None, str(error)
            if items is None:
                message = f'Job {job.id}: its item list could not be made: {detail[:300]}'
                self.db.add_event('error', message, job_id=job.id)
                if not row['items_known']:
                    self.db.update_job(job.id, load_error=message)
                continue
            validator = LabelValidator(self.settings.app_dir)
            template_version = resolve_template_version(job, self.settings.app_dir)
            self.sync_items(job, items, template_version, validator)
            self.db.update_job(job.id, items_known=1)

    def requeue_transient_uploads(self) -> None:
        rows = self.db.query("SELECT asset_id, job_id FROM items WHERE state = 'failed' "
                             "AND failure_kind = 'upload_transient'")
        for row in rows:
            self.db.update_item(row['asset_id'], state='uploading', failure_kind='', last_error='')
            self.db.upsert_upload(row['asset_id'], row['job_id'], state='waiting', first_failure_at=None,
                                  next_attempt_at=0)

    # job states

    def refresh_job_states(self) -> None:
        jobs = self.db.query('SELECT * FROM jobs WHERE removed = 0 ORDER BY position')
        states = {row['id']: row['state'] for row in jobs}
        positions = {row['id']: row['position'] for row in jobs}
        counts = {row['job_id']: row for row in self.db.query(
            "SELECT job_id, COUNT(*) AS total, SUM(state = 'done') AS done, SUM(state = 'failed') AS failed, "
            "SUM(state IN ('running', 'rendered', 'uploading') OR active_phase != '') AS busy "
            'FROM items GROUP BY job_id')}
        for job in jobs:
            state, reason = self._job_state(job, counts.get(job['id']), states, positions)
            states[job['id']] = state
            if state != job['state'] or reason != job['reason']:
                self.db.update_job(job['id'], state=state, reason=reason)

    def _job_state(self, job: object, count: object, states: dict[str, str],
                   positions: dict[str, int]) -> tuple[str, str]:
        if job['load_error']:
            return 'failed', job['load_error']
        definition = json.loads(job['definition'] or '{}')
        for other in definition.get('after') or list():
            if other not in states:
                return 'waiting', f'waiting for job {other} (no such job)'
            if states[other] == 'failed':
                return 'waiting', f'waiting for job {positions[other]} (it failed)'
            if states[other] != 'done':
                return 'waiting', f'after job {positions[other]}'
        if not job['inputs_ok']:
            return 'waiting', job['inputs_reason'] or 'waiting for its inputs'
        if not job['models_ok']:
            return 'waiting', job['models_reason'] or 'waiting for its models'
        if not job['items_known']:
            return 'waiting', 'waiting for its item list'
        total = count['total'] if count is not None else 0
        done = count['done'] or 0 if count is not None else 0
        failed = count['failed'] or 0 if count is not None else 0
        busy = count['busy'] or 0 if count is not None else 0
        if done == total:
            if job['repeat']:
                return 'waiting', self._repeat_reason(job)
            return 'done', str()
        if done + failed == total:
            return 'failed', f'{failed} of {total} items failed'
        if busy:
            return 'running', str()
        return 'queued', str()

    # repeating jobs (repeat: daily)

    def _all_items_done(self, job_id: str) -> bool:
        row = self.db.one("SELECT COUNT(*) AS total, SUM(state = 'done') AS done FROM items WHERE job_id = ?", (job_id,))
        return row is not None and row['total'] > 0 and row['done'] == row['total']

    def _registered_since(self, job: object) -> bool:
        """Whether any item of another job was registered after this job's last run."""
        since = job['last_run_at'] or 0.0
        row = self.db.one("SELECT 1 FROM uploads WHERE state = 'confirmed' AND job_id != ? AND confirmed_at > ? LIMIT 1",
                          (job['id'], since))
        return row is not None

    @staticmethod
    def last_daily_boundary(now: float) -> float:
        """The most recent 06:00 local time at or before now."""
        moment = datetime.datetime.fromtimestamp(now)
        boundary = moment.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
        if boundary > moment:
            boundary -= datetime.timedelta(days=1)
        return boundary.timestamp()

    def _repeat_reason(self, job: object) -> str:
        next_run = f'next run after {DAILY_HOUR:02d}:00'
        if job['last_run_at'] is None:
            return next_run
        try:
            result = json.loads(job['last_result'] or '{}')
        except ValueError:
            result = dict()
        when = time.strftime('%H:%M', time.localtime(job['last_run_at']))
        summary = str()
        if isinstance(result, dict) and ('registered' in result or 'published' in result):
            registered = int(result.get('registered', int(result.get('published') or 0) + int(result.get('held_back') or 0)))
            summary = f': {registered:,} items, {int(result.get("held_back") or 0):,} held back'
        return f'last run {when}{summary}; {next_run}'

    def check_repeats(self, now: float | None = None) -> None:
        """A repeating job runs again once a day after 06:00, if any other item was registered since its last run.
        When every other job is done it runs at once if it is behind, so the final catalogue is current."""
        now = time.time() if now is None else now
        jobs = self.db.query("SELECT * FROM jobs WHERE removed = 0 AND load_error = ''")
        repeating = [job for job in jobs if job['repeat']]
        if not repeating:
            return
        others_done = all(job['state'] == 'done' for job in jobs if not job['repeat'])
        boundary = self.last_daily_boundary(now)
        for job in repeating:
            if not self._all_items_done(job['id']) or not self._registered_since(job):
                continue
            daily_due = (job['last_run_at'] or 0.0) < boundary
            if not daily_due and not others_done:
                continue
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE items SET state = 'pending', frames_done = 0, frames_total = 0, attempts = 0, "
                    "oom_retries = 0, failure_kind = '', last_error = '', registry_checked = 0, skipped = 0, "
                    "started_at = NULL, rendered_at = NULL, done_at = NULL, render_seconds = 0, updated_at = ? "
                    "WHERE job_id = ? AND active_phase = ''", (now, job['id']))
            reason = 'its daily run' if daily_due else 'a final run now that every other job is done'
            self.db.add_event('info', f'Job {job["id"]} is queued for {reason}.', job_id=job['id'])

    # finishing the whole run

    def _maybe_finish(self) -> bool:
        jobs = self.db.query('SELECT * FROM jobs WHERE removed = 0')
        if not jobs or self.active:
            return False
        for job in jobs:
            if job['repeat']:
                if job['state'] != 'waiting' or not self._all_items_done(job['id']) or self._registered_since(job):
                    return False
            elif job['state'] != 'done':
                return False
        if time.time() < self.finish_retry_at:
            return False
        last_done = self.db.one('SELECT MAX(done_at) AS at FROM items')
        if last_done is not None and last_done['at'] is not None and self.last_maintenance_at < last_done['at']:
            # Look once more for new jobs in the repo before declaring the run finished.
            self.next_maintenance_at = 0.0
            return False
        outcome, message = finish(self.settings, self.db, self.api)
        if outcome == 'finished':
            return True
        if outcome == 'retry':
            logger.warning(message)
            self.finish_retry_at = time.time() + FINISH_RETRY_SECONDS
        return False

    # scheduling

    def _fits(self, threads: int, ram_gb: float) -> bool:
        cpu_units = [unit for unit in self.active.values() if unit.lane == 'cpu']
        used_threads = sum(unit.threads for unit in cpu_units)
        used_ram = sum(unit.ram_gb for unit in self.active.values() if unit.lane in ('cpu', 'gpu'))
        if cpu_units and used_threads + threads > self.settings.max_cpu_threads:
            return False
        if self.active and used_ram + ram_gb > self.settings.max_ram_gb:
            return False
        if psutil is not None and self.active:
            if psutil.virtual_memory().available < ram_gb * 1024 ** 3 + MEMORY_HEADROOM_BYTES:
                return False
        return True

    def _check_disk(self) -> bool:
        now = time.monotonic()
        if now - self.disk_checked_at >= DISK_CHECK_SECONDS:
            self.disk_checked_at = now
            free = free_bytes(self.settings.work_dir)
            was_ok = self.disk_ok
            self.disk_ok = free is None or free >= self.settings.min_free_gb * 1024 ** 3
            if was_ok and not self.disk_ok:
                self.db.add_event('error', f'Less than {self.settings.min_free_gb:.0f} GB free for work; new units '
                                           'wait until uploads free space.')
            self.db.set_meta('hold_reason', str() if self.disk_ok else 'waiting for disk space')
        return self.disk_ok

    def _candidates(self, states: tuple[str, ...], lane: str | None, ready_only: bool) -> list:
        placeholders = ', '.join('?' for _ in states)
        sql = (f'SELECT items.*, jobs.definition AS job_definition, jobs.device AS job_device, '
               f'jobs.priority AS job_priority FROM items JOIN jobs ON jobs.id = items.job_id '
               f"WHERE jobs.removed = 0 AND items.active_phase = '' AND items.state IN ({placeholders})")
        params: list[object] = list(states)
        if ready_only:
            sql += " AND jobs.state IN ('queued', 'running')"
        if lane is not None:
            sql += ' AND jobs.device = ?'
            params.append(lane)
        sql += " ORDER BY jobs.priority, jobs.position, (items.state = 'running') DESC, items.position"
        now = time.time()
        return [row for row in self.db.query(sql, tuple(params)) if self.retry_after.get(row['asset_id'], 0) <= now]

    def schedule(self) -> None:
        for row in self._candidates(('rendered',), None, ready_only=False):
            if not self._fits(self.settings.finish_threads, self.settings.finish_ram_gb):
                break
            self._start('finish', 'cpu', row, self.settings.finish_threads, self.settings.finish_ram_gb, self._finish)
        disk_ok = self._check_disk()
        if disk_ok and not any(unit.lane == 'gpu' for unit in self.active.values()):
            for row in self._candidates(('running', 'pending'), 'gpu', ready_only=True)[:1]:
                definition = json.loads(row['job_definition'])
                self._start('render', 'gpu', row, 0, float(definition.get('ram_gb') or self.settings.gpu_ram_gb),
                            self._render)
        if disk_ok:
            for row in self._candidates(('running', 'pending'), 'cpu', ready_only=True):
                definition = json.loads(row['job_definition'])
                threads = int(definition.get('threads') or 1)
                ram_gb = float(definition.get('ram_gb') or 1.0)
                if not self._fits(threads, ram_gb):
                    break
                self._start('render', 'cpu', row, threads, ram_gb, self._render)
        uploading = sum(1 for unit in self.active.values() if unit.phase == 'upload')
        if uploading < self.settings.max_uploads:
            now = time.time()
            rows = [row for row in self._candidates(('uploading',), None, ready_only=False)]
            waits = {row['asset_id']: row['next_attempt_at'] for row in self.db.query(
                'SELECT asset_id, next_attempt_at FROM uploads')}
            for row in rows:
                if uploading >= self.settings.max_uploads:
                    break
                if waits.get(row['asset_id'], 0) > now:
                    continue
                self._start('upload', 'net', row, 0, 0.0, self._upload)
                uploading += 1

    def _start(self, phase: str, lane: str, row: object, threads: int, ram_gb: float,
               target: Callable[[ActiveUnit, object], None]) -> None:
        unit = ActiveUnit(key=f'{phase}:{row["asset_id"]}', asset_id=row['asset_id'], job_id=row['job_id'],
                          phase=phase, lane=lane, threads=threads, ram_gb=ram_gb, control=UnitControl(),
                          started_at=time.time())
        self.db.update_item(row['asset_id'], active_phase=phase)
        thread = threading.Thread(target=self._run_unit, args=(unit, target, row), name=unit.key, daemon=True)
        unit.thread = thread
        self.active[unit.key] = unit
        thread.start()

    def _run_unit(self, unit: ActiveUnit, target: Callable[[ActiveUnit, object], None], row: object) -> None:
        try:
            target(unit, row)
        except Exception as error:
            logger.exception(f'The runner hit a problem with {unit.asset_id} ({unit.phase})')
            message = f'Runner problem while {unit.phase}ing: {error}'
            current = self.db.item(unit.asset_id)
            attempts = (current['attempts'] if current is not None else 0) + 1
            if attempts > self.settings.crash_retries + 1:
                self.db.update_item(unit.asset_id, state='failed', failure_kind='runner', last_error=message,
                                    attempts=attempts)
            else:
                self.db.update_item(unit.asset_id, last_error=message, attempts=attempts)
                self.retry_after[unit.asset_id] = time.time() + self.settings.retry_delay_seconds
            self.db.add_event('error', f'{unit.asset_id}: {message}', job_id=unit.job_id, asset_id=unit.asset_id)
        finally:
            try:
                self.db.update_item(unit.asset_id, active_phase='', pid=None, pid_started=None)
            finally:
                self.db.close()

    def _reap(self) -> None:
        for key, unit in list(self.active.items()):
            if unit.thread is not None and not unit.thread.is_alive():
                del self.active[key]

    # the three kinds of unit

    def _fail(self, unit: ActiveUnit, kind: str, message: str, **fields: object) -> None:
        self.db.update_item(unit.asset_id, state='failed', failure_kind=kind, last_error=message, **fields)
        self.db.add_event('error', f'{unit.asset_id}: {message}', job_id=unit.job_id, asset_id=unit.asset_id)
        logger.error(f'{unit.asset_id} failed: {message}')

    def _render(self, unit: ActiveUnit, row: object) -> None:
        item = json.loads(row['data'])
        job = json.loads(row['job_definition'])
        work_dir = self.item_dir(row['job_id'], row['slug'])
        if row['state'] == 'pending' and not row['registry_checked'] and not job.get('repeat'):
            try:
                record = already_registered(self.api, row['asset_id'], row['fingerprint'])
            except (TransientError, ApiError) as error:
                logger.info(f'Could not check the library for {row["asset_id"]} ({error}); rendering it.')
                record = None
            if record is not None:
                self.db.update_item(row['asset_id'], state='done', skipped=1, done_at=time.time(), registry_checked=1)
                self.db.upsert_upload(row['asset_id'], row['job_id'], state='confirmed',
                                      bytes=int(record.get('total_bytes') or 0), confirmed_at=time.time())
                self.db.add_event('info', f'{row["asset_id"]} is already in the library; skipped.',
                                  job_id=row['job_id'], asset_id=row['asset_id'])
                return
            self.db.update_item(row['asset_id'], registry_checked=1)
        if unit.control.level:
            return
        self.db.update_item(row['asset_id'], state='running', started_at=row['started_at'] or time.time())
        input_files: dict[str, str] = dict()
        if item.get('sources'):
            try:
                input_files = fetch_item_sources(self.settings, self.api, list(item['sources']), work_dir,
                                                 self.source_listings)
            except InputError as error:
                self._fail(unit, 'inputs', str(error))
                return
            except (TransientError, ApiError) as error:
                self.db.update_item(row['asset_id'], last_error=f'Its input files could not be fetched yet: {error}')
                self.retry_after[row['asset_id']] = time.time() + self.settings.input_retry_seconds
                return
        is_frames = job.get('executor') == 'blender_item'

        def on_launch(pid: int, started_at: float | None) -> None:
            self.db.update_item(row['asset_id'], pid=pid, pid_started=started_at)

        def on_progress(done: int, total: int, seconds: float | None) -> None:
            self.db.update_item(row['asset_id'], frames_done=done, frames_total=total)
            if is_frames and seconds is not None:
                self.db.add_speed_sample(row['job_id'], 'frame', seconds)
            self.db.set_meta('checkpoint', str(time.time()))

        outcome = run_item(self.settings, job, item, work_dir, unit.control, on_launch, on_progress, input_files)
        current = self.db.item(row['asset_id'])
        render_seconds = (current['render_seconds'] if current is not None else 0) + outcome.seconds
        if outcome.status == 'completed':
            fields: dict[str, object] = dict(state='rendered', rendered_at=time.time(), render_seconds=render_seconds,
                                             last_error='', pid=None, pid_started=None)
            if outcome.frames_total:
                fields.update(frames_done=outcome.frames_done, frames_total=outcome.frames_total)
            self.db.update_item(row['asset_id'], **fields)
            if not is_frames:
                self.db.add_speed_sample(row['job_id'], 'item', outcome.seconds)
            self.db.set_meta('checkpoint', str(time.time()))
            return
        if outcome.status == 'interrupted':
            self.db.update_item(row['asset_id'], render_seconds=render_seconds)
            return
        if outcome.status == 'oom':
            retries = (current['oom_retries'] if current is not None else 0) + 1
            if retries > self.settings.oom_retries:
                self._fail(unit, 'oom', f'Out of graphics memory {retries} times; the item failed.',
                           oom_retries=retries, render_seconds=render_seconds)
                return
            message = f'Out of graphics memory; trying again ({retries} of {self.settings.oom_retries}).'
            self.db.update_item(row['asset_id'], oom_retries=retries, last_error=message,
                                render_seconds=render_seconds)
            self.db.add_event('error', f'{row["asset_id"]}: {message}', job_id=row['job_id'], asset_id=row['asset_id'])
            return
        attempts = (current['attempts'] if current is not None else 0) + 1
        if attempts > self.settings.crash_retries:
            self._fail(unit, 'render', outcome.message, attempts=attempts, render_seconds=render_seconds)
            return
        self.db.update_item(row['asset_id'], attempts=attempts, last_error=outcome.message,
                            render_seconds=render_seconds)
        self.db.add_event('error', f'{row["asset_id"]}: {outcome.message} Trying once more.', job_id=row['job_id'],
                          asset_id=row['asset_id'])
        self.retry_after[row['asset_id']] = time.time() + self.settings.retry_delay_seconds

    def _finish(self, unit: ActiveUnit, row: object) -> None:
        job = json.loads(row['job_definition'])
        work_dir = self.item_dir(row['job_id'], row['slug'])
        try:
            if job.get('executor') == 'blender_item':
                manifest = finish_plate(self.settings, work_dir, unit.control)
            else:
                manifest = finish_python(work_dir)
        except FinishingInterrupted:
            return
        except FinishingError as error:
            self._fail(unit, 'check', str(error))
            return
        total = sum(int(entry['size']) for entry in manifest['files'])
        self.db.update_item(row['asset_id'], state='uploading', last_error='')
        self.db.upsert_upload(row['asset_id'], row['job_id'], state='waiting', bytes=total, attempts=0,
                              first_failure_at=None, next_attempt_at=0, last_error='')
        self.db.set_meta('checkpoint', str(time.time()))

    def _upload(self, unit: ActiveUnit, row: object) -> None:
        item = json.loads(row['data'])
        job = json.loads(row['job_definition'])
        work_dir = self.item_dir(row['job_id'], row['slug'])
        upload = self.db.one('SELECT * FROM uploads WHERE asset_id = ?', (row['asset_id'],))
        manifest = read_manifest(work_dir)
        self.db.upsert_upload(row['asset_id'], row['job_id'], state='active')
        outcome = upload_item(self.api, job, item, row['fingerprint'], work_dir, self.taxonomy_version or
                              taxonomy_version(self.settings.app_dir), str(job.get('template_version') or '1'))
        now = time.time()
        if outcome.status == 'done':
            self.db.update_item(row['asset_id'], state='done', done_at=now, last_error='', failure_kind='')
            self.db.upsert_upload(row['asset_id'], row['job_id'], state='confirmed', bytes=outcome.bytes,
                                  confirmed_at=now, last_error='', first_failure_at=None)
            if job.get('repeat'):
                measured = (manifest or dict()).get('measured') or dict()
                self.db.update_job(row['job_id'], last_run_at=now, last_result=json.dumps(measured))
            remove_folder(work_dir)
            self.db.set_meta('checkpoint', str(now))
            logger.info(f'{row["asset_id"]} registered and removed from this PC.')
            return
        if outcome.status == 'missing':
            remove_folder(work_dir)
            self._reset_item(row['asset_id'])
            self.db.upsert_upload(row['asset_id'], row['job_id'], state='waiting', last_error=outcome.message)
            self.db.add_event('error', f'{row["asset_id"]}: {outcome.message}', job_id=row['job_id'],
                              asset_id=row['asset_id'])
            return
        if outcome.status == 'failed':
            self.db.upsert_upload(row['asset_id'], row['job_id'], state='failed', last_error=outcome.message)
            self._fail(unit, 'api', outcome.message)
            return
        attempts = (upload['attempts'] if upload is not None else 0) + 1
        first_failure = upload['first_failure_at'] if upload is not None and upload['first_failure_at'] else now
        if now - first_failure >= self.settings.upload_retry_window:
            self.db.upsert_upload(row['asset_id'], row['job_id'], state='waiting', attempts=attempts,
                                  last_error=outcome.message)
            self._fail(unit, 'upload_transient', f'Uploads kept failing for '
                                                 f'{self.settings.upload_retry_window / 60:.0f} minutes: '
                                                 f'{outcome.message} It is tried again at the next check.')
            return
        delay = min(self.settings.backoff_max, self.settings.backoff_initial * 2 ** (attempts - 1))
        self.db.upsert_upload(row['asset_id'], row['job_id'], state='waiting', attempts=attempts,
                              first_failure_at=first_failure, next_attempt_at=now + delay,
                              last_error=outcome.message)
        self.db.update_item(row['asset_id'], last_error=f'Upload waiting to retry: {outcome.message}')
        if attempts == 1:
            self.db.add_event('error', f'{row["asset_id"]}: upload failed ({outcome.message}); retrying.',
                              job_id=row['job_id'], asset_id=row['asset_id'])
