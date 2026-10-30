"""Generate the sample inputs (seeded, so every run produces identical files).

    python samples/make_samples.py

Two clients:
  Kestrel - ~15,700 sends a month after a volume push, several causes at once.
  Juniper - healthy control client, ~5,400 sends a month.

Planted in Kestrel (the report should separate all of these):
  1. new list source `apollo_oct_upload` full of 5.1.1 / 5.1.10 (bad data)
  2. new domain kestrelreach-hq.com (M365) with no DMARC and no custom DKIM:
     Gmail 5.7.40 / 4.7.40 and Outlook.com 5.7.515 (auth)
  3. three inboxes pushed from 42/day to 70/day: Gmail 4.7.28, Yahoo TSS04,
     Microsoft 4.7.650 (throttling)
  4. Mimecast "Administrative prohibition" cluster (recipient gateway)
  5. step 1 variant B of the Ops campaign carries a tracked link on a
     DBL-listed domain: Gmail content blocks, Yahoo PH01, Mimecast content
     policy - across all sending domains (copy fingerprint)
  6. small stuff: 6 Gmail 5.4.5 on one inbox and 4 Microsoft 5.7.708
     (watch list), mailbox-full noise, 4 unclassifiable texts (unknown queue)
  7. inputs the filter must drop: auto-replies, delay notices, webhook retries
  8. apollo_oct_upload was verified 21 days before sending (stale verification)

Planted in Juniper (still healthy overall):
  - lena@juniperhealth.io goes bad: greylisted 451 4.7.1 (noise), sending-limit
    hits, Gmail rate limits and a few dead addresses. No single cause is big
    enough for a verdict, but the inbox bounces at >3x the workspace rate.
These are appended after the shuffle, so Kestrel's random stream is unchanged.

Formats are modelled on real NDRs: Gmail ("The response was:", "- gsmtp",
550-5.1.1 continuation lines), Exchange Online ("Remote Server returned '...'",
"Diagnostic information for administrators"), Postfix ("host X said: ...
(in reply to RCPT TO command)"), and RFC 3464 multipart/report raw bodies.
"""
import csv
import json
import os
import random
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
rng = random.Random(20261030)
INSTANCE = "https://send.example-agency.com"
START = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)
DAYS = 30

FIRST = ["alex", "jordan", "sam", "taylor", "casey", "morgan", "riley", "jamie", "drew", "quinn", "avery", "parker",
         "reese", "rowan", "sky", "devon", "emery", "harper", "logan", "marlow", "nico", "oakley", "peyton", "sage"]
LAST = ["nguyen", "patel", "garcia", "kim", "smith", "muller", "rossi", "silva", "cohen", "okafor", "lopez", "berg",
        "tanaka", "novak", "walsh", "khan", "dubois", "jensen", "costa", "ibrahim", "reyes", "fischer", "moreau"]
CO_A = ["bright", "north", "stone", "blue", "iron", "clear", "summit", "harbor", "pine", "orbit", "nova", "true",
        "swift", "keystone", "redwood", "silver", "granite", "maple", "copper", "lumen"]
CO_B = ["forge", "logistics", "analytics", "freight", "health", "labs", "systems", "works", "partners", "capital",
        "foods", "supply", "robotics", "metrics", "industrial", "dental", "retail", "energy", "cargo", "clinics"]
TLD = [".com", ".io", ".co", ".net"]

MX = {
    "google": ["1 aspmx.l.google.com.", "5 alt1.aspmx.l.google.com."],
    "microsoft": ["0 {slug}.mail.protection.outlook.com."],
    "mimecast": ["10 us-smtp-inbound-1.mimecast.com.", "10 us-smtp-inbound-2.mimecast.com."],
    "proofpoint": ["10 mx0a-00{n}.pphosted.com.", "10 mx0b-00{n}.pphosted.com."],
    "barracuda": ["10 d{n}.ess.barracudanetworks.com."],
    "other": ["10 mail.{domain}."],
}
CONSUMER_MX = {
    "gmail.com": ["5 gmail-smtp-in.l.google.com."],
    "outlook.com": ["5 outlook-com.olc.protection.outlook.com."],
    "hotmail.com": ["2 hotmail-com.olc.protection.outlook.com."],
    "yahoo.com": ["1 mta5.am0.yahoodns.net.", "1 mta6.am0.yahoodns.net."],
    "aol.com": ["1 mx-aol.mail.gm0.yahoodns.net."],
}

# ---------------------------------------------------------------- clients --

