"""`bn status`: the layout Boss approved (render-node-plan.md section 11), read from the state database.

ETA: each job's speed is the rolling average of its last units (seconds a frame for blender jobs, seconds an item
otherwise), times the units left; before any measurement the job's `estimate` is used. GPU jobs run one after another
and CPU jobs share the pool, so the overall ETA is the later of the GPU queue and the CPU queue.
"""

import dataclasses
import json
import time

from .control import read_pause
from .db import Database
from .jobs import item_estimate
from .settings import IS_WINDOWS
from .settings import Settings
from .sysinfo import drives_to_report
from .sysinfo import folder_bytes
from .sysinfo import format_gb
from .sysinfo import free_bytes

DEFAULT_SECONDS_PER_FRAME = 6.0
ALIVE_SECONDS = 90.0
UNIT_NOUNS = dict(plate_3d='plates', character_action='clips', environment_2d='sets', music_bed='beds', sfx='sounds',
                  photo_motion='shots', photo_upscaled='photos', photo_cutout='cutouts', footage_shot='shots')
TITLE_WIDTH = 29
COLUMNS = (33, 20, 8, 17)


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return 'under 1 min'
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f'{minutes} min'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} h {minutes:02d} min'


def format_ago(seconds: float) -> str:
    if seconds < 60:
        return f'{int(seconds)} s ago'
    if seconds < 3600:
        return f'{int(seconds // 60)} min ago'
    return f'{format_duration(seconds)} ago'


def clock(timestamp: float, with_day: bool = False) -> str:
    return time.strftime('%a %H:%M' if with_day else '%H:%M', time.localtime(timestamp))


def format_speed(seconds: float, is_frames: bool, noun: str, estimated: bool) -> str:
    if seconds < 1:
        value = f'{seconds:.2f} s'
    elif seconds < 10:
        value = f'{seconds:.1f} s'
    elif seconds < 60:
        value = f'{seconds:.0f} s'
    else:
        value = f'{seconds / 60:.1f} min'
    if is_frames:
        text = f'{value} a frame'
    elif noun:
        singular = noun[:-1] if noun.endswith('s') else noun
        text = f'{value} a {singular}'
    else:
        text = f'{value} each'
    return f'est. {text}' if estimated else text


@dataclasses.dataclass
class JobProgress:
    job_id: str
    position: int
    title: str
    state: str
    reason: str
    device: str
    is_frames: bool
    noun: str
    items_total: int
    items_done: int
    items_failed: int
    units_total: float
    units_done: float
    seconds_per_unit: float | None
    measured: bool
    eta_seconds: float | None
    now_lines: list[str]

    @property
    def items_left(self) -> int:
        return self.items_total - self.items_done - self.items_failed

    @property
    def waits_for_after(self) -> bool:
        return self.state == 'waiting' and self.reason.startswith('after job')

    @property
    def counts_toward_eta(self) -> bool:
        return self.state in ('queued', 'running') or self.waits_for_after


@dataclasses.dataclass
class Overview:
    jobs: list[JobProgress]
    units_total: float
    units_done: float
    gpu_seconds: float
    cpu_seconds: float

    @property
    def eta_seconds(self) -> float:
        return max(self.gpu_seconds, self.cpu_seconds)

    @property
    def percent(self) -> float:
        return 100.0 * self.units_done / self.units_total if self.units_total else 0.0


def _now_line(item: object, paused: bool) -> str | None:
    frames = f', frame {item["frames_done"] + 1 if item["frames_done"] < item["frames_total"] else item["frames_done"]} ' \
             f'of {item["frames_total"]}' if item['frames_total'] else str()
    if item['active_phase'] == 'render':
        prefix = 'stopping after this frame' if paused else 'now'
        return f'{prefix}: {item["asset_id"]}{frames or ", starting"}'
    if item['active_phase'] == 'finish':
        return f'now: {item["asset_id"]}, making the video and checking it'
    if item['state'] == 'running' and item['frames_done']:
        return f'paused at: {item["asset_id"]}, frame {item["frames_done"]} of {item["frames_total"]} done'
    return None


