"""The reviewer's 66 negative phrasings retain the original base routing."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from integrations.hermes_radio.simple_controller import call_class
from integrations.hermes_radio.tests.test_p1_radio import controller, Stub, say

CORPUS = json.loads((Path(__file__).parent / 'fixtures/threat_review_routes.json').read_text())
PICTURE = {'connected': True, 'data_age_s': {'units': 0, 'ownship': 0},
    'ownship': {'type': 'F-16C_50', 'coalition': 'blue', 'speed_kt': 400, 'alt_ft': 12000, 'heading_true': '090'},
    'cockpit': {'fresh': True, 'aircraft': 'F-16C_50', 'flight': {'ias_kt': 350, 'alt_msl_ft': 12000, 'heading_mag': 80}},
    'threats': {'sam_sites': [{'sam': 'SA-13', 'clock': 12, 'range_nm': 6, 'nominal_range_nm': 2.7,
        'caution_call': "Caution, SA-13, heat seeker, your twelve o'clock, six miles, lethal inside three."}]},
    'air_traffic': {'blue': [{'call': 'Friendly A-10C, north, ten miles.', 'range_nm': 10}]}}

class ReviewRouteTests(unittest.TestCase):
    def test_all_66_negative_routes_match_base(self):
        self.assertEqual(len(CORPUS), 66)
        for row in CORPUS:
            with self.subTest(text=row['text']):
                p = deepcopy(PICTURE)
                if row['group'] == 'A8':
                    p['cockpit']['aircraft'] = 'F-100D'
                c, _ = controller(p, Stub(answer='Prometheus, Athena, model sentinel.'))
                if row['group'] == 'A2':
                    say(c, 'The SAM seems dormant.', 'prior')
                f = say(c, row['text'], 'probe')[0][0]
                self.assertEqual(call_class(row['text'], p), row['call_class'])
                self.assertEqual(f['route'], row['route'])
