"""Library search: catalogue records in, ranked results out, and never empty while the kind has any item.

Pure Python (the taxonomy is read with PyYAML), so the production server reuses this module as it is.

How one search works:
1. Normalise words. Lowercase, apostrophes dropped ("pin's" reads "pins"), light stemming ("holding" reads "hold",
   "bedposts" reads "bedpost"), hyphens kept only between digits ("7-10"), and inside phrases number words read as
   digits ("seven ten split" reads "7-10 split").
2. Interpret. Every taxonomy value name and synonym is a phrase; the longest phrase wins, so "pin's eye view" maps to
   camera_angle=pin_cam and "holding its breath" maps to story_uses=tension. Joined compounds match too ("slowmo",
   "lays down" for "laydown").
3. Filter by the given facets. If nothing passes, facets are relaxed in a fixed order (mood, then speed, then
   camera_move, then camera_angle, then the rest) and the result says exactly which were relaxed.
4. Rank. BM25 over a document made of the title, description, label values with their definitions and synonyms, and
   aliases; plus a boost for every facet value the query maps to that the item carries, weighted by how rare it is.
"""

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from catalogue.taxonomy import (YES_NO_VALUES, Facet, Taxonomy, label_values, load_taxonomy, normalise_labels,
                                value_words)

logger = logging.getLogger(__name__)

RELAX_ORDER = ('mood', 'speed', 'camera_move', 'camera_angle')
# Facets given beyond the fixed four are relaxed after them, the least defining first.
LATER_RELAX_ORDER = ('energy', 'series_fit', 'rights', 'shot_size', 'lane_zone', 'ball', 'instruments', 'perspective',
                     'intensity', 'story_uses', 'pins_state', 'result', 'subjects', 'angle', 'framing', 'era',
                     'photo_motion', 'loopable', 'pairs_with', 'arc', 'music_mood', 'events', 'leave', 'action',
                     'character', 'place', 'sfx_category', 'photo_subject', 'footage_source', 'person', 'aliases')
# Facets that say nothing about what is seen or heard stay out of the search document.
UNSEARCHED_FACETS = ('rights',)

BM25_K1 = 1.2
BM25_B = 0.75
FACET_WEIGHT = 3.0
# How much naming a value of each facet says about what the beat wants. Story uses are the bridge from screenplay to
# library (design section 4), so they count most; subjects and zones are the nouns any sentence mentions in passing
# ("the ball", "the lane", "the house"), so they count least. Facets not listed count 1.
FACET_IMPORTANCE = {'story_uses': 1.3, 'leave': 1.2, 'subjects': 0.6, 'lane_zone': 0.6, 'series_fit': 0.5,
                    'energy': 0.5, 'intensity': 0.5, 'rights': 0.0}
# A phrase of several words ("holding its breath", "pin's eye view") is a deliberate choice; each extra word adds this.
PHRASE_WORD_BONUS = 0.25
ALIAS_WEIGHT = 4.0
EXPANSION_WEIGHT = 0.5
TITLE_REPEAT = 2
VALUE_REPEAT = 2
ALIAS_REPEAT = 2
# Below this, the best result is a guess rather than a match: the search is reported weak (for the demand log).
WEAK_SCORE = 2.0
SCORE_EPSILON = 1e-9
MAX_COMPOUND_PARTS = 3

APOSTROPHE_REGEX = re.compile(r"['`‘’´]")
DIGIT_SLASH_REGEX = re.compile(r'(?<=\d)\s*[/–]\s*(?=\d)')
NON_TOKEN_REGEX = re.compile(r'[^a-z0-9\-]+')
LOOSE_HYPHEN_REGEX = re.compile(r'(?<![0-9])-|-(?![0-9])')
NUMBER_TOKEN_REGEX = re.compile(r'^\d+(-\d+)*$')
UNDOUBLE_EXCEPTIONS = ('l', 's', 'z')

SPELLINGS = {'centre': 'center', 'centres': 'centers', 'colour': 'color', 'colours': 'colors', 'grey': 'gray',
             'metre': 'meter', 'metres': 'meters', 'theatre': 'theater', 'favourite': 'favorite'}


