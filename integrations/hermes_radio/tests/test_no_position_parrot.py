"""r12.3 live miss 2026-11-11 11:19: with a pilot position (the TEST stand-in) the model answered 'No position call.' to
'nearest friendly airfield' and 'what ground targets are near me': it parroted the picture key name the prompt
told it to say. Prompt grounding remains; model replies have no semantic filter."""
import json
from pathlib import Path
import unittest
from integrations.hermes_radio.simple_controller import PROMPT, clean
from integrations.hermes_radio.tests.test_p1_radio import Stub, controller, replies, say
LIVE = json.loads((Path(__file__).parent / "fixtures" / "no_ownship_live_20261111.json").read_text())["picture"]
OWN = dict(LIVE, ownship={"type": "FA-18C_hornet", "coalition": "blue", "alt_ft": 144, "speed_kt": 0}, test_ownship="TEST",
           ground_targets_within_20nm=[], ground_empty_call="No ground targets within twenty miles.",
           airbases={"count": 1, "nearest": [], "nearest_friendly_note": "no friendly airfield known; do not offer one"})
OWN.pop("no_position_call", None)
PARROTS = ("Prometheus, Athena, No position call.", "Prometheus, Athena, no_position_call",
           "Prometheus, Athena, ground_empty_call.", "Prometheus, Athena, nearest_friendly_note says none.",
           "Prometheus, Athena, missile_scan off.", "Prometheus, Athena, test_ownship.")
ASKS = ("Athena, Prometheus, nearest friendly airfield", "Athena, Prometheus, what ground targets are near me")
PROMPT_8BC76BF = 10208  # characters of PROMPT in r12.2; it must not grow
class ParrotTests(unittest.TestCase):
    def test_prompt_names_no_picture_key_and_did_not_grow(self):
        for name in ("no_position_call", "ground_empty_call", "nearest_friendly_note", "test_ownship"):
            self.assertNotIn(name, PROMPT)
        self.assertLessEqual(len(PROMPT), PROMPT_8BC76BF)
    def test_ownship_present_model_keys_are_not_filtered(self):
        for said in PARROTS:
            for ask in ASKS:
                with self.subTest(said=said, ask=ask):
                    c, _ = controller(OWN, Stub(answer=said))
                    frames, _ = say(c, ask)
                    spoken = frames[0]["speech"]
                    self.assertEqual(spoken.replace("Test pilot. ", ""), clean(said))
                    self.assertEqual(replies(c)[0]["route"], "llm")
    def test_ownship_null_does_not_filter_model_sentence(self):
        c, _ = controller(LIVE, Stub(answer=PARROTS[0]))
        frames, _ = say(c, "Athena, Prometheus, how many tanks are left")
        self.assertEqual(frames[0]["speech"], PARROTS[0])
    def test_normal_answers_and_aircraft_names_are_untouched(self):
        for said in ("Prometheus, Athena, no ground targets within twenty miles.",
                     "Prometheus, Athena, one FA-18C_hornet on the runway.", "Prometheus, Athena, no position yet from the FA-18C."):
            with self.subTest(said=said):
                c, _ = controller(OWN, Stub(answer=said))
                frames, _ = say(c, ASKS[1])
                self.assertIn(said[len("Prometheus, Athena, "):], frames[0]["speech"])
