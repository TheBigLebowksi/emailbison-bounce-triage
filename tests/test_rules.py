"""Health band, inbox outliers, the repeat soft-bounce action, and causes found in an unexpected place."""
from __future__ import annotations

import unittest

from bounce_triage.actions import build_actions, repeat_soft_bounce_actions
from bounce_triage.config import load_config
from bounce_triage.diagnose import WorkspaceContext, check_verifier, diagnose_workspace, health_band, inbox_outliers
from bounce_triage.enrich import Resolver
from bounce_triage.models import Bounce, Decoded, Sender, SendCount

CONFIG = load_config()


def sender(age: int | None) -> dict[str, Sender]:
    return {"a@x.co": Sender("W", "a@x.co", 1, "smtp", 30, 30, 30, domain_age_days=age)}


def bounce(inbox: str, lead: str = "", enhanced: str = "") -> Bounce:
    return Bounce(event_index=0, sender_email=inbox, lead_email=lead, kept=True, decoded=Decoded(enhanced_code=enhanced))


class HealthBands(unittest.TestCase):
    def band(self, rate: float, age: int | None = None) -> str:
        return health_band(rate, sender(age), CONFIG.health).band

    def test_default_lines(self):
        self.assertEqual([self.band(r) for r in (0.019, 0.02, 0.05, 0.051)], ["green", "yellow", "yellow", "red"])

    def test_young_pool_uses_stricter_green_line(self):
        self.assertEqual(self.band(0.017, age=10), "yellow")
        self.assertEqual(self.band(0.017, age=45), "green")

    def test_red_line_does_not_move_with_age(self):
        self.assertEqual([self.band(0.051, age=a) for a in (10, 45, 400)], ["red", "red", "red"])


class InboxOutliers(unittest.TestCase):
    def ctx(self) -> WorkspaceContext:
        sends = [SendCount("W", inbox, "x.co", "1", "C", "1", "A", "s", "google", 1000) for inbox in ("a@x.co", "b@x.co",
                                                                                                    "c@x.co", "d@x.co")]
        return WorkspaceContext(CONFIG.thresholds, sends, {}, Resolver("mock"), {}, {}, {}, {})

    def test_flags_inbox_at_3x_workspace_rate(self):
        kept = [bounce("a@x.co")] * 12 + [bounce("b@x.co")] * 2 + [bounce("c@x.co")] * 2
        notes = inbox_outliers(kept, self.ctx())
        self.assertEqual([n.key for n in notes], ["a@x.co"])

    def test_needs_minimum_bounces(self):
        self.assertEqual(inbox_outliers([bounce("a@x.co")] * 9, self.ctx()), [])


class RepeatSoftBounces(unittest.TestCase):
    def test_mailbox_full_twice_is_suppressed(self):
        kept = [bounce("a@x.co", "full@y.com", "5.2.2"), bounce("a@x.co", "full@y.com", "4.2.2"),
                bounce("a@x.co", "once@y.com", "5.2.2"), bounce("a@x.co", "gone@y.com", "5.1.1"),
                bounce("a@x.co", "gone@y.com", "5.1.1")]
        acts = repeat_soft_bounce_actions("W", kept)
        self.assertEqual([a.items for a in acts], [[{"email": "full@y.com"}]])
        self.assertFalse(acts[0].auto)

    def test_message_too_large_is_not_a_full_mailbox(self):
        kept = [bounce("a@x.co", "big@y.com", "5.2.3"), bounce("a@x.co", "big@y.com", "5.2.3")]
        self.assertEqual(repeat_soft_bounce_actions("W", kept), [])


class VerifierEvidence(unittest.TestCase):
    def test_missing_verifier_result_is_not_evidence(self):
        ctx = WorkspaceContext(CONFIG.thresholds, [], {}, Resolver("mock"), {}, {}, {}, {})
        agrees, lines = check_verifier(ctx, [], [bounce("a@x.co", f"l{i}@y.com", "5.1.1") for i in range(5)])
        self.assertFalse(agrees)
        self.assertEqual(lines, ["No verifier results on these leads."])


class UnexpectedPlace(unittest.TestCase):
    """Dead addresses that pile up on one inbox instead of one list: named there, but only fairly sure."""

    INBOXES, LISTS = ("a@x.co", "b@x.co", "c@x.co", "d@x.co"), ("s1", "s2", "s3")

    def run_case(self, on_a: int) -> tuple[list, WorkspaceContext]:
        sends = [SendCount("W", i, "x.co", "1", "C", "1", "A", src, "google", 400)
                 for i in self.INBOXES for src in self.LISTS]
        ctx = WorkspaceContext(CONFIG.thresholds, sends, {}, Resolver("mock"), {}, {}, {}, {})
        dead = Decoded(enhanced_code="5.1.1", match_type="code", category="bad_data")
        kept = [Bounce(event_index=0, sender_email=i, list_source=self.LISTS[k % 3], recipient_provider="google",
                       campaign_id="1", step="1", variant="A", verifier_result="catch_all", kept=True, decoded=dead)
                for i, n in zip(self.INBOXES, (on_a, 2, 2, 2)) for k in range(n)]
        verdicts, _, _ = diagnose_workspace("W", kept, ctx, CONFIG)
        return [v for v in verdicts if v.cause == "bad_data"], ctx

    def test_named_on_the_inbox_capped_at_medium_with_no_list_action(self):
        (v,), ctx = self.run_case(on_a=45)
        self.assertEqual(([h.key for h in v.hot], v.unexpected, v.confidence), (["a@x.co"], True, "Medium"))
        kinds = [a.operation for a in build_actions(v, ctx)]
        self.assertIn("manual", kinds)  # "check why before acting on that place"
        self.assertNotIn("Stop uploads from list source", " ".join(a.why for a in build_actions(v, ctx)))

    def test_too_few_bounces_is_not_named(self):
        verdicts, _ = self.run_case(on_a=15)  # under the 20-bounce bar for a place outside the usual one
        self.assertEqual([v.hot for v in verdicts], [])


if __name__ == "__main__":
    unittest.main()
