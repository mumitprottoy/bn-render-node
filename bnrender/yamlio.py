"""Reading the repo's YAML files (job files, models list, taxonomy)."""

import json
from pathlib import Path


class YamlReadError(Exception):
    """A YAML file could not be read; the message is plain language."""


def load_yaml_file(path: Path) -> object:
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        raise YamlReadError(f'{path.name} could not be opened: {error}') from error
    try:
        import yaml
    except ImportError:
        # Without PyYAML only JSON-style files can be read (YAML is a superset of JSON). The PC always has PyYAML.
        try:
            return json.loads(text)
        except ValueError as error:
            raise YamlReadError(f'{path.name} needs PyYAML to be read (pip install -r requirements.txt).') from error
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise YamlReadError(f'{path.name} is not valid YAML: {error}') from error
