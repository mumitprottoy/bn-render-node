"""Reading job files (library/jobs/*.yaml, CONTRACT.md section 2) for the label check and the catalogue build.

Only what the catalogue needs: each item's asset id, kind, title, group, description, labels and params. The runner
has its own, fuller loader; this one stays independent so the catalogue never breaks when the runner changes.
"""

import importlib
import inspect
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from catalogue.taxonomy import REPO_ROOT, Taxonomy, load_taxonomy, validate_item

logger = logging.getLogger(__name__)

# The backend's STUDIO_ASSET_ID_REGEX (the runner uses the same), so an id the API would refuse is caught here too.
ASSET_ID_REGEX = re.compile(r'^[a-z0-9_]+(/[a-z0-9][a-z0-9-]*){2,4}$')
GENERATOR_REGEX = re.compile(r'^[A-Za-z_][\w.]*:[A-Za-z_]\w*$')

# The catalogue job's own items describe the catalogue, not library content.
NON_LIBRARY_KINDS = ('catalogue',)


@dataclass
class JobItem:
    job_id: str
    job_file: Path
    kind: str
    asset_id: str
    title: str
    group: str
    description: str
    labels: dict
    params: dict
    problems: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.problems


@dataclass
class JobFile:
    path: Path
    job_id: str
    kind: str
    items: list[JobItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _run_generator(spec: str, job: dict, app_dir: Path) -> list:
    """Calls a job's `generator: module:function` (imported from the repo) for its item list, the way the runner does
    (bnrender/exec.py): generator(job, sources) or generator(job). The label check has no source listing, so a
    generator that needs one gets an empty mapping."""
    module_name, function_name = spec.split(':', 1)
    app_text = str(app_dir)
    if app_text not in sys.path:
        sys.path.insert(0, app_text)
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    parameters = inspect.signature(function).parameters
    if 'sources' in parameters or len(parameters) >= 2:
        items = function(job, dict())
    elif parameters:
        items = function(job)
    else:
        items = function()
    return list(items or list())


def _text(value: object) -> str:
    return str(value).strip() if value is not None else str()


def load_job_file(path: Path, taxonomy: Taxonomy | None = None, app_dir: Path | None = None) -> JobFile:
    """One job file with its items, each item checked for shape and labels."""
    taxonomy = taxonomy or load_taxonomy()
    app_dir = app_dir or REPO_ROOT
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as error:
        return JobFile(path=path, job_id=path.stem, kind=str(), problems=[f'The file could not be read: {error}'])
    if not isinstance(raw, dict):
        return JobFile(path=path, job_id=path.stem, kind=str(), problems=['The file must be a mapping at the top level.'])
    job_id = _text(raw.get('id')) or path.stem
    kind = _text(raw.get('kind'))
    job = JobFile(path=path, job_id=job_id, kind=kind)
    if kind not in taxonomy.kinds:
        job.problems.append(f'The job\'s kind "{kind}" is not a kind in the taxonomy.' if kind
                            else 'The job has no "kind".')
    raw_items = raw.get('items')
    generator = raw.get('generator')
    if (raw_items is None) == (generator is None):
        job.problems.append('Give either "items" or "generator", not both and not neither.')
        return job
    if generator is not None:
        if not isinstance(generator, str) or not GENERATOR_REGEX.match(generator):
            job.problems.append('"generator" must look like module:function.')
            return job
        try:
            raw_items = _run_generator(generator, raw, app_dir)
        except Exception as error:
            job.problems.append(f'The generator {generator} could not be run: {error}')
            return job
    if not isinstance(raw_items, list):
        job.problems.append('"items" must be a list.')
        return job
    for position, raw_item in enumerate(raw_items, start=1):
        job.items.append(_load_item(raw_item, position, job, taxonomy))
    return job


def _load_item(raw_item: object, position: int, job: JobFile, taxonomy: Taxonomy) -> JobItem:
    if not isinstance(raw_item, dict):
        return JobItem(job_id=job.job_id, job_file=job.path, kind=job.kind, asset_id=f'(item {position})', title=str(),
                       group=str(), description=str(), labels=dict(), params=dict(),
                       problems=['The item must be a mapping.'])
    kind = _text(raw_item.get('kind')) or job.kind
    asset_id = _text(raw_item.get('asset_id'))
    labels = raw_item.get('labels')
    params = raw_item.get('params')
    item = JobItem(job_id=job.job_id, job_file=job.path, kind=kind, asset_id=asset_id or f'(item {position})',
                   title=_text(raw_item.get('title')), group=_text(raw_item.get('group')),
                   description=_text(raw_item.get('description')),
                   labels=labels if isinstance(labels, dict) else dict(),
                   params=params if isinstance(params, dict) else dict())
    if not asset_id:
        item.problems.append('The item has no asset_id.')
    elif not ASSET_ID_REGEX.match(asset_id) or not asset_id.startswith(f'{kind}/'):
        item.problems.append(f'The asset_id "{asset_id}" must look like {kind}/group/name in lowercase words joined by '
                             f'hyphens.')
    if not item.title:
        item.problems.append('The item has no title.')
    if labels is not None and not isinstance(labels, dict):
        item.problems.append('The labels must be a mapping of facet names to values.')
    elif kind not in NON_LIBRARY_KINDS:
        item.problems.extend(validate_item(kind, item.labels, taxonomy))
    if params is not None and not isinstance(params, dict):
        item.problems.append('The params must be a mapping.')
    return item


def load_jobs(jobs_dir: Path, taxonomy: Taxonomy | None = None, app_dir: Path | None = None) -> list[JobFile]:
    """Every job file in the folder, in name order, with duplicate job ids and asset ids reported."""
    taxonomy = taxonomy or load_taxonomy()
    if not jobs_dir.is_dir():
        logger.warning(f'There is no jobs folder at {jobs_dir}.')
        return list()
    paths = sorted(jobs_dir.glob('*.yaml')) + sorted(jobs_dir.glob('*.yml'))
    jobs = [load_job_file(path, taxonomy, app_dir) for path in paths]
    seen_jobs: dict[str, Path] = dict()
    seen_assets: dict[str, str] = dict()
    for job in jobs:
        if job.job_id in seen_jobs:
            job.problems.append(f'The job id "{job.job_id}" is also used by {seen_jobs[job.job_id].name}.')
        seen_jobs.setdefault(job.job_id, job.path)
        for item in job.items:
            if item.asset_id in seen_assets:
                item.problems.append(f'The asset_id is also used in the job "{seen_assets[item.asset_id]}".')
            seen_assets.setdefault(item.asset_id, job.job_id)
    return jobs