CLIENTS = {
    "Kestrel": {
        "workspace_id": 3,
        "domains": {
            "kestrelreach.co": ("google_workspace_oauth", ["sam", "maya", "leo", "nina"]),
            "getkestrelreach.com": ("google_workspace_oauth", ["ari", "jo", "theo", "ivy"]),
            "kestrelreach-hq.com": ("microsoft_oauth", ["dan", "eve", "kai", "zoe"]),
        },
        "pushed": {"maya@kestrelreach.co", "leo@kestrelreach.co", "jo@getkestrelreach.com"},
        "new_domain": "kestrelreach-hq.com",
        "campaigns": [
            {"id": 201, "name": "Kestrel - Ops leaders Q4", "variants": {1: ["A", "B"], 2: ["A"], 3: ["A"]}},
            {"id": 202, "name": "Kestrel - Finance VPs Q4", "variants": {1: ["A", "B"], 2: ["A"], 3: ["A"]}},
        ],
        "sources": {"clay_enriched": 0.42, "apollo_oct_upload": 0.25, "event_attendees_2025": 0.33},
        "bad_source": "apollo_oct_upload",
        "fingerprint": (201, 1, "B"),
    },
    "Juniper": {
        "workspace_id": 7,
        "domains": {
            "juniperhealth.io": ("google_workspace_oauth", ["mia", "owen", "lena"]),
            "juniper-mail.net": ("smtp", ["hugo", "iris", "pia"]),
        },
        "pushed": set(), "new_domain": None,
        "campaigns": [{"id": 301, "name": "Juniper - Clinic owners", "variants": {1: ["A", "B"], 2: ["A"]}}],
        "sources": {"clay_enriched": 0.6, "event_attendees_2025": 0.4},
        "bad_source": None, "fingerprint": None,
    },
}
DOMAIN_AGE_DAYS = {"kestrelreach.co": 240, "getkestrelreach.com": 180, "kestrelreach-hq.com": 19,
                   "juniperhealth.io": 900, "juniper-mail.net": 120}
STALE_SOURCE, STALE_VERIFIED_AT = "apollo_oct_upload", START - timedelta(days=21)
PROVIDER_MIX = [("google", 0.40), ("gmail.com", 0.04), ("microsoft", 0.27), ("outlook.com", 0.04),
                ("hotmail.com", 0.03), ("yahoo.com", 0.04), ("aol.com", 0.02), ("mimecast", 0.07),
                ("proofpoint", 0.05), ("barracuda", 0.01), ("other", 0.03)]

dns_mx, company_domains = {}, {}
for d, mx in CONSUMER_MX.items():
    dns_mx[d] = mx


def provider_of(domain):
    mx = " ".join(dns_mx[domain])
    for name, marker in [("google", "google.com"), ("microsoft", "outlook.com"), ("yahoo", "yahoodns"),
                         ("mimecast", "mimecast"), ("proofpoint", "pphosted"), ("barracuda", "barracuda")]:
        if marker in mx:
            return name
    return "other"


def company_domain(kind):
    pool = company_domains.setdefault(kind, [])
    if len(pool) < 40 or rng.random() < 0.15:
        while True:
            d = rng.choice(CO_A) + rng.choice(["", "-"]) + rng.choice(CO_B) + rng.choice(TLD)
            if d not in dns_mx:
                break
        n = rng.randint(100000, 999999)
        dns_mx[d] = [m.format(slug=d.replace(".", "-"), n=n, domain=d) for m in MX[kind]]
        pool.append(d)
        return d
    return rng.choice(pool)


def weighted(pairs):
    x, acc = rng.random(), 0.0
    for value, w in pairs:
        acc += w
        if x < acc:
            return value
    return pairs[-1][0]


lead_seq = [5000]


def make_lead(source, bad_source):
    kind = weighted(PROVIDER_MIX)
    domain = kind if "." in kind else company_domain(kind)
    lead_seq[0] += 1
    first, last = rng.choice(FIRST), rng.choice(LAST)
    email = f"{first}.{last}{rng.randint(1, 99) if '.' in kind else ''}@{domain}"
    if source == bad_source:
        verifier = weighted([("valid", 0.25), ("catch_all", 0.5), ("unknown", 0.25)])
    else:
        verifier = weighted([("valid", 0.92), ("catch_all", 0.08)])
    return {"id": lead_seq[0], "email": email, "domain": domain, "provider": provider_of(domain),
            "consumer": domain in CONSUMER_MX, "source": source, "verifier": verifier}


# ---------------------------------------------------------------- NDR text --

def gmail_ndr(lead, response, kind, remote):
    heading = {"notfound": "Address not found", "blocked": "Message blocked", "failed": "Message not delivered",
               "delay": "Delivery incomplete"}[kind]
    intro = {
        "notfound": f"Your message wasn't delivered to {lead} because the address couldn't be found, or is "
                    f"unable to receive mail.",
        "blocked": f"Your message to {lead} has been blocked. See technical details below for more information.",
        "failed": f"There was a problem delivering your message to {lead}. Gmail stopped trying after 2 days. "
                  f"See the technical details below.",
        "delay": f"There was a temporary problem delivering your message to {lead}. Gmail will retry for "
                 f"{rng.randint(30, 47)} more hours. You'll be notified if the delivery fails permanently.",
    }[kind]
    label = "The response from the remote server was:" if remote else "The response was:"
    return f"** {heading} **\n\n{intro}\n\n{label}\n\n{response}\n"


