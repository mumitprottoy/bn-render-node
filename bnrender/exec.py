"""Run a python job function in its own process (CONTRACT section 3, python executor).

    python -m bnrender.exec <module:function> --item <work>/item.json --out <work>/out
        calls function(item, out_dir); the function writes its files into out/ and returns (or writes out/meta.json)
        {"files": [{"role": ..., "path": ...}], "measured": {...}}. Paths are relative to out/ or absolute.

    python -m bnrender.exec --generate <module:function> --job <job.json> --out <items.json>
        calls generator(job, sources) and writes the item list it returns. `sources` maps each of the job's input
        folders to its files ({key, size, url}).

A function may print `PROGRESS <done> <total>` lines; `bn status` shows them.
"""

import argparse
import importlib
import inspect
import json
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path


def load_function(reference: str) -> Callable:
    module_name, function_name = reference.split(':', 1)
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    module = importlib.import_module(module_name)
    function = getattr(module, function_name)
    if not callable(function):
        raise TypeError(f'{reference} is not a function.')
    return function


def _write_json(path: Path, content: object) -> None:
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def run_item(reference: str, item_path: Path, out_dir: Path) -> int:
    item = json.loads(item_path.read_text(encoding='utf-8'))
    out_dir.mkdir(parents=True, exist_ok=True)
    result = load_function(reference)(item, out_dir)
    meta_path = out_dir / 'meta.json'
    if isinstance(result, dict):
        _write_json(meta_path, result)
    if not meta_path.exists():
        print('ERROR MissingResult: the function returned nothing and wrote no out/meta.json', flush=True)
        return 1
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    if not isinstance(meta, dict) or not isinstance(meta.get('files'), list) or not meta['files']:
        print('ERROR MissingResult: out/meta.json lists no files', flush=True)
        return 1
    print('DONE', flush=True)
    return 0


def run_generator(reference: str, job_path: Path, out_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding='utf-8'))
    sources = job.pop('_sources', dict())
    function = load_function(reference)
    parameters = inspect.signature(function).parameters
    if 'sources' in parameters or len(parameters) >= 2:
        items = function(job, sources)
    else:
        items = function(job)
    if not isinstance(items, list) or not all(isinstance(entry, dict) for entry in items):
        print('ERROR BadItems: the generator must return a list of items', flush=True)
        return 1
    _write_json(out_path, items)
    print('DONE', flush=True)
    return 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m bnrender.exec')
    parser.add_argument('reference', nargs='?')
    parser.add_argument('--generate')
    parser.add_argument('--item')
    parser.add_argument('--job')
    parser.add_argument('--out', required=True)
    options = parser.parse_args(arguments)
    try:
        if options.generate:
            return run_generator(options.generate, Path(options.job), Path(options.out))
        if not options.reference or not options.item:
            parser.error('give module:function and --item')
        return run_item(options.reference, Path(options.item), Path(options.out))
    except Exception as error:
        print(f'ERROR {type(error).__name__}: {error}', flush=True)
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return 1


if __name__ == '__main__':
    sys.exit(main())