def stem(word: str) -> str:
    """A light suffix stemmer: plurals, -ing, -ed, -ly and a final e. Both the documents and the queries go through
    it, so all that matters is that the forms of one word meet."""
    if len(word) <= 3 or not word.isalpha():
        return word
    if word.endswith('ies') and len(word) > 4:
        word = word[:-3] + 'y'
    elif word.endswith('sses'):
        word = word[:-2]
    elif word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        word = word[:-1]
    if word.endswith('ing') and len(word) - 3 >= 3:
        word = _undouble(word[:-3])
    elif word.endswith('ed') and not word.endswith('eed') and len(word) - 2 >= 3:
        word = _undouble(word[:-2])
    if word.endswith('ly') and len(word) - 2 >= 4:
        word = word[:-2]
    if word.endswith('e') and len(word) >= 4:
        word = word[:-1]
    return word


def _undouble(word: str) -> str:
    if len(word) >= 3 and word[-1] == word[-2] and word[-1] not in UNDOUBLE_EXCEPTIONS and word[-1] not in 'aeiou':
        return word[:-1]
    return word


RAW_NUMBER_WORDS = {'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4', 'five': '5', 'six': '6',
                    'seven': '7', 'eight': '8', 'nine': '9', 'ten': '10', 'eleven': '11', 'twelve': '12'}
NUMBER_WORDS = {stem(word): digits for word, digits in RAW_NUMBER_WORDS.items()}
# Dropped inside phrases, so "holding its breath" and "holding her breath" meet "holding breath".
ARTICLES = frozenset(stem(word) for word in ('a', 'an', 'the', 'its', 'his', 'her', 'their', 'your', 'my', 'our'))
# Dropped from the BM25 text. Direction words (up, down, behind, above, front, back, left, right) stay: they matter.
STOPWORDS = frozenset(stem(word) for word in (
    'a an the and or but nor of to in on at by for with from into onto as is are was were be been being it its this '
    'that these those there here we us our ours you your yours he him his she her hers they them their theirs i me my '
    'mine so than then just very too also not no all any each every some more most much such only own same both few '
    'other what which who whom whose when where why how can will would should could may might must do does did doing '
    'has have had having get gets got about again against between through during until while once if else like '
    'really quite even still yet one ones shot shots plate plates clip clips scene scenes we\'re it\'s that\'s '
    'something someone thing things way while'
).replace("'", '').split())


def word_tokens(text: str) -> list[str]:
    """Lowercased, stemmed word tokens. Hyphens survive only between digits ("7-10", "1-2-10")."""
    text = APOSTROPHE_REGEX.sub(str(), str(text).lower())
    text = DIGIT_SLASH_REGEX.sub('-', text)
    text = NON_TOKEN_REGEX.sub(' ', text)
    text = LOOSE_HYPHEN_REGEX.sub(' ', text)
    return [stem(SPELLINGS.get(token, token)) for token in text.split()]


def _is_number(token: str) -> bool:
    return bool(NUMBER_TOKEN_REGEX.match(token))


def phrase_tokens(tokens: list[str]) -> list[str]:
    """Tokens for phrase matching: articles dropped, number words as digits, neighbouring numbers joined ("7 10" and
    "seven ten" both read "7-10")."""
    phrase: list[str] = list()
    for token in tokens:
        if token in ARTICLES:
            continue
        token = NUMBER_WORDS.get(token, token)
        if phrase and _is_number(token) and _is_number(phrase[-1]):
            phrase[-1] = f'{phrase[-1]}-{token}'
            continue
        phrase.append(token)
    return phrase


def text_tokens(tokens: list[str]) -> list[str]:
    return [token for token in tokens if token not in STOPWORDS]


def phrase_of(text: str) -> tuple[str, ...]:
    return tuple(phrase_tokens(word_tokens(text)))


def value_phrases(facet: Facet, value_name: str) -> list[str]:
    """Every way the vocabulary writes one value: its name as words and its synonyms. A bare number or yes/no is only
    a phrase with its facet ("energy 3", "leave 10"); "none" is never a phrase on its own."""
    value = facet.values[value_name]
    forms: list[str] = list()
    if value_name.isdigit() or value_name in YES_NO_VALUES:
        forms.extend([f'{facet.words} {value_name}', f'{value_name} {facet.words}'])
    elif value_name != 'none':
        forms.append(value_name.replace('_', ' '))
    forms.extend(value.synonyms)
    return forms


