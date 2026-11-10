"""The findability test (library-labels-and-search.md section 5.3), run nightly and blocking publication.

1. From its own labels: for every item, queries are written from its facets with templates and synonym swaps. Each
   query names enough of the item's labels to single it out if its labels can single it out at all, and the item must
   rank in the top 3 among the items of its kind (a tie counts against it).
2. From real screenplays: catalogue/queries.yaml holds beats from our films, each with the facet conditions the result
   must meet. A beat passes when one of its top 3 results meets them. If no item meets them, the library is missing
   something: that is a demand-log entry, not a failure. If items meet them but none reaches the top 3, every one of
   those items fails, because the director wanted something we had and could not find it.
3. No silent twins: items of one kind with identical labels are flagged.
4. An item that fails any check is not published until its labels are fixed.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from catalogue.search import Ranking, SearchIndex, value_phrases
from catalogue.taxonomy import (Taxonomy, label_values, load_taxonomy, normalise_labels, validate_expectation,
                                value_words)

logger = logging.getLogger(__name__)

TOP_N = 3
DEFAULT_QUERIES_PATH = Path(__file__).resolve().parent / 'queries.yaml'

# Facets in the order they describe an item best: queries are built from the front of the list, and further facets
# are added only while they still tell the item apart from its neighbours.
QUERY_FACETS: dict[str, tuple[str, ...]] = {
    'plate_3d': ('events', 'leave', 'camera_angle', 'speed', 'mood', 'camera_move', 'shot_size', 'story_uses',
                 'subjects', 'lane_zone', 'pins_state', 'result', 'ball', 'energy', 'series_fit'),
    'character_action': ('character', 'action', 'angle', 'story_uses', 'loopable', 'pairs_with', 'energy',
                         'series_fit'),
    'environment_2d': ('place', 'framing', 'mood', 'story_uses', 'series_fit'),
    'music_bed': ('music_mood', 'arc', 'energy', 'instruments', 'story_uses', 'series_fit'),
    'sfx': ('sfx_category', 'intensity', 'perspective', 'story_uses'),
    'photo_motion': ('photo_subject', 'person', 'era', 'photo_motion', 'story_uses', 'series_fit'),
    'photo_upscaled': ('photo_subject', 'person', 'era'),
    'photo_cutout': ('photo_subject', 'person', 'era'),
    'footage_shot': ('footage_source', 'person', 'shot_size', 'story_uses'),
}
BASE_FACETS: dict[str, tuple[str, ...]] = {
    'plate_3d': ('events', 'leave', 'camera_angle'),
    'character_action': ('character', 'action'),
    'environment_2d': ('place', 'framing'),
    'music_bed': ('music_mood', 'arc'),
    'sfx': ('sfx_category',),
    'photo_motion': ('photo_subject', 'person', 'photo_motion'),
    'photo_upscaled': ('photo_subject', 'person'),
    'photo_cutout': ('photo_subject', 'person'),
    'footage_shot': ('footage_source', 'person'),
}
UNQUERIED_FACETS = ('rights', 'aliases')


@dataclass(frozen=True)
class Template:
    name: str
    # 0 writes value names as words; n picks the n-th way the vocabulary writes the value (its synonyms), cycling.
    phrasing: int
    # Facets mentioned for colour even when they do not tell the item apart, one value each.
    extras: tuple[str, ...]
    # Facets written first, in this order; the rest follow in QUERY_FACETS order.
    lead: tuple[str, ...]


TEMPLATES = (
    Template('labels', 0, (), ()),
    Template('synonyms', 1, ('subjects', 'story_uses'), ('camera_angle', 'subjects', 'events', 'leave', 'speed')),
    Template('story', 2, ('story_uses',), ('story_uses', 'events', 'leave', 'camera_angle')),
    Template('more synonyms', 3, ('subjects',), ('subjects', 'events', 'leave', 'speed')),
)


@dataclass
class QueryCheck:
    source: str
    template: str
    want: str
    rank: int | None
    passed: bool
    top: list[str]
    expect: dict | None = None

    def as_dict(self) -> dict:
        result = {'source': self.source, 'template': self.template, 'want': self.want, 'rank': self.rank,
                  'passed': self.passed, 'top': self.top}
        if self.expect is not None:
            result['expect'] = self.expect
        return result


@dataclass
class ItemResult:
    asset_id: str
    kind: str
    checks: list[QueryCheck] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    twins: list[str] = field(default_factory=list)

    @property
    def failing(self) -> list[QueryCheck]:
        return [check for check in self.checks if not check.passed]

    @property
    def passed(self) -> bool:
        return not self.failing and not self.reasons

    def as_dict(self) -> dict:
        return {'asset_id': self.asset_id, 'kind': self.kind, 'passed': self.passed, 'queries': len(self.checks),
                'failing': [check.as_dict() for check in self.failing], 'reasons': list(self.reasons),
                'twins': list(self.twins)}


@dataclass
class ScreenplayResult:
    want: str
    kind: str
    expect: dict
    source: str
    status: str
    top: list[str]
    satisfying: list[str]
    found: str | None = None
    found_rank: int | None = None

    def as_dict(self) -> dict:
        return {'want': self.want, 'kind': self.kind, 'expect': self.expect, 'source': self.source,
                'status': self.status, 'top': self.top, 'found': self.found, 'found_rank': self.found_rank,
                'items_meeting_expect': len(self.satisfying)}


@dataclass
class FindabilityReport:
    items: dict[str, ItemResult]
    screenplay: list[ScreenplayResult]
    twins: list[list[str]]
    query_problems: list[str]

    @property
    def passed_items(self) -> list[str]:
        return sorted(asset_id for asset_id, result in self.items.items() if result.passed)

    @property
    def failed_items(self) -> list[str]:
        return sorted(asset_id for asset_id, result in self.items.items() if not result.passed)

    @property
    def pass_rate(self) -> float:
        return len(self.passed_items) / len(self.items) if self.items else 1.0

    @property
    def demand(self) -> list[dict]:
        return [{'want': result.want, 'kind': result.kind, 'expect': result.expect, 'source': result.source,
                 'reason': 'missing from the library'}
                for result in self.screenplay if result.status == 'missing']

    def summary(self) -> dict:
        statuses = defaultdict(int)
        for result in self.screenplay:
            statuses[result.status] += 1
        label_checks = [check for result in self.items.values() for check in result.checks if check.source == 'labels']
        return {
            'items_tested': len(self.items),
            'items_passed': len(self.passed_items),
            'items_failed': len(self.failed_items),
            'pass_rate': round(self.pass_rate, 4),
            'label_queries': len(label_checks),
            'label_queries_passed': sum(1 for check in label_checks if check.passed),
            'screenplay_queries': len(self.screenplay),
            'screenplay_found': statuses['found'],
            'screenplay_not_found': statuses['not_found'],
            'screenplay_missing': statuses['missing'],
            'twin_groups': len(self.twins),
        }

    def as_dict(self) -> dict:
        return {'summary': self.summary(),
                'items': {asset_id: result.as_dict() for asset_id, result in sorted(self.items.items())},
                'screenplay': [result.as_dict() for result in self.screenplay],
                'twins': self.twins, 'demand': self.demand, 'query_problems': self.query_problems}


def satisfies(labels: dict, expect: dict, taxonomy: Taxonomy) -> bool:
    """Whether normalised labels meet a facet condition. A multi-value facet must carry every listed value; a
    single-value facet must carry one of them."""
    for facet_name, raw in expect.items():
        facet = taxonomy.facets.get(str(facet_name))
        wanted = [str(value).strip() for value in (raw if isinstance(raw, (list, tuple, set)) else [raw])]
        carried = label_values(labels, str(facet_name))
        if facet is not None and facet.free:
            wanted = [value.lower() for value in wanted]
            carried = [value.lower() for value in carried]
        if facet is not None and facet.multi:
            if not all(value in carried for value in wanted):
                return False
        elif not any(value in carried for value in wanted):
            return False
    return True


def find_twins(records: list[dict], taxonomy: Taxonomy | None = None) -> list[list[str]]:
    """Groups of items of one kind whose labels (aliases aside) are identical."""
    taxonomy = taxonomy or load_taxonomy()
    groups: dict[tuple, list[str]] = defaultdict(list)
    for record in records:
        labels = normalise_labels(record.get('labels') or dict(), taxonomy)
        signature = tuple(sorted((facet_name, tuple(sorted(label_values(labels, facet_name))))
                                 for facet_name in labels if facet_name != 'aliases'))
        groups[(str(record.get('kind')), signature)].append(str(record.get('asset_id')))
    return sorted(sorted(ids) for ids in groups.values() if len(ids) > 1)


def _query_facets(kind: str, taxonomy: Taxonomy) -> tuple[str, ...]:
    if kind in QUERY_FACETS:
        return QUERY_FACETS[kind]
    spec = taxonomy.kinds.get(kind)
    return tuple(name for name in (spec.facet_names if spec else ()) if name not in UNQUERIED_FACETS)


def _covers(peer_labels: dict, labels: dict, facet_name: str) -> bool:
    theirs = label_values(peer_labels, facet_name)
    return all(value in theirs for value in label_values(labels, facet_name))


def discriminating_facets(labels: dict, peers: list[dict], kind: str,
                          taxonomy: Taxonomy) -> tuple[list[str], list[int]]:
    """The facets a query needs to tell this item apart, and the peers (by position) that still carry every one of
    them. Starts from the kind's base facets and adds the next facet only while it removes a competitor."""
    priority = _query_facets(kind, taxonomy)
    base = BASE_FACETS.get(kind, priority[:2])
    chosen = [facet_name for facet_name in base if label_values(labels, facet_name)]
    competitors = [position for position, peer in enumerate(peers)
                   if all(_covers(peer, labels, facet_name) for facet_name in chosen)]
    for facet_name in priority:
        if not competitors:
            break
        if facet_name in chosen or not label_values(labels, facet_name):
            continue
        remaining = [position for position in competitors if _covers(peers[position], labels, facet_name)]
        if len(remaining) < len(competitors):
            chosen.append(facet_name)
            competitors = remaining
    return chosen, competitors


