"""Independent review reproductions, ported before the bounded Brief 3 fixes.

All telemetry/model bodies are explicit offline fixtures; no live endpoints.
"""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from integrations.hermes_radio.contract import Request
from integrations.hermes_radio.eye import Eye
from integrations.hermes_radio.unprompted import Unprompted
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_p1_radio import controller, Stub, say
from integrations.hermes_radio.tests.test_p1_eye import MissileTests
from integrations.hermes_radio.tests.test_eye_round4 import f4e_eye, f4e_packet, SA11_LN, SA11_SR


def setup(eye, clock):
    c, _ = controller({}, Stub(), eye=eye)
    c.transcript.outcome_of = lambda _: None
    u = Unprompted(c, clock=clock)
    stamp = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
    u.remember(Request('pilot', 'Radio check', 'Prometheus', 251000000, 'AM', stamp, stamp))
    return c, u


def fixture(rng=7, sites=1):
    now = [1000.]
    e = Eye('Prometheus', clock=lambda: now[0])
    e.set_connected(True)
    own = fx.unit(100, 'my-plane', 'F-16C_50', 'blue', 'Viper', 'AIRPLANE',
                  42., 42., 1000, 90., 200., player='Prometheus')
    e.on_unit(own)
    for i in range(sites):
        e.on_unit(fx.unit(300+i, 'sam-'+str(i), 'Strela-10M3', 'red', 'site-'+str(i),
                          'GROUND', *fx.offset(90, rng+i*.001), 0))
    e.tick()
    c, u = setup(e, lambda: now[0])
    return now, e, c, u, own


class ThreatReviewTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'ATHENA_UNPROMPTED': 'on'})
        env.start()
        self.addCleanup(env.stop)

    def missile_fixture(self):
        e, clock = f4e_eye(extra=(SA11_SR, SA11_LN))
        c, u = setup(e, lambda: clock.t)
        return e, clock, c, u

    def test_grouped_cautions_cover_all_flat_keys(self):
        _, e, _, u, _ = fixture(sites=2)
        f = u.poll('group', 'pilot')
        self.assertIn('Caution, SA-13', f['speech'])
        self.assertEqual(len(e.announced), 2)
        self.assertTrue(all(isinstance(k, str) for k in e.announced))
        self.assertTrue(u.receipt('group', 'check'))
        u.receipt('group', 'failed')
        self.assertEqual(e.announced, set())

    def test_salvos_cover_identical_and_mixed_keys(self):
        for ranges in ((10, 10), (6, 10, 10)):
            with self.subTest(ranges=ranges):
                e, _, _, u = self.missile_fixture()
                for i, rng in enumerate(ranges):
                    e.on_event(MissileTests().shot(90+i, rng=rng))
                f = u.poll('salvo', 'pilot')
                self.assertEqual(f['priority'], 'launch')
                self.assertEqual(len(e.announced), len(ranges))
                self.assertTrue(u.receipt('salvo', 'check'))
                u.receipt('salvo', 'failed')
                self.assertFalse(e.announced)

    def test_failed_reservation_is_atomic(self):
        _, e, c, u, _ = fixture(sites=2)
        c.transcript.log = lambda **kw: (_ for _ in ()).throw(OSError('offline journal'))
        with self.assertRaises(OSError):
            u.poll('broken', 'pilot')
        self.assertFalse(e.announced)
        self.assertFalse(u.claims)

    def test_stale_ownship_at_reservation_and_check(self):
        for reserve_first in (False, True):
            with self.subTest(reserve_first=reserve_first):
                now, e, _, u, _ = fixture()
                if reserve_first:
                    self.assertIsNotNone(u.poll('stale', 'pilot'))
                now[0] += 120
                e.on_unit(fx.unit(900, 'truck', 'Ural-375', 'red', 'truck', 'GROUND', *fx.offset(0, 20), 0))
                e.tick()
                if reserve_first:
                    self.assertFalse(u.receipt('stale', 'check'))
                    self.assertFalse(e.announced)
                else:
                    self.assertIsNone(u.poll('stale', 'pilot'))

    def test_caution_revoked_when_site_dies_or_leaves_band(self):
        for destroyed in (True, False):
            with self.subTest(destroyed=destroyed):
                _, e, _, u, _ = fixture()
                self.assertIsNotNone(u.poll('site', 'pilot'))
                if destroyed:
                    e.on_gone(300)
                else:
                    e.on_unit(fx.unit(300, 'sam-0', 'Strela-10M3', 'red', 'site-0', 'GROUND', *fx.offset(90, 20), 0))
                self.assertFalse(u.receipt('site', 'check'))
                self.assertFalse(e.announced)

    def test_missile_gone_after_reservation(self):
        e, clock, _, u = self.missile_fixture()
        e.on_cockpit_packet(f4e_packet())
        e.on_weapons([])
        e.on_event(MissileTests().shot(4294967290))
        self.assertIsNotNone(u.poll('shot', 'pilot'))
        clock.t += 1
        e.on_weapons([MissileTests().scan_row(16786001, rng=9.9)])
        self.assertTrue(u.receipt('shot', 'check'))
        clock.t += 1
        e.on_weapons([])
        self.assertTrue(u.receipt('shot', 'check'))  # recent launch fact survives scan loss
        clock.t += 1.01
        self.assertFalse(u.receipt('shot', 'check'))
        self.assertFalse(e.announced)

    def test_kill_confirmation_and_pending_splash_are_protected(self):
        for text in ('Athena, confirm kill', 'Athena, radio check', 'Athena, explain the weather'):
            with self.subTest(text=text):
                _, e, c, _, own = fixture()
                e.on_gone(300)
                target = fx.unit(401, 'truck', 'Ural-4320-31', 'red', 'convoy', 'GROUND', *fx.offset(90, 2), 0)
                e.on_unit(target)
                e.on_event(fx.kill(own, 123, 'weapons.missiles.AGM_65D', target))
                c._wire_priority = True
                f = say(c, text, 'kill')[0][0]
                self.assertEqual(f['priority'], 'threat')

    def test_cockpit_launch_keeps_action(self):
        _, e, _, u, _ = fixture()
        e.on_gone(300)
        e.on_cockpit_packet({'v': 1, 'seq': 1, 'ac': 'F-16C_50', 'unit': 'my-plane', 'coal': 2,
                            'pilot': 'Prometheus', 'lat': 42., 'lon': 42., 'alt': 1000., 'hdg': 90.,
                            'rwr': {'e': [{'id': 10, 'type': 'SA-11', 'sig': 'launch', 'az': 0}]}})
        f = u.poll('rwr', 'pilot')
        self.assertEqual(f['priority'], 'launch')
        self.assertIn('launch', f['speech'].lower())
        self.assertIn('SA-11', f['speech'])

    def test_locks_cannot_hide_dcs_launch(self):
        e, _, _, u = self.missile_fixture()
        e.on_cockpit_packet(f4e_packet(rwr={'e': [
            {'id': 21, 'type': 'SA-2', 'sig': 'lock', 'az': 0},
            {'id': 22, 'type': 'SA-11', 'sig': 'lock', 'az': 1}]}))
        e.on_event(MissileTests().shot(83))
        self.assertEqual(u.poll('hidden', 'pilot')['priority'], 'launch')

    def test_range_only_is_not_unsolicited_tracking(self):
        _, _, _, u, _ = fixture(rng=1.5)
        self.assertIsNone(u.poll('range-only', 'pilot'))

    def test_radar_without_launcher_has_no_lethal_envelope(self):
        _, e, c, u, _ = fixture()
        e.on_gone(300)
        e.on_unit(fx.unit(501, 'search-radar', 'p-19 s-125 sr', 'red', 'radar-only',
                          'GROUND', *fx.offset(90, 17), 0))
        e.tick()
        self.assertIsNone(u.poll('radar', 'pilot'))
        self.assertNotIn('lethal', say(c, 'That SAM seems dormant.', 'radar')[0][0]['speech'])

    def test_model_loss_filter_is_deleted_not_refined(self):
        for body in ('Your aircraft has crashed.', 'You have been shot down.',
                     "You're down to five hundred pounds of fuel.", 'You have lost radar lock.'):
            with self.subTest(body=body):
                _, e, _, _, _ = fixture()
                e.on_gone(300)
                c, _ = controller({}, Stub(answer='Prometheus, Athena, '+body), eye=e)
                self.assertIn(body, say(c, 'Athena, what happened?', 'model')[0][0]['speech'])

    def test_positive_standalone_radar_only_gets_short_radar_call(self):
        _, e, _, u, _ = fixture()
        e.on_gone(300)
        e.on_unit(fx.unit(501, 'search-radar', 'p-19 s-125 sr', 'red', 'radar-only',
                          'GROUND', *fx.offset(90, 17), 0))
        e.on_radars([{'name': 'search-radar', 'has_radar': True, 'on': True, 'tracking_me': True}])
        e.tick()
        f = u.poll('radar', 'pilot')
        self.assertEqual(f['speech'], "SA-3 radar active, your twelve o'clock, seventeen miles.")
        self.assertTrue(u.receipt('radar', 'check'))

    def test_prompted_two_missiles_use_shared_short_call(self):
        e, _, c, _ = self.missile_fixture()
        e.on_event(MissileTests().shot(81))
        e.on_event(MissileTests().shot(82))
        f = say(c, 'Athena, missile launch?', 'two')[0][0]
        self.assertIn('Two SA-11 missiles', f['speech'])
        self.assertNotIn('Also ', f['speech'])

    def test_failed_launch_retry_is_bounded_but_new_launch_is_not(self):
        e, clock, _, u = self.missile_fixture()
        e.on_event(MissileTests().shot(12))
        self.assertIsNotNone(u.poll('first', 'pilot'))
        u.receipt('first', 'failed')
        clock.t += .1
        self.assertIsNone(u.poll('retry', 'pilot'))
        e.on_event(MissileTests().shot(13))
        self.assertIsNotNone(u.poll('new-launch', 'pilot'))

    def test_shot_only_evidence_expires_after_three_seconds(self):
        e, clock, _, u = self.missile_fixture()
        e.on_event(MissileTests().shot(12))
        self.assertIsNotNone(u.poll('shot', 'pilot'))
        clock.t += 3.01
        self.assertFalse(u.receipt('shot', 'check'))

    def test_sixty_sites_cover_all_grouped_keys_without_deque_loss(self):
        _, e, _, u, _ = fixture(sites=60)
        self.assertIsNotNone(u.poll('sixty', 'pilot'))
        self.assertEqual(len(e.announced), 60)

    def test_journal_recovery_floor_starts_at_observed_completion(self):
        now, e, c, u, own = fixture()
        e.on_unit(fx.unit(301, 'different', 'Strela-10M3', 'red', 'different', 'GROUND', *fx.offset(0, 7), 0))
        e.tick()
        self.assertIsNotNone(u.poll('first', 'pilot'))
        now[0] += 8
        e.on_unit(own)
        c.transcript.outcome_of = lambda key: 'sent' if key == 'first' else None
        self.assertIsNone(u.poll('second', 'pilot'))