@dataclass(frozen=True)
class Interpretation:
    """Words in the query that name one or more facet values."""
    words: str
    targets: tuple[tuple[str, str], ...]

    @property
    def weight(self) -> float:
        # A phrase naming two different things counts half for each; "deck" naming pin_deck in two facets does not.
        share = 1.0 / max(1, len({value for _, value in self.targets}))
        return share * (1.0 + PHRASE_WORD_BONUS * (len(self.words.split()) - 1))


class PhraseTable:
    """Phrase (normalised token tuple) to the facet values it names, for the facets of one kind."""

    def __init__(self, taxonomy: Taxonomy, kind: str | None) -> None:
        self.phrases: dict[tuple[str, ...], set[tuple[str, str]]] = defaultdict(set)
        for facet in taxonomy.kind_facets(kind):
            if facet.free:
                continue
            for value_name in facet.values:
                for form in value_phrases(facet, value_name):
                    phrase = phrase_of(form)
                    if not phrase:
                        continue
                    self.phrases[phrase].add((facet.name, value_name))
                    if 1 < len(phrase) <= MAX_COMPOUND_PARTS:
                        self.phrases[(''.join(phrase),)].add((facet.name, value_name))
        self.longest = max((len(phrase) for phrase in self.phrases), default=1)

    def lookup(self, words: list[str]) -> set[tuple[str, str]] | None:
        found = self.phrases.get(tuple(words))
        if found is None and 1 < len(words) <= MAX_COMPOUND_PARTS:
            found = self.phrases.get((''.join(words),))
        return found

    def interpret(self, tokens: list[str]) -> list[Interpretation]:
        """Greedy longest match, left to right; each word belongs to at most one phrase."""
        found: list[Interpretation] = list()
        position = 0
        while position < len(tokens):
            for length in range(min(self.longest, len(tokens) - position), 0, -1):
                words = tokens[position:position + length]
                targets = self.lookup(words)
                if targets:
                    found.append(Interpretation(words=' '.join(words), targets=tuple(sorted(targets))))
                    position += length
                    break
            else:
                position += 1
        return found


@dataclass
class _Entry:
    record: dict
    asset_id: str
    kind: str
    title: str
    labels: dict
    terms: Counter
    length: int
    pairs: frozenset
    alias_phrases: tuple[tuple[str, ...], ...]


def word_pairs(tokens: list[str]) -> list[str]:
    """Neighbouring word pairs ("low lan" from "low on the lane"), so a want that echoes a description's wording scores
    above one that only shares its words."""
    return [f'{first} {second}' for first, second in zip(tokens, tokens[1:])]


def _field_terms(text: str, repeat: int = 1, with_pairs: bool = True) -> list[str]:
    words = text_tokens(word_tokens(text))
    return (words + (word_pairs(words) if with_pairs else list())) * repeat


def _entry_text(record: dict, labels: dict, taxonomy: Taxonomy) -> list[str]:
    # Word pairs come from the written sentences (title, description, definitions, aliases); synonyms are already
    # matched as whole phrases.
    terms: list[str] = list()
    terms.extend(_field_terms(str(record.get('title') or str()), TITLE_REPEAT))
    terms.extend(_field_terms(str(record.get('description') or str())))
    for facet_name in labels:
        facet = taxonomy.facets.get(facet_name)
        if facet_name in UNSEARCHED_FACETS or facet_name == 'aliases':
            continue
        for value_name in label_values(labels, facet_name):
            if facet is None or facet.free:
                terms.extend(_field_terms(value_name, ALIAS_REPEAT))
                continue
            value = facet.values.get(value_name)
            if value is None:
                continue
            terms.extend(_field_terms(value_words(facet, value_name), VALUE_REPEAT, with_pairs=False))
            terms.extend(_field_terms(value.definition))
            for synonym in value.synonyms:
                terms.extend(_field_terms(synonym, with_pairs=False))
    for alias in _record_aliases(record, labels):
        terms.extend(_field_terms(alias, ALIAS_REPEAT))
    return terms