def _phrase(taxonomy: Taxonomy, facet_name: str, value: str, phrasing: int) -> str:
    facet = taxonomy.facets.get(facet_name)
    if facet is None or facet.free or value not in facet.values:
        return value
    if phrasing == 0:
        return value_words(facet, value)
    options = value_phrases(facet, value)
    return options[phrasing % len(options)] if options else value_words(facet, value)


def label_queries(labels: dict, chosen: list[str], kind: str, taxonomy: Taxonomy) -> list[tuple[str, str]]:
    """(template name, query) pairs written from the item's labels: the facets that tell it apart, with each
    template's phrasing and extras."""
    priority = _query_facets(kind, taxonomy)
    queries: list[tuple[str, str]] = list()
    seen: set[str] = set()
    for template_number, template in enumerate(TEMPLATES):
        facet_order = [name for name in template.lead if name in chosen or name in template.extras]
        facet_order += [name for name in priority if name in chosen and name not in facet_order]
        facet_order += [name for name in template.extras if name not in facet_order]
        pieces: list[str] = list()
        for facet_name in facet_order:
            values = label_values(labels, facet_name)
            if not values:
                continue
            if facet_name not in chosen:
                values = [values[template_number % len(values)]]
            for value in values:
                phrase = _phrase(taxonomy, facet_name, value, template.phrasing)
                if phrase:
                    pieces.append(phrase)
        query = ' '.join(pieces)
        if query and query not in seen:
            seen.add(query)
            queries.append((template.name, query))
    return queries


