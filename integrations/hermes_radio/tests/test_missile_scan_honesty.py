"""When the DCS Eval missile scan cannot run (DCS server, Eval disabled), Athena must not say "no missile in the
air": the picture and the fixed launch reply say missile tracking is limited. Shot-event launch calls still work."""
import unittest
from integrations.hermes_radio.simple_controller import PROMPT
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_eye_round4 import SA11_LN, f4e_eye, f4e_packet
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say

OFF = "No launch seen; missile tracking is limited, missile scan is not answering."
EVAL_OFF_REPLY = "No launch seen; missile tracking is limited, DCS Eval is off."
DENIED = "unavailable: DCS-gRPC Eval is disabled on the DCS PC. Set evalEnabled = true in dcs-grpc.lua"
ON = "No missile in the air that I can see."


def ask(eye, text="Athena, launch, launch"):
    c, _ = controller(None, Stub(), eye=eye)
    return say(c, text)[0][0]["speech"]


def hostile_shot(heading=281.0, wid=900):
    lat, lon = fx.offset(101, 10.2)
    return {"time": 50.0, "shot": {"initiator": {"unit": SA11_LN}, "weapon": {
        "id": wid, "type": "SA9M38M1", "position": {"lat": lat, "lon": lon, "alt": 400.0},
        "velocity": {"heading": heading, "speed": 700.0}}}}


class MissileScanHonestyTests(unittest.TestCase):
    def eye(self):
        eye, clock = f4e_eye(extra=(SA11_LN,))
        eye.on_cockpit_packet(f4e_packet())
        return eye, clock

    def test_scan_never_ran_the_picture_says_off_and_the_reply_is_not_an_all_clear(self):
        eye, _ = self.eye()
        self.assertEqual(eye.picture()["threats"]["missile_scan"], "off: missile scan is not answering")
        self.assertTrue(ask(eye).endswith(OFF))

    def test_eval_denied_says_eval_is_off(self):
        eye, _ = self.eye()
        eye.set_stores_status(DENIED)
        self.assertEqual(eye.picture()["threats"]["missile_scan"], "off: DCS Eval is off")
        self.assertTrue(ask(eye).endswith(EVAL_OFF_REPLY))

    def test_scan_running_keeps_the_plain_all_clear(self):
        eye, _ = self.eye()
        eye.on_weapons([])
        self.assertEqual(eye.picture()["threats"]["missile_scan"], "on")
        self.assertTrue(ask(eye).endswith(ON))
        self.assertNotIn("limited", ask(eye))

    def test_a_stale_scan_is_off_again(self):
        eye, clock = self.eye()
        eye.on_weapons([])
        clock.t += 30
        self.assertEqual(eye.picture()["threats"]["missile_scan"], "off: missile scan is not answering")
        self.assertTrue(ask(eye).endswith(OFF))

    def test_a_shot_event_launch_is_still_called_with_the_scan_off(self):
        eye, _ = self.eye()
        eye.on_event(hostile_shot())
        pic = eye.picture()
        self.assertEqual(pic["threats"]["missile_scan"], "off: missile scan is not answering")
        self.assertIn("Missile launch, SA9M38M1", pic["rwr_alert_to_announce"][0])
        said = ask(eye)
        self.assertIn("in the air", said)
        self.assertNotIn("No launch seen", said)

    def test_no_position_has_no_scan_claim_to_make(self):
        eye, _ = f4e_eye(extra=())
        eye.reset("x")
        self.assertIsNone(eye.picture()["threats"]["missiles_inbound"])

    def test_prompt_tells_the_model_the_same(self):
        self.assertIn("missile_scan", PROMPT)
        self.assertLessEqual(len(PROMPT), 10184)  # r12.4 size; the reason comes from the picture, not the prompt


if __name__ == "__main__":
    unittest.main()
