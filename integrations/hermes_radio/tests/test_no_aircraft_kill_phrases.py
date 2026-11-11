"""Re-review minor 1: a pilot with no aircraft (just crashed or ejected) still gets kill reports and kill questions
handled the normal way; only true range/bearing asks get the no-position sentence."""
import json
from pathlib import Path
import unittest
from integrations.hermes_radio.simple_controller import NO_POSITION
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, say

LIVE = json.loads((Path(__file__).parent / "fixtures" / "no_ownship_live_20261111.json").read_text())["picture"]
SAID = "Prometheus, Athena, " + NO_POSITION
NORMAL = (  # reports and kill questions: never the no-position sentence
    "SA-2 is down", "tally SA-2", "shack on the SA-6", "SA-6 down", "SA-2 neutralized", "SA-2 suppressed",
    "what's my mission status threats", "splash one", "target destroyed", "good hit on the radar",
    "shack, SA-11 is down", "tally two SA-3", "SA-8 neutralised", "SA-2 site suppressed", "splash two tanks",
    "good hit on the SA-6", "I got the SA-10, it's down", "confirm SA-2 destroyed", "SA-6 shack",
    "threats down, SA-2 is dead", "tally", "confirm kills", "any hits", "what remains", "how many left",
    "did I kill that tank", "I got the SA-2", "SA-2 is burning", "SA-2 is out", "SA-2 is toast",
    "the radar is off the air", "scratch one SA-6", "he's gone", "SA-3 off the air", "I got the SA-6, he's burning",
    "SA-11 is toast", "scratch two SA-2", "SA-10 is out", "got the SA-8", "good shack on the SA-3",
    "mission status", "what's my mission status", "kill status", "tally two tanks", "splash one SA-2",
    "SA-6 site is down", "radar is down, SA-2 is out")
POSITION = (  # range / bearing asks: still the no-position sentence
    "where is the SA-2", "nearest SAM", "picture", "any threats", "any nearby threats", "closest bandit",
    "am I clear", "vectors to the tanker", "what ground targets are near me", "where are the SA-6s",
    "bearing to the nearest SA-2", "do you have a tally on the nearest bandit", "any SAM threats",
    "nearest friendly airfield", "RTB", "launch launch", "bogey dope", "what's painting me",
    "SA-2 down, any other threats", "any threats down there", "SAM status ahead", "threat status",
    "what's the threat picture", "any other SAMs", "am I clear of the SA-6", "SA-2 down, any more threats",
    "SA-2 down, anything else", "SA-6 down, what else", "SA-6 down, am I good", "SA-2 is down, safe to continue",
    "any threats down range", "any threats down low", "threat status in the target area", "SAM status at the target",
    "threats status on the route", "SAM status, am I still being tracked", "RWR status", "SAM status",
    "SAM tally", "tally on any threats", "status of the SAMs ahead", "status of any SAMs I should worry about")


class NoAircraftKillPhrases(unittest.TestCase):
    def speech(self, ask):
        c, _ = controller(LIVE, Stub(answer="Prometheus, Athena, copy."))
        return say(c, "Athena, Prometheus, " + ask)[0][0]["speech"]

    def test_kill_reports_and_questions_are_not_blocked(self):
        for ask in NORMAL:
            with self.subTest(ask=ask):
                self.assertNotEqual(self.speech(ask), SAID)

    def test_range_and_bearing_asks_keep_the_no_position_reply(self):
        for ask in POSITION:
            with self.subTest(ask=ask):
                self.assertEqual(self.speech(ask), SAID)


if __name__ == "__main__":
    unittest.main()