def load_screenplay_queries(path: Path | None = None,
                            taxonomy: Taxonomy | None = None) -> tuple[list[dict], list[str]]:
    """The hand-written screenplay set and any problems with it (unknown kinds, facets or values in `expect`)."""
    taxonomy = taxonomy or load_taxonomy()
    path = path or DEFAULT_QUERIES_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as error:
        return list(), [f'The screenplay query set could not be read: {error}']
    raw_entries = raw.get('queries') if isinstance(raw, dict) else raw
    if not isinstance(raw_entries, list):
        return list(), ['The screenplay query set needs a "queries" list.']
    entries: list[dict] = list()
    problems: list[str] = list()
    for number, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            problems.append(f'Query {number} must be a mapping.')
            continue
        want = str(raw_entry.get('want') or str()).strip()
        kind = str(raw_entry.get('kind') or str()).strip()
        expect = raw_entry.get('expect')
        facets = raw_entry.get('facets')
        entry_problems = list()
        if not want:
            entry_problems.append('it has no "want".')
        entry_problems.extend(validate_expectation(kind, expect, taxonomy))
        if facets is not None:
            entry_problems.extend(f'facets: {problem}' for problem in validate_expectation(kind, facets, taxonomy))
        if entry_problems:
            problems.extend(f'Query {number} ("{want[:60]}"): {problem}' for problem in entry_problems)
            continue
        entries.append({'want': want, 'kind': kind, 'expect': expect, 'facets': facets,
                        'source': str(raw_entry.get('source') or str())})
    return entries, problems