def gmail_response(code_text, gsmtp=True, multiline=False):
    """Gmail wraps long responses as 550-5.1.1 continuation lines."""
    basic, enhanced, text = code_text
    if multiline and len(text) > 70:
        cut = text.rfind(" ", 0, 70)
        body = f"{basic}-{enhanced} {text[:cut]}\n{basic}-{enhanced} {text[cut + 1:]}"
    else:
        body = f"{basic} {enhanced} {text}"
    if gsmtp:
        body += f" {uuid.UUID(int=rng.getrandbits(128)).hex[:20]}.{rng.randint(10, 99)} - gsmtp"
    return body


def exchange_ndr(lead, sender, response, reason, delay=False):
    host = f"{rng.choice(['BN8PR', 'SA1PR', 'DM6PR', 'CH3PR'])}{rng.randint(10, 19)}MB{rng.randint(1000, 9999)}" \
           f".namprd{rng.randint(10, 19)}.prod.outlook.com"
    if delay:
        return (f"Delivery has been delayed to these recipients or groups:\n\n{lead}\n\n"
                f"This message hasn't been delivered yet. Delivery will continue to be attempted.\n\n"
                f"Diagnostic information for administrators:\nGenerating server: {host}\n"
                f"Last error: {response}\n")
    return (f"Your message to {lead} couldn't be delivered.\n\n{reason}\n\n"
            f"{sender.split('@')[0]}    Office 365    {lead.split('@')[1]}\nAction Required    Recipient\n\n"
            f"How to Fix It\nSee the error details below.\n\n"
            f"Diagnostic information for administrators:\n\nGenerating server: {host}\n\n{lead}\n"
            f"Remote Server returned '{response}'\n\nOriginal message headers:\n\n"
            f"Received: from {host} by {host} with HTTPS\n")


def postfix_ndr(lead, mx_host, response, command="RCPT TO"):
    return ("This is the mail system at host mail.juniper-mail.net.\n\n"
            "I'm sorry to have to inform you that your message could not\nbe delivered to one or more recipients. "
            "It's attached below.\n\n"
            f"<{lead}>: host {mx_host}[203.0.113.{rng.randint(2, 250)}] said: {response} "
            f"(in reply to {command} command)\n")


def html_of(text):
    paras = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in text.split("\n\n"))
    return f"<html><head><style>p{{font-family:Arial}}</style></head><body>{paras}</body></html>"


def dsn_raw(sender, lead, human, status, diag, remote_mta, reporting, action="failed", from_hdr=None):
    b = uuid.UUID(int=rng.getrandbits(128)).hex[:24]
    from_hdr = from_hdr or "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    diag_lines = diag.replace("\n", "\n    ")
    return (f"From: {from_hdr}\nTo: {sender}\nSubject: Delivery Status Notification\nMIME-Version: 1.0\n"
            f"Content-Type: multipart/report; boundary=\"{b}\"; report-type=delivery-status\n\n"
            f"--{b}\nContent-Type: text/plain; charset=\"UTF-8\"\n\n{human}\n"
            f"--{b}\nContent-Type: message/delivery-status\n\n"
            f"Reporting-MTA: dns; {reporting}\n\n"
            f"Final-Recipient: rfc822; {lead}\nAction: {action}\nStatus: {status}\n"
            f"Remote-MTA: dns; {remote_mta}\nDiagnostic-Code: smtp; {diag_lines}\n\n--{b}--\n")


# Remote responses per planted cause: (basic, enhanced, text)
R = {
    "g511": ("550", "5.1.1", "The email account that you tried to reach does not exist. Please try double-checking "
             "the recipient's email address for typos or unnecessary spaces. For more information, go to "
             "https://support.google.com/mail/?p=NoSuchUser"),
    "g521": ("550", "5.2.1", "The email account that you tried to reach is inactive. For more information, go to "
             "https://support.google.com/mail/?p=DisabledUser"),
    "ms5110": ("550", "5.1.10", "RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup"),
    "ms541": ("550", "5.4.1", "Recipient address rejected: Access denied. For more information see "
              "https://aka.ms/EXOSmtpErrors [AM4PEPF00027A6C.eurprd04.prod.outlook.com 2026-10-12T10:11:12.000Z]"),
    "g5740": ("550", "5.7.40", "Your message was blocked because the sending domain doesn't have a DMARC record or "
              "the DMARC record doesn't specify a DMARC policy. Gmail requires all bulk email senders to add a "
              "DMARC record to their sending domain."),
    "g4740": ("421", "4.7.40", "Your email has been rate limited because the sending domain doesn't have a DMARC "
              "record, or the DMARC record doesn't specify a DMARC policy. Gmail requires all bulk email senders "
              "to add a DMARC record to their sending domain."),
    "ms515": ("550", "5.7.515", "Access denied, sending domain kestrelreach-hq.com does not meet the required "
              "authentication level."),
    "g4728": ("421", "4.7.28", "Gmail has detected an unusual rate of email originating from your IP address. To "
              "protect our users from spam, email sent from your IP address has been temporarily rate limited."),
    "ms4650": ("451", "4.7.650", "The mail server [40.107.{a}.{b}] has been temporarily rate limited due to IP "
               "reputation. For e-mail delivery information, see https://aka.ms/postmaster (S775)"),
    "g470content": ("421", "4.7.0", "This message is suspicious due to the nature of the content or the links "
                    "within. To best protect our users from spam, the message has been blocked."),
    "g4728url": ("421", "4.7.28", "Gmail has detected an unusual rate of unsolicited email containing one of your "
                 "URL domains. To protect our users from spam, email with the URL has been temporarily rate limited."),
    "g571spam": ("550", "5.7.1", "This message is likely unsolicited email. To reduce the amount of spam sent to "
                 "Gmail, this message has been blocked."),
    "g422": ("452", "4.2.2", "The recipient's inbox is out of storage space. Please direct the recipient to "
             "https://support.google.com/mail/?p=OverQuotaTemp"),
    "ms522": ("552", "5.2.2", "STOREDRV.Deliver; mailbox full"),
    "ms447": ("550", "4.4.7", "QUEUE.Expired; message expired"),
    "g545": ("550", "5.4.5", "Daily user sending limit exceeded. For more information on Gmail sending limits, go "
             "to https://support.google.com/a/answer/166852"),
    "ms708": ("550", "5.7.708", "Service unavailable. Access denied, traffic not accepted from this IP. For more "
              "information please go to http://go.microsoft.com/fwlink/?LinkId=526653 AS(843)"),
}
YAHOO_NOUSER = "554 delivery error: dd This user doesn't have a yahoo.com account ({lead}) [0] - mta1012.mail.gq1.yahoo.com"
YAHOO_TSS04 = ("421 4.7.0 [TSS04] Messages from 209.85.{a}.{b} temporarily deferred due to unexpected volume or user "
               "complaints - 4.16.55.1; see https://postmaster.yahooinc.com/error-codes")
