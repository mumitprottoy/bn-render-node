"""Tests for the catalogue: label checks, search, the findability test, the prompt index and the nightly build.

    /usr/local/bin/python3 -m unittest tests.test_catalogue -v

The fixtures are fake 3D plates labelled the way job files label them (energy as a YAML number, as a job file would
write it): strikes from several angles and speeds, leaves (10, 7, 7-10, washout, bucket, big four) from overhead and
pin cam, spares, ball motion, the Ten Frames V2 shots, and house shots in all four moods.
"""

import contextlib
import copy
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from catalogue.build import build, registered_asset_ids  # noqa: E402
from catalogue.findability import (discriminating_facets, find_twins, label_queries, load_screenplay_queries,  # noqa: E402
                                   run_findability)
from catalogue.search import (SearchIndex, demand_entry, estimate_tokens, phrase_tokens, prompt_index,  # noqa: E402
                              prompt_line, word_tokens)
from catalogue.taxonomy import load_taxonomy, normalise_labels, taxonomy_version, validate_item  # noqa: E402
from catalogue.validate import main as validate_main  # noqa: E402

TAXONOMY = load_taxonomy()


def plate(slug: str, title: str, description: str, *, subjects: list[str], events: list[str], angle: str, move: str,
          size: str, speed: str, mood: str, story: list[str], series: list[str], energy: int, **optional: object) -> dict:
    labels: dict = {'subjects': subjects, 'events': events, 'camera_angle': angle, 'camera_move': move,
                    'shot_size': size, 'speed': speed, 'mood': mood, 'story_uses': story, 'series_fit': series,
                    'energy': energy, 'rights': 'owned_render'}
    labels.update(optional)
    return {'asset_id': f'plate_3d/{slug}', 'kind': 'plate_3d', 'title': title, 'group': slug.split('/')[0],
            'description': description, 'labels': labels, 'approved': True, 'published': True}


def overhead_leave(slug: str, leave: str, result: str, title: str, description: str) -> dict:
    """The overhead leave job template: every label shared except the leave (and split or not)."""
    return plate(f'leave/{slug}-overhead-league-night', title, description,
                 subjects=['pins', 'pin_deck'], events=['leave', 'pins_settled'], angle='overhead', move='static',
                 size='wide', speed='real_time', mood='league_night_warm', story=['explanation', 'tension'],
                 series=['bowling_iq'], energy=2, leave=leave, result=result, pins_state='leave', lane_zone=['pin_deck'])


def pin_cam_leave(slug: str, leave: str, result: str, speed: str, mood: str, title: str, description: str) -> dict:
    return plate(f'leave/{slug}', title, description,
                 subjects=['pins', 'pin_deck', 'lane'], events=['leave', 'pins_settled'], angle='pin_cam',
                 move='static', size='medium', speed=speed, mood=mood, story=['tension', 'pressure'],
                 series=['promo_emotional', 'bowling_iq'], energy=2, leave=leave, result=result, pins_state='leave')