def _record_aliases(record: dict, labels: dict) -> list[str]:
    aliases = label_values(labels, 'aliases')
    extra = record.get('aliases') or list()
    for alias in extra if isinstance(extra, list) else [extra]:
        if isinstance(alias, str) and alias.strip() and alias.strip() not in aliases:
            aliases.append(alias.strip())
    return aliases


def _make_entry(record: dict, taxonomy: Taxonomy) -> _Entry | None:
    asset_id = str(record.get('asset_id') or record.get('id') or str())
    kind = str(record.get('kind') or str())
    if not asset_id or not kind:
        return None
    labels = normalise_labels(record.get('labels') or dict(), taxonomy)
    terms = Counter(_entry_text(record, labels, taxonomy))
    pairs = set()
    for facet_name in labels:
        facet = taxonomy.facets.get(facet_name)
        for value_name in label_values(labels, facet_name):
            pairs.add((facet_name, value_name.lower() if facet is None or facet.free else value_name))
    alias_phrases = list()
    for alias in _record_aliases(record, labels) + label_values(labels, 'person'):
        phrase = phrase_of(alias)
        if phrase and phrase not in alias_phrases:
            alias_phrases.append(phrase)
    return _Entry(record=record, asset_id=asset_id, kind=kind, title=str(record.get('title') or str()), labels=labels,
                  terms=terms, length=sum(terms.values()), pairs=frozenset(pairs), alias_phrases=tuple(alias_phrases))


class _Corpus:
    """BM25 statistics and facet-value postings over the items of one kind (or of every kind)."""

    def __init__(self, entries: list[_Entry]) -> None:
        self.entries = entries
        self.count = len(entries)
        self.average_length = (sum(entry.length for entry in entries) / self.count) if self.count else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.value_docs: dict[tuple[str, str], set[int]] = defaultdict(set)
        self.alias_docs: dict[tuple[str, ...], set[int]] = defaultdict(set)
        for position, entry in enumerate(entries):
            for term, frequency in entry.terms.items():
                self.postings[term].append((position, frequency))
            for pair in entry.pairs:
                self.value_docs[pair].add(position)
            for phrase in entry.alias_phrases:
                self.alias_docs[phrase].add(position)
        self.alias_longest = max((len(phrase) for phrase in self.alias_docs), default=0)

    def _idf(self, frequency: int) -> float:
        return math.log(1.0 + (self.count - frequency + 0.5) / (frequency + 0.5))

    def bm25(self, query_terms: dict[str, float]) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for term, weight in query_terms.items():
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self._idf(len(postings))
            for position, frequency in postings:
                length_ratio = self.entries[position].length / self.average_length
                saturation = frequency * (BM25_K1 + 1) / (frequency + BM25_K1 * (1 - BM25_B + BM25_B * length_ratio))
                scores[position] += weight * idf * saturation
        return scores

    def value_idf(self, pair: tuple[str, str]) -> float:
        return self._idf(len(self.value_docs.get(pair, ())))


@dataclass
class RankedItem:
    asset_id: str
    kind: str
    title: str
    score: float
    text_score: float
    facet_score: float
    matched: list[str]
    labels: dict
    record: dict


