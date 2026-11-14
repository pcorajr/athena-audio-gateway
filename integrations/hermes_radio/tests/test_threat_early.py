"""11-12 five-flight evidence replay. No model/network access; model bodies are explicit stubs.

The fixture retains the final picture actually used per exchange (the busy launch
retries used a later picture than the first tool row). Stationary SAM coordinates
are from the copied mission DB; a site's presence is checked against each recorded
hostile picture before using it. Non-threat fields remain the captured picture.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest import mock

from integrations.hermes_radio import eye as eye_module
from integrations.hermes_radio import simple_controller as sc
from integrations.hermes_radio.eye import Eye, GrpcFeeder, missile_spoken_name, sam_profile
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say

DATA = json.loads((Path(__file__).parent / 'fixtures/threat_flights_20261112.json').read_text())
LIVE = {r['time'][11:19]: r for r in DATA['exchanges']}
PART_A = json.loads((Path(__file__).parent / 'fixtures/threat_flights_part_a_20261112.json').read_text())
LATE = {r['time'][11:19]: r for r in PART_A['exchanges']}


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def move_own(eye, picture):
    own = picture['ownship']
    eye.on_units_message({'unit': fx.unit(17096, 'Prometheus', own['type'], 'blue', own['group'], 'AIRPLANE',
        own['lat'], own['lon'], own['alt_ft'] / eye_module.M_TO_FT,
        float(own['heading_true']), own['speed_kt'] / eye_module.MS_TO_KT, player='Prometheus')})
    eye.set_connected(True)


def amended_picture(entry, eye):
    """Replay geometry with Eye; preserve unrelated captured data, not a rebuilt fictional world."""
    pic = deepcopy(entry['picture'])
    if pic.get('ownship'):
        move_own(eye, pic)
    eye.tick()
    generated = eye.picture()
    sites = generated['threats'].get('sam_sites') or []
    known = {t for g in pic.get('hostile_groups', []) for t in g.get('types', {})}
    # Flight 1 lists its SA-2 in sam_in_range even when the ground picture is capped.
    known.update(r['type'] for r in pic['threats'].get('sam_in_range') or [])
    pic['threats']['sam_sites'] = [s for s in sites if s['type'] in known]
    replacements = {}
    for group in pic.get('hostile_groups', []):
        labels = list(dict.fromkeys(label for t in group.get('types', {}) if (label := sam_profile(t)[1])))
        if group.get('category') in ('SAM', 'AAA') and labels:
            old = group['call']
            group['call'] = old.replace(group['label'], ' and '.join(labels) + ' site', 1)
            replacements[old] = group['call']
    for row in pic.get('ground_targets_within_20nm', []):
        row['call'] = replacements.get(row['call'], row['call'])
    for field in ('missiles_inbound', 'missiles_in_flight'):
        for missile in pic['threats'].get(field) or []:
            name = missile_spoken_name(missile.get('raw_type') or missile['type'], missile.get('launcher'))
            replacements[missile['call']] = missile['call'].replace(missile['spoken_name'], name)
            missile['spoken_name'] = name
            missile['call'] = replacements[missile['call']]
    alerts = [replacements.get(a, a) for a in pic.get('rwr_alert_to_announce', [])]
    keys = ['recorded-' + str(i) for i in range(len(alerts))]
    for alert, key in zip(generated.get('rwr_alert_to_announce', []), generated.get('_rwr_keys', [])):
        if alert.startswith('Caution,') and any(s['caution_call'] == alert for s in pic['threats']['sam_sites']):
            alerts.append(alert); keys.append(key)
    pic['rwr_alert_to_announce'], pic['_rwr_keys'] = alerts, keys
    if entry['time'][11:19] == '20:21:07':
        # The old snapshot predates the session. Actual reset is separately exercised below.
        pic['connected'] = True
        pic['data_age_s']['units'] = generated['data_age_s']['units']
        pic['threats']['sam_in_range'] = []
    for event in ((pic.get('ownship') or {}).get('damage') or {}).get('events', []):
        event['call'] = event['call'].replace('You took damage, life down to ', "You're hit, airframe at ")
    # Naming only; target IDs, confidence, ownership, kill timing and counts are untouched.
    for field in ('my_kills_this_mission', 'my_kills_recent', 'friendly_kills_this_mission', 'friendly_kills_recent'):
        for kill in pic.get(field) or []:
            if kill.get('target') == 'Ural-4320-31':
                kill['target'] = eye_module.spoken_type(kill['target'])
            if kill.get('splash_call'):
                kill['splash_call'] = kill['splash_call'].replace('Ural-4320-31', eye_module.spoken_type('Ural-4320-31'))
    if pic.get('splash_to_announce'):
        pic['splash_to_announce'] = [s.replace('Ural-4320-31', eye_module.spoken_type('Ural-4320-31')) for s in pic['splash_to_announce']]

    return pic, replacements


def replay(data=DATA):
    now = [0.0]
    c, stub_eye = controller({}, Stub())
    eye, mission, results = None, None, []
    for entry in data['exchanges']:
        seq = str(entry['mission']) if 'mission' in entry else ('13' if entry['picture']['ownship']['type'].startswith('F-4') else '14')
        now[0] = stamp(entry['picture_time'])
        if seq != mission:
            eye = Eye('Prometheus', clock=lambda: now[0])

            for unit in data['missions'][seq]['units']:
                eye.on_units_message(unit)
            mission = seq
            stub_eye.reset_at = now[0]
            stub_eye.announced.clear()
            applied = set()
        for event in data['missions'][seq]['events']:
            if event.get('kind') in ('pilot_dead', 'crash', 'ejection') and event['received_at'] <= now[0] and event['raw_id'] not in applied:
                eye.on_event(event['payload'])
                applied.add(event['raw_id'])
        pic, replacements = amended_picture(entry, eye)
        stub_eye.pic = pic
        # Original recorded model text is held constant, including raw IDs. Only
        # controller-owned calls may replace it; this is not new model inference.
        body = entry['old_reply']
        c._llm = Stub(answer=body)
        with mock.patch.object(sc.time, 'time', return_value=now[0]), mock.patch.object(sc.time, 'monotonic', return_value=now[0]):
            frames, _ = say(c, entry['text'], tid=entry['time'])
        result = frames[0]
        eye.mark_announced(stub_eye.announced)
        # Synthetic keys are only for the current captured pending set.
        stub_eye.announced.difference_update(k for k in list(stub_eye.announced) if k.startswith('recorded-'))
        results.append({'time': entry['time'][11:19], 'mission': int(seq), 'picture_row': entry['picture_row'],
                        'picture_time': entry['picture_time'], 'text': entry['text'], 'old': entry['old_reply'],
                        'new': result['speech'], 'old_route': entry['old_route'], 'route': result['route'],
                        'stub_calls': c._llm.calls})
    return results


class FlightReplayTests(unittest.TestCase):
    def test_two_flights_and_unchanged_fixed_replies(self):
        results = replay()
        by_time = {r['time']: r for r in results}
        self.assertEqual(len(results), 39)
        self.assertIn('Caution, SA-13, heat seeker', by_time['20:32:08']['new'])
        self.assertIn('eight miles, lethal inside three', by_time['20:32:08']['new'])
        self.assertIn('SA-13 site, bearing two two two, eleven miles', by_time['20:31:30']['new'])
        self.assertIn('Negative, SA-13 is alive', by_time['20:33:44']['new'])
        self.assertEqual(by_time['20:33:44']['stub_calls'], 0)
        # r12.10: said once; "I'm going to get closer" 13 s later is not contradicted a second time
        self.assertNotIn('is alive', by_time['20:33:57']['new'])
        for t in ('20:34:31', '20:34:37', '20:34:45'):
            self.assertIn('SA-13', by_time[t]['new'])
            if by_time[t]['route'] == 'fixed':
                self.assertNotRegex(by_time[t]['new'], r'SA-9\b|SA9M333')
        self.assertIn('missile launch, sa-13,', by_time['20:34:31']['new'].lower())
        changed_fixed = {'20:21:07', '20:21:31', '20:21:57', '20:22:19', '20:31:30', '20:31:56',
                         '20:34:37', '20:34:45', '20:35:06'}
        for row in results:
            with self.subTest(time=row['time']):
                self.assertLessEqual(len(row['new']), 280)
                if row['old_route'] == 'fixed' and row['time'] not in changed_fixed:
                    self.assertEqual(row['new'], row['old'])
        self.assertLessEqual(sum('Caution,' in r['new'] for r in results if r['time'] < '20:20:00'), 1)
        self.assertEqual(sum('Caution, SA-13' in r['new'] for r in results), 1)
        for t in ('20:21:31', '20:21:57', '20:22:19'):
            self.assertEqual(by_time[t]['new'], 'Prometheus, Athena, No clearance from me. No traffic.')
        self.assertNotIn('four hundred nine', by_time['20:21:07']['new'])

    def test_actual_ground_geometry(self):
        e = LIVE['20:31:30']
        eye = Eye('Prometheus')
        for unit in DATA['missions']['14']['units']:
            eye.on_units_message(unit)
        move_own(eye, e['picture'])
        eye.declination = -11.8  # captured picture says magnetic declination -11.8
        # Recorded heading is rounded at the 12/1 clock boundary. The exchange
        # replay checks the recorded clock; this independently checks geometry.
        self.assertTrue(eye.picture()['ground_targets_within_20nm'][0]['call'].startswith(
            'SA-13 site, bearing two two two, eleven miles,'))

    def test_every_recorded_hostile_launch_uses_launcher(self):
        count = 0
        for seq in ('13', '14'):
            expected = 'SA-2' if seq == '13' else 'SA-13'
            for event in DATA['missions'][seq]['events']:
                body = event['payload'].get('shot')
                if not body or body['initiator']['unit']['coalition'] != 'COALITION_RED':
                    continue
                count += 1
                prior = [e for e in DATA['exchanges'] if stamp(e['picture_time']) <= event['received_at']]
                eye = Eye('Prometheus', clock=lambda: event['received_at'])
                move_own(eye, prior[-1]['picture'])
                eye.on_event(event['payload'])
                missiles = eye.picture()['threats']['missiles_in_flight']
                self.assertTrue(missiles)
                self.assertEqual(missiles[0]['spoken_name'], expected)
                self.assertIn('Missile launch, ' + expected + ',', missiles[0]['call'])
        self.assertEqual(count, 6)


class ApproachTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.eye = Eye('Prometheus', clock=lambda: self.now)
        self.eye.on_units_message(fx.units_message(fx.PROMETHEUS))
        self.eye.set_connected(True)

    def site(self, nm=7, kind='Strela-10M3', uid=1, coal='red', group='SAM'):
        self.eye.on_units_message({'unit': fx.unit(uid, str(uid), kind, coal, group, 'GROUND',
            *fx.offset(90, nm), 6096.0)})
        self.eye.tick()
        return self.eye.picture()

    def caution(self, pic):
        return [a for a in pic.get('rwr_alert_to_announce', []) if a.startswith('Caution,')]

    def test_formula_all_range_tiers(self):
        for _, nominal, name in eye_module.SAM_RANGES:
            distance = min(150, max(1.5 * nominal, 8))
            self.assertGreater(distance, nominal)
            self.assertLessEqual(distance, 150)
        self.assertEqual(min(150, max(1.5 * sam_profile('Strela-10M3')[0], 8)), 8)
        self.assertEqual(min(150, max(1.5 * sam_profile('S_75M_Volhov')[0], 8)), 34.5)

    def test_once_per_approach_and_delivery_rearm(self):
        pic = self.site()
        keys = pic['_rwr_keys']
        self.assertEqual(len(self.caution(pic)), 1)
        self.eye.mark_announced(keys)
        for nm in (6, 3, 2, 5, 7):
            self.now += 100
            self.assertFalse(self.caution(self.site(nm)))
        self.site(9)
        self.assertEqual(len(self.caution(self.site(7))), 1)
        new = self.eye.picture()['_rwr_keys']
        self.assertNotEqual(new, keys)
        self.eye.mark_announced(new)
        self.assertFalse(self.caution(self.eye.picture()))
        self.eye.unmark_announced(new)
        self.assertEqual(len(self.caution(self.eye.picture())), 1)

    def test_pending_survives_silence_refreshes_geometry_and_dies_with_site(self):
        self.site()
        self.now += 120
        self.assertIn('five miles', self.caution(self.site(5))[0])
        self.eye.on_event(fx.dead(fx.unit(1, '1', 'Strela-10M3', 'red', 'SAM', 'GROUND', *fx.offset(90, 5), 6096)))
        self.assertFalse(self.caution(self.eye.picture()))

    def test_no_rearm_from_missing_feed_or_inside_weapon_range(self):
        p = self.site()
        self.eye.mark_announced(p['_rwr_keys'])
        old = self.eye.units.pop(1)
        self.now += 200
        self.eye.tick()
        self.eye.units[1] = old
        self.assertFalse(self.caution(self.site()))
        self.eye.reset('new')
        self.eye.on_units_message(fx.units_message(fx.PROMETHEUS))
        self.assertFalse(self.caution(self.site(2)))
        self.assertFalse(self.caution(self.site(5)))

    def test_no_friendly_ewr_optical_or_support_caution(self):
        for kind, coal in [('Strela-10M3', 'blue'), ('Strela-10M3', 'neutral'), ('55G6 EWR', 'red'),
                           ('SA-18 Igla comm', 'red'), ('ZU-23 Emplacement', 'red')]:
            with self.subTest(kind=kind, coal=coal):
                self.assertFalse(self.caution(self.site(5, kind, coal=coal)))

    def test_unestablished_launcher_has_no_lethal_caution(self):
        pic = self.site(5, 'SAM unknown')
        self.assertFalse(self.caution(pic))
        self.assertFalse(self.caution(self.site(6, 'ZSU-23-4 Shilka', uid=2, group='AAA')))

    def test_cancelled_reply_reoffers_same_caution_key(self):
        self.site()
        c, _ = controller({}, Stub(), eye=self.eye)
        first, _ = say(c, 'radio check', 'first')
        announced = set(self.eye.announced)
        c.transcript.outcome_of = lambda tid: 'expired'
        second, _ = say(c, 'radio check', 'second')
        self.assertEqual(first[0]['speech'], second[0]['speech'])
        self.assertEqual(self.eye.announced, announced)
        self.assertEqual(len([a for a in self.eye.threat_alerts if a['kind'] == 'SAM approach']), 1)

    def test_no_doubled_caution_on_fixed_counterstatement(self):
        self.site()
        c, _ = controller({}, Stub(answer='bad echo'), eye=self.eye)
        frames, _ = say(c, 'The SAM looks dormant')
        self.assertEqual(frames[0]['speech'].count('SA-13'), 1)
        self.assertTrue(self.eye.announced)
        self.assertIn('Negative, SA-13 is alive', frames[0]['speech'])


class NarrowReplyTests(unittest.TestCase):
    def setUp(self):
        self.pic = deepcopy(LIVE['20:33:44']['picture'])
        self.pic['threats']['sam_sites'] = [{'sam': 'SA-13', 'clock': 3, 'range_nm': 3.4, 'nominal_range_nm': 2.7}]

    def test_only_sam_complacency_and_approach_match(self):
        for text in ('SAM looks dormant', 'SAM looks dead', 'SAM seems quiet', 'SAM is not shooting',
                     'going in closer on the SAM', 'pressing on the SAM', 'running in on the SA-13'):
            self.assertEqual(sc.call_class(text, self.pic), 'sam_caution', text)
        for text in ('SAM is dead', 'target destroyed', 'SAM launch', 'the engine is quiet',
                     'going closer to Creech', 'pressing on the armor', 'feature request: SAM looks dormant'):
            self.assertNotEqual(sc.call_class(text, self.pic), 'sam_caution', text)
        self.assertEqual(sc.call_class("I'm going to get closer", self.pic, ['the SAM looks dormant']), 'sam_caution')
        self.assertNotEqual(sc.call_class("I'm going to get closer", self.pic, ['vector to Creech']), 'sam_caution')

    def test_no_live_site_lets_model_answer(self):
        self.pic['threats']['sam_sites'] = []
        stub = Stub(answer='Prometheus, Athena, no live SAM site showing.')
        c, _ = controller(self.pic, stub)
        say(c, 'SAM looks dormant')
        self.assertEqual(stub.calls, 1)

    def test_launcher_names_and_raw_unknown_fallback(self):
        for raw, launcher, expected in [('SA9M333', 'Strela-10M3', 'SA-13'),
                ('SA2V755', 'S_75M_Volhov', 'SA-2'), ('SA9M333', None, 'missile'),
                ('SA9M38M1', None, 'missile'), ('SA-2', None, 'SA-2'), ('SA-9', 'SAM unknown', 'missile'),
                ('some.raw_id', None, 'missile')]:
            self.assertEqual(sc._missile_name(raw, launcher), expected)

    def test_site_alert_never_swallows_same_designation_missile(self):
        site = "SAM SA-13 could be tracking you, your two o'clock, three miles."
        launch = "Missile launch, SA-13, your two o'clock, two miles."
        self.assertFalse(sc.announcement_echo(launch, [site], self.pic))
        self.assertFalse(sc.announcement_echo(site, [launch], self.pic))
        self.assertTrue(sc.announcement_echo(launch, [launch], self.pic))

    def test_scan_resolves_known_launcher_unit(self):
        eye = Eye('Prometheus')
        eye.on_units_message(fx.units_message(fx.PROMETHEUS))
        eye.on_units_message({'unit': fx.unit(1, 'launcher', 'Strela-10M3', 'red', 'SAM', 'GROUND', *fx.offset(90, 2), 0)})
        lat, lon = fx.offset(90, 1)
        eye.on_weapons([{'id': 2, 'type': 'SA9M333', 'launcher': 'launcher', 'lat': lat, 'lon': lon,
                         'coal': 1, 'target': fx.PROMETHEUS['name']}])
        self.assertEqual(eye.picture()['threats']['missiles_inbound'][0]['spoken_name'], 'SA-13')

    def test_scan_retains_launcher_from_shot_bridge_and_previous_scan(self):
        event = next(e for e in DATA['missions']['14']['events'] if 'shot' in e['payload']
                     and e['payload']['shot']['initiator']['unit']['coalition'] == 'COALITION_RED')
        now = [event['received_at']]
        eye = Eye('Prometheus', clock=lambda: now[0])
        move_own(eye, LIVE['20:34:31']['picture'])
        eye.on_event(event['payload'])
        position = event['payload']['shot']['weapon']['position']
        scan = {'id': 222, 'type': 'SA9M333', 'coal': 1, 'lat': position['lat'], 'lon': position['lon'],
                'target': 'Prometheus'}
        for _ in range(2):
            now[0] += 1
            eye.on_weapons([scan])
            row = eye.picture()['threats']['missiles_inbound'][0]
            self.assertEqual(row['spoken_name'], 'SA-13')
            self.assertIn('Missile launch, SA-13,', row['call'])

    def test_pending_launches_capped_on_fixed_and_model_routes(self):
        pic = deepcopy(LIVE['20:34:45']['picture'])
        pic['rwr_alert_to_announce'] = ["Missile launch, SA-13, your %s o'clock, two miles." % sc.words(i)
                                      for i in range(1, 5)]
        pic['_rwr_keys'] = ['launch-%d' % i for i in range(4)]
        for text in ('fuel status', 'radio check'):
            with self.subTest(text=text):
                c, eye = controller(deepcopy(pic), Stub(answer='copy.'))
                frames, _ = say(c, text)
                speech = frames[0]['speech']
                self.assertEqual(speech.count('Missile launch,'), 2)
                self.assertEqual(speech.count('Two more in the air.'), 1)
                self.assertLessEqual(len(speech), 280)
                self.assertEqual(eye.announced, set(pic['_rwr_keys']))

    def test_two_missile_details_rest_counted_and_not_doubled(self):
        pic = deepcopy(LIVE['20:34:45']['picture'])
        missiles = []
        for i in range(4):
            missiles.append({'id': str(i), 'type': 'SA9M333', 'spoken_name': 'SA-13', 'launcher': 'Strela-10M3',
                'clock': i + 1, 'range_nm': i + 1, 'call': "Missile launch, SA-13, your %s o'clock, %s miles." % (sc.words(i + 1), sc.words(i + 1))})
        pic['threats']['missiles_in_flight'] = missiles
        pic['rwr_alert_to_announce'] = [m['call'] for m in missiles]
        pic['_rwr_keys'] = [m['id'] for m in missiles]
        c, eye = controller(pic, Stub())
        frames, _ = say(c, 'SAM launch')
        speech = frames[0]['speech']
        self.assertEqual(speech.count('SA-13'), 1)
        self.assertIn("Two SA-13 missiles, one and two o'clock.", speech)
        self.assertIn('Two more in the air.', speech)
        self.assertLessEqual(len(speech), 280)
        self.assertEqual(eye.announced, {'0', '1', '2', '3'})

    def test_clearance_repeat_timeout_and_real_traffic(self):
        pic = deepcopy(LIVE['20:21:31']['picture'])
        c, _ = controller(pic, Stub())
        with mock.patch.object(sc.time, 'monotonic', return_value=1000):
            first, _ = say(c, 'request permission to take off', 'one')
        with mock.patch.object(sc.time, 'monotonic', return_value=1020):
            second, _ = say(c, 'request permission to take off', 'two')
        with mock.patch.object(sc.time, 'monotonic', return_value=1081):
            third, _ = say(c, 'request permission to take off', 'three')
        self.assertIn("Athena, no tower: I can't give clearances.", first[0]['speech'])
        self.assertEqual(second[0]['speech'], 'Prometheus, Athena, No clearance from me. No traffic.')
        self.assertIn("I can't give clearances.", third[0]['speech'])

    def test_session_change_and_mission_start_drop_previous_picture(self):
        for boundary in ('session', 'event'):
            now = [1000.0]
            eye = Eye('Prometheus', clock=lambda: now[0])
            eye.on_units_message(fx.units_message(fx.PROMETHEUS))
            eye.set_connected(False)
            eye.session_id = 1
            now[0] += 409
            if boundary == 'session':
                feeder = GrpcFeeder(eye, host='offline.invalid')
                stub = mock.Mock()
                stub.GetSessionId.return_value.session_id = 2
                with mock.patch.object(feeder, 'resubscribe_units'):
                    feeder._check_session(stub)
            else:
                eye.on_event({'time': 0, 'mission_start': {}})
            self.assertIsNone(eye.picture()['data_age_s']['units'])
            self.assertFalse(eye.units)
            c, _ = controller({}, Stub(), eye=eye)
            frames, _ = say(c, 'request permission to take off')
            self.assertNotIn('last known', frames[0]['speech'])
            self.assertNotIn('four hundred nine', frames[0]['speech'])

    def test_cancelled_clearance_is_not_counted_as_said(self):
        c, _ = controller(deepcopy(LIVE['20:21:31']['picture']), Stub())
        first, _ = say(c, 'request permission to take off', 'first')
        c.transcript.outcome_of = lambda tid: 'expired'
        second, _ = say(c, 'request permission to take off', 'second')
        self.assertIn("I can't give clearances.", first[0]['speech'])
        self.assertEqual(first[0]['speech'], second[0]['speech'])

    def test_real_human_failure_retained_and_new_life_wording(self):
        row = next(e for e in DATA['missions']['14']['events'] if 'human_failure' in e['payload'])
        eye = Eye('Prometheus', clock=lambda: row['received_at'])
        unit = row['payload']['human_failure']['initiator']['unit']
        eye.on_units_message({'unit': unit})
        eye.on_event(row['payload'])
        self.assertEqual(eye.damage_events[-1]['call'], 'Systems damage reported.')
        eye.on_life(eye.ownship(), {'life': 100, 'life0': 100})
        eye.on_life(eye.ownship(), {'life': 97, 'life0': 100})
        self.assertEqual(eye.damage_events[-1]['call'], "You're hit, airframe at ninety seven percent.")


class PartARecordedTests(unittest.TestCase):
    def test_five_flights_and_exact_scoped_fixed_changes(self):
        results = replay() + replay(PART_A)
        self.assertEqual(len(results), 94)
        self.assertEqual({r['mission'] for r in results}, {13, 14, 15, 16, 17})
        rows = {r['time']: r for r in results}
        expected = {
            '21:19:37': "No missile seen; SA-2 tracking you, your twelve o'clock, four miles.",

            '21:38:43': 'Confirmed, splash one Ural truck.',

        }
        for t, body in expected.items():
            self.assertEqual(rows[t]['new'], sc.ADDRESS + body)
            self.assertEqual(rows[t]['stub_calls'], 0)
        self.assertIn('Caution, SA-13 is alive', rows['21:41:00']['new'])
        self.assertEqual(rows['21:41:00']['stub_calls'], 0)
        for t in ('21:35:44', '21:35:58'):
            self.assertEqual(rows[t]['route'], 'fixed')
            self.assertIn('Friendly A-10C', rows[t]['new'])
            self.assertNotIn("can't see your target", rows[t]['new'])
        self.assertEqual(rows['21:42:14']['new'], rows['21:42:14']['old'])
        changed_fixed = {'21:19:37', '21:21:33', '21:24:06', '21:28:31', '21:35:44', '21:38:43', '21:40:45'}
        for row in results:
            self.assertLessEqual(len(row['new']), 280)
            if row['mission'] >= 15 and row['old_route'] == 'fixed' and row['time'] not in changed_fixed:
                self.assertEqual(row['new'], row['old'], row['time'])
            if row['route'] == 'fixed':
                lines = sc.sentences(row['new'].removeprefix(sc.ADDRESS))
                self.assertEqual(len(lines), len(set(lines)), row['time'])

    def test_sa2_recorded_geometry_admitted_bridge_then_scan_disappearance(self):
        mission = PART_A['missions']['16']
        shot = next(e for e in mission['events'] if e['kind'] == 'shot' and
                    e['payload']['shot']['weapon']['type'] == 'SA2V755')
        packet = next(o['payload']['packet'] for o in mission['observations'] if o['type'] == 'cockpit_input')
        body = shot['payload']['shot']
        self.assertEqual(body['initiator']['unit']['name'], 'Ground-8-7')
        self.assertEqual(body['weapon']['id'], 4294967294)
        brg, rng = eye_module.bearing_range(packet['lat'], packet['lon'],
            body['weapon']['position']['lat'], body['weapon']['position']['lon'])
        angle = abs(((brg + 180 - body['weapon']['orientation']['heading']) + 180) % 360 - 180)
        self.assertLess(rng, eye_module.MISSILE_ALERT_NM)
        self.assertLess(angle, 45)
        now = [shot['received_at']]
        eye = Eye('Prometheus', clock=lambda: now[0])
        eye.on_units_message({'unit': fx.unit(17091, 'Aerial-3-2', packet['ac'], 'blue', packet['group'],
            'AIRPLANE', packet['lat'], packet['lon'], packet['alt'], packet['hdg'], packet['gs'], player='Prometheus')})
        eye.on_weapons([])  # explicit boundary probe: recent pre-shot scan, not recorded raw Eval
        eye.on_event(shot['payload'])
        bridge = eye.missile_shots['4294967294']
        self.assertTrue(bridge['closing'])
        self.assertEqual(bridge['spoken_name'], 'SA-2')
        now[0] += 1
        eye.on_weapons([])  # an answered post-shot empty scan cannot outlive the bridge floor
        self.assertTrue(eye._shot_live(bridge, now[0]))
        now[0] += 2
        self.assertFalse(eye._shot_live(bridge, now[0]))
        observed = [o for o in mission['observations'] if o['type'] == 'sam_threat']
        live = [o for o in observed if o['payload']['value']['missiles_in_flight']]
        first = live[0]['payload']['value']['missiles_in_flight'][0]
        last = live[-1]['payload']['value']['missiles_in_flight'][0]
        self.assertEqual(first['id'], '16906496')
        self.assertAlmostEqual(first['first_seen'], shot['received_at'], places=3)  # bridge really merged
        self.assertEqual((first['range_nm'], last['range_nm']), (5.8, 1.4))
        gone = next(o for o in observed if o['time'] > live[-1]['time'])
        self.assertEqual(gone['payload']['value']['missiles_in_flight'], [])
        self.assertEqual(gone['payload']['value']['missile_scan'], 'on')
        self.assertLess(gone['time'] - shot['received_at'], eye_module.EVAL_OFF_KEEP_S)
        self.assertEqual(LATE['21:19:37']['picture']['threats']['sam_in_range'][0]['confidence'], 'DCS radar: tracking you')

    def test_sa13_latest_picture_is_after_hit_not_launch_time(self):
        entry = LATE['21:42:14']
        self.assertEqual(entry['picture_time'][11:19], '21:42:26')
        mission = PART_A['missions']['17']
        hits = [e for e in mission['events'] if e['kind'] == 'hit' and e['payload']['hit'].get('weapon_name') == 'SA9M333']
        self.assertEqual(len(hits), 1)
        self.assertGreater(stamp(entry['picture_time']) - hits[0]['received_at'], 15)
        th = entry['picture']['threats']
        self.assertEqual(th['missile_scan'], 'on')
        self.assertEqual(th['missiles_in_flight'], [])
        self.assertEqual(th['sam_in_range'][0]['confidence'], 'range only: could be tracking')

    def test_speed_was_real_cockpit_ias_not_invented_from_velocity(self):
        pic = LATE['21:33:07']['picture']
        self.assertEqual(pic['ownship']['speed_kt'], 590)
        self.assertTrue(pic['cockpit']['fresh'])
        self.assertEqual(pic['cockpit']['flight']['ias_kt'], 489)
        self.assertEqual(pic['cockpit']['flight']['tas_kt'], 589)


    def test_ural_naming_only_preserves_delayed_kill_identity(self):
        for name in ('Ural-4320T', 'Ural-4320-31', 'Ural truck'):
            self.assertEqual(eye_module.spoken_type(name), 'Ural truck')
        pic = deepcopy(LATE['21:38:43']['picture'])
        original = deepcopy(pic['my_kills_this_mission'])
        for row in pic['my_kills_this_mission']:
            row['target'] = eye_module.spoken_type(row['target'])
        c, _ = controller(pic, Stub())
        frames, _ = say(c, 'tally')
        self.assertIn('four Ural truck', frames[0]['speech'])
        for old, new in zip(original, pic['my_kills_this_mission']):
            self.assertEqual({k: v for k, v in old.items() if k != 'target'}, {k: v for k, v in new.items() if k != 'target'})


class PartABoundaryTests(unittest.TestCase):
    def test_tracking_launch_empty_scan_keeps_limitation_and_no_double_alert(self):
        pic = deepcopy(LATE['21:19:37']['picture'])
        call = pic['threats']['sam_in_range'][0]['call']
        pic.update(rwr_alert_to_announce=[call], _rwr_keys=['tracking'])
        c, eye = controller(pic, Stub())
        frames, _ = say(c, 'SAM launch')
        self.assertEqual(frames[0]['speech'].count('SA-2'), 1)
        self.assertIn('No missile seen;', frames[0]['speech'])
        self.assertIn('tracking', eye.announced)
        pic['threats']['missile_scan'] = 'off: missile scan is not answering'
        c, _ = controller(pic, Stub())
        frames, _ = say(c, 'SAM launch')
        self.assertIn('Missile tracking limited.', frames[0]['speech'])

    def test_referent_uses_immediate_athena_reply_only_and_still_live(self):
        pic = deepcopy(LATE['21:41:00']['picture'])
        eye = Eye('Prometheus')
        for unit in PART_A['missions']['17']['units']:
            eye.on_units_message(unit)
        pic, _ = amended_picture(LATE['21:41:00'], eye)
        for phrase in ('Roger, headed towards it.', "I'm going in."):
            for previous, live, expected in [('SA-13 site, ten miles.', True, True),
                    ('Creech, ten miles.', True, False), ('copy.', True, False),
                    ('SA-13 site, ten miles.', False, False)]:
                with self.subTest(phrase=phrase, previous=previous, live=live):
                    p = deepcopy(pic)
                    if not live:
                        p['threats']['sam_sites'] = []
                    stub = Stub(answer=sc.ADDRESS + previous)
                    c, _ = controller(p, stub)
                    say(c, 'radio check', 'first')
                    stub.answer_text = sc.ADDRESS + 'copy.'
                    frames, _ = say(c, phrase, 'next')
                    self.assertEqual('Caution, SA-13 is alive' in frames[0]['speech'], expected)
        for phrase in ("I'm going in on the convoy.", "I'm not going in.", 'Going towards Creech.'):
            c, _ = controller(pic, Stub(answer=sc.ADDRESS + 'SA-13 site, ten miles.'))
            say(c, 'radio check', 'one')
            frames, _ = say(c, phrase, 'two')
            self.assertNotIn('SA-13 is alive', frames[0]['speech'])


    def test_model_launch_is_not_filtered_beside_controller_evidence(self):
        for entry in (LIVE['20:34:31'], LATE['21:42:06']):
            eye = Eye('Prometheus')
            pic, _ = amended_picture(entry, eye)
            stub = Stub(answer=sc.ADDRESS + 'Missile launch, SA9M333, two miles. No RWR feed from this jet.')
            c, _ = controller(pic, stub)
            frames, _ = say(c, entry['text'])
            speech = frames[0]['speech']
            self.assertEqual(stub.calls, 1)
            self.assertIn('SA9M333', speech)
            self.assertEqual(speech.lower().count('missile launch'), 2)
            self.assertIn('SA-13', speech)
            self.assertLessEqual(len(speech), 280)