def _top(ranking: Ranking) -> list[str]:
    return [item.asset_id for item in ranking.items[:TOP_N]]


def run_findability(records: list[dict], taxonomy: Taxonomy | None = None,
                    screenplay: list[dict] | None = None, query_problems: list[str] | None = None,
                    index: SearchIndex | None = None) -> FindabilityReport:
    """Tests every record against its own label queries and the screenplay set. `records` is everything that exists
    (what the director could be given); `screenplay` defaults to catalogue/queries.yaml."""
    taxonomy = taxonomy or load_taxonomy()
    if screenplay is None:
        screenplay, loaded_problems = load_screenplay_queries(DEFAULT_QUERIES_PATH, taxonomy)
        query_problems = list(query_problems or list()) + loaded_problems
    index = index or SearchIndex(records, taxonomy)
    by_kind: dict[str, list] = defaultdict(list)
    for entry in index.entries:
        by_kind[entry.kind].append(entry)
    results: dict[str, ItemResult] = dict()

    for kind, entries in by_kind.items():
        peer_labels = [entry.labels for entry in entries]
        for position, entry in enumerate(entries):
            result = ItemResult(asset_id=entry.asset_id, kind=kind)
            results[entry.asset_id] = result
            others = peer_labels[:position] + peer_labels[position + 1:]
            other_ids = [peer.asset_id for peer in entries[:position] + entries[position + 1:]]
            chosen, competitors = discriminating_facets(entry.labels, others, kind, taxonomy)
            if len(competitors) >= TOP_N:
                names = ', '.join(other_ids[number] for number in competitors[:5])
                result.reasons.append(f'{len(competitors)} other {kind} items carry every label this one has, so its '
                                      f'labels cannot set it apart ({names}). Add the facet that makes it different.')
            for template_name, want in label_queries(entry.labels, chosen, kind, taxonomy):
                ranking = index.rank(want, kind=kind)
                rank = ranking.pessimistic_rank(entry.asset_id)
                result.checks.append(QueryCheck(source='labels', template=template_name, want=want, rank=rank,
                                                passed=rank is not None and rank <= TOP_N, top=_top(ranking)))

    screenplay_results: list[ScreenplayResult] = list()
    for query in screenplay:
        kind = query['kind']
        entries = by_kind.get(kind, list())
        satisfying = [entry.asset_id for entry in entries if satisfies(entry.labels, query['expect'], taxonomy)]
        if not satisfying:
            screenplay_results.append(ScreenplayResult(want=query['want'], kind=kind, expect=query['expect'],
                                                       source=query.get('source', str()), status='missing', top=list(),
                                                       satisfying=list()))
            continue
        ranking = index.rank(query['want'], query.get('facets'), kind)
        ranks = sorted((ranking.pessimistic_rank(asset_id), asset_id) for asset_id in satisfying)
        best_rank, best_id = ranks[0]
        found = best_rank is not None and best_rank <= TOP_N
        screenplay_results.append(ScreenplayResult(want=query['want'], kind=kind, expect=query['expect'],
                                                   source=query.get('source', str()),
                                                   status='found' if found else 'not_found', top=_top(ranking),
                                                   satisfying=satisfying, found=best_id if found else None,
                                                   found_rank=best_rank))
        if not found:
            for asset_id in satisfying:
                rank = ranking.pessimistic_rank(asset_id)
                results[asset_id].checks.append(QueryCheck(source='screenplay', template=query.get('source', str()),
                                                           want=query['want'], rank=rank, passed=False,
                                                           top=_top(ranking), expect=query['expect']))

    twins = find_twins([entry.record | {'asset_id': entry.asset_id, 'kind': entry.kind} for entry in index.entries],
                       taxonomy)
    for group in twins:
        for asset_id in group:
            if asset_id in results:
                results[asset_id].twins = [other for other in group if other != asset_id]
    return FindabilityReport(items=results, screenplay=screenplay_results, twins=twins,
                             query_problems=list(query_problems or list()))