@dataclass
class Ranking:
    """Every candidate of one search, best first, with what the search understood and what it relaxed."""
    want: str
    kind: str | None
    facets: dict
    ignored_facets: list[dict]
    interpreted: list[Interpretation]
    relaxed: list[str]
    items: list[RankedItem]
    not_in_library: list[str]
    error: str = str()

    @property
    def weak(self) -> bool:
        if not self.items:
            return True
        top = self.items[0]
        if self.relaxed or top.score < WEAK_SCORE:
            return True
        return bool(self.interpreted) and not top.matched

    def pessimistic_rank(self, asset_id: str) -> int | None:
        """The item's rank counting every tie against it: with four items on the same score, each is 4th."""
        score = next((item.score for item in self.items if item.asset_id == asset_id), None)
        if score is None:
            return None
        return sum(1 for item in self.items if item.score >= score - SCORE_EPSILON)

    def as_dict(self, limit: int = 10) -> dict:
        result = {
            'want': self.want,
            'kind': self.kind,
            'facets': self.facets,
            'ignored_facets': self.ignored_facets,
            'interpreted': [{'words': entry.words, 'values': [f'{facet}={value}' for facet, value in entry.targets]}
                            for entry in self.interpreted],
            'relaxed': list(self.relaxed),
            'exact': not self.relaxed,
            'weak': self.weak,
            'not_in_library': list(self.not_in_library),
            'candidates': len(self.items),
            'results': [{'asset_id': item.asset_id, 'kind': item.kind, 'title': item.title,
                         'score': round(item.score, 3), 'matched': item.matched, 'labels': item.labels}
                        for item in self.items[:max(0, limit)]],
        }
        if self.error:
            result['error'] = self.error
        return result


