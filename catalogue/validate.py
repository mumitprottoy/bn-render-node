"""Check every item of every job file against the taxonomy, and the screenplay query set's expectations.

    python -m catalogue.validate library/jobs [--taxonomy library/taxonomy.yaml] [--queries catalogue/queries.yaml]

Prints one plain line per problem and exits 1 if there is any, so the runner and a git pre-commit check can refuse a
job file before anything is rendered.
"""

import argparse
import logging
import sys
from pathlib import Path

from catalogue.findability import DEFAULT_QUERIES_PATH, load_screenplay_queries
from catalogue.jobs import load_jobs
from catalogue.taxonomy import REPO_ROOT, TaxonomyError, load_taxonomy

logger = logging.getLogger(__name__)


def _shown(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def validate_jobs_folder(jobs_dir: Path, taxonomy_path: Path | None = None, queries_path: Path | None = None,
                         check_queries: bool = True) -> tuple[list[str], int]:
    """Every problem as a printable line, and the number of items checked."""
    taxonomy = load_taxonomy(taxonomy_path)
    lines: list[str] = list()
    checked = 0
    for job in load_jobs(jobs_dir, taxonomy, REPO_ROOT):
        for problem in job.problems:
            lines.append(f'{_shown(job.path)}: {problem}')
        for item in job.items:
            checked += 1
            for problem in item.problems:
                lines.append(f'{_shown(job.path)}: {item.asset_id}: {problem}')
    if check_queries:
        _, query_problems = load_screenplay_queries(queries_path or DEFAULT_QUERIES_PATH, taxonomy)
        lines.extend(f'{_shown(queries_path or DEFAULT_QUERIES_PATH)}: {problem}' for problem in query_problems)
    return lines, checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m catalogue.validate',
                                     description='Check job file labels against the taxonomy.')
    parser.add_argument('jobs_dir', nargs='?', default=str(REPO_ROOT / 'library' / 'jobs'),
                        help='the folder of job files (default: library/jobs)')
    parser.add_argument('--taxonomy', default=None, help='the taxonomy file (default: library/taxonomy.yaml)')
    parser.add_argument('--queries', default=None, help='the screenplay query set (default: catalogue/queries.yaml)')
    parser.add_argument('--no-queries', action='store_true', help='skip checking the screenplay query set')
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(message)s')
    jobs_dir = Path(arguments.jobs_dir)
    try:
        lines, checked = validate_jobs_folder(jobs_dir, Path(arguments.taxonomy) if arguments.taxonomy else None,
                                              Path(arguments.queries) if arguments.queries else None,
                                              check_queries=not arguments.no_queries)
    except TaxonomyError as error:
        print(f'The taxonomy could not be loaded: {error}')
        return 2
    for line in lines:
        print(line)
    if lines:
        print(f'{len(lines)} problem{"s" if len(lines) != 1 else ""} found in {checked} items.')
        return 1
    print(f'All {checked} items are labelled from the taxonomy.' if checked
          else f'No job items found in {_shown(jobs_dir)}; nothing to check.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
