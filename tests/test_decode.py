"""Decoder, filter and payload tests on real-format bounce texts.  Run: python -m unittest"""
from __future__ import annotations

import unittest
from pathlib import Path

from bounce_triage.config import load_config
from bounce_triage.decode import decode
from bounce_triage.enrich import Resolver, check_sending_domain, dbl_result
from bounce_triage.ingest import filter_bounce, normalise, read_events
from bounce_triage.models import Bounce, Decoded
from bounce_triage.pipeline import process_events

FIX = Path(__file__).parent / "fixtures"
CODEBOOK = load_config().codebook


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def rec(text: str = "", raw: str = "", html: str = "", subject: str = "Delivery Status Notification (Failure)",
        sender: str = "mailer-daemon@googlemail.com", rtype: str = "Bounced", automated: bool = False) -> Bounce:
    return Bounce(event_index=0, reply_type=rtype, folder=rtype, automated_reply=automated, subject=subject,
                  text_body=text, html_body=html, raw_body=raw, from_email=sender)


class DecodeRealFormats(unittest.TestCase):
    def check(self, b: Bounce, enhanced: str, provider: str, category: str, generator: str | None = None,
              hint: str | None = None) -> Decoded:
        d = decode(b, CODEBOOK, provider_hint=hint or "unknown")
        self.assertEqual(d.enhanced_code, enhanced)
        if not hint:
            self.assertEqual(d.rejecting_provider, provider)
        self.assertEqual(d.category, category, d)
        if generator:
            self.assertEqual(d.generator, generator)
        return d

    def test_gmail_multiline_511(self):
        d = self.check(rec(fixture("gmail_550_5_1_1.txt")), "5.1.1", "google", "bad_data", "google")
        self.assertEqual(d.basic_code, "550")
        self.assertEqual(d.codebook_id, "g-511")

    def test_gmail_quoting_outlook_515(self):
        self.check(rec(fixture("gmail_remote_5_7_515.txt")), "5.7.515", "microsoft", "auth", "google")

    def test_yahoo_tss04_tag(self):
        d = self.check(rec(fixture("gmail_yahoo_tss04.txt")), "4.7.0", "yahoo", "throttling")
        self.assertEqual(d.provider_tags, "TSS04")
        self.assertEqual(d.bounce_class, "soft")

    def test_exchange_5110(self):
        self.check(rec(fixture("exchange_5_1_10.txt"), subject="Undeliverable: hi", sender="postmaster@x.com"),
                   "5.1.10", "microsoft", "bad_data", "microsoft")

    def test_exchange_quoting_mimecast(self):
        d = self.check(rec(fixture("exchange_mimecast.txt"), subject="Undeliverable: hi"),
                       "5.0.0", "mimecast", "recipient_gateway", "microsoft")
        self.assertEqual(d.basic_code, "550")

    def test_exchange_quote_with_apostrophes(self):
        d = self.check(rec(fixture("exchange_gmail_apostrophe.txt"), subject="Undeliverable: hi"),
                       "5.7.40", "google", "auth")
        self.assertIn("DMARC policy", d.diagnostic)

    def test_postfix_user_unknown(self):
        self.check(rec(fixture("postfix_user_unknown.txt"), subject="Undelivered Mail Returned to Sender"),
                   "5.1.1", "other", "bad_data", "postfix", hint="other")

    def test_rfc3464_dsn_preferred(self):
        d = self.check(rec(text="ignored", raw=fixture("dsn_multipart.eml")), "5.7.1", "google", "content")
        self.assertEqual(d.decode_source, "dsn")
        self.assertEqual(d.codebook_id, "g-571-unsolicited")

    def test_html_only(self):
        html = "<html><body><p>" + fixture("gmail_550_5_1_1.txt").replace("\n", "<br>") + "</p></body></html>"
        self.check(rec(html=html), "5.1.1", "google", "bad_data")

    def test_unknown_kept_with_raw_text(self):
        d = decode(rec("The response from the remote server was:\n\n554 Transaction failed\n"), CODEBOOK)
        self.assertEqual(d.category, "unknown")
        self.assertIn("Transaction failed", d.diagnostic)

    def test_mx_hint_when_text_has_no_marker(self):
        b = rec("The response from the remote server was:\n\n554 5.7.1 <a@b.com>: Blocked by policy\n")
        self.assertEqual(decode(b, CODEBOOK, provider_hint="proofpoint").category, "recipient_gateway")


