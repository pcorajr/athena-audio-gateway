"""Re-review minor: every reply frame says whether the TEST stand-in is the pilot (optional boolean `test_pilot`), so
the Go gateway can mark its own "say again" the same way. Sent only when the stand-in setting is configured: with
ATHENA_TEST_OWNSHIP unset the frames are exactly what they were, so an old Go gateway still accepts them."""
import unittest

from integrations.hermes_radio.contract import ContractError, encode_response, response
from integrations.hermes_radio.tests import eye_fixture as fx
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say
from integrations.hermes_radio.tests.test_test_ownship import STAND_IN, ai_eye


class Req:
    transmission_id = "t1"


def frame(**extra):
    return response(Req, speech="Prometheus, Athena, copy.", state="ok", route="fixed", **extra)


class ContractTests(unittest.TestCase):
    def test_flag_is_optional_boolean_on_a_deliverable_frame(self):
        self.assertNotIn("test_pilot", frame())
        self.assertIs(frame(test_pilot=True)["test_pilot"], True)
        self.assertIs(frame(test_pilot=False)["test_pilot"], False)
        for value in (frame(test_pilot=True), frame(test_pilot=False)):
            encode_response(value)

    def test_bad_flag_is_refused(self):
        for bad in ("yes", 1, None):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                encode_response(dict(frame(), test_pilot=bad))
        with self.assertRaises(ContractError):
            encode_response(dict(response(Req, reason="unaddressed"), test_pilot=True))


class ControllerFrameTests(unittest.TestCase):
    def frame_for(self, eye, ask="Athena, Prometheus, radio check"):
        c, _ = controller(None, Stub(), eye=eye)
        return say(c, ask)[0][0]

    def test_stand_in_is_the_pilot_true(self):
        eye = ai_eye(STAND_IN)
        eye.tick()
        out = self.frame_for(eye)
        self.assertIs(out["test_pilot"], True)
        self.assertIn("Test pilot.", out["speech"])

    def test_stand_in_configured_but_a_real_pilot_flies_false(self):
        eye = ai_eye(STAND_IN, fx.UNITS)
        eye.tick()
        out = self.frame_for(eye)
        self.assertIs(out["test_pilot"], False)
        self.assertNotIn("Test pilot.", out["speech"])

    def test_setting_unset_the_frame_is_unchanged(self):
        eye = ai_eye(None, fx.UNITS)
        eye.tick()
        self.assertNotIn("test_pilot", self.frame_for(eye))


if __name__ == "__main__":
    unittest.main()