def fixture_plates() -> list[dict]:
    return [
        # Strikes from several angles, speeds and moods.
        plate('strike/pocket-strike-pin-cam-slow5-league-night', 'Pocket strike, pin cam, slow motion',
              'The ball rolls straight at us into the 1-3 pocket and the rack explodes toward the camera.',
              subjects=['ball', 'rack', 'pins', 'pin_deck'], events=['strike', 'pocket_hit', 'pins_falling'],
              angle='pin_cam', move='static', size='close', speed='slow_5x', mood='league_night_warm',
              story=['impact', 'anticipation'], series=['promo_emotional', 'promo_hype'], energy=4,
              result='strike', pins_state='falling', ball='bn_hero_green'),
        plate('strike/incoming-pin-cam-slow5-league-night', 'The ball coming at us, ending before contact',
              'From behind the rack, the green ball rolls toward the camera in slow motion and the shot ends just '
              'before it reaches the pins.',
              subjects=['ball', 'rack', 'lane'], events=['roll', 'entry_into_pocket'], angle='pin_cam',
              move='push_in', size='close', speed='slow_5x', mood='league_night_warm',
              story=['tension', 'anticipation'], series=['promo_emotional'], energy=2, pins_state='full_rack',
              ball='bn_hero_green'),
        plate('strike/pocket-strike-pin-cam-real-tournament', 'Pocket strike, pin cam, real speed, broadcast light',
              'A flush pocket hit at full speed under bright, even television light.',
              subjects=['ball', 'rack', 'pins'], events=['strike', 'pocket_hit', 'pins_falling'], angle='pin_cam',
              move='static', size='medium', speed='real_time', mood='tournament_tv', story=['impact'],
              series=['promo_hype'], energy=5, result='strike'),
        plate('strike/pocket-strike-low-front-slow5-league-night', 'Strike at lane level, slow motion',
              'Low on the lane in front of the pins: the ball hooks into the pocket and every pin goes.',
              subjects=['ball', 'pins', 'rack', 'lane'], events=['strike', 'pocket_hit', 'hook', 'pins_falling'],
              angle='low_front', move='static', size='medium', speed='slow_5x', mood='league_night_warm',
              story=['impact', 'payoff'], series=['promo_emotional'], energy=4, result='strike'),
        plate('strike/pocket-strike-low-front-real-league-night', 'Strike from the front at real speed',
              'Real speed from the front of the deck, so the crash lands on the beat.',
              subjects=['ball', 'pins', 'rack'], events=['strike', 'pocket_hit', 'pins_falling'], angle='low_front',
              move='static', size='medium', speed='real_time', mood='league_night_warm', story=['impact'],
              series=['promo_emotional', 'promo_hype'], energy=5, result='strike'),
        plate('strike/strike-replay-side-high-slow5-tournament', 'Strike replay from the high side',
              'The television replay angle: high inside the kickbacks, looking down across the deck as the pins go.',
              subjects=['pins', 'pin_deck', 'ball'], events=['strike', 'pocket_hit', 'pins_falling'],
              angle='side_high', move='static', size='medium', speed='slow_5x', mood='tournament_tv',
              story=['impact', 'reveal'], series=['promo_hype', 'pro_wiki'], energy=4, result='strike'),
        plate('strike/strike-low-side-slow5-league-night', 'Strike side on, slow-motion replay',
              'Side on from the gutter, the camera follows the ball into the rack in slow motion.',
              subjects=['ball', 'pins', 'lane', 'gutter'], events=['strike', 'pins_falling'], angle='low_side',
              move='track_with_subject', size='wide', speed='slow_5x', mood='league_night_warm',
              story=['impact', 'release_of_tension'], series=['promo_emotional'], energy=4, result='strike'),
        plate('strike/strike-overhead-real-morning', 'Strike from straight above',
              'Top down on the deck in the morning light: the ball meets the pocket and the pins clear.',
              subjects=['ball', 'pins', 'pin_deck'], events=['strike', 'pocket_hit', 'pins_falling'],
              angle='overhead', move='static', size='wide', speed='real_time', mood='morning_practice',
              story=['explanation', 'precision'], series=['bowling_iq'], energy=3, result='strike'),
        plate('strike/strike-behind-bowler-real-league-night', 'Strike from behind the bowler',
              'Over the shoulder, following the ball all the way down the lane until the pins fall.',
              subjects=['ball', 'lane', 'rack', 'arrows'], events=['roll', 'strike', 'pins_falling'],
              angle='behind_bowler', move='track_with_subject', size='wide', speed='real_time',
              mood='league_night_warm', story=['anticipation', 'payoff'], series=['promo_emotional'], energy=3,
              result='strike'),
        plate('strike/strike-pin-cam-slow10-cosmic', 'Cosmic strike, pin cam, ultra slow',
              'Blacklight and neon: the glowing ball creeps into the pocket and the pins burst toward us.',
              subjects=['ball', 'rack', 'pins'], events=['strike', 'pocket_hit', 'pins_falling'], angle='pin_cam',
              move='push_in', size='close', speed='slow_10x', mood='cosmic', story=['impact', 'celebration'],
              series=['promo_hype'], energy=5, result='strike'),
        plate('strike/strike-card-three-quarter-slow5-tournament', 'Strike for the card, raised three-quarter',
              'Raised at three-quarters to the rack, the strike in slow motion; built to be frozen behind a card.',
              subjects=['ball', 'rack', 'pins', 'pin_deck'], events=['strike', 'pins_falling'],
              angle='elevated_three_quarter', move='static', size='medium', speed='slow_5x', mood='tournament_tv',
              story=['triumph', 'payoff'], series=['promo_hype', 'platform_feature'], energy=4, result='strike'),
        plate('strike/head-pin-contact-macro-slow10-league-night', 'The ball meets the head pin, macro',
              'Extremely close as the ball kisses the head pin, ultra slow.',
              subjects=['ball', 'head_pin'], events=['pocket_hit', 'entry_into_pocket'], angle='macro',
              move='static', size='extreme_close', speed='slow_10x', mood='league_night_warm',
              story=['impact', 'precision'], series=['bowling_iq', 'promo_hype'], energy=3),
        plate('strike/brooklyn-strike-low-front-cosmic', 'Brooklyn strike under blacklight',
              'The ball crosses over into the 1-2 pocket and still carries all ten.',
              subjects=['ball', 'pins', 'rack'], events=['brooklyn_hit', 'strike', 'pins_falling'],
              angle='low_front', move='static', size='medium', speed='real_time', mood='cosmic',
              story=['celebration'], series=['promo_hype'], energy=4, result='strike'),
        plate('strike/pocket-entry-low-front-slow10-tournament', 'Entry angle into the pocket, ultra slow',
              'Lane level at the deck: the ball drives into the 1-3 pocket at its entry angle.',
              subjects=['ball', 'pins', 'head_pin'], events=['entry_into_pocket', 'pocket_hit', 'strike'],
              angle='low_front', move='static', size='close', speed='slow_10x', mood='tournament_tv',
              story=['explanation', 'precision'], series=['bowling_iq'], energy=3, result='strike'),
        plate('strike/pin-scatter-side-high-slow10-league-night', 'Pin scatter, high side, ultra slow',
              'Pins flying off the deck and the kickbacks in ultra slow motion.',
              subjects=['pins', 'pin_deck'], events=['pins_falling', 'strike'], angle='side_high', move='static',
              size='wide', speed='slow_10x', mood='league_night_warm', story=['impact', 'celebration'],
              series=['promo_hype'], energy=5, result='strike'),
        plate('strike/aftermath-deck-three-quarter-league-night', 'The deck after the strike',
              'The pins lie still in the pit and the deck is clear: the breath out after the hit.',
              subjects=['pin_deck', 'pins'], events=['pins_settled'], angle='elevated_three_quarter', move='static',
              size='medium', speed='real_time', mood='league_night_warm', story=['release_of_tension', 'payoff'],
              series=['promo_emotional'], energy=1, pins_state='all_down', result='strike'),
        plate('strike/messenger-pin-side-high-slow5-league-night', 'Messenger pin across the deck',
              'A pin slides across the deck and takes out the 10 for the strike.',
              subjects=['pins', 'pin_deck'], events=['messenger_pin', 'strike', 'pins_falling'], angle='side_high',
              move='static', size='wide', speed='slow_5x', mood='league_night_warm', story=['reveal', 'celebration'],
              series=['promo_hype'], energy=4, result='strike'),

        # Leaves from overhead (one job template) and from the pin cam.
        overhead_leave('10-pin', '10', 'leave', '10 pin leave from above', 'The 10 pin alone in the corner, seen '
                       'straight down on the deck.'),
        overhead_leave('7-pin', '7', 'leave', '7 pin leave from above', 'The 7 pin alone on the left corner, '
                       'seen straight down on the deck.'),
        overhead_leave('7-10-split', '7-10', 'split', '7-10 split from above', 'Both back corners standing, the '
                       'widest gap in bowling, straight down on the deck.'),
        overhead_leave('washout', 'washout', 'leave', 'Washout from above', 'The head pin, the 2 and the 10 '
                       'standing, straight down on the deck.'),
        overhead_leave('bucket', 'bucket', 'leave', 'Bucket leave from above', 'The 2-4-5-8 cluster standing, '
                       'straight down on the deck.'),
        pin_cam_leave('10-pin-pin-cam-slow2-league-night', '10', 'leave', 'slow_2x', 'league_night_warm',
                      'Ringing 10, pin cam, half speed', 'The 10 pin wobbles and stays up while everything else goes.'),
        pin_cam_leave('7-pin-pin-cam-real-tournament', '7', 'leave', 'real_time', 'tournament_tv',
                      '7 pin leave, pin cam', 'A lefty\'s pocket seven, from behind the deck.'),
        pin_cam_leave('7-10-split-pin-cam-slow5-cosmic', '7-10', 'split', 'slow_5x', 'cosmic',
                      'The 7-10 under blacklight, pin cam', 'The pins clear and two posts stay standing.'),
        pin_cam_leave('washout-pin-cam-real-morning', 'washout', 'leave', 'real_time', 'morning_practice',
                      'Washout, pin cam, morning', 'A high hit leaves the 1-2-10.'),
        pin_cam_leave('bucket-pin-cam-real-cosmic', 'bucket', 'leave', 'real_time', 'cosmic',
                      'Bucket, pin cam, cosmic', 'A light hit leaves the 2-4-5-8.'),
        pin_cam_leave('big-four-pin-cam-slow2-tournament', 'big_four', 'split', 'slow_2x', 'tournament_tv',
                      'Big four, pin cam', 'Both corners and both fours left: the 4-6-7-10.'),

        # Spares.
        plate('spare/10-pin-cross-lane-behind-bowler-league-night', '10 pin spare, shot cross-lane',
              'From behind the bowler: a straight ball thrown across the lane takes out the 10 pin.',
              subjects=['ball', 'lane', 'pins', 'arrows'], events=['spare_conversion', 'roll'],
              angle='behind_bowler', move='track_with_subject', size='wide', speed='real_time',
              mood='league_night_warm', story=['precision', 'release_of_tension'],
              series=['bowling_iq', 'promo_emotional'], energy=3, leave='10', result='spare'),
        plate('spare/7-10-conversion-low-front-slow5-tournament', 'The 7-10 converted',
              'The ball clips the 7 into the 10 and both go: the impossible spare.',
              subjects=['ball', 'pins', 'pin_deck'], events=['spare_conversion', 'pins_falling'],
              angle='low_front', move='static', size='medium', speed='slow_5x', mood='tournament_tv',
              story=['triumph', 'celebration'], series=['promo_hype', 'pro_wiki'], energy=5, leave='7-10',
              result='spare'),

        # Ball motion.
        plate('ball/release-laydown-low-side-slow5-league-night', 'Release and laydown, side on, slow motion',
              'Low and side on: the ball drops into frame and lays down on the oil past the foul line, skidding.',
              subjects=['ball', 'lane', 'foul_line', 'guide_dots'], events=['release', 'laydown', 'skid'],
              angle='low_side', move='static', size='close', speed='slow_5x', mood='league_night_warm',
              story=['anticipation', 'focus'], series=['promo_emotional', 'bowling_iq'], energy=2,
              lane_zone=['foul_line', 'heads'], ball='bn_hero_green', pins_state='none'),
        plate('ball/skid-tracking-low-side-slow5-morning', 'Skid phase, tracking low beside the ball',
              'Riding low beside the ball as it slides on the oil, spinning before it grabs.',
              subjects=['ball', 'lane'], events=['skid'], angle='low_side', move='track_with_subject', size='close',
              speed='slow_5x', mood='morning_practice', story=['explanation'], series=['bowling_iq'], energy=2,
              lane_zone=['heads', 'arrows'], pins_state='none'),
        plate('ball/hook-behind-ball-slow2-tournament', 'Behind the ball as it hooks',
              'Behind the ball as it turns and hooks left past the breakpoint, boards streaming by.',
              subjects=['ball', 'lane'], events=['hook', 'roll'], angle='behind_bowler', move='track_with_subject',
              size='close', speed='slow_2x', mood='tournament_tv', story=['anticipation', 'precision'],
              series=['promo_hype', 'bowling_iq'], energy=3, lane_zone=['backend']),
        plate('ball/hook-path-overhead-real-tournament', 'The hook path from above',
              'The broadcast view from above: the path bends at the breakpoint toward the pocket.',
              subjects=['ball', 'lane', 'arrows'], events=['skid', 'hook', 'roll'], angle='overhead', move='static',
              size='wide', speed='real_time', mood='tournament_tv', story=['explanation'], series=['bowling_iq'],
              energy=2, lane_zone=['midlane', 'backend']),
        plate('ball/roll-behind-bowler-real-league-night', 'Down the lane behind her as the ball rolls',
              'Behind the bowler as she lets go; the ball rolls slow and straight toward the pins.',
              subjects=['ball', 'lane', 'approach_dots', 'arrows'], events=['release', 'roll'],
              angle='behind_bowler', move='push_in', size='wide', speed='real_time', mood='league_night_warm',
              story=['anticipation', 'ritual'], series=['promo_emotional'], energy=2),
        plate('ball/ball-return-orbit-league-night', 'The ball waiting on the return, a slow orbit',
              'The pearl ball waits on the ball return while the camera circles it.',
              subjects=['ball', 'ball_return'], events=['idle', 'turntable'], angle='orbit_close', move='orbit',
              size='close', speed='real_time', mood='league_night_warm', story=['anticipation', 'reveal'],
              series=['promo_emotional', 'platform_feature'], energy=1, ball='bn_hero_green'),
        plate('ball/pov-down-the-lane-morning', 'The bowler\'s own view down the lane',
              'First person at the top of the approach, eyes on the arrows, nobody else around.',
              subjects=['lane', 'arrows', 'approach_dots', 'foul_line'], events=['idle'], angle='pov_bowler',
              move='static', size='wide', speed='real_time', mood='morning_practice', story=['focus', 'precision'],
              series=['bowling_iq'], energy=1, lane_zone=['approach']),

        # House shots in all four moods.
        plate('house/house-league-night-crane-up', 'The whole house on league night, rising',
              'Every pair of lanes full under warm light; the camera rises and pulls back over the settees.',
              subjects=['house', 'lane', 'monitor'], events=['idle'], angle='elevated_three_quarter',
              move='crane_up', size='establishing', speed='real_time', mood='league_night_warm',
              story=['establishing', 'community'], series=['promo_emotional'], energy=2),
        plate('house/house-cosmic-pan', 'Cosmic night across the house',
              'Blacklight and neon over every lane, a slow pan across the house.',
              subjects=['house', 'lane'], events=['idle'], angle='elevated_three_quarter', move='pan_right',
              size='establishing', speed='real_time', mood='cosmic', story=['establishing', 'celebration'],
              series=['promo_hype'], energy=4),
        plate('house/house-tournament-push-in', 'Tournament house under television light',
              'Bright, even broadcast light over the pair under the cameras; a slow push in.',
              subjects=['house', 'lane', 'monitor'], events=['idle'], angle='elevated_three_quarter', move='push_in',
              size='establishing', speed='real_time', mood='tournament_tv', story=['establishing', 'pressure'],
              series=['promo_hype', 'pro_wiki'], energy=3),
        plate('house/house-morning-empty', 'The empty house in the morning',
              'Cool daylight on an empty house; one lane lit, nobody else around.',
              subjects=['house', 'lane'], events=['idle'], angle='elevated_three_quarter', move='static',
              size='establishing', speed='real_time', mood='morning_practice', story=['loneliness', 'establishing'],
              series=['promo_emotional', 'history'], energy=1),
        plate('house/end-card-ceiling-rise-league-night', 'Rising into the dark ceiling for the end card',
              'The camera keeps rising past the lane lights into the dark: quiet enough for the logo and sign-off.',
              subjects=['house'], events=['idle'], angle='elevated_three_quarter', move='crane_up', size='wide',
              speed='real_time', mood='league_night_warm', story=['call_to_action_backdrop', 'transition'],
              series=['promo_emotional', 'platform_feature'], energy=1),

        # The rest of Ten Frames V2.
        plate('rack/rack-lowering-low-front-league-night', 'A fresh rack lowered onto the deck',
              'The setting table lowers ten pins onto the deck; they settle and the table lifts away.',
              subjects=['pinsetter', 'rack', 'pins', 'pin_deck'], events=['rack_lowering'], angle='low_front',
              move='static', size='medium', speed='real_time', mood='league_night_warm',
              story=['establishing', 'ritual'], series=['promo_emotional'], energy=2, pins_state='full_rack'),
        plate('lane/lane-push-centre-line-league-night', 'Down the full lane from high on the centre line',
              'High on the centre line, a slow push down all sixty feet of the lane to the rack.',
              subjects=['lane', 'arrows', 'rack'], events=['idle'], angle='behind_bowler', move='push_in',
              size='establishing', speed='real_time', mood='league_night_warm', story=['establishing'],
              series=['promo_emotional'], energy=1, lane_zone=['heads', 'arrows', 'midlane', 'backend']),
        plate('lane/foul-line-low-push-league-night', 'Low along the foul line',
              'Low on the foul line, looking along it across the dark house; a slow push along the line.',
              subjects=['foul_line', 'lane'], events=['idle'], angle='foul_line_level', move='push_in', size='wide',
              speed='real_time', mood='league_night_warm', story=['tension', 'pressure'],
              series=['promo_emotional'], energy=1, lane_zone=['foul_line']),
        plate('lane/arrows-overhead-league-night', 'The arrows from straight above',
              'Straight down on the target arrows, where the ring and the line are drawn later.',
              subjects=['arrows', 'lane'], events=['idle'], angle='overhead', move='static', size='medium',
              speed='real_time', mood='league_night_warm', story=['precision', 'focus'],
              series=['promo_emotional', 'bowling_iq'], energy=1, lane_zone=['arrows']),
        plate('lane/focus-pull-foul-line-to-pins-league-night', 'Focus pull from the foul line to the pins',
              'Locked at the line while the focus pulls from the foul line to the pins sixty feet away.',
              subjects=['foul_line', 'lane', 'rack'], events=['idle'], angle='foul_line_level', move='rack_focus',
              size='wide', speed='real_time', mood='league_night_warm', story=['focus', 'tension'],
              series=['promo_emotional'], energy=1),
        plate('lane/approach-pool-of-light-league-night', 'The approach under one pool of light',
              'The approach and its dots under one pool of light; a slow push toward the line.',
              subjects=['approach_dots', 'foul_line'], events=['idle'], angle='elevated_three_quarter',
              move='push_in', size='wide', speed='real_time', mood='league_night_warm',
              story=['establishing', 'ritual'], series=['promo_emotional'], energy=1, lane_zone=['approach']),
        plate('lane/walk-to-line-pov-league-night', 'The walk to the line, her eyes',
              'Her eyes on the approach: four steps, each dip landing on a footstep, up to the foul line.',
              subjects=['approach_dots', 'foul_line', 'lane'], events=['walk_to_line'], angle='pov_bowler',
              move='walk', size='medium', speed='real_time', mood='league_night_warm', story=['ritual', 'focus'],
              series=['promo_emotional'], energy=2, lane_zone=['approach']),
        plate('lane/monitor-push-in-league-night', 'Push in on the lane monitor',
              'A level push in on the lane monitor; its screen corners are tracked so a score or clip can play on it.',
              subjects=['monitor'], events=['idle'], angle='elevated_three_quarter', move='push_in', size='medium',
              speed='real_time', mood='league_night_warm', story=['call_to_action_backdrop', 'transition'],
              series=['platform_feature'], energy=1),
    ]