def remote(response: str) -> Bounce:
    return rec(f"** Message not delivered **\n\nThe response from the remote server was:\n\n{response}\n")


class CodeBookFixes(unittest.TestCase):
    def test_greylisting_is_noise_not_throttling(self):
        for text in ("451 4.7.1 <a@b.io>: Recipient address rejected: Greylisted, please try again later",
                     "451 4.7.1 Greylisted, please try again later"):
            d = decode(remote(text), CODEBOOK, provider_hint="other")
            self.assertEqual((d.category, d.match_type, d.codebook_id), ("noise", "code", "any-greylist"), text)

    def test_greylist_keyword_beats_throttling_keyword(self):
        kw = [k["category"] for k in CODEBOOK["keywords"] if "greylist" in k["pattern"] or "try again later" in k["pattern"]]
        self.assertEqual(kw[0], "noise")

    def test_yahoo_511_is_a_specific_code_match(self):
        for text, hint in (("554 5.1.1 <jane@yahoo.com>: Recipient address rejected: not a valid recipient", "unknown"),
                           ("554 5.1.1 <jane@brand.com>: not a valid recipient", "yahoo")):
            d = decode(remote(text), CODEBOOK, provider_hint=hint)
            self.assertEqual((d.category, d.match_type, d.codebook_id), ("bad_data", "code", "y-511"), text)

    def test_blocklist_wording_is_domain_reputation(self):
        for text in ("550 5.7.1 Service unavailable; Client host [203.0.113.9] blocked using zen.spamhaus.org",
                     "554 5.7.1 Message rejected because 203.0.113.9 is listed on an RBL",
                     "550 5.7.1 Client host [203.0.113.9] blocked using bl.spamcop.net"):
            d = decode(remote(text), CODEBOOK, provider_hint="other")
            self.assertEqual((d.category, d.codebook_id), ("domain_reputation", "any-spamhaus"), text)

    def test_ms_541_access_denied_stays_ambiguous(self):
        d = decode(remote("550 5.4.1 Recipient address rejected: Access denied. AS(201806281)"), CODEBOOK)
        self.assertTrue(d.ambiguous)
        self.assertEqual(d.category, "bad_data")

    def test_gmail_continuation_lines_do_not_break_text_match(self):
        d = decode(rec("The response was:\n\n421-4.7.0 This message is suspicious due to the nature of the content "
                       "or the\n421-4.7.0 links within. To best protect our users from spam, the message has been "
                       "blocked. x1 - gsmtp\n"), CODEBOOK)
        self.assertEqual((d.category, d.codebook_id), ("content", "g-470-content"))


class FilterRealBounces(unittest.TestCase):
    def test_gmail_delay_dropped(self):
        self.assertEqual(filter_bounce(rec(fixture("gmail_delay.txt"))), (False, "delay_notice"))

    def test_dsn_action_delayed_dropped(self):
        self.assertEqual(filter_bounce(rec(raw=fixture("dsn_delayed.eml"))), (False, "delay_notice"))

    def test_out_of_office_misfiled_as_bounce_dropped(self):
        b = rec(fixture("out_of_office.txt"), subject="Automatic reply: hi", sender="jane@x.com", automated=True)
        self.assertEqual(filter_bounce(b), (False, "auto_reply"))

    def test_bounced_type_in_another_folder_dropped(self):
        b = rec("550 5.1.1 The email account that you tried to reach does not exist.")
        b.folder = "Inbox"
        self.assertFalse(filter_bounce(b)[0])

    def test_non_bounce_type_dropped(self):
        self.assertEqual(filter_bounce(rec("hello", rtype="Interested")), (False, "not_bounced_type"))

    def test_real_bounce_kept_even_if_flagged_automated(self):
        self.assertEqual(filter_bounce(rec(fixture("gmail_550_5_1_1.txt"), automated=True)), (True, ""))

    def test_empty_test_webhook_dropped(self):
        b = normalise({"event": {"type": "EMAIL_BOUNCED", "workspace_name": "Kestrel"}, "data": {}})
        self.assertEqual(filter_bounce(b), (False, "empty_event"))

    def test_flat_payload_with_only_the_event_type_kept(self):
        b = normalise({"type": "EMAIL_BOUNCED", "lead_email": "a@b.com", "text_body": fixture("gmail_550_5_1_1.txt")})
        self.assertEqual(filter_bounce(b), (True, ""))