def job_progress(db: Database, settings: Settings, paused: bool) -> Overview:
    jobs = db.query('SELECT * FROM jobs WHERE removed = 0 ORDER BY position')
    progress: list[JobProgress] = list()
    units_total = 0.0
    units_done = 0.0
    gpu_seconds = 0.0
    cpu_seconds = 0.0
    for job in jobs:
        definition = json.loads(job['definition'] or '{}')
        is_frames = job['executor'] == 'blender_item'
        items = db.query('SELECT * FROM items WHERE job_id = ? ORDER BY position', (job['id'],))
        done = sum(1 for item in items if item['state'] == 'done')
        failed = sum(1 for item in items if item['state'] == 'failed')
        known_totals = [item['frames_total'] or item_estimate(definition, json.loads(item['data'])).get('frames') or 0
                        for item in items]
        known = [float(value) for value in known_totals if value]
        typical_frames = sum(known) / len(known) if known else 0.0
        job_units_total = 0.0
        job_units_done = 0.0
        units_left = 0.0
        estimate_seconds: list[float] = list()
        now_lines: list[str] = list()
        for item, total in zip(items, known_totals):
            data = json.loads(item['data'])
            frames = float(total) if total else typical_frames
            item_units = frames if is_frames else 1.0
            job_units_total += item_units
            if item['state'] == 'done':
                job_units_done += item_units
            elif item['state'] != 'failed':
                if is_frames:
                    progress_done = float(min(item['frames_done'], frames)) if item['frames_total'] else 0.0
                    job_units_done += progress_done
                    units_left += frames - progress_done
                else:
                    units_left += 1.0
                    seconds = item_estimate(definition, data).get('seconds')
                    if isinstance(seconds, (int, float)):
                        estimate_seconds.append(float(seconds))
            line = _now_line(item, paused)
            if line is not None:
                now_lines.append(line)
        measured_speed = db.average_speed(job['id'], 'frame' if is_frames else 'item')
        if measured_speed is not None:
            seconds_per_unit, measured = measured_speed, True
        elif is_frames:
            estimate = definition.get('estimate') or dict()
            seconds_per_unit = float(estimate.get('seconds_per_frame') or DEFAULT_SECONDS_PER_FRAME)
            measured = False
        else:
            seconds_per_unit = sum(estimate_seconds) / len(estimate_seconds) if estimate_seconds else None
            measured = False
        parallel = 1
        if job['device'] == 'cpu' and not is_frames:
            threads = max(1, int(definition.get('threads') or 1))
            ram_gb = max(0.1, float(definition.get('ram_gb') or 1.0))
            parallel = max(1, min(settings.max_cpu_threads // threads, int(settings.max_ram_gb // ram_gb)))
        eta = None
        if seconds_per_unit is not None and (not is_frames or typical_frames or units_left == 0):
            eta = units_left * seconds_per_unit / parallel
        entry = JobProgress(
            job_id=job['id'], position=job['position'], title=job['title'] or job['id'], state=job['state'],
            reason=job['reason'], device=job['device'], is_frames=is_frames,
            noun=str(definition.get('unit_name') or UNIT_NOUNS.get(job['kind'], str())),
            items_total=len(items), items_done=done, items_failed=failed, units_total=job_units_total,
            units_done=job_units_done, seconds_per_unit=seconds_per_unit, measured=measured, eta_seconds=eta,
            now_lines=now_lines,
        )
        progress.append(entry)
        units_total += job_units_total
        units_done += job_units_done
        if entry.counts_toward_eta and eta is not None:
            if job['device'] == 'gpu':
                gpu_seconds += eta
            else:
                cpu_seconds += eta
    return Overview(jobs=progress, units_total=units_total, units_done=units_done, gpu_seconds=gpu_seconds,
                    cpu_seconds=cpu_seconds)


def _job_row(job: JobProgress) -> list[str]:
    name = f'{job.position:<3}{job.title[:TITLE_WIDTH]:<{TITLE_WIDTH + 1}}'.ljust(COLUMNS[0])
    counts = f'{job.items_done:,} / {job.items_total:,}' + (f' {job.noun}' if job.noun else str())
    rows: list[str] = list()
    if job.state == 'failed' and job.items_total == 0:
        rows.append(f'{name}failed: {job.reason}')
    elif job.state == 'waiting' and not job.waits_for_after:
        rows.append(f'{name}{job.reason}')
    elif job.state == 'done':
        rows.append(f'{name}{counts:<{COLUMNS[1]}}done')
    elif job.state == 'failed':
        rows.append(f'{name}{counts:<{COLUMNS[1]}}failed: {job.reason}')
    else:
        if job.waits_for_after:
            speed = f'({job.reason})'
        elif job.seconds_per_unit is not None:
            speed = format_speed(job.seconds_per_unit, job.is_frames, job.noun, not job.measured)
        else:
            speed = '-'
        eta = format_duration(job.eta_seconds) if job.eta_seconds is not None else '-'
        failed = f'   {job.items_failed} failed' if job.items_failed else str()
        rows.append(f'{name}{counts:<{COLUMNS[1]}}{job.items_left:<{COLUMNS[2]},}{speed:<{COLUMNS[3]}}{eta}{failed}')
    rows.extend(f'   {line}' for line in job.now_lines)
    return rows


def render_status(settings: Settings, now: float | None = None) -> str:
    now = time.time() if now is None else now
    if not settings.db_path.exists():
        return ('BowlersNetwork render node         NOT STARTED\n'
                'The runner has not run on this PC yet. It starts by itself at boot; `bn resume` starts it now.')
    db = Database(settings.db_path)
    try:
        return _render(settings, db, now)
    finally:
        db.close()


def _meta_float(db: Database, key: str) -> float | None:
    value = db.get_meta(key)
    try:
        return float(value) if value not in (None, str()) else None
    except ValueError:
        return None


def _render(settings: Settings, db: Database, now: float) -> str:
    pause = read_pause(settings)
    heartbeat = _meta_float(db, 'heartbeat')
    alive = heartbeat is not None and now - heartbeat <= ALIVE_SECONDS
    finished_at = _meta_float(db, 'finished_at')
    overview = job_progress(db, settings, paused=pause is not None)
    any_active = db.one("SELECT 1 FROM items WHERE active_phase != '' LIMIT 1") is not None and alive
    lines: list[str] = list()
    title = 'BowlersNetwork render node'
    if finished_at is not None:
        lines.append(f'{title:<35}FINISHED at {clock(finished_at, with_day=True)}')
    elif not alive:
        seen = f'last seen {clock(heartbeat, with_day=True)}' if heartbeat is not None else 'never started'
        lines.append(f'{title:<35}NOT RUNNING ({seen}){", PAUSED" if pause is not None else ""}')
    elif pause is not None:
        if any_active:
            lines.append(f'{title:<35}PAUSING (finishing the unit in progress; `bn pause` again stops it now)')
        else:
            lines.append(f'{title:<35}PAUSED since {clock(pause.since)} ({format_duration(now - pause.since)})')
    else:
        started = _meta_float(db, 'run_started_at') or _meta_float(db, 'worker_started_at') or now
        lines.append(f'{title:<35}RUNNING since {clock(started)} ({format_duration(now - started)})')

    if finished_at is not None:
        freed = _meta_float(db, 'space_freed') or 0.0
        lines.append('Every job is done and every item is confirmed in the library.')
        lines.append(f'Freed {format_gb(int(freed))} (work folder deleted). The API key was removed from this PC.')
    elif overview.units_total:
        eta = overview.eta_seconds
        # Rounded to the minute, like the time left beside it.
        finish_text = f'   done around {clock(now + eta + 30, with_day=True)}' if eta > 0 else str()
        left = f'about {format_duration(eta)} left' if eta > 0 else 'nothing left to run'
        lines.append(f'{"Overall":<10}{overview.units_done:,.0f} of {overview.units_total:,.0f} units done '
                     f'({overview.percent:.1f}%)   {left}{finish_text}')
    else:
        lines.append(f'{"Overall":<10}no jobs loaded yet')
    lines.append(str())
    if overview.jobs:
        header = f'{"Job":<{COLUMNS[0]}}{"Done / total":<{COLUMNS[1]}}{"Left":<{COLUMNS[2]}}{"Speed":<{COLUMNS[3]}}ETA'
        lines.append(header)
        for job in overview.jobs:
            lines.extend(_job_row(job))
        lines.append(str())

    confirmed = db.one("SELECT COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS bytes FROM uploads "
                       "WHERE state = 'confirmed'")
    in_progress = db.one("SELECT COUNT(*) AS count FROM uploads WHERE state = 'active'")['count'] if alive else 0
    waiting = db.one("SELECT COUNT(*) AS count FROM items WHERE state = 'uploading'")['count'] - in_progress
    lines.append(f'{"Library":<10}{confirmed["count"]:,} items confirmed in R2 ({format_gb(confirmed["bytes"])}); '
                 f'{in_progress} upload{"s" if in_progress != 1 else ""} in progress; {max(0, waiting)} waiting')

    work = folder_bytes(settings.work_dir)
    drives = '   '.join(f'{label} {format_gb(free_bytes(path))} free' for label, path in drives_to_report(settings.root))
    lines.append(f'{"This PC":<10}{format_gb(work)} of work in progress   {drives}')

    gpu_name = db.get_meta('gpu_name') or 'not tested yet (run selfcheck)'
    backend = db.get_meta('gpu_backend')
    gpu = f'{gpu_name} ({backend})' if backend and db.get_meta('gpu_name') else gpu_name
    last_error = db.last_error()
    error_text = 'none' if last_error is None else f'{clock(last_error["at"])} (below)'
    checkpoint = _meta_float(db, 'checkpoint')
    checkpoint_text = f'Last checkpoint {format_ago(now - checkpoint)}' if checkpoint is not None else \
        'No checkpoint yet'
    lines.append(f'{"GPU":<10}{gpu}    Last error: {error_text}    {checkpoint_text}')
    if last_error is not None:
        lines.append(f'{"Error":<10}{last_error["message"][:300]}')
    failed_items = db.query("SELECT asset_id, last_error FROM items WHERE state = 'failed' ORDER BY updated_at DESC")
    if failed_items:
        lines.append(f'{"Failed":<10}{len(failed_items)} item{"s" if len(failed_items) != 1 else ""}; the latest: '
                     f'{failed_items[0]["asset_id"]}: {failed_items[0]["last_error"][:200]}')
    update_error = db.get_meta('update_error')
    if update_error:
        lines.append(f'{"Update":<10}{update_error[:300]}')
    hold = db.get_meta('hold_reason')
    if hold and alive:
        lines.append(f'{"Holding":<10}{hold}')
    if not alive and finished_at is None:
        hint = 'It starts by itself at boot. `bn resume` starts it now.' if IS_WINDOWS else \
            'Start it with: python -m bnrender run'
        lines.append(f'{"Note":<10}{hint}')
    return '\n'.join(lines)