YAHOO_PH01 = "554 Message not allowed - [PH01] Email not accepted for policy reasons. Please visit https://postmaster.yahooinc.com/error-codes"
YAHOO_579 = "554 5.7.9 Message not accepted for policy reasons.  See https://senders.yahooinc.com/error-codes"
MIMECAST_ADMIN = "550 Administrative prohibition - envelope blocked - https://community.mimecast.com/docs/DOC-1369#550 [{id}.us{n}]"
MIMECAST_CONTENT = "550 Message bounced due to Content Examination Policy - https://community.mimecast.com/docs/DOC-1369#550 [{id}.us{n}]"
MIMECAST_INVALID = "550 Invalid Recipient - https://community.mimecast.com/docs/DOC-1369#550 [{id}.us{n}]"
PROOFPOINT_POLICY = "554 5.7.1 <{lead}>: Blocked by policy"
POSTFIX_UNKNOWN = "550 5.1.1 <{lead}>: Recipient address rejected: User unknown in virtual mailbox table"
UNKNOWN_TEXTS = [
    "550 Message rejected by recipient MTA (R7)",
    "550 5.7.0 Rejected - see https://help.example-filter.net/r/77",
    "553 sorry, that domain isn't in my list of allowed rcpthosts (#5.7.1)",
    "554 Transaction failed",
]
OOO = [
    "Thank you for your email. I am currently out of the office until {d} with limited access to email.",
    "Automatic reply: I'm on annual leave and will respond when I return on {d}.",
    "I am away from the office. For urgent matters contact our main line. Kind regards.",
]


def fill(s, lead=""):
    return s.format(lead=lead, a=rng.randint(1, 250), b=rng.randint(1, 250), id=uuid.UUID(int=rng.getrandbits(128)).hex[:22],
                    n=rng.randint(1, 300), d="November 4")


# ------------------------------------------------------------ simulation --

def outcome(client, inbox, domain, camp_id, step, variant, lead):
    """Return (cause_key, response) for the first planted cause that fires, else None."""
    c = CLIENTS[client]
    prov = lead["provider"]
    r = rng.random

    # 1. bad data: first-touch only
    if step == 1:
        p = 0.165 if lead["source"] == c["bad_source"] else (0.007 if client == "Juniper" else 0.011)
        if r() < p:
            if prov == "google":
                return "bad", R["g511"] if r() < 0.9 else R["g521"]
            if prov == "microsoft":
                return "bad", R["ms5110"] if r() < 0.75 else R["ms541"]
            if prov == "yahoo":
                return "bad", YAHOO_NOUSER
            if prov == "mimecast":
                return "bad", MIMECAST_INVALID
            return "bad", POSTFIX_UNKNOWN
    # 2. auth: new domain without DMARC/DKIM
    if domain == c["new_domain"]:
        if prov == "google" and r() < 0.07:
            return "auth", R["g5740"] if r() < 0.75 else R["g4740"]
        if prov == "microsoft" and lead["consumer"] and r() < 0.25:
            return "auth", R["ms515"]
        if prov == "yahoo" and r() < 0.08:
            return "auth", YAHOO_579
        if prov == "microsoft" and not lead["consumer"] and r() < 0.0012:
            return "rep", R["ms708"]
    # 5. copy fingerprint on one variant, any domain
    if c["fingerprint"] and (camp_id, step, variant) == c["fingerprint"]:
        if prov == "google" and r() < 0.065:
            return "content", rng.choice([R["g470content"], R["g4728url"], R["g571spam"]])
        if prov == "yahoo" and r() < 0.2:
            return "content", YAHOO_PH01
        if prov == "mimecast" and r() < 0.15:
            return "content", MIMECAST_CONTENT
    elif prov == "google" and client == "Kestrel" and r() < 0.0008:
        return "content", R["g571spam"]
    # 3. throttling on pushed inboxes
    pushed = inbox in c["pushed"]
    if prov == "google" and r() < (0.026 if pushed else 0.0007):
        return "throttle", R["g4728"]
    if prov == "yahoo" and r() < (0.18 if pushed else 0.004):
        return "throttle", YAHOO_TSS04
    if prov == "microsoft" and r() < (0.01 if pushed else 0.0004):
        return "throttle", R["ms4650"]
    # 4. recipient gateway
    if client == "Kestrel" and prov == "mimecast" and r() < 0.045:
        return "gateway", MIMECAST_ADMIN
    if client == "Kestrel" and prov == "proofpoint" and r() < 0.015:
        return "gateway", PROOFPOINT_POLICY
    # 6. noise
    if r() < 0.0016:
        if prov == "google":
            return "noise", R["g422"]
        if prov == "microsoft":
            return "noise", rng.choice([R["ms522"], R["ms447"]])
    return None


