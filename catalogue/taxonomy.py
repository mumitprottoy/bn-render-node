"""The controlled vocabulary in library/taxonomy.yaml, and the check every job item's labels must pass.

Labels come from the job definition, never from looking at the result, so a label that is not in the taxonomy is
refused before anything is rendered. `validate_item(kind, labels)` returns plain-language problems (an empty list means
the labels are good); the runner and `python -m catalogue.validate` both call it.
"""

import difflib
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TAXONOMY_PATH = REPO_ROOT / 'library' / 'taxonomy.yaml'

# Facets whose values are numbers or yes/no read badly on their own ("3", "yes"), so messages and search phrases name
# the facet with them ("energy 3", "loopable yes").
YES_NO_VALUES = ('yes', 'no')


class TaxonomyError(Exception):
    """The taxonomy file itself is broken."""


@dataclass(frozen=True)
class FacetValue:
    name: str
    definition: str
    synonyms: tuple[str, ...]


@dataclass(frozen=True)
class Facet:
    name: str
    multi: bool
    free: bool
    values: dict[str, FacetValue]

    @property
    def words(self) -> str:
        return self.name.replace('_', ' ')


@dataclass(frozen=True)
class Kind:
    name: str
    definition: str
    required: tuple[str, ...]
    optional: tuple[str, ...]

    @property
    def facet_names(self) -> tuple[str, ...]:
        return self.required + self.optional


def coerce_label_value(value: object) -> str | None:
    """One label value as the taxonomy spells it. YAML turns `energy: 3` into a number and `loopable: yes` into True;
    both are accepted and read as the taxonomy's text values. Returns None for a value of an unusable type."""
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return value.strip()
    return None


def value_words(facet: Facet, value: str) -> str:
    """A value as plain words: `pin_cam` reads "pin cam"; a bare number or yes/no carries its facet ("energy 3")."""
    if value.isdigit() or value in YES_NO_VALUES:
        return f'{facet.words} {value}'
    return value.replace('_', ' ')


class Taxonomy:
    """The parsed vocabulary: kinds with their required and optional facets, and facets with their values."""

    def __init__(self, raw: dict, source: str = 'taxonomy') -> None:
        if not isinstance(raw, dict):
            raise TaxonomyError(f'{source} must be a mapping at the top level.')
        self.source = source
        try:
            self.version = int(raw.get('version'))
        except (TypeError, ValueError) as error:
            raise TaxonomyError(f'{source} needs a whole-number "version".') from error
        self.facets = self._parse_facets(raw.get('facets'))
        self.kinds = self._parse_kinds(raw.get('kinds'))
        self.rules = self._parse_rules(raw.get('rules'))

    def _parse_rules(self, raw_rules: object) -> list[dict]:
        """Rules linking facets: when an item's labels match `when`, every facet in `require` must be present."""
        if raw_rules is None:
            return list()
        if not isinstance(raw_rules, list):
            raise TaxonomyError(f'{self.source}: "rules" must be a list.')
        rules: list[dict] = list()
        for rule in raw_rules:
            when = rule.get('when') if isinstance(rule, dict) else None
            if not isinstance(when, dict) or when.get('facet') not in self.facets or not isinstance(rule.get('require'), list):
                raise TaxonomyError(f'{self.source}: each rule needs when.facet (a known facet) and a require list.')
            rules.append(dict(facet=str(when['facet']), contains=when.get('contains'),
                              any_of=[str(value) for value in when.get('in', list())], require=[str(name) for name in rule['require']],
                              reason=str(rule.get('reason') or str())))
        return rules

    def _parse_facets(self, raw_facets: object) -> dict[str, Facet]:
        if not isinstance(raw_facets, dict) or not raw_facets:
            raise TaxonomyError(f'{self.source} needs a "facets" mapping.')
        facets: dict[str, Facet] = dict()
        for facet_name, raw_facet in raw_facets.items():
            if not isinstance(raw_facet, dict):
                raise TaxonomyError(f'Facet "{facet_name}" must be a mapping.')
            values: dict[str, FacetValue] = dict()
            raw_values = raw_facet.get('values') or dict()
            if not isinstance(raw_values, dict):
                raise TaxonomyError(f'Facet "{facet_name}" has "values" that are not a mapping.')
            for raw_value_name, raw_value in raw_values.items():
                value_name = coerce_label_value(raw_value_name)
                if value_name is None:
                    raise TaxonomyError(f'Facet "{facet_name}" has a value name that is not text: {raw_value_name!r}.')
                raw_value = raw_value or dict()
                synonyms = tuple(str(coerce_label_value(synonym)) for synonym in raw_value.get('synonyms') or list()
                                 if coerce_label_value(synonym))
                values[value_name] = FacetValue(name=value_name, definition=str(raw_value.get('definition') or str()),
                                                synonyms=synonyms)
            free = bool(raw_facet.get('free', False))
            if not free and not values:
                raise TaxonomyError(f'Facet "{facet_name}" has no values and is not marked free.')
            facets[str(facet_name)] = Facet(name=str(facet_name), multi=bool(raw_facet.get('multi', False)), free=free,
                                            values=values)
        return facets

    def _parse_kinds(self, raw_kinds: object) -> dict[str, Kind]:
        if not isinstance(raw_kinds, dict) or not raw_kinds:
            raise TaxonomyError(f'{self.source} needs a "kinds" mapping.')
        kinds: dict[str, Kind] = dict()
        for kind_name, raw_kind in raw_kinds.items():
            raw_kind = raw_kind or dict()
            required = tuple(str(name) for name in raw_kind.get('required') or list())
            optional = tuple(str(name) for name in raw_kind.get('optional') or list())
            for facet_name in required + optional:
                if facet_name not in self.facets:
                    raise TaxonomyError(f'Kind "{kind_name}" uses the facet "{facet_name}", which is not defined.')
            kinds[str(kind_name)] = Kind(name=str(kind_name), definition=str(raw_kind.get('definition') or str()),
                                         required=required, optional=optional)
        return kinds

    def kind_facets(self, kind: str | None) -> list[Facet]:
        """The facets an item of this kind can carry; every facet when the kind is None."""
        if kind is None:
            return list(self.facets.values())
        spec = self.kinds.get(kind)
        if spec is None:
            return list()
        return [self.facets[name] for name in spec.facet_names]

    def resolve_value(self, facet_name: str, text: str) -> str | None:
        """A value of the facet from its name, its name written loosely ("pin cam") or one of its synonyms."""
        facet = self.facets.get(facet_name)
        if facet is None or facet.free:
            return None
        if text in facet.values:
            return text
        loose = _loose(text)
        for value in facet.values.values():
            if _loose(value.name) == loose or any(_loose(synonym) == loose for synonym in value.synonyms):
                return value.name
        return None