def records_by_id(records: list[dict]) -> dict[str, dict]:
    return {record['asset_id']: record for record in records}


class TaxonomyTests(unittest.TestCase):

    def test_fixture_labels_are_valid(self) -> None:
        for record in fixture_plates():
            with self.subTest(asset_id=record['asset_id']):
                self.assertEqual(validate_item('plate_3d', record['labels']), list())

    def test_unknown_kind(self) -> None:
        problems = validate_item('plate3d', {'subjects': ['ball']})
        self.assertEqual(len(problems), 1)
        self.assertIn('"plate3d" is not a kind', problems[0])
        self.assertIn('plate_3d', problems[0])

    def test_missing_required_facet(self) -> None:
        labels = copy.deepcopy(fixture_plates()[0]['labels'])
        del labels['camera_angle']
        labels['story_uses'] = list()
        problems = validate_item('plate_3d', labels)
        self.assertIn('The required label "camera_angle" is missing for a plate_3d item.', problems)
        self.assertIn('The required label "story_uses" is missing for a plate_3d item.', problems)

    def test_facet_not_for_kind_and_unknown_facet(self) -> None:
        labels = copy.deepcopy(fixture_plates()[0]['labels'])
        labels['character'] = 'mia'
        labels['colour'] = 'green'
        problems = validate_item('plate_3d', labels)
        self.assertTrue(any('cannot carry the label "character"' in problem for problem in problems))
        self.assertTrue(any('"colour" is not a facet in the taxonomy' in problem for problem in problems))

    def test_value_not_in_facet_suggests_the_value(self) -> None:
        labels = copy.deepcopy(fixture_plates()[0]['labels'])
        labels['camera_angle'] = 'pin view'
        labels['events'] = ['strike', 'big_crash']
        problems = validate_item('plate_3d', labels)
        self.assertIn('"pin view" is not a value of "camera_angle". Did you mean "pin_cam"?', problems)
        self.assertTrue(any(problem.startswith('"big_crash" is not a value of "events".') for problem in problems))

    def test_list_for_single_value_facet(self) -> None:
        labels = copy.deepcopy(fixture_plates()[0]['labels'])
        labels['camera_angle'] = ['pin_cam', 'overhead']
        problems = validate_item('plate_3d', labels)
        self.assertEqual(problems, ['"camera_angle" takes one value, but a list was given (pin_cam, overhead).'])

    def test_free_facet_and_yaml_scalars(self) -> None:
        labels = copy.deepcopy(fixture_plates()[0]['labels'])
        labels['aliases'] = ['tuesday strike', 'the big one']
        labels['leave'] = 10
        self.assertEqual(validate_item('plate_3d', labels), list())
        action = {'character': 'mia', 'action': 'cheer', 'angle': 'front', 'story_uses': ['celebration'],
                  'series_fit': ['promo_emotional'], 'energy': 4, 'rights': 'owned_render', 'loopable': True}
        self.assertEqual(validate_item('character_action', action), list())
        self.assertEqual(normalise_labels(action)['loopable'], 'yes')

    def test_taxonomy_version(self) -> None:
        self.assertIsInstance(taxonomy_version(), int)
        self.assertEqual(taxonomy_version(), TAXONOMY.version)

    def test_validate_cli(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            jobs_dir = Path(folder)
            good = {'id': 'plates-test', 'title': 'Test plates', 'kind': 'plate_3d', 'device': 'gpu',
                    'executor': 'blender_item', 'entry': 'render_item.py',
                    'items': [{key: value for key, value in record.items() if key not in ('kind', 'approved',
                                                                                         'published')}
                              for record in fixture_plates()[:3]]}
            (jobs_dir / 'plates-test.yaml').write_text(yaml.safe_dump(good), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(validate_main([str(jobs_dir), '--no-queries']), 0)
            self.assertIn('All 3 items are labelled from the taxonomy.', output.getvalue())
            bad = copy.deepcopy(good)
            bad['id'] = 'plates-bad'
            bad['items'][0]['asset_id'] = 'plate_3d/strike/another-one'
            bad['items'][0]['labels']['camera_angle'] = 'from the moon'
            (jobs_dir / 'plates-bad.yaml').write_text(yaml.safe_dump(bad), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(validate_main([str(jobs_dir), '--no-queries']), 1)
            self.assertIn('plate_3d/strike/another-one: "from the moon" is not a value of "camera_angle".',
                          output.getvalue())

    def test_screenplay_set_is_valid_and_large_enough(self) -> None:
        entries, problems = load_screenplay_queries(taxonomy=TAXONOMY)
        self.assertEqual(problems, list())
        self.assertGreaterEqual(len(entries), 40)
        kinds = {entry['kind'] for entry in entries}
        self.assertTrue({'plate_3d', 'character_action', 'music_bed', 'sfx'} <= kinds)


class NormalisationTests(unittest.TestCase):

    def test_words(self) -> None:
        self.assertEqual(phrase_tokens(word_tokens("the pin's eye view")), ['pin', 'eye', 'view'])
        self.assertEqual(phrase_tokens(word_tokens('Seven-ten split')), ['7-10', 'split'])
        self.assertEqual(phrase_tokens(word_tokens('a 7/10 split')), ['7-10', 'split'])
        self.assertEqual(phrase_tokens(word_tokens('the 1-2-10')), ['1-2-10'])
        self.assertEqual(word_tokens('Holding its breath'), word_tokens('holding her breath')[:1] + ['its', 'breath'])
        self.assertEqual(word_tokens('bedposts'), word_tokens('bedpost'))
        self.assertEqual(word_tokens('slow-mo'), ['slow', 'mo'])


class SearchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.records = fixture_plates()
        cls.index = SearchIndex(cls.records, TAXONOMY)
        cls.by_id = records_by_id(cls.records)

    def top_labels(self, result: dict, count: int = 3) -> list[dict]:
        return [self.by_id[entry['asset_id']]['labels'] for entry in result['results'][:count]]

    def test_pins_eye_view_finds_pin_cam(self) -> None:
        result = self.index.search("pin's eye view of a strike", kind='plate_3d')
        self.assertIn('camera_angle=pin_cam', [value for entry in result['interpreted'] for value in entry['values']])
        for labels in self.top_labels(result):
            self.assertEqual(labels['camera_angle'], 'pin_cam')

    def test_bedposts_finds_the_7_10(self) -> None:
        result = self.index.search('the dreaded bedposts', kind='plate_3d')
        top = self.top_labels(result)
        self.assertEqual(str(top[0].get('leave')), '7-10')
        self.assertTrue(all(str(labels.get('leave')) == '7-10' for labels in top))

    def test_holding_its_breath_finds_tension(self) -> None:
        result = self.index.search('holding its breath', kind='plate_3d')
        self.assertTrue(all('tension' in labels['story_uses'] for labels in self.top_labels(result)))
        # With "whole house" in the beat, a house plate titled "The whole house..." may lead, but tension still
        # holds the top 3 (the findability rule).
        result = self.index.search('And a whole house holding its breath.', kind='plate_3d')
        self.assertGreaterEqual(sum('tension' in labels['story_uses'] for labels in self.top_labels(result)), 2)

    def test_facets_take_synonyms(self) -> None:
        result = self.index.search('strike', facets={'camera_angle': "pin's eye view", 'speed': 'slow motion'},
                                   kind='plate_3d')
        self.assertEqual(result['facets'], {'camera_angle': ['pin_cam'], 'speed': ['slow_5x']})
        self.assertTrue(result['exact'])
        self.assertTrue(all(labels['camera_angle'] == 'pin_cam' and labels['speed'] == 'slow_5x'
                            for labels in self.top_labels(result, len(result['results']))))

    def test_relaxes_in_order_and_reports_only_what_it_gave_up(self) -> None:
        # No league-night pin-cam strike at half speed. Relaxing mood alone is not enough; relaxing speed alone is.
        result = self.index.search('strike', kind='plate_3d', facets={
            'mood': 'league_night_warm', 'speed': 'slow_2x', 'camera_angle': 'pin_cam', 'events': ['strike']})
        self.assertEqual(result['relaxed'], ['speed'])
        self.assertFalse(result['exact'])
        self.assertTrue(result['weak'])
        top = self.top_labels(result, len(result['results']))
        self.assertTrue(all(labels['mood'] == 'league_night_warm' and labels['camera_angle'] == 'pin_cam'
                            for labels in top))

    def test_relaxes_mood_then_speed(self) -> None:
        result = self.index.search('strike from above', kind='plate_3d', facets={
            'mood': 'cosmic', 'speed': 'slow_10x', 'camera_angle': 'overhead', 'events': ['strike']})
        self.assertEqual(result['relaxed'], ['mood', 'speed'])
        self.assertEqual(result['results'][0]['asset_id'], 'plate_3d/strike/strike-overhead-real-morning')

    def test_never_empty_while_the_kind_has_items(self) -> None:
        impossible = {'events': ['brooklyn_hit', 'rack_lowering'], 'leave': 'big_four', 'mood': 'morning_practice',
                      'camera_move': 'orbit', 'speed': 'slow_10x', 'camera_angle': 'macro', 'subjects': ['sweep']}
        result = self.index.search('zzz qqq', kind='plate_3d', facets=impossible)
        self.assertGreater(len(result['results']), 0)
        self.assertEqual(result['relaxed'][:4], ['mood', 'speed', 'camera_move', 'camera_angle'])
        self.assertTrue(set(result['relaxed']) <= set(impossible))
        self.assertGreater(len(self.index.search('', kind='plate_3d')['results']), 0)
        self.assertEqual(self.index.search('mia cheering', kind='character_action')['results'], list())
        self.assertIn('error', self.index.search('anything', kind='plates'))

    def test_reports_values_missing_from_the_library(self) -> None:
        result = self.index.search('the sweep bar comes down', kind='plate_3d')
        self.assertIn('subjects=sweep', result['not_in_library'])
        self.assertTrue(result['weak'])
        entry = demand_entry(result)
        self.assertEqual(entry['want'], 'the sweep bar comes down')
        self.assertEqual(entry['not_in_library'], ['subjects=sweep'])
        self.assertIsNone(demand_entry(self.index.search('the dreaded bedposts', kind='plate_3d')))


class FindabilityTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.screenplay, problems = load_screenplay_queries(taxonomy=TAXONOMY)
        assert not problems, problems
        cls.report = run_findability(fixture_plates(), TAXONOMY, cls.screenplay)

    def test_every_fixture_item_passes(self) -> None:
        failing = {asset_id: result.as_dict() for asset_id, result in self.report.items.items() if not result.passed}
        self.assertEqual(failing, dict(), json.dumps(failing, indent=2))
        self.assertEqual(self.report.pass_rate, 1.0)

    def test_screenplay_beats_are_found_or_missing_never_missed(self) -> None:
        missed = [result.as_dict() for result in self.report.screenplay if result.status == 'not_found']
        self.assertEqual(missed, list(), json.dumps(missed, indent=2))
        found = [result for result in self.report.screenplay if result.status == 'found']
        self.assertGreaterEqual(len(found), 40)

    def test_missing_beats_go_to_the_demand_log(self) -> None:
        demand_wants = {entry['want'] for entry in self.report.demand}
        self.assertIn('Tony meets Linda with a high-five.', demand_wants)
        self.assertIn('The ball comes back up the return after the shot.', demand_wants)
        self.assertTrue(all(entry['kind'] != 'plate_3d' or entry['expect'] in (
            {'events': ['ball_return_arrival']}, {'subjects': ['sweep']}) for entry in self.report.demand))

    def test_label_queries_swap_in_synonyms(self) -> None:
        record = fixture_plates()[0]
        labels = normalise_labels(record['labels'], TAXONOMY)
        peers = [normalise_labels(other['labels'], TAXONOMY) for other in fixture_plates()[1:]]
        chosen, competitors = discriminating_facets(labels, peers, 'plate_3d', TAXONOMY)
        self.assertEqual(competitors, list())
        queries = dict(label_queries(labels, chosen, 'plate_3d', TAXONOMY))
        self.assertGreaterEqual(len(queries), 3)
        self.assertIn("pin's eye view", queries['synonyms'])
        self.assertIn('slow motion', queries['synonyms'])

    def test_badly_labelled_item_fails_and_is_held_back(self) -> None:
        # Copied from the overhead 10 pin job item but without saying which pins are left.
        bad = copy.deepcopy(records_by_id(fixture_plates())['plate_3d/leave/10-pin-overhead-league-night'])
        bad['asset_id'] = 'plate_3d/leave/some-leave-overhead-league-night'
        del bad['labels']['leave']
        report = run_findability(fixture_plates() + [bad], TAXONOMY, self.screenplay)
        result = report.items[bad['asset_id']]
        self.assertFalse(result.passed)
        self.assertTrue(any('cannot set it apart' in reason for reason in result.reasons))
        others = [asset_id for asset_id in report.failed_items if asset_id != bad['asset_id']]
        self.assertEqual(others, list())

    def test_twins_are_flagged(self) -> None:
        original = records_by_id(fixture_plates())['plate_3d/strike/brooklyn-strike-low-front-cosmic']
        twin = copy.deepcopy(original)
        twin['asset_id'] = 'plate_3d/strike/brooklyn-strike-low-front-cosmic-take-2'
        twin['labels']['aliases'] = ['second take']
        records = fixture_plates() + [twin]
        self.assertEqual(find_twins(records, TAXONOMY), [sorted([original['asset_id'], twin['asset_id']])])
        report = run_findability(records, TAXONOMY, self.screenplay)
        self.assertEqual(report.twins, [sorted([original['asset_id'], twin['asset_id']])])
        self.assertEqual(report.items[twin['asset_id']].twins, [original['asset_id']])


class PromptIndexTests(unittest.TestCase):

    LINE_REGEX = re.compile(r'^plate_3d/[a-z0-9-]+/[a-z0-9-]+ \| [a-z_0-9]+ [a-z_]+ [a-z_]+ [a-z_0-9]+ '
                            r'\| [a-z_+]+( leave:[a-z0-9_-]+)? \| [a-z_+]+ \| [a-z_]+ e[1-5]$')

    def test_line_format(self) -> None:
        record = fixture_plates()[0]
        self.assertEqual(prompt_line(record, TAXONOMY),
                         'plate_3d/strike/pocket-strike-pin-cam-slow5-league-night | pin_cam static close slow_5x | '
                         'strike+pocket_hit+pins_falling | impact+anticipation | league_night_warm e4')
        leave = records_by_id(fixture_plates())['plate_3d/leave/7-10-split-pin-cam-slow5-cosmic']
        self.assertIn('| leave+pins_settled leave:7-10 |', prompt_line(leave, TAXONOMY))

    def test_index_has_one_line_per_approved_published_item(self) -> None:
        records = fixture_plates()
        records[0]['published'] = False
        records[1]['approved'] = None
        text = prompt_index(records, TAXONOMY)
        lines = text.strip().split('\n')
        self.assertEqual(lines[0], '# plate_3d: id | camera_angle camera_move shot_size speed | events leave | '
                                   'story_uses | mood energy')
        body = lines[1:]
        self.assertEqual(len(body), len(records) - 2)
        self.assertNotIn(records[0]['asset_id'], text)
        self.assertNotIn(records[1]['asset_id'] + ' ', text)
        for line in body:
            self.assertRegex(line, self.LINE_REGEX)
        average = sum(map(estimate_tokens, body)) / len(body)
        self.assertLess(average, 60)


class BuildTests(unittest.TestCase):

    def make_root(self, folder: Path, registered: list[str]) -> tuple[Path, Path]:
        root = folder / 'root'
        jobs_dir = folder / 'jobs'
        (root / 'state').mkdir(parents=True)
        jobs_dir.mkdir()
        items = [{key: value for key, value in record.items() if key not in ('kind', 'approved', 'published')}
                 for record in fixture_plates()]
        broken = copy.deepcopy(items[0])
        broken['asset_id'] = 'plate_3d/strike/broken-labels'
        broken['labels']['camera_angle'] = 'upside down'
        job = {'id': 'plates-test', 'title': 'Test plates', 'kind': 'plate_3d', 'device': 'gpu',
               'executor': 'blender_item', 'entry': 'render_item.py', 'items': items + [broken]}
        (jobs_dir / 'plates-test.yaml').write_text(yaml.safe_dump(job, sort_keys=False), encoding='utf-8')
        connection = sqlite3.connect(root / 'state' / 'runner.db')
        connection.executescript('CREATE TABLE items (asset_id TEXT PRIMARY KEY, job_id TEXT, state TEXT, '
                                 'skipped INTEGER DEFAULT 0, registry_checked INTEGER DEFAULT 0);'
                                 'CREATE TABLE uploads (asset_id TEXT PRIMARY KEY, state TEXT, confirmed_at REAL);'
                                 'CREATE TABLE events (id INTEGER PRIMARY KEY, asset_id TEXT, message TEXT);')
        for number, asset_id in enumerate(registered):
            if number % 2:
                connection.execute('INSERT INTO uploads VALUES (?, ?, ?)', (asset_id, 'confirmed', 1.0))
            else:
                connection.execute('INSERT INTO items VALUES (?, ?, ?, 0, 0)', (asset_id, 'plates-test', 'done'))
        connection.execute('INSERT INTO items VALUES (?, ?, ?, 0, 0)', ('plate_3d/strike/not-yet', 'x', 'pending'))
        connection.commit()
        connection.close()
        return root, jobs_dir

    def test_registered_asset_ids(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(registered_asset_ids(Path(folder)), set())
            root, _ = self.make_root(Path(folder), ['plate_3d/a/b', 'plate_3d/c/d', 'plate_3d/e/f'])
            self.assertEqual(registered_asset_ids(root), {'plate_3d/a/b', 'plate_3d/c/d', 'plate_3d/e/f'})
            (root / 'state' / 'runner.db').write_bytes(b'not a database')
            self.assertEqual(registered_asset_ids(root), set())

    def test_build_writes_the_catalogue_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            all_ids = [record['asset_id'] for record in fixture_plates()]
            # The cosmic plates are not rendered yet, so the cosmic and Brooklyn beats become demand-log entries.
            unregistered = [record['asset_id'] for record in fixture_plates() if record['labels']['mood'] == 'cosmic']
            registered = [asset_id for asset_id in all_ids if asset_id not in unregistered]
            root, jobs_dir = self.make_root(Path(folder), registered)
            out_dir = Path(folder) / 'out'
            item = {'asset_id': 'catalogue/nightly/catalogue', 'title': 'Library catalogue', 'job_id': 'catalogue',
                    'params': {'root': str(root), 'jobs_dir': str(jobs_dir)}}
            item_path = Path(folder) / 'item.json'
            item_path.write_text(json.dumps(item), encoding='utf-8')
            result = build(item_path, out_dir)
            self.assertEqual([entry['role'] for entry in result['files']], ['catalogue', 'report', 'report_text'])
            for entry in result['files']:
                self.assertTrue((out_dir / entry['path']).is_file())
            catalogue = json.loads((out_dir / 'catalogue.json').read_text(encoding='utf-8'))
            records = records_by_id(catalogue['records'])
            self.assertEqual(len(records), len(all_ids))
            self.assertNotIn('plate_3d/strike/broken-labels', records)
            self.assertTrue(all(records[asset_id]['published'] for asset_id in registered))
            self.assertTrue(all(not records[asset_id]['published'] and records[asset_id]['held_back']
                                for asset_id in unregistered))
            self.assertEqual(records[all_ids[0]]['labels']['energy'], '4')
            report = json.loads((out_dir / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['counts']['items_with_label_errors'], 1)
            self.assertEqual(report['counts']['published'], len(registered))
            self.assertEqual(report['counts']['not_registered_yet'], len(unregistered))
            demand_wants = {entry['want'] for entry in report['demand']}
            self.assertIn('Cosmic bowling night: blacklight and neon over the lanes.', demand_wants)
            self.assertIn('preview', report)
            self.assertEqual(report['preview']['summary']['items_failed'], 0)
            self.assertEqual(result['measured']['published'], len(registered))
            text = (out_dir / 'report.txt').read_text(encoding='utf-8')
            self.assertIn('Missing from the library', text)
            self.assertIn('upside down', text)
            # The runner passes the parsed item rather than its path; both work.
            self.assertEqual(build(item, Path(folder) / 'out2')['measured'], result['measured'])


if __name__ == '__main__':
    unittest.main()
