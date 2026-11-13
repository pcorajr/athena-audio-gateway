"""Brief 4: ports of rv-threat-2 cases; no model, radio or telemetry I/O."""
from copy import deepcopy
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.unprompted import Unprompted
from integrations.hermes_radio.contract import Request
from integrations.hermes_radio.tests.test_p1_radio import controller, Stub, say
from integrations.hermes_radio.tests.test_p1_eye import MissileTests
from integrations.hermes_radio.tests.test_eye_round4 import f4e_eye, f4e_packet, SA11_SR, SA11_LN, F4E


class Round2Tests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, ATHENA_UNPROMPTED='on')
        env.start()
        self.addCleanup(env.stop)

    def fixture(self, count=0):
        e, k = f4e_eye(extra=(SA11_SR, SA11_LN))
        c, _ = controller({}, Stub(), eye=e)
        c.transcript.outcome_of = lambda _: None
        u = Unprompted(c, clock=lambda: k.t)
        t = datetime.fromtimestamp(k.t, timezone.utc).isoformat()
        u.remember(Request('pilot', 'radio check', 'Prometheus', 251000000, 'AM', t, t))
        for i in range(count):
            e.on_event(MissileTests().shot(80 + i, rng=5 + i))
        return e, k, c, u

    def counted(self, count):
        e, k, c, u = self.fixture(count)
        f = say(c, 'SAM launch, SAM launch', 'short')[0][0]
        self.assertIn('Two SA-11 missiles', f['speech'])
        self.assertIn('One more' if count == 3 else 'Two more', f['speech'])
        self.assertLessEqual(len(f['speech']), 280)
        self.assertEqual(e.announced, {'launch-' + str(80 + i) for i in range(count)})
        return e, k, c, u

    def test_prompted_3_missiles(self):
        self.counted(3)

    def test_prompted_4_missiles(self):
        self.counted(4)

    def test_counted_missiles_repeat_3(self):
        _, _, c, u = self.counted(3)
        c._settle_previous(None, reported='sent')
        self.assertIsNone(u.poll('after-count', 'pilot'))

    def test_counted_missiles_repeat_4(self):
        _, _, c, u = self.counted(4)
        c._settle_previous(None, reported='sent')
        self.assertIsNone(u.poll('after-count', 'pilot'))

    def test_counted_salvo_repeated_prompted_off(self):
        for mode in ('on', 'off'):
            for count in (3, 4):
                with self.subTest(mode=mode, count=count), patch.dict(os.environ, ATHENA_UNPROMPTED=mode):
                    _, _, c, _ = self.counted(count)
                    c._settle_previous(None, reported='sent')
                    self.assertNotIn('Missile launch', say(c, 'Athena, radio check', 'after-count')[0][0]['speech'])

    def test_counted_salvo_failed_delivery_rearms_every_key(self):
        for mode in ('on', 'off'):
            for count in (3, 4):
                with self.subTest(mode=mode, count=count), patch.dict(os.environ, ATHENA_UNPROMPTED=mode):
                    e, _, c, _ = self.counted(count)
                    c._settle_previous(None, reported='failed')
                    self.assertFalse(e.announced)
                    say(c, 'SAM launch', 'retry')
                    self.assertEqual(len(e.announced), count)


    def test_counted_sentence_trim_does_not_settle_unspoken_keys(self):
        e, _, c, _ = self.fixture()
        for i in range(3):  # identical calls must not obscure individual coverage
            e.on_event(MissileTests().shot(80 + i, rng=5))
        with patch.object(sc, '_fit_calls', side_effect=lambda klass, calls, room: (calls[:1], True)):
            f = say(c, 'SAM launch', 'trimmed')[0][0]
        self.assertIn('Two SA-11 missiles', f['speech'])
        self.assertNotIn('more in the air', f['speech'])
        self.assertEqual({key for key in e.announced if key.startswith('launch-')}, {'launch-80', 'launch-81'})

    def test_recent_launch_one_second_after_scan_loss(self):
        e, k, _, u = self.fixture()
        e.on_event(MissileTests().shot(71))
        f = u.poll('recent', 'pilot')
        self.assertIn('missile launch', f['speech'])
        self.assertNotIn('in the air', f['speech'])
        k.t += .1
        e.on_weapons([MissileTests().scan_row(71, rng=9.9)])
        k.t += .15
        e.on_weapons([])
        k.t += 1
        self.assertTrue(u.receipt('recent', 'check'))
        k.t += 1.75
        self.assertTrue(u.receipt('recent', 'check'))  # launch age exactly 3 s
        k.t += .01
        self.assertFalse(u.receipt('recent', 'check'))
        self.assertFalse(e.announced)

    def test_aged_rwr_hides_new_dcs_launch(self):
        for age, sent in ((0, True), (3.01, False), (3, True)):
            with self.subTest(age=age, sent=sent):
                e, k, _, u = self.fixture()
                e.on_cockpit_packet(f4e_packet(rwr={'e': [{'id': 21, 'type': 'SA-11', 'sig': 'launch', 'az': 0}]}))
                if sent:
                    self.assertIsNotNone(u.poll('rwr', 'pilot'))
                    u.receipt('rwr', 'sent')
                k.t += age
                e.on_cockpit_packet(f4e_packet(seq=2, rwr={'e': []}))
                e.on_event(MissileTests().shot(72))
                f = u.poll('new-dcs', 'pilot')
                self.assertIsNotNone(f)
                self.assertEqual(f['priority'], 'launch')
                self.assertIn('launch-72', e.announced)

    def test_loss_questions_return_to_model_without_owned_loss(self):
        for kind in ('crash', 'pilot_dead', 'ejection'):
            for gone_first in (False, True):
                with self.subTest(kind=kind, gone_first=gone_first):
                    e, _, c, _ = self.fixture()
                    e.on_cockpit_packet(f4e_packet())
                    if gone_first:
                        e.on_gone(F4E['id'])
                    e.on_event({'time': 10, kind: {'initiator': {'unit': deepcopy(F4E)}}})
                    self.assertFalse(hasattr(e, 'own_loss'))
                    self.assertNotIn('own_loss', e.picture())
                    c._llm = Stub(answer='Prometheus, Athena, model sentinel.')
                    f = say(c, 'Am I down?', 'loss')[0][0]
                    self.assertEqual(f['route'], 'llm')
                    self.assertTrue(f['speech'].endswith('model sentinel.'))
                    self.assertNotIn("you're down", f['speech'])