class SearchIndex:
    """An index over catalogue records. `published_only` and `approved_only` keep held-back items out of production
    searches; the findability test searches every item it is judging."""

    def __init__(self, records: list[dict], taxonomy: Taxonomy | None = None, published_only: bool = False,
                 approved_only: bool = False) -> None:
        self.taxonomy = taxonomy or load_taxonomy()
        self.entries: list[_Entry] = list()
        for record in records:
            if published_only and record.get('published') is not True:
                continue
            if approved_only and record.get('approved') is not True:
                continue
            entry = _make_entry(record, self.taxonomy)
            if entry is not None:
                self.entries.append(entry)
        self._corpora: dict[str | None, _Corpus] = dict()
        self._phrase_tables: dict[str | None, PhraseTable] = dict()

    def corpus(self, kind: str | None) -> _Corpus:
        if kind not in self._corpora:
            self._corpora[kind] = _Corpus([entry for entry in self.entries if kind is None or entry.kind == kind])
        return self._corpora[kind]

    def phrase_table(self, kind: str | None) -> PhraseTable:
        if kind not in self._phrase_tables:
            self._phrase_tables[kind] = PhraseTable(self.taxonomy, kind)
        return self._phrase_tables[kind]

    def interpret(self, want: str, kind: str | None = None) -> list[Interpretation]:
        return self.phrase_table(kind).interpret(phrase_tokens(word_tokens(want or str())))

    def _normalise_filters(self, facets: dict | None, kind: str | None) -> tuple[dict[str, list[str]], list[dict]]:
        filters: dict[str, list[str]] = dict()
        ignored: list[dict] = list()
        allowed = {facet.name for facet in self.taxonomy.kind_facets(kind)}
        for facet_name, raw in (facets or dict()).items():
            facet = self.taxonomy.facets.get(str(facet_name))
            if facet is None or facet.name not in allowed:
                ignored.append({'facet': str(facet_name), 'value': raw,
                                'reason': f'"{facet_name}" is not a facet of {kind or "any kind"}.'})
                continue
            values: list[str] = list()
            for raw_value in raw if isinstance(raw, (list, tuple, set)) else [raw]:
                text = str(raw_value).strip() if not isinstance(raw_value, bool) else ('yes' if raw_value else 'no')
                if facet.free:
                    if text:
                        values.append(text.lower())
                    continue
                value = self.taxonomy.resolve_value(facet.name, text)
                if value is None:
                    ignored.append({'facet': facet.name, 'value': text,
                                    'reason': f'"{text}" is not a value of "{facet.name}".'})
                elif value not in values:
                    values.append(value)
            if values:
                filters[facet.name] = values
        return filters, ignored

    def _passing(self, corpus: _Corpus, filters: dict[str, list[str]]) -> list[int]:
        passing = list()
        for position, entry in enumerate(corpus.entries):
            if all(_entry_matches(entry, facet_name, values, self.taxonomy) for facet_name, values in filters.items()):
                passing.append(position)
        return passing

    def _filter_with_relaxation(self, corpus: _Corpus, filters: dict[str, list[str]]) -> tuple[list[int], list[str]]:
        passing = self._passing(corpus, filters)
        if passing or not filters or not corpus.entries:
            return passing, list()
        order = [name for name in RELAX_ORDER if name in filters]
        later = sorted((name for name in filters if name not in RELAX_ORDER),
                       key=lambda name: (LATER_RELAX_ORDER.index(name) if name in LATER_RELAX_ORDER
                                         else len(LATER_RELAX_ORDER), name))
        order.extend(later)
        for count in range(1, len(order) + 1):
            relaxed = order[:count]
            passing = self._passing(corpus, {name: values for name, values in filters.items() if name not in relaxed})
            if not passing:
                continue
            # Put back every earlier facet the result does not need, so the report names only what was given up.
            for facet_name in order[:count - 1]:
                trial = [name for name in relaxed if name != facet_name]
                trial_passing = self._passing(corpus, {name: values for name, values in filters.items()
                                                       if name not in trial})
                if trial_passing:
                    relaxed, passing = trial, trial_passing
            return passing, relaxed
        return list(range(len(corpus.entries))), order

    def rank(self, want: str, facets: dict | None = None, kind: str | None = None) -> Ranking:
        """Every candidate, best first. `search` is the same, cut to `limit` and returned as a plain dict."""
        want = str(want or str())
        if kind is not None and kind not in self.taxonomy.kinds:
            return Ranking(want=want, kind=kind, facets=dict(), ignored_facets=list(), interpreted=list(),
                           relaxed=list(), items=list(), not_in_library=list(),
                           error=f'"{kind}" is not a kind in the taxonomy.')
        corpus = self.corpus(kind)
        tokens = word_tokens(want)
        interpreted = self.phrase_table(kind).interpret(phrase_tokens(tokens))
        filters, ignored = self._normalise_filters(facets, kind)
        candidates, relaxed = self._filter_with_relaxation(corpus, filters)

        query_words = text_tokens(tokens)
        query_terms: dict[str, float] = {term: 1.0 for term in query_words + word_pairs(query_words)}
        hits: list[tuple[tuple[tuple[str, str], ...], float]] = list()
        for entry in interpreted:
            hits.append((entry.targets, entry.weight))
            for facet_name, value_name in entry.targets:
                facet = self.taxonomy.facets[facet_name]
                for term in text_tokens(word_tokens(value_words(facet, value_name))):
                    query_terms[term] = max(query_terms.get(term, 0.0), EXPANSION_WEIGHT)
        for facet_name, values in filters.items():
            for value_name in values:
                hits.append((((facet_name, value_name),), 1.0))
        # One boost per distinct set of named values, however often the query repeats it.
        unique_hits: dict[tuple[tuple[str, str], ...], float] = dict()
        for targets, weight in hits:
            unique_hits[targets] = max(unique_hits.get(targets, 0.0), weight)

        text_scores = corpus.bm25(query_terms)
        alias_hits = _alias_hits(corpus, phrase_tokens(tokens))
        items: list[RankedItem] = list()
        for position in candidates:
            entry = corpus.entries[position]
            facet_score = 0.0
            matched: list[str] = list()
            for targets, weight in unique_hits.items():
                carried = [pair for pair in targets if pair in entry.pairs]
                if not carried:
                    continue
                facet_score += FACET_WEIGHT * weight * max(FACET_IMPORTANCE.get(pair[0], 1.0) * corpus.value_idf(pair)
                                                           for pair in carried)
                for facet_name, value_name in carried:
                    label = f'{facet_name}={value_name}'
                    if label not in matched:
                        matched.append(label)
            facet_score += ALIAS_WEIGHT * alias_hits.get(position, 0)
            text_score = text_scores.get(position, 0.0)
            items.append(RankedItem(asset_id=entry.asset_id, kind=entry.kind, title=entry.title,
                                    score=text_score + facet_score, text_score=text_score, facet_score=facet_score,
                                    matched=matched, labels=entry.labels, record=entry.record))
        items.sort(key=lambda item: (-item.score, item.asset_id))

        not_in_library = sorted({f'{facet_name}={value_name}' for entry in interpreted
                                 for facet_name, value_name in entry.targets
                                 if not corpus.value_docs.get((facet_name, value_name))
                                 and len(entry.targets) == 1})
        return Ranking(want=want, kind=kind, facets=filters, ignored_facets=ignored, interpreted=interpreted,
                       relaxed=relaxed, items=items, not_in_library=not_in_library)

    def search(self, want: str, facets: dict | None = None, kind: str | None = None, limit: int = 10) -> dict:
        """Ranked results as a plain dict: results with scores and matched values, `relaxed` (the facets given up, in
        order), `exact`, `weak` (for the demand log), `interpreted` and `not_in_library`. Never empty while the kind
        has any item."""
        return self.rank(want, facets, kind).as_dict(limit)


