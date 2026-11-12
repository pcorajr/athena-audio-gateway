"""Offline checks for the model-accuracy study tool (no model calls)."""
import os
import unittest

from integrations.hermes_radio.tools import model_eval as me

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "..", "docs", "model-study", "eval-data.json.gz")


class NumberWords(unittest.TestCase):
    def test_spoken_numbers(self):
        self.assertEqual(me.words_to_numbers("bearing three four eight, seven miles"), [348, 7])
        self.assertEqual(me.words_to_numbers("three thousand six hundred forty pounds"), [3640])
        self.assertEqual(me.words_to_numbers("thirty nine and forty six miles"), [39, 46])
        self.assertEqual(me.words_to_numbers("one hundred and five"), [105])


@unittest.skipUnless(os.path.exists(DATA), "eval data not present")
class RecordedFlight(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = {i["id"]: i for i in me.load_set(DATA)[0]}

    def test_recorded_flight_failures_are_scored_wrong(self):
        for key in ("20261111:1663", "20261111:1689", "20261111:1858", "20261111:1866", "20261111:1890"):
            item = self.items[key]
            verdict, reasons, _ = me.score_reply(item, item["recorded_reply"])
            self.assertEqual(verdict, "wrong", (key, reasons))

    def test_reply_check_rejects_false_unknown_and_clearance(self):
        for key in ("20261111:1663", "20261111:1689", "20261111:1866", "20261111:1890"):
            item = self.items[key]
            self.assertIsNotNone(me.reply_check(item["recorded_reply"], item["picture"], item["ask"]), key)

    def test_reply_check_passes_a_right_answer(self):
        item = self.items["20261111:1874"]  # Creech, bearing three four eight, seven miles
        self.assertIsNone(me.reply_check(item["recorded_reply"], item["picture"], item["ask"]))
        self.assertEqual(me.score_reply(item, item["recorded_reply"])[0], "right")

    def test_facts_and_slim_keep_the_answer(self):
        item = self.items["20261111:1689"]
        self.assertIn("Fuel: 3,640 pounds.", me.fact_lines(item["picture"], item["ask"]))
        slim = me.slim_picture(item["picture"], item["ask"])
        self.assertEqual(slim["ownship"]["fuel"]["total_lb"], 3640)
        self.assertNotIn("airbases", slim)
        base = self.items["20261111:1866"]
        self.assertTrue(any("Creech" in line for line in me.fact_lines(base["picture"], base["ask"])))


if __name__ == "__main__":
    unittest.main()