def _loose(text: str) -> str:
    return ' '.join(text.lower().replace('_', ' ').replace('-', ' ').replace("'", str()).split())


@lru_cache(maxsize=8)
def _load_cached(path_text: str, modified: float) -> Taxonomy:
    path = Path(path_text)
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except OSError as error:
        raise TaxonomyError(f'Could not read the taxonomy at {path}: {error}') from error
    except yaml.YAMLError as error:
        raise TaxonomyError(f'The taxonomy at {path} is not valid YAML: {error}') from error
    return Taxonomy(raw, source=str(path))


def load_taxonomy(path: str | Path | None = None) -> Taxonomy:
    """The taxonomy from library/taxonomy.yaml (or the given file). Re-read when the file changes."""
    taxonomy_path = Path(path) if path is not None else DEFAULT_TAXONOMY_PATH
    try:
        modified = taxonomy_path.stat().st_mtime
    except OSError as error:
        raise TaxonomyError(f'There is no taxonomy at {taxonomy_path}.') from error
    return _load_cached(str(taxonomy_path.resolve()), modified)


def taxonomy_version(taxonomy: Taxonomy | None = None) -> int:
    """The taxonomy's version, stored on every registered item."""
    return (taxonomy or load_taxonomy()).version


def normalise_labels(labels: dict | None, taxonomy: Taxonomy | None = None) -> dict[str, str | list[str]]:
    """Labels with every value as text: a list for a multi-value facet, a single string otherwise. Values that cannot
    be read are dropped; validate_item reports them."""
    taxonomy = taxonomy or load_taxonomy()
    normalised: dict[str, str | list[str]] = dict()
    for facet_name, raw in (labels or dict()).items():
        facet = taxonomy.facets.get(str(facet_name))
        raw_values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        values = list()
        for raw_value in raw_values:
            value = coerce_label_value(raw_value)
            if value and value not in values:
                values.append(value)
        if not values:
            continue
        if facet is not None and facet.multi:
            normalised[str(facet_name)] = values
        else:
            normalised[str(facet_name)] = values[0]
    return normalised


def label_values(labels: dict, facet_name: str) -> list[str]:
    """A facet's values from normalised labels, always as a list."""
    value = labels.get(facet_name)
    if value is None:
        return list()
    return list(value) if isinstance(value, list) else [value]


def _suggestion(facet: Facet, text: str) -> str:
    loose = _loose(text)
    for value in facet.values.values():
        if _loose(value.name) == loose:
            return value.name
    for value in facet.values.values():
        if any(_loose(synonym) == loose for synonym in value.synonyms):
            return value.name
    close = difflib.get_close_matches(text, list(facet.values), n=1, cutoff=0.6)
    return close[0] if close else str()


def _value_problem(facet: Facet, value: str) -> str:
    message = f'"{value}" is not a value of "{facet.name}".'
    suggestion = _suggestion(facet, value)
    if suggestion:
        return f'{message} Did you mean "{suggestion}"?'
    if len(facet.values) <= 12:
        return f'{message} Its values are: {", ".join(facet.values)}.'
    return f'{message} See library/taxonomy.yaml for its values.'