def _entry_matches(entry: _Entry, facet_name: str, values: list[str], taxonomy: Taxonomy) -> bool:
    facet = taxonomy.facets.get(facet_name)
    carried = label_values(entry.labels, facet_name)
    if facet is not None and facet.free:
        carried = [value.lower() for value in carried]
    if facet is not None and facet.multi:
        return all(value in carried for value in values)
    return any(value in carried for value in values)


def _alias_hits(corpus: _Corpus, tokens: list[str]) -> dict[int, int]:
    hits: dict[int, int] = defaultdict(int)
    if not corpus.alias_docs:
        return hits
    seen: set[tuple[str, ...]] = set()
    for start in range(len(tokens)):
        for length in range(1, min(corpus.alias_longest, len(tokens) - start) + 1):
            phrase = tuple(tokens[start:start + length])
            if phrase in seen:
                continue
            seen.add(phrase)
            for position in corpus.alias_docs.get(phrase, ()):
                hits[position] += 1
    return hits


def demand_entry(result: dict) -> dict | None:
    """The demand-log entry for a search result (design section 5.4), or None when the search found a good match.
    Production logs these; the log becomes the node's next jobs."""
    if not result.get('weak'):
        return None
    top = result.get('results') or list()
    return {'want': result.get('want'), 'kind': result.get('kind'), 'facets': result.get('facets'),
            'relaxed': result.get('relaxed'), 'not_in_library': result.get('not_in_library'),
            'top': [entry['asset_id'] for entry in top[:3]], 'top_score': top[0]['score'] if top else None}


_default_index: SearchIndex | None = None