NESTED = {"event": {"type": "EMAIL_BOUNCED", "workspace_id": 3, "workspace_name": "Kestrel"},
          "data": {"reply": {"id": 1, "type": "Bounced", "folder": "Bounced", "text_body": "x"},
                   "lead": {"id": 9, "email": "A@B.com", "custom_variables": [{"name": "list_source", "value": "s1"}]},
                   "campaign": {"id": 201, "name": "C"},
                   "scheduled_email": {"id": 77, "sequence_step_order": 1, "sequence_step_variant": "B"},
                   "sender_email": {"id": 5, "email": "me@d.co", "account_type": "smtp"}}}
CAMEL = {"event": {"type": "EMAIL_BOUNCED", "workspaceName": "Kestrel"},
         "data": {"reply": {"type": "Bounced", "textBody": "x"},
                  "lead": {"id": 9, "email": "a@b.com", "customVariables": [{"name": "list_source", "value": "s1"}]},
                  "campaign": {"id": 201}, "scheduledEmail": {"id": 77, "sequenceStepOrder": 1,
                                                                "sequenceStepVariant": "B"},
                  "senderEmail": {"id": 5, "email": "me@d.co"}}}
FLAT = {"event": "EMAIL_BOUNCED", "workspace_name": "Kestrel", "type": "Bounced", "text_body": "x",
        "lead_email": "a@b.com", "list_source": "s1", "campaign_id": 201, "scheduled_email_id": 77,
        "sequence_step_order": 1, "sequence_step_variant": "B", "sender_email": "me@d.co"}


class PayloadShapes(unittest.TestCase):
    def test_three_shapes_normalise_the_same(self):
        keys = ("workspace", "lead_email", "list_source", "campaign_id", "scheduled_email_id", "step", "variant",
                "sender_email", "sender_domain", "dedupe_key")
        a, b, c = (normalise(p) for p in (NESTED, CAMEL, FLAT))
        for k in keys:
            self.assertEqual(getattr(a, k), getattr(b, k), k)
            self.assertEqual(getattr(a, k), getattr(c, k), k)

    def test_retry_deduped_but_delay_does_not_hide_final_bounce(self):
        delay = {**FLAT, "text_body": fixture("gmail_delay.txt")}
        final = {**FLAT, "text_body": fixture("gmail_550_5_1_1.txt")}
        rows = process_events([delay, final, final], CODEBOOK, Resolver("mock"))
        self.assertEqual([b.drop_reason for b in rows], ["delay_notice", "", "duplicate_webhook"])

    def test_odd_custom_variables_keep_the_bounce(self):
        b = normalise({**NESTED, "data": {**NESTED["data"], "lead": {"id": 9, "email": "a@b.com", "custom_variables": 5}}})
        self.assertEqual((b.lead_email, b.list_source), ("a@b.com", "unknown"))

    def test_state_file_starting_with_a_list_event_still_reads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text('[[1]]\n{"event": "EMAIL_BOUNCED"}\n', encoding="utf-8")
            self.assertEqual(read_events(path), ([[[1]], {"event": "EMAIL_BOUNCED"}], []))

    def test_malformed_payload_kept_as_error_row(self):
        rows = process_events(["not an object", FLAT], CODEBOOK, Resolver("mock"))
        self.assertTrue(rows[0].drop_reason.startswith("malformed:"))
        self.assertTrue(rows[1].kept)


class DnsNotChecked(unittest.TestCase):
    def test_domain_missing_from_mock_is_not_checked_not_failing(self):
        info = check_sending_domain("nowhere.io", Resolver("mock", {"TXT": {"known.io": ["v=spf1 -all"]}}))
        self.assertEqual((info.checked, info.problems, info.auth_ok), (False, (), False))


class SpamhausErrors(unittest.TestCase):
    def test_public_resolver_answer_is_not_a_listing(self):
        self.assertIsNone(dbl_result(["127.255.255.254"], "live:public-mirror").listed)

    def test_real_listing(self):
        self.assertTrue(dbl_result(["127.0.1.2"], "live").listed)


if __name__ == "__main__":
    unittest.main()