def render(client, sender, account_type, lead, cause, resp):
    """Build (text_body, html_body, raw_body, subject, from) for one bounce in the sender's NDR format."""
    remote_prov = lead["provider"]
    if isinstance(resp, tuple):
        basic, enhanced, text = resp
        resp_text = f"{basic} {enhanced} {fill(text)}"
    else:
        basic = enhanced = None
        resp_text = fill(resp, lead["email"])
    mx_host = dns_mx[lead["domain"]][0].split()[-1].rstrip(".")
    raw = ""
    if account_type == "google_workspace_oauth":
        google_remote = remote_prov == "google"
        if google_remote and isinstance(resp, tuple):
            resp_text = gmail_response((basic, enhanced, fill(resp[2])), gsmtp=True, multiline=rng.random() < 0.4)
        kind = "notfound" if cause == "bad" else ("failed" if (basic or resp_text[:1]).startswith("4") else "blocked")
        text = gmail_ndr(lead["email"], resp_text, kind, remote=not google_remote)
        subject, frm = "Delivery Status Notification (Failure)", "mailer-daemon@googlemail.com"
        if rng.random() < 0.25:
            status = enhanced or ("5.0.0" if resp_text.startswith("5") else "4.0.0")
            raw = dsn_raw(sender, lead["email"], text, status, resp_text,
                          f"{mx_host}. (142.250.{rng.randint(1, 250)}.{rng.randint(1, 250)}, the server for the "
                          f"domain {lead['domain']}.)", "googlemail.com")
    elif account_type == "microsoft_oauth":
        if remote_prov == "google" and isinstance(resp, tuple):
            resp_text = gmail_response((basic, enhanced, fill(resp[2])), gsmtp=True)
        if remote_prov == "mimecast" and not isinstance(resp, tuple):
            resp_text = f"<{mx_host} #5.0.0 smtp; {resp_text}>"
        reason = (f"{lead['email'].split('@')[0]} wasn't found at {lead['domain']}." if cause == "bad" else
                  f"{lead['domain']} suspects your message is spam and rejected it." if cause == "content" else
                  "Your message wasn't delivered because the recipient's email provider rejected it.")
        text = exchange_ndr(lead["email"], sender, resp_text, reason)
        subject, frm = "Undeliverable: quick question", f"postmaster@{sender.split('@')[1].split('.')[0]}.onmicrosoft.com"
        if rng.random() < 0.25:
            status = enhanced or "5.0.0"
            raw = dsn_raw(sender, lead["email"], text, status, resp_text, mx_host,
                          "BN8PR12MB3456.namprd12.prod.outlook.com",
                          from_hdr=f"Microsoft Outlook <{frm}>")
    else:  # smtp -> Postfix-style
        text = postfix_ndr(lead["email"], mx_host, resp_text)
        subject, frm = "Undelivered Mail Returned to Sender", "MAILER-DAEMON@mail.juniper-mail.net"
    html = html_of(text)
    if rng.random() < 0.05:  # some integrations only pass the HTML
        text = ""
    return text, html, raw, subject, frm