def load_catalogue(path: str | Path) -> list[dict]:
    """The records from a catalogue.json (either {"records": [...]} or a bare list)."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    records = data.get('records', list()) if isinstance(data, dict) else data
    return [record for record in records if isinstance(record, dict)]


def use_catalogue(source: str | Path | list[dict], taxonomy: Taxonomy | None = None,
                  published_only: bool = True, approved_only: bool = False) -> SearchIndex:
    """Sets the index that the module-level `search` uses, from a catalogue.json path or a list of records."""
    global _default_index
    records = load_catalogue(source) if isinstance(source, (str, Path)) else source
    _default_index = SearchIndex(records, taxonomy, published_only=published_only, approved_only=approved_only)
    return _default_index


def search(want: str, facets: dict | None = None, kind: str | None = None, limit: int = 10,
           index: SearchIndex | None = None) -> dict:
    """Search the library. Uses `index`, or the one set by `use_catalogue`."""
    index = index or _default_index
    if index is None:
        raise RuntimeError('No catalogue is loaded: call use_catalogue(path) first, or pass index=.')
    return index.search(want, facets, kind, limit)


# The director's prompt index: one line per approved, published item. Columns per kind, in order; a group renders as
# space-separated values, a multi-value facet joins with "+", and "-" stands for an empty group.
PROMPT_LAYOUT: dict[str, tuple[tuple[str, ...], ...]] = {
    'plate_3d': (('camera_angle', 'camera_move', 'shot_size', 'speed'), ('events', 'leave'), ('story_uses',),
                 ('mood', 'energy')),
    'character_action': (('character', 'action', 'angle'), ('story_uses',), ('loopable', 'pairs_with', 'energy')),
    'environment_2d': (('place', 'framing', 'mood'), ('story_uses',)),
    'music_bed': (('music_mood', 'arc', 'energy'), ('story_uses',), ('instruments',)),
    'sfx': (('sfx_category', 'intensity', 'perspective'), ('story_uses',)),
    'photo_motion': (('photo_subject', 'era', 'photo_motion'), ('person',), ('story_uses',)),
    'photo_upscaled': (('photo_subject', 'era'), ('person',)),
    'photo_cutout': (('photo_subject', 'era'), ('person',)),
    'footage_shot': (('footage_source', 'shot_size'), ('person',), ('story_uses',)),
}
PROMPT_PREFIXES = {'leave': 'leave:', 'energy': 'e', 'intensity': 'i', 'pairs_with': 'pairs:'}
# Lengths are worth a column for kinds the director cuts to time.
PROMPT_DURATION_KINDS = ('music_bed', 'sfx', 'footage_shot', 'character_action')


def _prompt_value(facet_name: str, value: str) -> str:
    if facet_name == 'loopable':
        return 'loop' if value == 'yes' else str()
    return f'{PROMPT_PREFIXES.get(facet_name, str())}{value.replace(" ", "_")}'


def prompt_line(record: dict, taxonomy: Taxonomy | None = None) -> str:
    """One record as `<asset_id> | <group> | <group> ...` following PROMPT_LAYOUT for its kind."""
    taxonomy = taxonomy or load_taxonomy()
    labels = normalise_labels(record.get('labels') or dict(), taxonomy)
    kind = str(record.get('kind') or str())
    layout = PROMPT_LAYOUT.get(kind, ((name,) for name in labels if name not in ('aliases', 'rights')))
    columns = [str(record.get('asset_id') or record.get('id'))]
    for group in layout:
        parts = list()
        for facet_name in group:
            values = [_prompt_value(facet_name, value) for value in label_values(labels, facet_name)]
            values = [value for value in values if value]
            if values:
                parts.append('+'.join(values))
        columns.append(' '.join(parts) if parts else '-')
    duration = (record.get('measured') or dict()).get('duration_s')
    if kind in PROMPT_DURATION_KINDS and isinstance(duration, (int, float)):
        columns.append(f'{duration:.0f}s' if duration >= 10 else f'{duration:.1f}s')
    return ' | '.join(columns)


def prompt_header(kind: str) -> str:
    groups = [' '.join(group) for group in PROMPT_LAYOUT.get(kind, ())]
    duration = ' | length' if kind in PROMPT_DURATION_KINDS else str()
    return f'# {kind}: id | {" | ".join(groups)}{duration}'


def prompt_index(records: list[dict], taxonomy: Taxonomy | None = None) -> str:
    """The catalogue index for the director's cached prompt: for each kind a header naming the columns, then one line
    per approved, published item, sorted by id."""
    taxonomy = taxonomy or load_taxonomy()
    chosen = [record for record in records if record.get('approved') is True and record.get('published') is True]
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for record in chosen:
        by_kind[str(record.get('kind') or str())].append(record)
    blocks: list[str] = list()
    for kind in sorted(by_kind):
        lines = [prompt_header(kind)]
        lines.extend(prompt_line(record, taxonomy) for record in sorted(by_kind[kind],
                                                                        key=lambda record: str(record.get('asset_id'))))
        blocks.append('\n'.join(lines))
    return '\n'.join(blocks) + ('\n' if blocks else str())


def estimate_tokens(line: str) -> int:
    """A rough token count (word pieces plus punctuation), for keeping prompt lines near their budget."""
    return len(re.findall(r'[A-Za-z]+|\d+|[^\sA-Za-z\d]', line))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m catalogue.search', description='Search a built catalogue.')
    parser.add_argument('catalogue', help='a catalogue.json')
    parser.add_argument('want', help='what the screenplay wants, in plain words')
    parser.add_argument('--kind', default=None)
    parser.add_argument('--facet', action='append', default=list(), help='facet=value (repeatable)')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--all', action='store_true', help='include items that are not published')
    arguments = parser.parse_args(argv)
    facets: dict[str, list[str]] = defaultdict(list)
    for pair in arguments.facet:
        name, _, value = pair.partition('=')
        facets[name.strip()].append(value.strip())
    index = SearchIndex(load_catalogue(arguments.catalogue), published_only=not arguments.all)
    print(json.dumps(index.search(arguments.want, dict(facets) or None, arguments.kind, arguments.limit), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
