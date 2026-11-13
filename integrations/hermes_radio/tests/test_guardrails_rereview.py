import json
from pathlib import Path
import unittest
from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.tools.model_eval_guardrails import Eye
from integrations.hermes_radio.tools.model_eval import _Log, _request

FIX = Path(__file__).parent / 'fixtures'

class ReReviewTests(unittest.TestCase):
    def make(self, pic, text):
        return sc.SimpleController(None, _Log(), model='stub', api_key='', eye=Eye(pic), llm=lambda m: text)

    def test_fresh_real_echoes(self):
        pictures = json.loads((FIX / 'guardrails-review.json').read_text())
        rows = [json.loads(x) for x in (FIX / 'guardrails-real-extra.jsonl').read_text().splitlines()]
        for row in rows:
            line = row['id'].split(':')[1]
            pic = pictures[line]
            field = 'rwr_alert_to_announce' if line == '1890' else 'splash_to_announce'
            alert = sc.clean(pic[field][0]).rstrip('. ') + '.'
            with self.subTest(id=row['id']):
                result = self.make(pic, row['raw_reply']).exchange(_request(row['ask']))['speech']
                self.assertEqual(result.count(alert), 1)
                tail = result.replace(alert, '')
                self.assertNotRegex(tail.lower(), r'sa[- ]2 radar sees|splash one t fifty|destroyed a t[- ]|kills, including one t')
                self.assertLessEqual(len(result), 280)

    def test_alert_specific_not_other_alert(self):
        pic = {'ownship': {'type': 'F-4E'}, 'rwr_alert_to_announce': ['SAM SA-2 radar sees you.'],
               'splash_to_announce': ['Splash one T-55.']}
        for echo in ('An SA two radar sees you.', 'The SA-2 is tracking you.', 'Splash one T fifty-five.'):
            with self.subTest(echo=echo):
                result = self.make(pic, echo + ' SA-6 radar sees you. Splash one Ural-375.').exchange(_request('give me your assessment'))['speech']
                self.assertNotIn(echo, result)
                self.assertIn('SA-6 radar sees you.', result)
                self.assertIn('Splash one Ural-375.', result)
        # Geometry shared by different threats is not an alert identity.
        pic['rwr_alert_to_announce'] = ["SAM SA-2 radar sees you, your twelve o'clock, eighteen miles."]
        result = self.make(pic, "SA-6 radar sees you, your twelve o'clock, eighteen miles.").exchange(_request('give me your assessment'))['speech']
        self.assertIn("SA-6 radar sees you, your twelve o'clock, eighteen miles.", result)
        pic['threats'] = {'sam_in_range': [{'sam': 'SA-2', 'type': 'S_75M_Volhov'}]}
        result = self.make(pic, 'S-75 radar sees you.').exchange(_request('give me your assessment'))['speech']
        self.assertNotIn('S-75 radar sees you.', result)
        self.assertLessEqual(len(sc.PROMPT), 9686)




    def test_model_sentences_are_not_fact_checked(self):
        pic = {'ownship': {'type': 'F-4E', 'fuel': {'total_lb': 3640}},
               'threats': {'sam_in_range': [{'call': 'SAM SA-2 radar sees you.'}]},
               'my_kills_this_mission': []}
        for text in ('Fuel unknown.', 'No threats.', 'Splash nine kills.'):
            with self.subTest(text=text):
                result = self.make(pic, text).exchange(_request('give me your assessment'))
                self.assertEqual(result['speech'], sc.ADDRESS + text)
                self.assertEqual(result['route'], 'llm')

    def test_repeat_last_aliases_and_history_dependents(self):
        for ask in ('say again your last', 'say your last again', 'repeat your last'):
            ctl = self.make({'ownship': {'type': 'F-4E'}}, 'Stay sharp.')
            first = ctl.exchange(_request('give me your assessment'))['speech']
            ctl._llm = lambda m: self.fail('repeat called model')
            self.assertEqual(ctl.exchange(_request(ask))['speech'], first)
        for ask in ('say again fuel', 'say again bearing'):
            self.assertFalse(sc.REPEAT_ASK.fullmatch(ask))
        for text in ('Fuel unknown. It is dangerously low.', 'No position available. Turn left to reach it.',
                     'Fuel unavailable. That is bad.'):
            self.assertEqual(sc.factual_history(text), '')
        self.assertEqual(sc.factual_history('Unknown aircraft, bearing one five zero.'), sc.ADDRESS + 'Unknown aircraft, bearing one five zero.')