def payload(client, ws_id, reply_id, se_id, lead, camp, step, variant, step_id, inbox, sender_id, account_type,
            sent_at, body, rtype="Bounced", folder="Bounced", automated=False):
    text, html, raw, subject, frm = body
    received = sent_at + timedelta(minutes=rng.randint(1, 600))
    verified_at = STALE_VERIFIED_AT if lead["source"] == STALE_SOURCE else sent_at - timedelta(days=3)
    return {
        "event": {"type": "EMAIL_BOUNCED", "name": "Email Bounced", "instance_url": INSTANCE,
                  "workspace_id": ws_id, "workspace_name": client},
        "data": {
            "reply": {"id": reply_id, "uuid": str(uuid.UUID(int=rng.getrandbits(128))), "email_subject": subject,
                      "automated_reply": automated, "html_body": html, "text_body": text, "raw_body": raw or None,
                      "headers": None, "from_email_address": frm, "from_name": "Mail Delivery Subsystem",
                      "type": rtype, "folder": folder, "date_received": received.isoformat(),
                      "raw_message_id": f"<{uuid.UUID(int=rng.getrandbits(128)).hex}@mail.gmail.com>"},
            "lead": {"id": lead["id"], "email": lead["email"], "status": "bounced" if rtype == "Bounced" else "active",
                     "custom_variables": [{"name": "list_source", "value": lead["source"]},
                                          {"name": "verifier_result", "value": lead["verifier"]},
                                          {"name": "verified_at", "value": verified_at.isoformat()}]},
            "campaign": {"id": camp["id"], "name": camp["name"]},
            "scheduled_email": {"id": se_id, "sequence_step_id": step_id, "sequence_step_order": step,
                                "sequence_step_variant": variant,
                                "raw_message_id": f"<{uuid.UUID(int=rng.getrandbits(128)).hex}@{inbox.split('@')[1]}>",
                                "sent_at": sent_at.isoformat()},
            "sender_email": {"id": sender_id, "email": inbox, "account_type": account_type},
        },
    }