def validate_facet_values(facet: Facet, raw: object, kind: str) -> list[str]:
    """Problems with one facet's value(s): shape, type and membership in the facet's values."""
    problems: list[str] = list()
    if isinstance(raw, (list, tuple, set)):
        if not facet.multi:
            return [f'"{facet.name}" takes one value, but a list was given ({", ".join(map(str, raw))}).']
        raw_values = list(raw)
    else:
        raw_values = [raw]
    if raw is None or not raw_values:
        return [f'The label "{facet.name}" is empty; give it at least one value.']
    for raw_value in raw_values:
        value = coerce_label_value(raw_value)
        if value is None:
            problems.append(f'"{facet.name}" has a value of a type that cannot be used ({type(raw_value).__name__}); '
                            f'write it as text.')
            continue
        if not value:
            problems.append(f'"{facet.name}" has an empty value.')
            continue
        if facet.free:
            continue
        if value not in facet.values:
            problems.append(_value_problem(facet, value))
    return problems


def validate_item(kind: str, labels: dict, taxonomy: Taxonomy | None = None) -> list[str]:
    """Plain-language problems with an item's labels; an empty list means they are good.

    Checks: the kind exists; every required facet is present and not empty; every facet is one the kind can carry;
    every value is one of the facet's values (unless the facet is free); a single-value facet is not given a list.
    """
    taxonomy = taxonomy or load_taxonomy()
    spec = taxonomy.kinds.get(str(kind))
    if spec is None:
        close = difflib.get_close_matches(str(kind), list(taxonomy.kinds), n=1, cutoff=0.6)
        hint = f' Did you mean "{close[0]}"?' if close else f' Known kinds: {", ".join(taxonomy.kinds)}.'
        return [f'"{kind}" is not a kind in the taxonomy.{hint}']
    if labels is None:
        labels = dict()
    if not isinstance(labels, dict):
        return ['The labels must be a mapping of facet names to values.']
    problems: list[str] = list()
    for facet_name in spec.required:
        raw = labels.get(facet_name)
        if facet_name not in labels or raw is None or (isinstance(raw, (list, tuple, set, str)) and not raw):
            problems.append(f'The required label "{facet_name}" is missing for a {kind} item.')
    for facet_name, raw in labels.items():
        facet_name = str(facet_name)
        facet = taxonomy.facets.get(facet_name)
        if facet is None:
            close = difflib.get_close_matches(facet_name, list(spec.facet_names), n=1, cutoff=0.6)
            hint = f' Did you mean "{close[0]}"?' if close else str()
            problems.append(f'"{facet_name}" is not a facet in the taxonomy.{hint}')
            continue
        if facet_name not in spec.facet_names:
            problems.append(f'A {kind} item cannot carry the label "{facet_name}". It can carry: '
                            f'{", ".join(spec.facet_names)}.')
            continue
        if facet_name in spec.required and (raw is None or (isinstance(raw, (list, tuple, set, str)) and not raw)):
            continue
        problems.extend(validate_facet_values(facet, raw, kind))
    problems.extend(_rule_problems(labels, taxonomy))
    return problems


def _rule_problems(labels: dict, taxonomy: Taxonomy) -> list[str]:
    problems: list[str] = list()
    for rule in getattr(taxonomy, 'rules', list()):
        raw = labels.get(rule['facet'])
        values = [str(value) for value in raw] if isinstance(raw, (list, tuple, set)) else ([str(raw)] if raw is not None else list())
        matched = (rule['contains'] is not None and str(rule['contains']) in values) or any(value in rule['any_of'] for value in values)
        if not matched:
            continue
        for required in rule['require']:
            present = labels.get(required)
            if present is None or (isinstance(present, (list, tuple, set, str)) and not present):
                problems.append(f'The label "{required}" is needed because {rule["facet"]} is {", ".join(values)}: {rule["reason"]}.')
    return problems


def validate_expectation(kind: str, expect: dict, taxonomy: Taxonomy | None = None) -> list[str]:
    """Problems with a facet condition (a screenplay query's `expect`): the facets must belong to the kind and the
    values must exist. A list for a single-value facet is allowed here and means any of those values."""
    taxonomy = taxonomy or load_taxonomy()
    spec = taxonomy.kinds.get(str(kind))
    if spec is None:
        return [f'"{kind}" is not a kind in the taxonomy.']
    if not isinstance(expect, dict) or not expect:
        return ['"expect" must be a mapping of facet names to values.']
    problems: list[str] = list()
    for facet_name, raw in expect.items():
        facet = taxonomy.facets.get(str(facet_name))
        if facet is None or facet.name not in spec.facet_names:
            problems.append(f'A {kind} item cannot carry the label "{facet_name}".')
            continue
        raw_values = list(raw) if isinstance(raw, (list, tuple, set)) else [raw]
        multi_view = Facet(name=facet.name, multi=True, free=facet.free, values=facet.values)
        problems.extend(validate_facet_values(multi_view, raw_values, kind))
    return problems
