"""Offline delivery reservations; never connects to DCS or a radio/model."""
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from integrations.hermes_radio.unprompted import Unprompted, control_request
from integrations.hermes_radio.contract import ContractError, encode_response


class FakeEye:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset_at, self.session_id = 0, 'mission'
        self.announced = set()
        self.rows = [('Caution, SA-13, heat seeker, your one o\'clock, eight miles, lethal inside three.', 'caution')]
        self.pic = dict(connected=True, ownship={'type': 'F-100D'}, data_age_s={'units': 0})

    def picture(self):
        pic = deepcopy(self.pic)
        rows = [(a, k) for a, k in self.rows if k not in self.announced]
        pic['rwr_alert_to_announce'] = [a for a, _ in rows]
        pic['_rwr_keys'] = [k for _, k in rows]
        if pic.get('_own_loss_key') in self.announced:
            pic.pop('_own_loss_key')
        return pic

    def mark_announced(self, keys):
        self.announced.update(keys)

    def unmark_announced(self, keys):
        self.announced.difference_update(keys)


class UnpromptedTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'ATHENA_UNPROMPTED': 'on'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.now = 0
        self.eye = FakeEye()
        self.log = []
        self.c = SimpleNamespace(eye=self.eye, _turn=threading.Lock(), _last_exchange=None,
            transcript=SimpleNamespace(outcome_of=lambda _: None, log=lambda **kw: self.log.append(kw)))
        self.u = Unprompted(self.c, clock=lambda: self.now)
        self.req = SimpleNamespace(transmission_id='pilot', received_at=datetime.now(timezone.utc).isoformat(),
                                   transcript='Radio check', frequency_hz=251000000, modulation='AM')

    def poll(self, ident='alert'):
        return self.u.poll(ident, 'pilot')

    def test_silent_until_heard_then_once_shared_keys(self):
        self.assertIsNone(self.poll())
        self.assertTrue(self.u.remember(self.req))
        frame = self.poll()
        self.assertEqual(frame['route'], 'unprompted')
        self.assertEqual(frame['priority'], 'threat')
        encode_response(frame)
        self.assertEqual(self.eye.picture()['rwr_alert_to_announce'], [])
        self.assertTrue(self.u.receipt('alert', 'check'))
        self.assertTrue(self.u.receipt('alert', 'sent'))
        self.assertEqual(self.c._last_delivered[1], frame['speech'])
        self.now = 6
        self.assertIsNone(self.poll('again'))

    def test_failure_rearms_and_five_second_floor(self):
        self.u.remember(self.req); self.poll()
        self.u.receipt('alert', 'failed')
        self.assertEqual(self.eye.announced, set())
        self.now = 4.9
        self.assertIsNone(self.poll('retry'))
        self.now = 5
        self.assertIsNotNone(self.poll('retry'))

    def test_launch_bypasses_floor_and_combines_two(self):
        self.u.remember(self.req); self.poll(); self.u.receipt('alert', 'sent')
        self.eye.rows += [("Missile launch, SA-13, your seven o'clock, two miles.", 'l1'),
                          ("Missile launch, SA-13, your three o'clock, two miles.", 'l2')]
        self.now = 1
        frame = self.poll('launch')
        self.assertEqual(frame['priority'], 'launch')
        self.assertEqual(frame['speech'], "Two SA-13 missiles, seven and three o'clock.")
        self.assertTrue({'l1','l2'} <= self.eye.announced)
        self.u.receipt('launch', 'failed')
        self.assertFalse({'l1','l2'} & self.eye.announced)

    def test_suppression_matrix(self):
        self.u.remember(self.req)
        for patch_pic in ({'connected': False}, {'ownship': None}, {'data_age_s': {'units': 11}},
                          {'cockpit': {'fresh': True, 'aircraft': 'A-10C'}}):
            original = deepcopy(self.eye.pic)
            self.eye.pic.update(patch_pic)
            self.assertIsNone(self.poll())
            self.eye.pic = original
        with patch.dict('os.environ', {'ATHENA_UNPROMPTED':'off'}):
            self.assertIsNone(self.poll())
        self.assertIsNone(self.u.poll('x', 'wrong-pilot-call'))
        self.eye.session_id = 'next'
        self.assertIsNone(self.poll())

    def test_check_rejects_changed_session_stale_lease_or_new_radio(self):
        self.u.remember(self.req); self.poll()
        self.now = 5
        self.assertFalse(self.u.receipt('alert', 'check'))
        self.now = 1
        self.eye.session_id = 'next'
        self.assertFalse(self.u.receipt('alert', 'check'))

    def test_loss_and_tracking_allowed_not_other_alerts(self):
        self.u.remember(self.req)
        self.eye.rows = [('Bandit, twelve miles.', 'bandit'), ('Systems damage reported.', 'damage')]
        self.assertIsNone(self.poll())
        self.eye.pic['_own_loss_key'] = 'loss'
        self.assertEqual(self.poll()['speech'], "Prometheus, Athena, you're down.")
        self.u.receipt('alert', 'sent')
        self.eye.rows.append(("SAM SA-2 tracking you, your three o'clock, four miles.", 'radar'))
        self.now = 5
        self.assertIn('tracking you', self.poll('radar')['speech'])

    def test_busy_controller_never_blocks_or_reserves(self):
        self.u.remember(self.req)
        with self.c._turn:
            self.assertIsNone(self.poll())
        self.assertEqual(self.eye.announced, set())

    def test_expired_or_failed_journal_rearms(self):
        self.u.remember(self.req); self.poll()
        self.now = 46
        self.assertIsNotNone(self.poll('retry'))
        self.c.transcript.outcome_of = lambda _: 'failed'
        self.now = 52
        self.assertIsNotNone(self.poll('journal-retry'))

    def test_strict_control_body(self):
        for value in ({'transmission_id':'x','reply_to':'y','frequency':123},
                      {'transmission_id':'x','outcome':'invented'},
                      {'transmission_id':None,'outcome':'sent'}):
            with self.assertRaises(ContractError):
                control_request(json.dumps(value).encode(), 'delivery')
        with self.assertRaises(ContractError):
            control_request(b'{"transmission_id":"x","transmission_id":"y","reply_to":"z"}', 'poll')

    def test_launch_jumps_an_outstanding_caution(self):
        self.u.remember(self.req); self.poll('caution')
        self.eye.rows.append(("Missile launch, SA-13, your five o'clock, one mile.", 'launch'))
        frame = self.poll('launch')
        self.assertEqual(frame['priority'], 'launch')
        self.assertEqual(len(self.u.claims), 2)

    def test_stand_in_stays_explicitly_marked(self):
        self.u.remember(self.req)
        self.eye.test_ownship = 'fixture-unit'
        self.eye.pic['test_ownship'] = True
        frame = self.poll()
        self.assertTrue(frame['speech'].startswith('Prometheus, Athena, Test pilot. '))
        self.assertIs(frame['test_pilot'], True)

    def test_nonstream_reply_preserves_priority(self):
        from integrations.hermes_radio.tests.test_p1_radio import controller, Stub, req
        c, _ = controller({}, Stub())
        c._wire_priority = True
        frame = c.exchange(req('Athena, radio check'))
        self.assertIn(frame['priority'], ('normal', 'threat'))

    def test_real_eye_scan_inherits_announced_shot_key(self):
        from integrations.hermes_radio.tests.test_p1_eye import MissileTests, f4e_eye, f4e_packet, SA11_SR, SA11_LN
        eye, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        fixture = MissileTests()
        eye.on_cockpit_packet(f4e_packet())
        eye.on_weapons([])
        eye.on_event(fixture.shot(4294967290))
        pic = eye.picture()
        key = pic['_rwr_keys'][0]
        eye.mark_announced([key])
        self.assertTrue(eye.picture()['threats']['missiles_in_flight'][0]['announced'])
        clock.t += 1
        eye.on_weapons([fixture.scan_row(16786001, rng=9.9)])
        pic = eye.picture()
        self.assertTrue(pic['threats']['missiles_in_flight'][0]['announced'])
        self.assertFalse(any(a.startswith('Missile launch') for a in pic.get('rwr_alert_to_announce', [])))
        eye.unmark_announced([key])
        self.assertTrue(any(a.startswith('Missile launch') for a in eye.picture().get('rwr_alert_to_announce', [])))

    def test_http_control_round_trip(self):
        from integrations.hermes_radio.server import RadioServer
        self.c.queues = True
        server = RadioServer(self.c, port=0, request_timeout=1)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
        worker.start()
        def post(path, body):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                conn.request('POST', '/radio/v1/' + path, json.dumps(body), {'Content-Type':'application/json'})
                reply = conn.getresponse()
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.getheader('X-Athena-Protocol'), '15')
                return json.loads(reply.read())
            finally:
                conn.close()
        try:
            self.assertFalse(post('unprompted', {'transmission_id':'early','reply_to':'pilot'})['deliverable'])
            req = dict(vars(self.req), speaker='Prometheus', transcribed_at=self.req.received_at)
            self.assertEqual(post('heard', req)['suppression_reason'], 'accepted')
            frame = post('unprompted', {'transmission_id':'actual','reply_to':'pilot'})
            self.assertTrue(frame['deliverable'])
            self.assertEqual(frame['priority'], 'threat')
            self.assertEqual(post('delivery', {'transmission_id':'actual','outcome':'check'})['suppression_reason'], 'accepted')
            self.assertEqual(post('delivery', {'transmission_id':'actual','outcome':'failed'})['suppression_reason'], 'accepted')
            self.assertEqual(self.eye.announced, set())
        finally:
            server.shutdown(); worker.join(); server.server_close()