def to_camel(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            parts = k.split("_")
            out[parts[0] + "".join(p.title() for p in parts[1:])] = to_camel(v)
        return out
    if isinstance(obj, list):
        return [to_camel(v) for v in obj]
    return obj


def flatten(p):
    d = p["data"]
    return {"event": "EMAIL_BOUNCED", "workspace_name": p["event"]["workspace_name"],
            "workspace_id": p["event"]["workspace_id"], "reply_id": d["reply"]["id"], "type": d["reply"]["type"],
            "folder": d["reply"]["folder"], "text_body": d["reply"]["text_body"], "html_body": d["reply"]["html_body"],
            "email_subject": d["reply"]["email_subject"], "from_email_address": d["reply"]["from_email_address"],
            "automated_reply": d["reply"]["automated_reply"], "date_received": d["reply"]["date_received"],
            "lead_id": d["lead"]["id"], "lead_email": d["lead"]["email"],
            "list_source": d["lead"]["custom_variables"][0]["value"],
            "verifier_result": d["lead"]["custom_variables"][1]["value"],
            "campaign_id": d["campaign"]["id"], "campaign_name": d["campaign"]["name"],
            "scheduled_email_id": d["scheduled_email"]["id"], "sequence_step_order": d["scheduled_email"]["sequence_step_order"],
            "sequence_step_variant": d["scheduled_email"]["sequence_step_variant"],
            "sequence_step_id": d["scheduled_email"]["sequence_step_id"],
            "sender_email": d["sender_email"]["email"], "sender_email_id": d["sender_email"]["id"],
            "account_type": d["sender_email"]["account_type"]}


GREYLIST = "451 4.7.1 <{lead}>: Recipient address rejected: Greylisted, please try again later"
OUTLIER_PLAN = [("greylist", 5), ("limit", 8), ("throttle", 9), ("bad", 10)]


def outlier_inbox(sends, senders, step_ids, se_id, reply_id):
    """Juniper's lena@juniperhealth.io: many small problems, no single verdict, >3x the workspace bounce rate."""
    client, c = "Juniper", CLIENTS["Juniper"]
    s = next(x for x in senders if x["sender_email"] == "lena@juniperhealth.io")
    inbox, atype, camp = s["sender_email"], s["account_type"], c["campaigns"][0]
    events = []
    for kind, count in OUTLIER_PLAN:
        for _ in range(count):
            lead = make_lead("clay_enriched", None)
            prov = "other" if kind == "greylist" else "google"
            lead = dict(lead, provider=prov, consumer=False, domain=company_domain(prov))
            lead["email"] = f"{rng.choice(FIRST)}.{rng.choice(LAST)}@{lead['domain']}"
            sends[(client, inbox, inbox.split("@")[1], camp["id"], camp["name"], 1, "A", "clay_enriched", prov)] += 1
            sent_at = START + timedelta(days=rng.randint(0, DAYS - 1), minutes=rng.randint(0, 540))
            se_id[0] += 1
            reply_id[0] += 1
            if kind == "limit":
                text = (f"** You have reached a limit for sending mail **\n\nYour message was not sent.\n\n"
                        f"The response was:\n\n{gmail_response(R['g545'], gsmtp=True)}\n")
                body = (text, html_of(text), "", "Delivery Status Notification (Failure)", "mailer-daemon@googlemail.com")
            else:
                cause, resp = {"greylist": ("noise", GREYLIST), "throttle": ("throttle", R["g4728"]),
                               "bad": ("bad", R["g511"])}[kind]
                body = render(client, inbox, atype, lead, cause, resp)
            events.append(payload(client, c["workspace_id"], reply_id[0], se_id[0], lead, camp, 1, "A",
                                  step_ids[(camp["id"], 1, "A")], inbox, s["sender_email_id"], atype, sent_at, body))
    return events


def main():
    events, sends, senders, campaigns_out = [], Counter(), [], []
    reply_id, se_id, sender_id, step_seq = [90000], [700000], [400], [8000]
    step_ids = {}
    for client, c in CLIENTS.items():
        for camp in c["campaigns"]:
            steps = []
            for step, variants in camp["variants"].items():
                for v in variants:
                    step_seq[0] += 1
                    step_ids[(camp["id"], step, v)] = step_seq[0]
                    link = ""
                    if c["fingerprint"] == (camp["id"], step, v):
                        link = "\n\nWorth 2 minutes? Here's the teardown: https://trk.kestrel-links.io/c/8f2a91?u={{lead_id}}"
                    elif step == 1 and client == "Kestrel":
                        link = "\n\nCase study if useful: kestrelreach.co/ops"
                    steps.append({"order": step, "variant": v, "sequence_step_id": step_seq[0],
                                  "subject": "quick question" if step == 1 else "re: quick question",
                                  "body": f"Hi {{FIRST_NAME}},\n\n{'Noticed' if v == 'A' else 'Saw'} your team is "
                                          f"hiring for ops roles.{link}\n\n{{SENDER_FIRST_NAME}}"})
            campaigns_out.append({"id": camp["id"], "name": camp["name"], "workspace": client, "steps": steps})

        inboxes = []
        for domain, (atype, names) in c["domains"].items():
            for n in names:
                sender_id[0] += 1
                inboxes.append((f"{n}@{domain}", domain, atype, sender_id[0]))
        for inbox, domain, atype, sid in inboxes:
            if inbox in c["pushed"]:
                per_day, prev, limit = 70, 42, 75
            elif domain == c["new_domain"]:
                per_day, prev, limit = 28, 0, 32
            else:
                per_day, prev, limit = (40, 42, 45) if client == "Kestrel" else (30, 30, 40)
            senders.append({"workspace": client, "sender_email": inbox, "sender_email_id": sid, "account_type": atype,
                            "daily_limit": limit, "avg_daily_sent_prev": prev, "avg_daily_sent_now": per_day,
                            "domain_age_days": DOMAIN_AGE_DAYS[domain]})

            for _ in range(per_day * DAYS):
                source = weighted(list(c["sources"].items()))
                if source == c["bad_source"]:
                    camp = c["campaigns"][1] if rng.random() < 0.8 else c["campaigns"][0]
                else:
                    camp = c["campaigns"][0] if rng.random() < 0.6 else c["campaigns"][-1]
                step = weighted([(1, 0.5), (2, 0.3), (3, 0.2)]) if 3 in camp["variants"] else weighted([(1, 0.6), (2, 0.4)])
                variant = rng.choice(camp["variants"][step])
                lead = make_lead(source, c["bad_source"])
                sends[(client, inbox, domain, camp["id"], camp["name"], step, variant, source, lead["provider"])] += 1
                res = outcome(client, inbox, domain, camp["id"], step, variant, lead)
                if not res:
                    continue
                cause, resp = res
                sent_at = START + timedelta(days=rng.randint(0, DAYS - 1), minutes=rng.randint(0, 540))
                se_id[0] += 1
                reply_id[0] += 1
                # Throttled sends often produce a delay notice before the final failure (same scheduled email).
                if cause == "throttle" and atype != "smtp" and rng.random() < 0.4:
                    if atype == "google_workspace_oauth":
                        rt = (gmail_response(resp, gsmtp=True) if isinstance(resp, tuple) else fill(resp))
                        dtext = gmail_ndr(lead["email"], rt, "delay", remote=lead["provider"] != "google")
                        body = (dtext, html_of(dtext), "", "Delivery Status Notification (Delay)", "mailer-daemon@googlemail.com")
                    else:
                        rt = f"{resp[0]} {resp[1]} {fill(resp[2])}" if isinstance(resp, tuple) else fill(resp)
                        dtext = exchange_ndr(lead["email"], inbox, rt, "", delay=True)
                        body = (dtext, html_of(dtext), "", "Delayed: quick question", "postmaster@outlook.com")
                    events.append(payload(client, c["workspace_id"], reply_id[0], se_id[0], lead, camp, step, variant,
                                          step_ids[(camp["id"], step, variant)], inbox, sid, atype, sent_at, body))
                    reply_id[0] += 1
                body = render(client, inbox, atype, lead, cause, resp)
                events.append(payload(client, c["workspace_id"], reply_id[0], se_id[0], lead, camp, step, variant,
                                      step_ids[(camp["id"], step, variant)], inbox, sid, atype, sent_at, body))

        # Planted extras for Kestrel: watch-list inbox, unknown texts, auto-replies.
        if client == "Kestrel":
            ivy = next(i for i in inboxes if i[0].startswith("ivy@"))
            for k in range(6 + 4 + 18 + 3):
                camp = c["campaigns"][0]
                lead = make_lead("clay_enriched", None)
                sent_at = START + timedelta(days=rng.randint(0, DAYS - 1), minutes=rng.randint(0, 540))
                se_id[0] += 1
                reply_id[0] += 1
                if k < 6:
                    inbox = ivy
                elif k >= 28:  # Microsoft 5.7.708 lands on the new M365 tenant's inboxes
                    inbox = rng.choice([i for i in inboxes if i[1] == c["new_domain"]])
                    lead = dict(lead, provider="microsoft", consumer=False, domain=company_domain("microsoft"))
                    lead["email"] = f"{rng.choice(FIRST)}.{rng.choice(LAST)}@{lead['domain']}"
                else:
                    inbox = rng.choice(inboxes)
                sends[(client, inbox[0], inbox[1], camp["id"], camp["name"], 1, "A", "clay_enriched",
                       lead["provider"])] += 1
                if k < 6:
                    resp = gmail_response(R["g545"], gsmtp=True)
                    text = (f"** You have reached a limit for sending mail **\n\nYour message was not sent.\n\n"
                            f"The response was:\n\n{resp}\n")
                    body = (text, html_of(text), "", "Delivery Status Notification (Failure)", "mailer-daemon@googlemail.com")
                    rtype, folder, auto = "Bounced", "Bounced", False
                elif k < 10:
                    body = render(client, inbox[0], inbox[2], lead, "unknown", UNKNOWN_TEXTS[k - 6])
                    rtype, folder, auto = "Bounced", "Bounced", False
                elif k >= 28:  # watch list: too few to call domain reputation
                    body = render(client, inbox[0], inbox[2], lead, "rep", R["ms708"])
                    rtype, folder, auto = "Bounced", "Bounced", False
                else:  # auto-replies: 12 misfiled as Bounced, 6 correctly typed
                    text = rng.choice(OOO).format(d="November 4")
                    body = (text, html_of(text), "", f"Automatic reply: quick question", lead["email"])
                    misfiled = k < 22
                    rtype, folder, auto = ("Bounced", "Bounced", True) if misfiled else ("Automated Reply", "Inbox", True)
                events.append(payload(client, c["workspace_id"], reply_id[0], se_id[0], lead, camp, 1, "A",
                                      step_ids[(camp["id"], 1, "A")], inbox[0], inbox[3], inbox[2], sent_at, body,
                                      rtype=rtype, folder=folder, automated=auto))

    # Shape variety: camelCase and flattened payloads, plus webhook retries (exact duplicates).
    shaped = []
    for p in events:
        x = rng.random()
        shaped.append(to_camel(p) if x < 0.10 else flatten(p) if x < 0.15 else p)
    retries = [json.loads(json.dumps(p)) for p in rng.sample(shaped, 45)]
    shaped += retries
    rng.shuffle(shaped)
    shaped += outlier_inbox(sends, senders, step_ids, se_id, reply_id)

    with open(os.path.join(HERE, "bounces.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for p in shaped:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(os.path.join(HERE, "sends.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["workspace", "sender_email", "sender_domain", "campaign_id", "campaign_name", "step", "variant",
                    "list_source", "recipient_provider", "sends"])
        for key in sorted(sends, key=lambda k: tuple(str(x) for x in k)):
            w.writerow(list(key) + [sends[key]])
    with open(os.path.join(HERE, "senders.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(senders[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(senders)
    with open(os.path.join(HERE, "campaigns.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"note": "Mock of EmailBison sequence steps (GET /api/campaigns/{id}/sequence-steps).",
                   "campaigns": campaigns_out}, f, indent=1)

    google = "v=spf1 include:_spf.google.com ~all"
    dkim_key = "v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAx3s9mock0key"
    txt = {
        "kestrelreach.co": [google], "_dmarc.kestrelreach.co": ["v=DMARC1; p=none; rua=mailto:dmarc@kestrelreach.co"],
        "google._domainkey.kestrelreach.co": [dkim_key],
        "getkestrelreach.com": [google], "_dmarc.getkestrelreach.com": ["v=DMARC1; p=quarantine; rua=mailto:d@getkestrelreach.com"],
        "google._domainkey.getkestrelreach.com": [dkim_key],
        "kestrelreach-hq.com": ["v=spf1 include:spf.protection.outlook.com -all"],
        "juniperhealth.io": [google], "_dmarc.juniperhealth.io": ["v=DMARC1; p=none; rua=mailto:dmarc@juniperhealth.io"],
        "google._domainkey.juniperhealth.io": [dkim_key],
        "juniper-mail.net": ["v=spf1 ip4:203.0.113.0/24 -all"], "_dmarc.juniper-mail.net": ["v=DMARC1; p=none"],
        "default._domainkey.juniper-mail.net": [dkim_key],
    }
    with open(os.path.join(HERE, "dns_mock.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"note": "Mocked DNS answers. kestrelreach-hq.com has SPF but no DKIM and no DMARC (planted).",
                   "MX": dict(sorted(dns_mx.items())), "TXT": txt}, f, indent=1)
    with open(os.path.join(HERE, "dbl_mock.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"note": "Mocked Spamhaus DBL answers. Real lookups: <domain>.<DQS_KEY>.dbl.dq.spamhaus.net",
                   "listed": {"trk.kestrel-links.io": "127.0.1.2"}}, f, indent=1)

    print(f"{len(shaped)} webhook events ({len(retries)} retries), {sum(sends.values()):,} sends")
    for client in CLIENTS:
        print(f"  {client}: {sum(v for k, v in sends.items() if k[0] == client):,} sends")


if __name__ == "__main__":
    main()
