"""Validate the Apex replacement ledger without treating screenshots as parity proof."""
import json
import unittest
from pathlib import Path


class ApexCoverageMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = json.loads(
            (Path(__file__).parent / "docs" / "apex_feature_matrix.json").read_text("utf-8")
        )

    def test_all_32_source_bots_are_present_once(self):
        names = [entry["bot"] for entry in self.ledger["sources"]]
        self.assertEqual(len(names), 32)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {
            ".fmbot", "Appy", "Arcane", "BibleBot", "Confessions",
            "Conquest", "Daedalus", "DISBOARD", "Discodus",
            "DiscordServers", "Dyno", "EazyAutodelete",
            "GiveawayBot", "Jockie Music", "kesarAI", "Kuro’s Pet",
            "Make it a Quote", "ModMail", "Security", "Server Tag Manager",
            "Ticket Tool", "Tupperbox", "Vetra", "YAGPDB.xyz",
            "Discohook Utils", "Emoji Stealer", "Honeypot",
            "InviteLogger", "SnipeBot", "TTS Bot", "Xenon", "YouTube",
        })

    def test_every_source_has_explicit_open_work(self):
        allowed = {"partial", "missing", "needs_research", "external"}
        for source in self.ledger["sources"]:
            with self.subTest(source=source["bot"]):
                self.assertIn(source["status"], allowed)
                self.assertTrue(source["gaps"])
                self.assertTrue(source["area"])
                self.assertIsInstance(source["implemented"], list)
                for item in source["gaps"]:
                    self.assertGreater(len(item.strip()), 10)

    def test_external_discovery_audience_is_not_falsely_marked_replaced(self):
        for name in ("DISBOARD", "Discodus", "DiscordServers"):
            entry = next(x for x in self.ledger["sources"] if x["bot"] == name)
            self.assertEqual(entry["status"], "external")
            self.assertFalse(entry["implemented"])

    def test_representative_existing_modules_are_reused(self):
        expected = {
            "Dyno": "warnings and warning history",
            "Arcane": "XP accumulation and levels",
            "Xenon": "server structure blueprints",
            "Ticket Tool": "native private ticket opening/closing (development)",
        }
        for name, value in expected.items():
            entry = next(x for x in self.ledger["sources"] if x["bot"] == name)
            self.assertIn(value, entry["implemented"])

    def test_none_is_claimed_full_parity_before_live_verification(self):
        self.assertFalse(any(s["status"] == "complete" for s in self.ledger["sources"]))
        self.assertIn("NOT confirmed production feature parity", self.ledger["rules"][1])

    def test_status_counts_are_bounded(self):
        counts = {status: sum(s["status"] == status for s in self.ledger["sources"])
                  for status in ("partial", "missing", "needs_research", "external")}
        self.assertEqual(counts, {
            "partial": 9, "missing": 12, "needs_research": 8, "external": 3,
        })


if __name__ == "__main__":
    unittest.main()
