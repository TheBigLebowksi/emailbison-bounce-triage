"""End-to-end: the sample replay gives the documented verdicts, and the same files every time."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bounce_triage.actions import SAFE_AUTO
from bounce_triage.pipeline import triage

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


class SampleReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        offline = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "SLACK_WEBHOOK_URL")}
        with mock.patch.dict(os.environ, offline, clear=True):  # the committed out/ is the zero-key run
            cls.result, _ = triage(SAMPLES / "bounces.jsonl", SAMPLES, cls.out)
        cls.by_ws = {r.workspace: r for r in cls.result.results}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_kestrel_five_high_causes(self):
        kestrel = self.by_ws["Kestrel"]
        self.assertEqual((kestrel.kept, kestrel.total_sends), (903, 15691))
        real = [(v.cause, v.confidence) for v in kestrel.verdicts if v.cause != "noise"]
        self.assertEqual(real, [("bad_data", "High"), ("auth", "High"), ("throttling", "High"),
                                ("content", "High"), ("recipient_gateway", "High")])
        self.assertEqual(sorted(w.cause for w in kestrel.watch), ["domain_reputation", "sender_mailbox"])
        self.assertEqual(len(kestrel.unknown), 4)
        self.assertEqual(kestrel.health.band, "red")

    def test_hot_segments(self):
        hot = {v.cause: [h.label for h in v.hot] for v in self.by_ws["Kestrel"].verdicts}
        self.assertEqual(hot["bad_data"], ["apollo_oct_upload"])
        self.assertEqual(hot["auth"], ["kestrelreach-hq.com"])
        self.assertEqual(hot["recipient_gateway"], ["mimecast"])

    def test_juniper_healthy_with_one_outlier_inbox(self):
        juniper = self.by_ws["Juniper"]
        self.assertEqual([v for v in juniper.verdicts if v.cause != "noise"], [])
        self.assertAlmostEqual(juniper.bounce_rate, 61 / 5432)
        self.assertEqual(juniper.health.band, "green")
        self.assertEqual([w.key for w in juniper.watch if w.cause == "inbox_outlier"], ["lena@juniperhealth.io"])

    def test_greylisting_lands_as_noise(self):
        grey = [b for b in self.result.bounces if b.kept and b.decoded.codebook_id == "any-greylist"]
        self.assertEqual(len(grey), 5)
        self.assertTrue(all(b.category == "noise" for b in grey))

    def test_only_safe_steps_on_sure_causes_run_on_their_own(self):
        actions = [a for r in self.result.results for a in r.actions]
        self.assertTrue(actions)
        self.assertTrue(all(a.auto == (a.confidence == "High" and a.operation in SAFE_AUTO) for a in actions))
        self.assertFalse(any(a.auto for a in actions if a.operation in ("remove_sender_emails", "toggle_variant",
                                                                          "bulk_update_daily_limits")))

    def test_ambiguous_access_denied_never_blocked_on_its_own(self):
        ambiguous = {b.lead_email for b in self.result.bounces if b.kept and b.decoded.ambiguous}
        blocked = {i["email"] for r in self.result.results for a in r.actions
                   if a.operation == "create_blacklisted_email" for i in a.items or []}
        self.assertTrue(ambiguous)
        self.assertFalse(ambiguous & blocked)

    def test_slack_says_what_and_what_to_do_in_plain_words(self):
        slack = (self.out / "slack.md").read_text(encoding="utf-8")
        self.assertIn("Above the 5% danger line: fix the problems below", slack)
        self.assertIn("*1. Dead addresses in the list `apollo_oct_upload`*", slack)
        self.assertIn("because DKIM and DMARC are missing", slack)
        self.assertIn("They send up to 70 emails a day; the safe level is 50.", slack)
        self.assertIn("Its link domain trk.kestrel-links.io is on the Spamhaus blocklist", slack)
        self.assertIn("Nothing has been changed in EmailBison.", slack)
        # each problem says what was ruled out, from where the bounces spread instead of piling up
        self.assertIn("Not the inboxes or domains (spread over 12 inboxes on 3 domains).", slack)
        self.assertIn("Your other domains, `getkestrelreach.com` and `kestrelreach.co`, pass SPF, DKIM and DMARC", slack)
        self.assertIn("Not the whole account: the other 9 inboxes have only 6 of these bounces.", slack)
        self.assertIn("Google and Microsoft got 12,289 of the same emails and blocked none.", slack)
        for jargon in ("pts", "Pause the affected pool", "auto,", "awaiting approval"):
            self.assertNotIn(jargon, slack)

    def test_outputs_match_committed_samples(self):
        for name in ("report.md", "actions.json", "slack.md", "bounces_decoded.csv"):
            self.assertEqual((self.out / name).read_bytes(), (ROOT / "out" / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
