"""Job files (`library/jobs/<job id>.yaml`): reading, checking, item fingerprints and label validation."""

import dataclasses
import hashlib
import importlib
import json
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path

from .yamlio import YamlReadError
from .yamlio import load_yaml_file

logger = logging.getLogger(__name__)

JOB_ID_REGEX = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
# Matches the backend's STUDIO_ASSET_ID_REGEX, so an item the API would refuse is refused before it is rendered.
ASSET_ID_REGEX = re.compile(r'^[a-z0-9_]+(/[a-z0-9][a-z0-9-]*){2,4}$')
ENTRY_FUNCTION_REGEX = re.compile(r'^[A-Za-z_][\w.]*:[A-Za-z_]\w*$')
EXECUTORS = ('blender_item', 'python')
DEVICES = ('gpu', 'cpu')
REPEATS = ('daily',)
DEFAULT_TEMPLATE_VERSION = '1'
# Keys that only steer the runner; they never change what is made, so they stay out of the fingerprint.
RUNNER_ONLY_ITEM_KEYS = ('estimate',)


class JobFileError(Exception):
    """A job file is not usable; the message says why in plain words."""


@dataclasses.dataclass
class JobDefinition:
    id: str
    title: str
    kind: str
    device: str
    priority: int
    executor: str
    entry: str
    after: list[str]
    inputs: list[str]
    models: list[str]
    items: list[dict] | None
    generator: str | None
    template_version: str
    threads: int
    ram_gb: float
    unit_name: str
    samples: int | None
    estimate: dict
    source: str
    repeat: str = str()

    def as_record(self) -> dict:
        """What the runner keeps about the job in its database (items are stored separately)."""
        record = dataclasses.asdict(self)
        record.pop('items')
        return record


def _string_list(raw: dict, name: str) -> list[str]:
    value = raw.get(name)
    if value is None:
        return list()
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise JobFileError(f'"{name}" must be a list of names.')
    return list(value)


def parse_job(raw: object, source: Path) -> JobDefinition:
    if not isinstance(raw, dict):
        raise JobFileError(f'{source.name} does not describe a job.')
    job_id = str(raw.get('id') or str())
    if not JOB_ID_REGEX.match(job_id):
        raise JobFileError(f'{source.name}: "id" must be lowercase words joined by hyphens.')
    if source.stem != job_id:
        raise JobFileError(f'{source.name}: the file name must be the job id ({job_id}.yaml).')
    kind = str(raw.get('kind') or str())
    if not re.match(r'^[a-z0-9_]+$', kind):
        raise JobFileError(f'Job {job_id}: "kind" is missing.')
    device = str(raw.get('device') or str())
    if device not in DEVICES:
        raise JobFileError(f'Job {job_id}: "device" must be gpu or cpu.')
    executor = str(raw.get('executor') or str())
    if executor not in EXECUTORS:
        raise JobFileError(f'Job {job_id}: "executor" must be blender_item or python.')
    entry = str(raw.get('entry') or str())
    if executor == 'blender_item' and not re.match(r'^[\w.-]+\.py$', entry):
        raise JobFileError(f'Job {job_id}: "entry" must be a script file name in blender/.')
    if executor == 'python' and not ENTRY_FUNCTION_REGEX.match(entry):
        raise JobFileError(f'Job {job_id}: "entry" must look like module:function.')
    items = raw.get('items')
    generator = raw.get('generator')
    if (items is None) == (generator is None):
        raise JobFileError(f'Job {job_id}: give either "items" or "generator", not both and not neither.')
    if items is not None and (not isinstance(items, list) or not all(isinstance(entry, dict) for entry in items)):
        raise JobFileError(f'Job {job_id}: "items" must be a list.')
    if generator is not None and (not isinstance(generator, str) or not ENTRY_FUNCTION_REGEX.match(generator)):
        raise JobFileError(f'Job {job_id}: "generator" must look like module:function.')
    try:
        priority = int(raw.get('priority', 100))
        threads = int(raw.get('threads', 4 if executor == 'blender_item' and device == 'cpu' else 1))
        ram_gb = float(raw.get('ram_gb', 4.0 if executor == 'blender_item' else 1.0))
        samples = int(raw['samples']) if raw.get('samples') is not None else None
    except (TypeError, ValueError) as error:
        raise JobFileError(f'Job {job_id}: priority, threads, ram_gb and samples must be numbers.') from error
    estimate = raw.get('estimate') or dict()
    if not isinstance(estimate, dict):
        raise JobFileError(f'Job {job_id}: "estimate" must be a mapping.')
    repeat = str(raw.get('repeat') or str())
    if repeat and repeat not in REPEATS:
        raise JobFileError(f'Job {job_id}: "repeat" can only be daily.')
    return JobDefinition(
        id=job_id,
        title=str(raw.get('title') or job_id),
        kind=kind,
        device=device,
        priority=priority,
        executor=executor,
        entry=entry,
        after=_string_list(raw, 'after'),
        inputs=_string_list(raw, 'inputs'),
        models=_string_list(raw, 'models'),
        items=items,
        generator=generator,
        template_version=str(raw.get('template_version') or str()),
        threads=max(1, threads),
        ram_gb=max(0.1, ram_gb),
        unit_name=str(raw.get('unit_name') or str()),
        samples=samples,
        estimate=estimate,
        source=source.name,
        repeat=repeat,
    )


