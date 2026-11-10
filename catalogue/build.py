"""The nightly catalogue job, a python executor (CONTRACT.md section 3): `catalogue.build:build`.

Reads every job file's items (titles, descriptions, labels) and which of them the runner has registered, runs the
findability test on what exists, and writes into out/:
- catalogue.json: every valid item's record, with `published` set by the findability test;
- report.json and report.txt: what passed, what is held back and why, the screenplay results, the demand log, twins,
  and a preview of the items not rendered yet.

The item's params may set `jobs_dir`, `root` (BN_RENDER_ROOT), `taxonomy` and `queries`; by default they come from this
repo and from BN_RENDER_ROOT (or the folder above the repo, where the node keeps app/).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from catalogue.findability import DEFAULT_QUERIES_PATH, FindabilityReport, load_screenplay_queries, run_findability
from catalogue.jobs import NON_LIBRARY_KINDS, JobItem, load_jobs
from catalogue.search import estimate_tokens, prompt_line
from catalogue.taxonomy import REPO_ROOT, Taxonomy, label_values, load_taxonomy, normalise_labels

logger = logging.getLogger(__name__)

CATALOGUE_FORMAT_VERSION = 1
# Item states in the runner's database that mean "registered with the API". The runner's schema is not final, so this
# list and registered_asset_ids() are the only places that know about it.
REGISTERED_STATES = ('registered', 'confirmed', 'done', 'complete', 'completed')


def registered_asset_ids(root: str | Path) -> set[str]:
    """Asset ids the runner has registered, read from <root>/state/runner.db. Read-only and defensive: any table with an
    asset_id column counts a row as registered when it has a registered/confirmed timestamp or flag, or a finished
    state. A missing or unreadable database means none are registered (so nothing is published by mistake)."""
    db_path = Path(root) / 'state' / 'runner.db'
    if not db_path.is_file():
        logger.info(f'No runner database at {db_path}; treating no items as registered.')
        return set()
    try:
        connection = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=10)
    except sqlite3.Error as error:
        logger.warning(f'Could not open {db_path} ({error}); treating no items as registered.')
        return set()
    registered: set[str] = set()
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if 'asset_id' not in columns:
                continue
            conditions: list[str] = list()
            parameters: list[str] = list()
            for column in ('registered_at', 'confirmed_at'):
                if column in columns:
                    conditions.append(f'"{column}" IS NOT NULL')
            if 'registered' in columns:
                conditions.append('"registered" = 1')
            if 'state' in columns:
                conditions.append(f'lower("state") IN ({", ".join("?" for _ in REGISTERED_STATES)})')
                parameters.extend(REGISTERED_STATES)
            # The runner skips an item the API already holds with the same fingerprint (CONTRACT.md section 6).
            if 'skipped' in columns and 'registry_checked' in columns:
                conditions.append('("skipped" = 1 AND "registry_checked" = 1)')
            if not conditions:
                continue
            query = f'SELECT asset_id FROM "{table}" WHERE {" OR ".join(conditions)}'
            registered.update(str(row[0]) for row in connection.execute(query, parameters) if row[0])
    except sqlite3.Error as error:
        logger.warning(f'Could not read registrations from {db_path} ({error}); treating no items as registered.')
        return set()
    finally:
        connection.close()
    return registered


def _record(item: JobItem, registered: set[str], taxonomy: Taxonomy) -> dict:
    labels = normalise_labels(item.labels, taxonomy)
    return {
        'asset_id': item.asset_id,
        'kind': item.kind,
        'job_id': item.job_id,
        'group': item.group,
        'title': item.title,
        'description': item.description,
        'labels': labels,
        'aliases': label_values(labels, 'aliases'),
        'registered': item.asset_id in registered,
        # Approval is Boss's call on the group's contact sheet, recorded in the office; the node does not know it.
        'approved': None,
        'published': False,
        'held_back': list(),
    }


def _held_back_reasons(result_dict: dict) -> list[str]:
    reasons = list(result_dict.get('reasons') or list())
    for check in result_dict.get('failing') or list():
        rank = check.get('rank')
        place = f'ranked {rank}' if rank is not None else 'was not ranked'
        source = 'its own labels' if check.get('source') == 'labels' else f'the screenplay set ({check.get("template")})'
        reasons.append(f'"{check.get("want")}" (from {source}) {place}; it must be in the top 3. '
                       f'Top 3: {", ".join(check.get("top") or list())}.')
    return reasons


def build_catalogue(jobs_dir: Path, root: Path, taxonomy: Taxonomy | None = None,
                    queries_path: Path | None = None, app_dir: Path | None = None) -> tuple[dict, dict]:
    """The catalogue and the report, as plain dicts."""
    taxonomy = taxonomy or load_taxonomy()
    jobs = load_jobs(jobs_dir, taxonomy, app_dir or REPO_ROOT)
    items = [item for job in jobs for item in job.items if item.kind not in NON_LIBRARY_KINDS]
    valid_items = [item for item in items if item.valid]
    invalid_items = [item for item in items if not item.valid]
    registered = registered_asset_ids(root)
    records = [_record(item, registered, taxonomy) for item in valid_items]
    screenplay, query_problems = load_screenplay_queries(queries_path or DEFAULT_QUERIES_PATH, taxonomy)

    live = [record for record in records if record['registered']]
    live_report = run_findability(live, taxonomy, screenplay, query_problems)
    for record in records:
        if not record['registered']:
            record['held_back'] = ['Not registered yet: the node has not rendered and uploaded it.']
            continue
        result = live_report.items[record['asset_id']].as_dict()
        record['published'] = result['passed']
        record['held_back'] = list() if result['passed'] else _held_back_reasons(result)
        record['findability'] = {'passed': result['passed'], 'queries': result['queries'],
                                 'failing': result['failing']}
        record['twins'] = result['twins']

    # The whole planned library, as if everything were rendered: label problems show up before the GPU time is spent.
    preview_report: FindabilityReport | None = None
    if any(not record['registered'] for record in records):
        preview_report = run_findability(records, taxonomy, screenplay, query_problems)

    built_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    catalogue = {'format': CATALOGUE_FORMAT_VERSION, 'taxonomy_version': taxonomy.version, 'built_at': built_at,
                 'records': records}
    report = _report(built_at, taxonomy, jobs, items, invalid_items, records, live_report, preview_report)
    return catalogue, report


def _report(built_at: str, taxonomy: Taxonomy, jobs: list, items: list[JobItem], invalid_items: list[JobItem],
            records: list[dict], live_report: FindabilityReport, preview_report: FindabilityReport | None) -> dict:
    live = [record for record in records if record['registered']]
    published = [record for record in live if record['published']]
    held_back = [record for record in live if not record['published']]
    summary = live_report.summary()
    prompt_lines = [prompt_line(record, taxonomy) for record in published]
    report = {
        'built_at': built_at,
        'taxonomy_version': taxonomy.version,
        'counts': {
            'job_files': len(jobs),
            'items_defined': len(items),
            'items_with_label_errors': len(invalid_items),
            'items_valid': len(records),
            'registered': len(live),
            'published': len(published),
            'held_back': len(held_back),
            'not_registered_yet': len(records) - len(live),
        },
        'pass_rate': summary['pass_rate'],
        'findability': summary,
        'job_file_problems': [{'job': job.job_id, 'file': job.path.name, 'problems': job.problems}
                              for job in jobs if job.problems],
        'invalid_items': [{'asset_id': item.asset_id, 'job': item.job_id, 'problems': item.problems}
                          for item in invalid_items],
        'held_back': [{'asset_id': record['asset_id'], 'reasons': record['held_back']} for record in held_back],
        'screenplay': [result.as_dict() for result in live_report.screenplay],
        'demand': live_report.demand,
        'twins': live_report.twins,
        'query_problems': live_report.query_problems,
        'prompt_index': {'lines': len(prompt_lines),
                         'average_estimated_tokens': round(sum(map(estimate_tokens, prompt_lines))
                                                           / len(prompt_lines), 1) if prompt_lines else 0},
    }
    if preview_report is not None:
        failing = [result.as_dict() for result in preview_report.items.values() if not result.passed]
        report['preview'] = {
            'note': 'Every valid item, rendered or not, tested together: what would be held back if all were rendered.',
            'summary': preview_report.summary(),
            'would_be_held_back': [{'asset_id': result['asset_id'], 'reasons': _held_back_reasons(result)}
                                   for result in failing],
            'twins': preview_report.twins,
            'demand': preview_report.demand,
        }
    return report


def _expect_text(expect: dict) -> str:
    parts = list()
    for facet_name, value in expect.items():
        shown = ' or '.join(map(str, value)) if isinstance(value, list) else str(value)
        parts.append(f'{facet_name}={shown}')
    return ', '.join(parts)


def report_text(report: dict) -> str:
    """The report as plain text for a person."""
    counts = report['counts']
    findability = report['findability']
    lines = [f'Library catalogue, built {report["built_at"]} (taxonomy version {report["taxonomy_version"]})', str()]
    lines.append(f'Items in job files: {counts["items_defined"]} in {counts["job_files"]} job files '
                 f'({counts["items_with_label_errors"]} with label errors, left out).')
    lines.append(f'Registered: {counts["registered"]}. Published: {counts["published"]}. '
                 f'Held back: {counts["held_back"]}. Not rendered yet: {counts["not_registered_yet"]}.')
    lines.append(f'Findability pass rate: {findability["pass_rate"] * 100:.1f}% '
                 f'({findability["items_passed"]} of {findability["items_tested"]} items; '
                 f'{findability["label_queries_passed"]} of {findability["label_queries"]} label queries).')
    lines.append(f'Screenplay beats: {findability["screenplay_queries"]} '
                 f'({findability["screenplay_found"]} found, {findability["screenplay_not_found"]} not found, '
                 f'{findability["screenplay_missing"]} missing from the library).')
    lines.append(str())

    def section(title: str, entries: list[str]) -> None:
        if not entries:
            return
        lines.append(title)
        lines.extend(entries)
        lines.append(str())

    section('Job file problems', [f'  {entry["file"]}: {problem}' for entry in report['job_file_problems']
                                  for problem in entry['problems']])
    section('Items with label errors (fix the job file)',
            [f'  {entry["asset_id"]} ({entry["job"]}): {problem}' for entry in report['invalid_items']
             for problem in entry['problems']])
    held = list()
    for entry in report['held_back']:
        held.append(f'  {entry["asset_id"]}')
        held.extend(f'    - {reason}' for reason in entry['reasons'])
    section('Held back (fix the labels in the job file)', held)
    section('Screenplay beats the search missed (we had it and could not find it)',
            [f'  [{entry["kind"]}] "{entry["want"]}" wants {_expect_text(entry["expect"])}; '
             f'{entry["items_meeting_expect"]} items meet it. Top 3: {", ".join(entry["top"])}. ({entry["source"]})'
             for entry in report['screenplay'] if entry['status'] == 'not_found'])
    section('Missing from the library (demand log: candidates for the next jobs)',
            [f'  [{entry["kind"]}] "{entry["want"]}" wants {_expect_text(entry["expect"])} ({entry["source"]})'
             for entry in report['demand']])
    section('Twins (identical labels; give each a facet that tells them apart)',
            [f'  {", ".join(group)}' for group in report['twins']])
    section('Screenplay set problems', [f'  {problem}' for problem in report['query_problems']])
    preview = report.get('preview')
    if preview:
        summary = preview['summary']
        lines.append(f'Preview of the whole planned library: {summary["items_passed"]} of {summary["items_tested"]} '
                     f'items would pass ({summary["pass_rate"] * 100:.1f}%).')
        lines.append(str())
        would = list()
        for entry in preview['would_be_held_back']:
            would.append(f'  {entry["asset_id"]}')
            would.extend(f'    - {reason}' for reason in entry['reasons'])
        section('Would be held back once rendered', would)
        section('Twins in the planned library', [f'  {", ".join(group)}' for group in preview['twins']])
    prompt = report['prompt_index']
    lines.append(f'Prompt index: {prompt["lines"]} lines, about {prompt["average_estimated_tokens"]} tokens each.')
    return '\n'.join(lines) + '\n'


def _write_json(path: Path, content: object) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def build(item: dict | str | Path, out_dir: str | Path) -> dict:
    """The python executor entry point. `item` is the item (the runner passes the parsed item.json) or the path to
    item.json. Writes catalogue.json, report.json and report.txt into out_dir and returns the files with their roles."""
    if not isinstance(item, dict):
        item = json.loads(Path(item).read_text(encoding='utf-8'))
    params = item.get('params') or dict()
    app_dir = Path(params.get('app_dir') or REPO_ROOT)
    root = Path(params.get('root') or os.environ.get('BN_RENDER_ROOT') or app_dir.parent)
    jobs_dir = Path(params.get('jobs_dir') or app_dir / 'library' / 'jobs')
    taxonomy = load_taxonomy(params.get('taxonomy') or None)
    queries_path = Path(params['queries']) if params.get('queries') else DEFAULT_QUERIES_PATH
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    catalogue, report = build_catalogue(jobs_dir, root, taxonomy, queries_path, app_dir)
    _write_json(out_path / 'catalogue.json', catalogue)
    _write_json(out_path / 'report.json', report)
    (out_path / 'report.txt').write_text(report_text(report), encoding='utf-8')
    counts = report['counts']
    logger.info(f'Catalogue built: {counts["published"]} published, {counts["held_back"]} held back, '
                f'{counts["not_registered_yet"]} not rendered yet.')
    return {
        'files': [{'role': 'catalogue', 'path': 'catalogue.json'},
                  {'role': 'report', 'path': 'report.json'},
                  {'role': 'report_text', 'path': 'report.txt'}],
        'measured': {**counts, 'pass_rate': report['pass_rate'],
                     'screenplay_not_found': report['findability']['screenplay_not_found'],
                     'demand': len(report['demand'])},
    }
