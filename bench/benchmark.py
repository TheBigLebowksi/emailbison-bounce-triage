"""Accuracy benchmark: generate clients with known, planted causes and score what Bounce Triage finds.

    python bench/benchmark.py              # every scenario x 8 seeds, writes bench/ACCURACY.md
    python bench/benchmark.py --seeds 2    # quicker

Each run builds one client from scratch: random domains, inboxes, volumes, lists and recipient mix,
plus the everyday mess (background dead addresses, mailbox-full noise, delay notices, auto-replies
filed as bounces, webhook retries, camelCase and flattened payloads). Then a scenario plants zero or
more causes, each on a random target (a list, a domain, inboxes, a variant, a gateway). The bounce
texts reuse the real-format templates in samples/make_samples.py.

Bounce Triage never sees the answer. Two scores come out:
  - per bounce: was each planted bounce put in the right category?
  - per client: did it name every planted cause (High or Medium), point at the right list, domain,
    inbox or variant, and raise no High/Medium verdict for a cause that was not planted?
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import random
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("ANTHROPIC_API_KEY", None)  # the template summary keeps runs fast and reproducible

from bounce_triage.config import load_config  # noqa: E402
from bounce_triage.enrich import Resolver  # noqa: E402
from bounce_triage.pipeline import Inputs, run  # noqa: E402

_spec = importlib.util.spec_from_file_location("make_samples", ROOT / "samples" / "make_samples.py")
ms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ms)  # templates and helpers only; nothing is written

CAUSE = {"bad": "bad_data", "auth": "auth", "throttle": "throttling", "content": "content", "gateway": "recipient_gateway",
         "rep": "domain_reputation", "mailbox": "sender_mailbox", "noise": "noise",
         "unknown": "unknown"}
REAL_CAUSES = ["bad_data", "auth", "throttling", "content", "recipient_gateway", "domain_reputation", "sender_mailbox"]
DAYS = 30
G_REP = ("421", "4.7.0", "Our system has detected that this message is suspicious due to the very low reputation of the "
         "sending domain. To best protect our users from spam, the message has been blocked.")
G_SUSPENDED = ("550", "5.7.0", "Mail relay denied. Your account has been suspended due to abuse.")
CLEAN_LINK, LISTED_LINK = "go.clientlinks.io", "trk.client-links.io"


@dataclass
class Scenario:
    key: str
    title: str
    plant: list[str]                              # cause keys planted at full strength
    expect: list[str] = field(default_factory=list)   # causes that must be named (default: the planted ones)
    small: bool = False                           # tiny client: planted cause stays below the evidence bar
    bad_in_one_campaign: bool = False             # the bad list is loaded into one campaign (copy lookalike)
    clean_link: bool = False                      # content blocks, but the link is not on a blocklist
    pushed_no_codes: bool = False                 # inboxes pushed above the cap without any rate-limit bounces
    truncate: int = 0                             # cut bounce text to N characters, no HTML or raw (as some sequencer APIs return it)
    strength: float = 1.0                         # scales how often planted causes fire (weak-signal scenarios)
    bad_on_inbox: bool = False                    # dead addresses come from one inbox's own leads, not from a list
    content_gmail_only: bool = False              # the blocked email is only blocked by Gmail


SCENARIOS = [
    Scenario("healthy", "Healthy client (nothing hidden)", []),
    Scenario("bad", "One bad list (dead addresses)", ["bad"]),
    Scenario("auth", "One domain missing DKIM and DMARC", ["auth"]),
    Scenario("throttle", "Inboxes sending too much", ["throttle"]),
    Scenario("content", "One email blocked as spam", ["content"]),
    Scenario("gateway", "Security filters blocking (Mimecast, Proofpoint)", ["gateway"]),
    Scenario("rep", "One sending domain on a blocklist", ["rep"]),
    Scenario("mailbox", "One inbox suspended", ["mailbox"]),
    Scenario("mixed", "Four problems at once", ["bad", "auth", "throttle", "content"]),
    Scenario("bad_one_campaign", "Trap: bad list used in one campaign only", ["bad"], bad_in_one_campaign=True),
    Scenario("content_clean_link", "Trap: blocked as spam, but the link is clean", ["content"], clean_link=True),
    Scenario("pushed_quiet", "Trap: inboxes sending a lot, no complaints", [], pushed_no_codes=True),
    Scenario("weak_bad", "Harder: bad list only a bit worse", ["bad"], strength=0.35),
    Scenario("weak_mixed", "Harder: four problems, each half as strong", ["bad", "auth", "throttle", "content"],
             strength=0.5),
    Scenario("small", "Too small to judge (tiny client)", ["bad"], expect=[], small=True),
    Scenario("cutoff", "Cut-off bounce text (as some sequencer APIs return it)", ["bad", "throttle"], truncate=200),
    Scenario("bad_inbox", "Unexpected place: dead addresses from one inbox", ["bad"], bad_on_inbox=True),
    Scenario("content_gmail", "Unexpected place: one email blocked as spam, only at Gmail", ["content"],
             content_gmail_only=True),
]


# ------------------------------------------------------------------ generation

def build_client(sc: Scenario, seed: int, out: Path) -> dict:
    """Write one client's inputs into `out`. Returns the truth: per-cause targets and the cause of every reply id."""
    rng = random.Random(f"{sc.key}:{seed}")
    ms.rng = rng                                   # the templates draw from this module-level generator
    ms.dns_mx.clear()
    ms.dns_mx.update({d: mx for d, mx in ms.CONSUMER_MX.items()})
    ms.company_domains.clear()

    stem = f"{rng.choice(ms.CO_A)}{rng.choice(ms.CO_B)}"
    n_domains = 2 if sc.small else rng.randint(2, 4)
    domains = [f"{stem}{suffix}" for suffix in [".co", "-mail.com", "hq.com", ".io"][:n_domains]]
    atypes = {d: rng.choice(["google_workspace_oauth", "google_workspace_oauth", "microsoft_oauth"]) for d in domains}
    names = iter(rng.sample(ms.FIRST, len(ms.FIRST)))
    inboxes = [(f"{next(names)}@{d}", d) for d in domains for _ in range(1 if sc.small else rng.randint(3, 5))]
    per_day_base = rng.randint(14, 18) if sc.small else rng.randint(24, 38)
    sources = {"clay_enriched": 0.45, "zoominfo_q3": 0.3, "fresh_upload": 0.25}
    campaigns = [{"id": 501, "name": f"{stem} - Ops leaders", "variants": {1: ["A", "B"], 2: ["A"], 3: ["A"]}},
                 {"id": 502, "name": f"{stem} - Finance VPs", "variants": {1: ["A", "B"], 2: ["A"], 3: ["A"]}}]
    plant = set(sc.plant)

    truth: dict = {"targets": {}, "reply_cause": {}}
    bad_source = rng.choice(list(sources)) if "bad" in plant and not sc.bad_on_inbox else None
    auth_domain = rng.choice(domains) if "auth" in plant else None
    rep_domain = rng.choice([d for d in domains if d != auth_domain]) if "rep" in plant else None
    pushed = set(rng.sample([i for i, _ in inboxes], k=min(3, max(1, len(inboxes) // 4)))) \
        if ("throttle" in plant or sc.pushed_no_codes) else set()
    hot_variant = (rng.choice([501, 502]), 1, "B") if "content" in plant else None
    google_inboxes = [i for i, d in inboxes if atypes[d] == "google_workspace_oauth" and i not in pushed]
    if "mailbox" in plant and not google_inboxes:  # suspension codes below are Gmail's
        atypes[domains[0]] = "google_workspace_oauth"
        google_inboxes = [i for i, d in inboxes if d == domains[0] and i not in pushed]
    sick_inbox = rng.choice(google_inboxes) if "mailbox" in plant else None
    bad_inbox = rng.choice([i for i, _ in inboxes if i not in pushed]) if sc.bad_on_inbox else None
    if bad_source:
        truth["targets"]["bad_data"] = {bad_source}
    if bad_inbox:
        truth["targets"]["bad_data"] = {bad_inbox}
    if auth_domain:
        truth["targets"]["auth"] = {auth_domain}
    if rep_domain:
        truth["targets"]["domain_reputation"] = {rep_domain}
    if "throttle" in plant:
        truth["targets"]["throttling"] = set(pushed)
    if hot_variant:
        cname = next(c["name"] for c in campaigns if c["id"] == hot_variant[0])
        truth["targets"]["content"] = {f"{cname} / step 1 variant B"}
    if "gateway" in plant:
        truth["targets"]["recipient_gateway"] = {"mimecast", "proofpoint"}
    truth["also"] = {"content": {"google"}} if sc.content_gmail_only else {}  # a second place that must be named
    if sick_inbox:
        truth["targets"]["sender_mailbox"] = {sick_inbox}

    small_bad = [0]

    k = sc.strength

    def respond(inbox: str, domain: str, camp: dict, step: int, variant: str, lead: dict):
        prov, r = lead["provider"], rng.random
        if sick_inbox and inbox == sick_inbox and r() < 0.06 * k:
            return "mailbox", G_SUSPENDED if r() < 0.5 else ms.R["g545"]
        if domain == rep_domain and r() < 0.05 * k and prov in ("google", "microsoft"):
            return "rep", G_REP if prov == "google" else ms.R["ms708"]
        if r() < 0.0006:  # odd texts no code book knows: they belong in the unknown queue, not a verdict
            return "unknown", rng.choice(ms.UNKNOWN_TEXTS)
        if step == 1:
            p = rng.uniform(0.12, 0.18) * k if lead["source"] == bad_source or inbox == bad_inbox else 0.008
            if sc.small and lead["source"] == bad_source:
                p = 0.15 if small_bad[0] < 7 else 0.0  # stay under the 10-bounce evidence bar
            if r() < p:
                small_bad[0] += lead["source"] == bad_source
                if prov == "google":
                    return "bad", ms.R["g511"] if r() < 0.9 else ms.R["g521"]
                if prov == "microsoft":
                    return "bad", ms.R["ms5110"] if r() < 0.8 else ms.R["ms541"]
                if prov == "yahoo":
                    return "bad", ms.YAHOO_NOUSER
                if prov == "mimecast":
                    return "bad", ms.MIMECAST_INVALID
                return "bad", ms.POSTFIX_UNKNOWN
        if domain == auth_domain:
            if prov == "google" and r() < 0.07 * k:
                return "auth", ms.R["g5740"] if r() < 0.75 else ms.R["g4740"]
            if prov == "microsoft" and lead["consumer"] and r() < 0.25 * k:
                b, e, t = ms.R["ms515"]
                return "auth", (b, e, t.replace("kestrelreach-hq.com", domain))
            if prov == "yahoo" and r() < 0.08 * k:
                return "auth", ms.YAHOO_579
        if hot_variant and (camp["id"], step, variant) == hot_variant:
            if prov == "google" and r() < 0.065 * k:
                return "content", rng.choice([ms.R["g470content"], ms.R["g4728url"], ms.R["g571spam"]])
            if prov == "yahoo" and r() < 0.2 * k and not sc.content_gmail_only:
                return "content", ms.YAHOO_PH01
            if prov == "mimecast" and r() < 0.15 * k and not sc.content_gmail_only:
                return "content", ms.MIMECAST_CONTENT
        if "throttle" in plant and inbox in pushed:
            if prov == "google" and r() < 0.026 * k:
                return "throttle", ms.R["g4728"]
            if prov == "yahoo" and r() < 0.18 * k:
                return "throttle", ms.YAHOO_TSS04
            if prov == "microsoft" and r() < 0.01 * k:
                return "throttle", ms.R["ms4650"]
        if "gateway" in plant:
            if prov == "mimecast" and r() < 0.045 * k:
                return "gateway", ms.MIMECAST_ADMIN
            if prov == "proofpoint" and r() < 0.02 * k:
                return "gateway", ms.PROOFPOINT_POLICY
        if r() < 0.0016:
            if prov == "google":
                return "noise", ms.R["g422"]
            if prov == "microsoft":
                return "noise", rng.choice([ms.R["ms522"], ms.R["ms447"]])
        return None

    events, sends, senders, step_ids = [], Counter(), [], {}
    seq = [8000]
    for camp in campaigns:
        for step, variants in camp["variants"].items():
            for v in variants:
                seq[0] += 1
                step_ids[(camp["id"], step, v)] = seq[0]
    reply_id, se_id = [90000], [700000]
    for n, (inbox, domain) in enumerate(inboxes):
        sid = 400 + n
        per_day, prev = (rng.randint(62, 75), 40) if inbox in pushed else (per_day_base, per_day_base)
        senders.append({"workspace": "Client", "sender_email": inbox, "sender_email_id": sid,
                        "account_type": atypes[domain], "daily_limit": max(per_day + 5, 45),
                        "avg_daily_sent_prev": prev, "avg_daily_sent_now": per_day, "domain_age_days": rng.randint(90, 400)})
        for _ in range(per_day * DAYS):
            source = ms.weighted(list(sources.items()))
            if sc.bad_in_one_campaign and source == bad_source:
                camp = campaigns[1]
            else:
                camp = campaigns[0] if rng.random() < 0.5 else campaigns[1]
            step = ms.weighted([(1, 0.5), (2, 0.3), (3, 0.2)])
            variant = rng.choice(camp["variants"][step])
            lead = ms.make_lead(source, source if inbox == bad_inbox else bad_source)  # that inbox's leads: unverified
            sends[("Client", inbox, domain, camp["id"], camp["name"], step, variant, source, lead["provider"])] += 1
            res = respond(inbox, domain, camp, step, variant, lead)
            if not res:
                continue
            cause, resp = res
            sent_at = ms.START + timedelta(days=rng.randint(0, DAYS - 1), minutes=rng.randint(0, 540))
            se_id[0] += 1
            reply_id[0] += 1
            if cause == "throttle" and rng.random() < 0.4:  # a delay notice first; the filter must drop it
                rt = ms.gmail_response(resp, gsmtp=True) if isinstance(resp, tuple) else ms.fill(resp)
                dtext = ms.gmail_ndr(lead["email"], rt, "delay", remote=lead["provider"] != "google")
                events.append(ms.payload("Client", 9, reply_id[0], se_id[0], lead, camp, step, variant,
                                         step_ids[(camp["id"], step, variant)], inbox, sid, atypes[domain], sent_at,
                                         (dtext, ms.html_of(dtext), "", "Delivery Status Notification (Delay)",
                                          "mailer-daemon@googlemail.com")))
                reply_id[0] += 1
            if cause == "mailbox":
                text = (f"** You have reached a limit for sending mail **\n\nYour message was not sent.\n\n"
                        f"The response was:\n\n{ms.gmail_response(resp, gsmtp=True)}\n")
                body = (text, ms.html_of(text), "", "Delivery Status Notification (Failure)", "mailer-daemon@googlemail.com")
            else:
                body = ms.render("Client", inbox, atypes[domain], lead, cause, resp)
            if sc.truncate:
                text = " ".join((body[0] or "").replace("*", "").split())[: sc.truncate]
                body = (text, "", "", body[3], body[4])
            events.append(ms.payload("Client", 9, reply_id[0], se_id[0], lead, camp, step, variant,
                                     step_ids[(camp["id"], step, variant)], inbox, sid, atypes[domain], sent_at, body))
            truth["reply_cause"][reply_id[0]] = CAUSE[cause]

    for k in range(max(2, len(events) // 60)):  # out-of-office replies misfiled as bounces
        inbox, domain = rng.choice(inboxes)
        lead = ms.make_lead("clay_enriched", None)
        text = rng.choice(ms.OOO).format(d="November 4")
        reply_id[0] += 1
        se_id[0] += 1
        events.append(ms.payload("Client", 9, reply_id[0], se_id[0], lead, campaigns[0], 1, "A",
                                 step_ids[(501, 1, "A")], inbox, 400, atypes[domain], ms.START,
                                 (text, ms.html_of(text), "", "Automatic reply", lead["email"]),
                                 rtype="Bounced", folder="Bounced", automated=True))

    shaped = [ms.to_camel(p) if (x := rng.random()) < 0.10 else ms.flatten(p) if x < 0.15 else p for p in events]
    shaped += [json.loads(json.dumps(p)) for p in rng.sample(shaped, len(shaped) // 25)]  # webhook retries
    rng.shuffle(shaped)

    out.mkdir(parents=True, exist_ok=True)
    with (out / "bounces.jsonl").open("w", encoding="utf-8") as f:
        for p in shaped:
            f.write(json.dumps(p) + "\n")
    with (out / "sends.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["workspace", "sender_email", "sender_domain", "campaign_id", "campaign_name", "step", "variant",
                    "list_source", "recipient_provider", "sends"])
        for key, n in sends.items():
            w.writerow(list(key) + [n])
    with (out / "senders.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(senders[0]))
        w.writeheader()
        w.writerows(senders)
    link = CLEAN_LINK if sc.clean_link else LISTED_LINK
    camp_json = [{"id": c["id"], "name": c["name"], "workspace": "Client",
                  "steps": [{"order": s, "variant": v, "sequence_step_id": step_ids[(c["id"], s, v)],
                             "subject": "quick question",
                             "body": "Hi {FIRST_NAME},\n\nSaw your team is hiring." +
                                     (f"\n\nTeardown: https://{link}/c/1?u={{lead_id}}"
                                      if hot_variant and (c["id"], s, v) == hot_variant else "")}
                            for s, vs in c["variants"].items() for v in vs]} for c in campaigns]
    (out / "campaigns.json").write_text(json.dumps({"campaigns": camp_json}), encoding="utf-8")
    spf = {"google_workspace_oauth": "v=spf1 include:_spf.google.com ~all",
           "microsoft_oauth": "v=spf1 include:spf.protection.outlook.com -all"}
    txt = {}
    for d in domains:
        txt[d] = [spf[atypes[d]]]
        if d != auth_domain:
            txt[f"_dmarc.{d}"] = [f"v=DMARC1; p=none; rua=mailto:dmarc@{d}"]
            sel = "google" if atypes[d] == "google_workspace_oauth" else "selector1"
            txt[f"{sel}._domainkey.{d}"] = ["v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0Bmock"]
    (out / "dns_mock.json").write_text(json.dumps({"MX": dict(ms.dns_mx), "TXT": txt}), encoding="utf-8")
    listed = {LISTED_LINK: "127.0.1.2"}
    if rep_domain:
        listed[rep_domain] = "127.0.1.2"
    (out / "dbl_mock.json").write_text(json.dumps({"listed": listed}), encoding="utf-8")
    truth.update(sends=sum(sends.values()), n_domains=len(domains), n_inboxes=len(inboxes), pushed=sorted(pushed))
    return truth


# --------------------------------------------------------------------- scoring

def label_matches(cause: str, hot: set[str], targets: set[str]) -> bool:
    """Right place: every hot segment is a planted target (throttling and gateways may name a subset)."""
    if not hot:
        return False
    if cause == "content":  # segment labels carry the campaign name; compare the step/variant tail
        return any(t.split(" / ")[-1] in h and t.split(" / ")[0] in h for h in hot for t in targets)
    return hot <= targets


def score(sc: Scenario, seed: int, workdir: Path, keep: bool = False) -> dict:
    truth = build_client(sc, seed, workdir)
    cfg = load_config()
    payloads = [json.loads(line) for line in (workdir / "bounces.jsonl").read_text(encoding="utf-8").splitlines()]
    result = run(payloads, Inputs.from_dir(workdir), cfg, Resolver.from_dir("mock", workdir))
    ws = next(r for r in result.results if r.workspace == "Client")

    per_bounce = Counter()
    for b in result.bounces:
        expected = truth["reply_cause"].get(b.reply_id)
        if expected and b.kept and not b.drop_reason:
            per_bounce[(expected, b.category)] += 1

    called = {v.cause: v for v in ws.verdicts if v.cause != "noise" and v.confidence in ("High", "Medium")}
    expect = set(CAUSE[c] for c in (sc.expect if sc.expect or sc.small else sc.plant))
    found = {c: c in called for c in expect}
    located = {c: c in called and label_matches(c, {h.label for h in called[c].hot}, truth["targets"].get(c, set()))
               and truth["also"].get(c, set()) <= {h.label for h in getattr(called[c], "also", [])}
               for c in expect}
    # A small client's planted cause may be named cautiously (Medium), but never at High on that little evidence.
    optional = set(CAUSE[c] for c in sc.plant) if sc.small else set()
    false_alarms = sorted(c for c in called if c not in expect and c not in optional)
    overconfident = sorted(c for c in optional if c in called and called[c].confidence == "High")
    out = {"scenario": sc.key, "seed": seed, "sends": truth["sends"], "kept": ws.kept, "rate": ws.bounce_rate or 0,
           "found": found, "located": located, "false_alarms": false_alarms + overconfident,
           "confidence": {c: called[c].confidence for c in called},
           "watch": sorted(w.cause for w in ws.watch), "per_bounce": per_bounce,
           "unexpected": sorted(c for c in called if getattr(called[c], "unexpected", False)),
           "also": sorted(f"{h.label} ({c})" for c in called for h in getattr(called[c], "also", [])),
           "ok": all(found.values()) and all(located.values()) and not false_alarms and not overconfident}
    if keep:  # bench/inspect.py shows the whole run, not just the score
        out.update(truth=truth, result=result, workspace=ws, expect=expect)
    return out


# --------------------------------------------------------------------- report

def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def write_report(results: list[dict], seeds: int, seconds: float, path: Path) -> str:
    by_sc = defaultdict(list)
    for r in results:
        by_sc[r["scenario"]].append(r)
    lines = ["# Bounce Triage accuracy benchmark", "",
             f"{len(results)} generated clients: {len(SCENARIOS)} scenarios x {seeds} random seeds "
             f"({min(r['sends'] for r in results):,}-{max(r['sends'] for r in results):,} sends each). "
             f"Run `python bench/benchmark.py --seeds {seeds}` to reproduce ({seconds:.0f}s).", "",
             "Every client is generated with a known answer. Bounce Triage does not see it. A client counts as "
             "**correct** only if every planted cause is named (High or Medium confidence), each points at the "
             "planted list, domain, inbox, variant or gateway, and no other cause is raised at High or Medium. "
             "The two \"unexpected place\" scenarios plant a cause where its rule does not look first; the Gmail one "
             "also needs Gmail named as a second place.", "",
             "## Per client", "", "| Scenario | Correct | Cause found | Right place | False alarms | Notes |",
             "|---|---:|---:|---:|---:|---|"]
    total_ok = 0
    for sc in SCENARIOS:
        rs = by_sc[sc.key]
        ok = sum(r["ok"] for r in rs)
        total_ok += ok
        need = [c for r in rs for c in r["found"]]
        found = sum(v for r in rs for v in r["found"].values())
        located = sum(v for r in rs for v in r["located"].values())
        fa = Counter(c for r in rs for c in r["false_alarms"])
        conf = Counter(v for r in rs for v in r["confidence"].values())
        notes = []
        if sc.small:
            notes.append(f"held back as watch-list in {sum('bad_data' in r['watch'] for r in rs)}/{len(rs)}")
        if conf:
            notes.append("confidence " + ", ".join(f"{k} {v}" for k, v in sorted(conf.items())))
        unexpected = Counter(c for r in rs for c in r["unexpected"])
        if unexpected:
            notes.append("named an unexpected place: " + ", ".join(f"{c} x{n}" for c, n in unexpected.items()))
        also = Counter(a for r in rs for a in r["also"])
        if also:
            notes.append("also named: " + ", ".join(f"{a} x{n}" for a, n in also.most_common(3)))
        lines.append(f"| {sc.title} | {ok}/{len(rs)} | {pct(found, len(need)) if need else '-'} | "
                     f"{pct(located, len(need)) if need else '-'} | "
                     f"{', '.join(f'{c} x{n}' for c, n in fa.items()) or '0'} | {'; '.join(notes)} |")
    lines += ["", f"**Overall: {total_ok}/{len(results)} clients fully correct ({pct(total_ok, len(results))}).**", ""]

    pb = Counter()
    for r in results:
        if r["scenario"] != "cutoff":
            pb.update(r["per_bounce"])
    cut = Counter()
    for r in by_sc["cutoff"]:
        cut.update(r["per_bounce"])
    lines += ["## Per bounce", "",
              "Each planted bounce, after the filter, against the category the decoder gave it "
              "(full bounce text, all scenarios except the cut-off one).", "",
              "| Planted cause | Bounces | Right category | Most common miss |", "|---|---:|---:|---|"]
    for cause in REAL_CAUSES + ["noise"]:
        n = sum(v for (e, _), v in pb.items() if e == cause)
        if not n:
            continue
        right = pb[(cause, cause)]
        misses = Counter({g: v for (e, g), v in pb.items() if e == cause and g != cause})
        miss = ", ".join(f"{g} x{v}" for g, v in misses.most_common(2)) or "-"
        lines.append(f"| {cause} | {n:,} | {pct(right, n)} | {miss} |")
    all_n = sum(pb.values())
    lines += ["", f"**{pct(sum(v for (e, g), v in pb.items() if e == g), all_n)} of {all_n:,} planted bounces "
                  f"decoded to the right cause.**", ""]
    n_cut = sum(cut.values())
    if n_cut:
        lines += ["With text cut to 200 characters and no HTML or raw copy (as some sequencer APIs return it): "
                  f"{pct(sum(v for (e, g), v in cut.items() if e == g), n_cut)} of {n_cut:,} bounces decoded "
                  f"correctly; misses: " + ", ".join(f"{e} as {g} x{v}" for (e, g), v in cut.most_common()
                                                    if e != g) + ".", ""]
    lines += ["## What this does and doesn't prove", "",
              "- It tests the logic end to end on many different clients, volumes and mixes, including lookalikes "
              "built to trick it. Each run is random, so it can't be tuned to one sample.",
              "- The bounce texts are templates modelled on real Google, Microsoft, Yahoo, Mimecast, Proofpoint and "
              "Postfix messages, and the same code wrote the scenarios and the rules. Real bounces will be messier.",
              "- It does not test EmailBison's real field names. Replaying a test webhook from a real workspace does that on day one.", ""]
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8", newline="\n")
    return text


def main(argv: list[str] | None = None) -> list[dict]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--only", help="comma-separated scenario keys")
    ap.add_argument("--out", default=str(ROOT / "bench" / "ACCURACY.md"))
    args = ap.parse_args(argv)
    chosen = [s for s in SCENARIOS if not args.only or s.key in args.only.split(",")]
    start, results = time.time(), []
    with tempfile.TemporaryDirectory() as tmp:
        for sc in chosen:
            for seed in range(args.seeds):
                r = score(sc, seed, Path(tmp) / f"{sc.key}_{seed}")
                results.append(r)
                flag = "ok " if r["ok"] else "MISS"
                print(f"{flag} {sc.key:<20} seed {seed}  sends {r['sends']:>6,}  rate {100 * r['rate']:.2f}%  "
                      f"found {r['found']}  place {r['located']}  false {r['false_alarms']}  watch {r['watch']}  "
                      f"unexpected {r['unexpected']}  also {r['also']}")
    if not args.only:
        print(write_report(results, args.seeds, time.time() - start, Path(args.out)))
    return results


if __name__ == "__main__":
    main()