def load_job_files(jobs_dir: Path) -> tuple[list[JobDefinition], list[tuple[str, str]]]:
    """Every job file in the folder. Returns the good jobs and (file or job id, problem) for the rest."""
    jobs: list[JobDefinition] = list()
    problems: list[tuple[str, str]] = list()
    if not jobs_dir.is_dir():
        return jobs, problems
    seen: set[str] = set()
    for path in sorted(jobs_dir.glob('*.yaml')) + sorted(jobs_dir.glob('*.yml')):
        try:
            job = parse_job(load_yaml_file(path), path)
        except (JobFileError, YamlReadError) as error:
            problems.append((path.stem, str(error)))
            continue
        if job.id in seen:
            problems.append((job.id, f'Job {job.id} is defined twice.'))
            continue
        seen.add(job.id)
        jobs.append(job)
    return jobs, problems


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


def item_fingerprint(item: dict, template_version: str) -> str:
    """SHA-256 of the item's canonical JSON (labels and params included) plus the template version."""
    content = {name: value for name, value in item.items() if name not in RUNNER_ONLY_ITEM_KEYS}
    version = str(item.get('template_version') or template_version)
    return hashlib.sha256(f'{canonical_json(content)}\n{version}'.encode('utf-8')).hexdigest()


def item_slug(asset_id: str) -> str:
    """The item's folder name under work/<job id>/: the asset id without its kind, parts joined by '__'."""
    parts = asset_id.split('/')
    return '__'.join(parts[1:]) if len(parts) > 1 else asset_id


def check_item_shape(job: JobDefinition, item: dict) -> list[str]:
    problems: list[str] = list()
    asset_id = str(item.get('asset_id') or str())
    if not ASSET_ID_REGEX.match(asset_id) or not asset_id.startswith(f'{job.kind}/'):
        problems.append(f'asset_id "{asset_id}" must look like {job.kind}/group/name in lowercase words joined by hyphens.')
    if not str(item.get('title') or str()).strip():
        problems.append('title is missing.')
    if not str(item.get('group') or str()).strip():
        problems.append('group is missing.')
    if not isinstance(item.get('labels', dict()), dict):
        problems.append('labels must be a mapping.')
    if not isinstance(item.get('params', dict()), dict):
        problems.append('params must be a mapping.')
    sources = item.get('sources', list())
    if not isinstance(sources, list) or not all(isinstance(key, str) and key.startswith('sources/') for key in sources):
        problems.append('sources must be a list of keys under sources/.')
    return problems


class LabelValidator:
    """Calls catalogue/taxonomy.py's validate_item(kind, labels) when that module exists. Without it, labels are not
    checked and that is logged once."""

    def __init__(self, app_dir: Path) -> None:
        self.app_dir = app_dir
        self.function: Callable[[str, dict], list[str]] | None = None
        self.unavailable_reason = str()
        self._load()

    def _load(self) -> None:
        module_path = self.app_dir / 'catalogue' / 'taxonomy.py'
        if not module_path.exists():
            self.unavailable_reason = 'catalogue/taxonomy.py is not in the repo yet, so labels are not checked.'
            logger.warning(self.unavailable_reason)
            return
        app_text = str(self.app_dir)
        if app_text not in sys.path:
            sys.path.insert(0, app_text)
        try:
            for name in [name for name in sys.modules if name == 'catalogue' or name.startswith('catalogue.')]:
                del sys.modules[name]
            module = importlib.import_module('catalogue.taxonomy')
        except Exception as error:
            self.unavailable_reason = f'catalogue/taxonomy.py could not be loaded ({error}), so labels are not checked.'
            logger.error(self.unavailable_reason, exc_info=True)
            return
        function = getattr(module, 'validate_item', None)
        if not callable(function):
            self.unavailable_reason = 'catalogue/taxonomy.py has no validate_item(kind, labels), so labels are not checked.'
            logger.warning(self.unavailable_reason)
            return
        self.function = function

    @property
    def available(self) -> bool:
        return self.function is not None

    def validate(self, kind: str, labels: dict) -> list[str]:
        if self.function is None:
            return list()
        try:
            errors = self.function(kind, labels)
        except Exception as error:
            logger.error(f'Label check crashed for a {kind} item: {error}', exc_info=True)
            return [f'the label check crashed: {error}']
        return [str(entry) for entry in (errors or list())]


TEMPLATE_VERSION_REGEX = re.compile(r'^TEMPLATE_VERSION\s*=\s*[\'"]?([\w.-]+)', re.MULTILINE)


def resolve_template_version(job: JobDefinition, app_dir: Path) -> str:
    """The job's `template_version`; for a blender job without one, the TEMPLATE_VERSION constant of its scripts in
    blender/ (the entry script first), so bumping that constant re-renders the plates it changes."""
    if job.template_version:
        return job.template_version
    if job.executor == 'blender_item':
        scripts_dir = app_dir / 'blender'
        candidates = [scripts_dir / job.entry] + sorted(scripts_dir.glob('*.py')) if scripts_dir.is_dir() else list()
        for script in candidates:
            try:
                match = TEMPLATE_VERSION_REGEX.search(script.read_text(encoding='utf-8', errors='replace'))
            except OSError:
                continue
            if match is not None:
                return match.group(1)
    return DEFAULT_TEMPLATE_VERSION


def taxonomy_version(app_dir: Path) -> str:
    try:
        raw = load_yaml_file(app_dir / 'library' / 'taxonomy.yaml')
    except YamlReadError:
        return str()
    if isinstance(raw, dict) and raw.get('version') is not None:
        return str(raw.get('version'))[:20]
    return str()


def item_estimate(job: dict, item: dict) -> dict:
    """The item's estimate, falling back to the job's."""
    merged = dict(job.get('estimate') or dict())
    merged.update(item.get('estimate') or dict())
    return merged
