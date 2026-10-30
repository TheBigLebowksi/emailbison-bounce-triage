"""Step 5: apply the rule book to each cause and score confidence.

Confidence counts three independent signals:
  code          most bounces of this cause decoded to a specific code-book entry
  concentration they sit in the segment the rule expects (and spread where it expects); when they do
                not, every other dimension is searched, and a strong pile-up there counts but caps at Medium
  check         an outside fact agrees (DNS, DBL, verifier result, volume, MX)
3 = High, 2 = Medium, 1 = Low. Same evidence, same verdict, every time.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Config, Health, Thresholds
from .enrich import Resolver
from .models import Bounce, DomainCheck, HealthBand, Note, Segment, Sender, SendCount, Verdict
from .patterns import DIMENSIONS, dim_key, pct, segments, surprise

FLAGGED_VERIFIER = {"catch_all", "catch-all", "accept_all", "unknown", "risky", "invalid", "unverified",
                    "not_verified", "none"}
ROLE_LOCALPARTS = {"info", "sales", "support", "contact", "hr", "admin", "hello", "office", "team"}
CONSUMER_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "aol.com",
                    "icloud.com", "me.com", "proton.me", "gmx.com"}
CONFIDENCE = {3: "High", 2: "Medium", 1: "Low", 0: "Low"}
SPREAD_FACT = {"sender_inbox": "inboxes", "sender_domain": "domains", "step_variant": "variants",
               "list_source": "lists", "recipient_provider": "providers"}  # Slack blanks for "spread over N"


@dataclass
class WorkspaceContext:
    """Everything the checks need about one client, besides its bounces."""

    th: Thresholds
    sends: list[SendCount]
    senders: dict[str, Sender]
    resolver: Resolver
    domain_checks: dict[str, DomainCheck]
    variant_links: dict[tuple[str, str, str], list[str]]
    step_ids: dict[tuple[str, str, str], Any]
    labels: dict[str, dict[str, str]]

    @property
    def total_sends(self) -> int:
        return sum(s.sends for s in self.sends)

    def segments(self, bounces: list[Bounce], dim: str) -> list[Segment]:
        return segments(bounces, self.sends, dim, self.th, self.labels.get(dim))


CheckResult = tuple[bool, list[str]]


# --- independent checks -------------------------------------------------------

def check_verifier(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    keys = {h.key for h in hot}
    pool = [b for b in bounces if not hot or dim_key(b, hot[0].dimension) in keys]
    # Ambiguous codes (Microsoft 5.4.1 'Access denied') may be real addresses behind a tenant block: not a verifier miss.
    ambiguous = sum(1 for b in pool if b.decoded.ambiguous)
    pool = [b for b in pool if not b.decoded.ambiguous and b.verifier_result]  # no result is not a miss
    if not pool:
        return False, ["No verifier results on these leads."]
    flagged = [b for b in pool if b.verifier_result.lower() in FLAGGED_VERIFIER]
    share = len(flagged) / len(pool)
    mix = ", ".join(f"{k} {v}" for k, v in Counter(b.verifier_result or "none" for b in pool).most_common(3))
    skipped = (f" The {ambiguous} ambiguous 5.4.1 'Access denied' bounces on this list are not counted as verifier misses."
               if ambiguous else "")
    return share >= 0.5, [f"Verifier: {pct(share)} of these bounced leads were not 'valid' at upload ({mix}).{skipped}"]


def check_dns_auth(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    domains = [h.key for h in hot] or sorted({b.sender_domain for b in bounces})
    evidence, failing = [], []
    for d in domains:
        info = ctx.domain_checks.get(d)
        if not info:
            continue
        if not info.checked:
            evidence.append(f"DNS: {d} not checked ({info.source}).")
        elif info.auth_ok:
            evidence.append(f"DNS: {d} passes SPF/DKIM/DMARC ({info.source}).")
        else:
            failing.append(d)
            evidence.append(f"DNS: {d} - {', '.join(info.problems)} ({info.source}).")
    healthy = [d for d, info in ctx.domain_checks.items() if info.auth_ok and d not in domains]
    if failing and healthy:
        evidence.append(f"Control: {', '.join(sorted(healthy))} pass all three and show no auth bounces.")
    return bool(failing), evidence


def _dbl_lines(domains: list[str], ctx: WorkspaceContext, kind: str) -> tuple[list[str], list[str]]:
    hits, lines = [], []
    for d in domains:
        res = ctx.resolver.dbl(d)
        if res.listed:
            hits.append(d)
            lines.append(f"Spamhaus DBL: {kind} {d} listed as '{res.meaning}' ({res.code}, {res.source}).")
        elif res.listed is None:
            lines.append(f"Spamhaus DBL: {kind} {d} lookup error '{res.meaning}' ({res.source}).")
        else:
            lines.append(f"Spamhaus DBL: {kind} {d} not listed ({res.source}).")
    return hits, lines


def check_dbl_sender(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    domains = [h.key for h in hot] or sorted({b.sender_domain for b in bounces})
    hits, lines = _dbl_lines(domains, ctx, "sending domain")
    return bool(hits), lines


def check_dbl_link(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    keys = [h.key for h in hot] or sorted({dim_key(b, "step_variant") for b in bounces})
    domains = sorted({d for k in keys for d in ctx.variant_links.get(tuple(k.split("|")), [])})
    if not domains:
        return False, ["Copy: no link domains found in the hot variant(s)."]
    hits, lines = _dbl_lines(domains, ctx, "link domain")
    return bool(hits), lines


def _big_provider_blocks(ctx: WorkspaceContext, bounces: list[Bounce]) -> tuple[int, int]:
    """(bounces, sends) for Google and Microsoft recipients: the control for a gateway block."""
    big = ("google", "microsoft")
    return (sum(1 for b in bounces if b.recipient_provider in big),
            sum(s.sends for s in ctx.sends if s.recipient_provider in big))


def check_gateway_only(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    big_b, big_sends = _big_provider_blocks(ctx, bounces)
    big_sends = big_sends or 1
    big_rate = big_b / big_sends

    def clean(domain: str) -> bool:
        info = ctx.domain_checks.get(domain)
        return bool(info and info.auth_ok and not info.dbl.listed)

    from_clean = sum(1 for b in bounces if clean(b.sender_domain)) / (len(bounces) or 1)
    ok = big_rate < 0.001 and from_clean >= 0.5
    return ok, [f"Control: same block category on Google/Microsoft recipients = {big_b} of {big_sends:,} sends "
                f"({pct(big_rate, 2)}).",
                f"Sender side ruled out: {pct(from_clean, 0)} of these blocks came from domains that pass "
                f"SPF/DKIM/DMARC and are not DBL-listed."]


def check_volume(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    th = ctx.th
    inboxes = [h.key for h in hot]
    lines, over = [], []
    for inbox in inboxes:
        s = ctx.senders.get(inbox)
        if not s:
            continue
        now, prev, cap = s.avg_daily_sent_now, s.avg_daily_sent_prev, th.safe_daily_cap
        jumped = bool(prev) and now / prev >= th.volume_jump
        if now > cap or jumped:
            over.append(inbox)
        lines.append(f"Volume: {inbox} sends {now:.0f}/day (was {prev:.0f}/day, safe cap {cap}, "
                     f"EmailBison limit {s.daily_limit:.0f}).")
    others = [s for k, s in ctx.senders.items() if k not in inboxes]
    if others:
        avg = sum(s.avg_daily_sent_now for s in others) / len(others)
        lines.append(f"Control: the other {len(others)} inboxes average {avg:.0f}/day.")
    return bool(inboxes) and 2 * len(over) >= len(inboxes), lines


def check_inbox_isolated(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    if len(hot) != 1:
        return False, ["More than one inbox affected."]
    inbox = hot[0].key
    domain = inbox.split("@")[-1]
    siblings = [s for s in ctx.senders if s.endswith("@" + domain) and s != inbox]
    sib_b = sum(1 for b in bounces if b.sender_email in siblings)
    return sib_b == 0, [f"Control: {len(siblings)} other inboxes on {domain} have {sib_b} of these bounces."]


def check_none(ctx: WorkspaceContext, hot: list[Segment], bounces: list[Bounce]) -> CheckResult:
    return False, []


CHECKS: dict[str, Callable[[WorkspaceContext, list[Segment], list[Bounce]], CheckResult]] = {
    "verifier": check_verifier, "dns_auth": check_dns_auth, "dbl_sender": check_dbl_sender,
    "dbl_link": check_dbl_link, "gateway_only": check_gateway_only, "volume": check_volume,
    "inbox_isolated": check_inbox_isolated, "none": check_none,
}


# --- diagnosis ----------------------------------------------------------------

def code_breakdown(bounces: list[Bounce], top: int = 4) -> str:
    c: Counter[str] = Counter()
    for b in bounces:
        d = b.decoded
        code = d.enhanced_code or d.basic_code or "no code"
        tag = f" [{d.provider_tags}]" if d.provider_tags else ""
        who = d.rejecting_provider if d.rejecting_provider != "unknown" else b.recipient_provider
        c[f"{code}{tag} {who}"] += 1
    return ", ".join(f"{k} x{v}" for k, v in c.most_common(top))


def _elsewhere(rule: dict[str, Any], cat_b: list[Bounce], ctx: WorkspaceContext,
               dimensions: list[str]) -> list[Segment]:
    """When a cause does not pile up where it usually does: the other place it piles up on hardest, if any.

    Picked by surprise, not size, so one inbox beats the domain around it when the inbox is the real pile-up.
    """
    n, best, best_z = len(cat_b), [], ctx.th.place_z
    floor, limit = rule.get("min_hot_rate", 0.0), rule["max_hot"] or 2
    skip = {rule["concentrate_on"], *rule.get("not_a_place", {})}
    for d in dimensions:
        if d in skip:
            continue
        segs = [s for s in ctx.segments(cat_b, d)
                if s.hot and s.bounces >= ctx.th.place_min_bounces and (s.rate or 0) >= floor]
        if segs and len(segs) <= limit and (z := surprise(segs, n)) >= best_z:
            best, best_z = segs, z
    return best


def _concentration(rule: dict[str, Any], cat_b: list[Bounce], ctx: WorkspaceContext,
                   dimensions: list[str]) -> tuple[list[Segment], bool, list[str], bool]:
    """Hot segments, whether the concentration (and spread) holds, the evidence lines, and whether the hot
    segments are an unexpected place (outside the dimension this cause usually piles up on)."""
    th, n, dim = ctx.th, len(cat_b), rule["concentrate_on"]
    if not dim:  # noise: the expected pattern is no pattern
        any_hot = [s for d in dimensions for s in ctx.segments(cat_b, d) if s.hot]
        if not any_hot:
            return [], True, ["Concentration: none, as expected for noise."], False
        where = ", ".join(f"{DIMENSIONS[s.dimension][0]} `{s.label}`" for s in any_hot[:3])
        return [], False, [f"Concentration: noise clusters on {where} - worth a look."], False

    singular = DIMENSIONS[dim][0]
    seg_rows = ctx.segments(cat_b, dim)
    allowed = rule.get("allowed_hot")
    floor = rule.get("min_hot_rate", 0.0)  # an over-represented segment still has to be bad in absolute terms
    lifted = [s for s in seg_rows if s.hot and (not allowed or s.key in allowed)]
    hot = [s for s in lifted if (s.rate or 0) >= floor]
    lines = [f"Concentration: {singular} `{s.label}` = {pct(s.share_sends)} of sends but {pct(s.share_bounces)} "
             f"of these bounces (lift {s.lift:.1f}, rate {pct(s.rate, 2)} on {s.sends:,} sends)." for s in hot]
    lines += [f"Concentration: {singular} `{s.label}` over-indexes (lift {s.lift:.1f}) but bounces only "
              f"{pct(s.rate, 2)}, under the {pct(floor, 0)} floor for this cause, so it is not called." for s in lifted
              if s not in hot]
    unexpected = False
    if not hot:
        top = seg_rows[0] if seg_rows else None
        lines.append(f"Concentration: no {singular} passes lift >= {th.min_lift} with >= {th.min_bounces} bounces "
                     f"and >= {th.min_sends} sends"
                     + (f" (top: `{top.label}`, lift {top.lift or 0:.1f})." if top else "."))
        hot = _elsewhere(rule, cat_b, ctx, dimensions)
        if hot:
            unexpected, dim = True, hot[0].dimension
            lines += [f"Concentration, unexpected place: {DIMENSIONS[dim][0]} `{s.label}` = {pct(s.share_sends)} of "
                      f"sends but {pct(s.share_bounces)} of these bounces (lift {s.lift:.1f}, rate {pct(s.rate, 2)} "
                      f"on {s.sends:,} sends)." for s in hot]
            lines.append(f"Concentration: that is {surprise(hot, n):.1f} standard deviations above its fair share "
                         f"(needs {th.place_z} and {th.place_min_bounces}+ bounces), too many to be chance. It is not "
                         f"where this cause usually piles up, so confidence is capped at Medium and nothing runs "
                         f"automatically.")
    hot_share = sum(s.bounces for s in hot) / n
    within = rule["max_hot"] is None or len(hot) <= rule["max_hot"]
    if hot and not within:
        lines.append(f"Concentration: {len(hot)} hot segments - wider than this cause allows "
                     f"(max {rule['max_hot']}).")
    return hot, bool(hot) and hot_share >= th.min_share and within, lines, unexpected


def _spread(rule: dict[str, Any], hot: list[Segment], cat_b: list[Bounce],
            also: list[list[Segment]]) -> tuple[bool, bool, list[str]]:
    """Whether the hot bounces spread where the rule expects, whether a miss was waived, and the evidence lines.

    A miss is waived when the bounces instead pile up hard on one value there (an `also` place): that is
    a second place to name, not a reason to drop the cause. A waived miss caps confidence at Medium.
    """
    dim = hot[0].dimension if hot else None
    keys = {h.key for h in hot}
    pool = [b for b in cat_b if dim_key(b, dim) in keys] if dim else cat_b
    also_dims = {p[0].dimension for p in also}
    ok, waived, lines = True, False, []
    for sdim, need in rule.get("spread", {}).items():
        if sdim == dim:  # they pile up there, so they cannot also spread there
            continue
        values = Counter(dim_key(b, sdim) for b in pool)
        biggest = values.most_common(1)[0][1] / len(pool) if values else 0
        line = (f"Spread: across {len(values)} {DIMENSIONS[sdim][1]} (needs >= {need}; "
                f"largest holds {pct(biggest, 0)})")
        if len(values) >= need:
            lines.append(line + ".")
        elif sdim in also_dims:
            waived = True
            lines.append(line + " - waived: they pile up on one of them instead, named below. Capped at Medium.")
        else:
            ok = False
            lines.append(line + ".")
    return ok, waived, lines


def diagnose_workspace(workspace: str, kept: list[Bounce], ctx: WorkspaceContext,
                       config: Config) -> tuple[list[Verdict], list[Note], list[Note]]:
    """Return (verdicts, watch list, baseline) for one client."""
    th, total_sends = ctx.th, ctx.total_sends
    verdicts: list[Verdict] = []
    watch: list[Note] = []
    baseline: list[Note] = []
    by_cat: dict[str, list[Bounce]] = defaultdict(list)
    for b in kept:
        by_cat[b.category].append(b)

    for rule in config.causes:
        cat_b = by_cat.get(rule["category"], [])
        n = len(cat_b)
        if n == 0:
            continue
        rate = n / total_sends if total_sends else 0.0
        if n < th.min_bounces:
            where = ", ".join(sorted({b.sender_email for b in cat_b}))[:120]
            watch.append(Note(rule["id"], rule["title"], n, f"({code_breakdown(cat_b)}) on {where}"))
            continue

        specific = [b for b in cat_b if b.decoded.match_type == "code" and not b.decoded.ambiguous]
        code_share = len(specific) / n
        code_ok = code_share >= th.code_share
        evidence = [f"{n} bounces = {pct(rate, 2)} of {total_sends:,} sends ({100 * rate:.2f} pts of the bounce rate).",
                    f"Codes: {code_breakdown(cat_b)}; {pct(code_share, 0)} matched a specific code-book entry."]
        hot, conc_ok, lines, unexpected = _concentration(rule, cat_b, ctx, config.dimensions)
        iso = isolation(rule, hot, cat_b, ctx, config.dimensions)
        also = [] if not rule["concentrate_on"] else sorted(  # noise reports its own clusters
            (p for _, p, z in iso.values()
             if p and len(p) <= 2 and z >= th.place_z and min(s.bounces for s in p) >= th.place_min_bounces),
            key=lambda p: -sum(s.bounces for s in p))
        spread_ok, waived, spread_lines = _spread(rule, hot, cat_b, also)
        conc_ok = conc_ok and spread_ok
        evidence += lines + spread_lines + [f"Also piles up on {DIMENSIONS[p[0].dimension][0]} "
                                            f"{', '.join(f'`{s.label}`' for s in p)} (lift {p[0].lift:.1f} within "
                                            f"the bounces above, too many to be chance)." for p in also]

        is_noise = rule["id"] == "noise"
        if not is_noise and not conc_ok and rate < th.min_cause_rate:
            baseline.append(Note(rule["id"], rule["title"], n, f"({pct(rate, 2)})"))
            continue

        check_ok, check_lines = CHECKS[rule["check"]](ctx, hot, cat_b)
        evidence += check_lines + extra_notes(rule, hot, by_cat, ctx)
        score = int(code_ok) + int(conc_ok) + int(conc_ok if is_noise else check_ok)
        if unexpected or waived:  # a pattern the rule book did not predict: a person looks before anything runs
            score = min(score, 2)
        facts = verdict_facts(rule, hot, iso, cat_b, ctx)
        fix = list(rule["fix"])
        if facts.get("listed_links"):  # a blocklisted link domain hurts every campaign that uses it
            fix.insert(1, f"Replace the link domain {facts['listed_links']} in every campaign that uses it: it is on "
                          f"the Spamhaus DBL. Request delisting once it is out of use.")
        verdicts.append(Verdict(
            workspace=workspace, cause=rule["id"], title=rule["title"], confidence=CONFIDENCE[score],
            signals={"code": code_ok, "concentration": conc_ok, "check": None if is_noise else check_ok},
            rate=rate, hot=hot, evidence=evidence, fix=fix, do_not=rule["do_not"],
            action_kinds=rule["actions"], rows=cat_b, slack=rule.get("slack", {}), facts=facts,
            unexpected=unexpected, also=[s for p in also for s in p], placed=sum(s.bounces for s in hot) or n,
        ))

    verdicts.sort(key=lambda v: (v.cause == "noise", -v.count))
    return verdicts, watch, baseline


def isolation(rule: dict[str, Any], hot: list[Segment], cat_b: list[Bounce], ctx: WorkspaceContext,
              dimensions: list[str]) -> dict[str, tuple[int, list[Segment], float]]:
    """For every other dimension: how many values the hot bounces span, any value they still pile up on,
    and how far above chance that pile-up is (see `surprise`).

    Compared within the hot segment's own sends, so the inboxes of a failing domain do not look hot
    just because the domain is. Dimensions the rule lists under `not_a_place` are skipped.
    """
    dim = hot[0].dimension if hot else None
    keys = {h.key for h in hot}
    pool = [b for b in cat_b if dim is None or dim_key(b, dim) in keys]
    sends = [s for s in ctx.sends if dim is None or dim_key(s, dim) in keys]
    out = {}
    for d in dimensions:
        if d != dim and d not in rule.get("not_a_place", {}):
            piled = [s for s in segments(pool, sends, d, ctx.th, ctx.labels.get(d)) if s.hot]
            out[d] = (len({dim_key(b, d) for b in pool}), piled, surprise(piled, len(pool)))
    return out


def _and(items: list[str]) -> str:
    return f"{', '.join(items[:-1])} and {items[-1]}" if len(items) > 1 else "".join(items)


def verdict_facts(rule: dict[str, Any], hot: list[Segment], iso: dict[str, tuple[int, list[Segment], float]],
                  cat_b: list[Bounce], ctx: WorkspaceContext) -> dict[str, str]:
    """Specifics the plain-English Slack text needs: what is missing, what is listed, how much is sent,
    and what is ruled out (a place the bounces spread over instead of piling up on)."""
    facts: dict[str, str] = {}
    for d, (count, piled, _) in iso.items():
        if count >= 2 and not piled:
            facts[SPREAD_FACT[d]] = str(count)
    hot_keys = {h.key for h in hot}
    if rule["id"] == "auth":
        problems = [p for h in hot if (info := ctx.domain_checks.get(h.key)) for p in info.problems]
        missing = sorted({p.split()[0] for p in problems if " missing" in p})
        other = [p for p in problems if " missing" not in p]
        parts = ([f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing"] if missing else []) + other
        if parts:
            facts["problems"] = "; ".join(parts)
        bounced = {b.sender_domain for b in cat_b}
        clean = sorted(d for d, info in ctx.domain_checks.items() if info.auth_ok and d not in bounced)
        if clean:
            facts["clean_domains"] = _and([f"`{d}`" for d in clean])
    elif rule["id"] == "content":
        links = sorted({d for k in hot_keys for d in ctx.variant_links.get(tuple(k.split("|")), [])})
        listed = [d for d in links if ctx.resolver.dbl(d).listed]
        if listed:
            facts["listed_links"] = ", ".join(listed)
    elif rule["id"] == "recipient_gateway":
        big_b, big_sends = _big_provider_blocks(ctx, cat_b)
        if big_sends:
            facts["big_sends"], facts["big_blocks"] = f"{big_sends:,}", f"{big_b:,}" if big_b else "none"
    elif rule["id"] == "throttling":
        facts["cap"] = str(ctx.th.safe_daily_cap)
        now = [s.avg_daily_sent_now for h in hot if (s := ctx.senders.get(h.key))]
        if now:
            facts["now"] = f"{max(now):.0f}"
        others = {s.sender_email for s in ctx.sends} - hot_keys
        if hot and others:
            other_b = sum(1 for b in cat_b if b.sender_email in others)
            facts["other_inboxes"], facts["other_bounces"] = str(len(others)), f"only {other_b}" if other_b else "none"
    elif rule["id"] == "sender_mailbox" and len(hot) == 1:
        domain = hot[0].key.split("@")[-1]
        siblings = [s for s in ctx.senders if s.endswith("@" + domain) and s != hot[0].key]
        if siblings:
            sib_b = sum(1 for b in cat_b if b.sender_email in siblings)
            facts["siblings"], facts["sibling_bounces"] = str(len(siblings)), str(sib_b) if sib_b else "none"
    return facts


def extra_notes(rule: dict[str, Any], hot: list[Segment], by_cat: dict[str, list[Bounce]],
                ctx: WorkspaceContext) -> list[str]:
    """The classic misread: blaming copy for bad-data bounces that sit on one variant."""
    notes = []
    if rule["id"] == "content" and hot:
        keys = {h.key for h in hot}
        addr = [b for b in by_cat.get("bad_data", []) if dim_key(b, "step_variant") in keys]
        if addr:
            notes.append(f"Excluded: {len(addr)} address bounces (5.1.x) on the same variant(s) are bad data, "
                         f"not copy, and are counted under that cause.")
    if rule["id"] == "bad_data":
        top = [s for s in ctx.segments(by_cat["bad_data"], "step_variant") if s.hot][:1]
        if top:
            notes.append(f"Note: these also over-index on `{top[0].label}` (lift {top[0].lift:.1f}) because "
                         f"that list was loaded into that campaign. That is the list, not the copy - "
                         f"do not pause the variant for it.")
        notes += lead_quality_notes(by_cat["bad_data"], ctx.th)
    return notes


def _days_between(earlier: str, later: str) -> float | None:
    try:
        return (datetime.fromisoformat(later) - datetime.fromisoformat(earlier)).total_seconds() / 86400
    except (TypeError, ValueError):  # unparseable, or naive vs aware timestamps
        return None


def lead_quality_notes(bounces: list[Bounce], th: Thresholds) -> list[str]:
    """List-hygiene evidence, each line only when the data for it is present."""
    leads = [b for b in bounces if b.lead_email]
    if not leads:
        return []
    n = len(leads)
    role = sum(1 for b in leads if b.lead_email.split("@")[0] in ROLE_LOCALPARTS)
    notes = [f"Role addresses (info@, sales@, support@...): {role} of {n} bounced leads ({pct(role / n)}); "
             f"keep them under {pct(th.role_share_max, 0)} of a batch."]
    consumer = Counter(b.lead_domain for b in leads if b.lead_domain in CONSUMER_DOMAINS)
    if consumer:
        top = ", ".join(d for d, _ in consumer.most_common(3))
        notes.append(f"Consumer domains: {pct(sum(consumer.values()) / n)} of these bounced leads are on {top}... "
                     f"On a B2B list, drop them or verify them separately.")
    ages = [age for b in leads if b.verified_at and b.sent_at
            for age in [_days_between(b.verified_at, b.sent_at)] if age is not None]
    if ages:
        stale = sum(1 for a in ages if a > th.verify_max_age_days)
        if stale:
            notes.append(f"Verification age: {stale} of {len(ages)} were verified more than {th.verify_max_age_days} "
                         f"days before the send. Re-verify at send time.")
    return notes


def health_band(rate: float | None, senders: dict[str, Sender], cfg: Health) -> HealthBand | None:
    """Green / yellow / red. A client whose youngest sending domain is under `young_days` gets a stricter
    green line: new domains have little reputation to absorb bounces."""
    if rate is None:
        return None
    green, red, age_txt = cfg.green_below, cfg.red_above, ""
    ages = [s.domain_age_days for s in senders.values() if s.domain_age_days is not None]
    if ages:
        youngest = min(ages)
        age_txt = f"; youngest sending domain {youngest} days"
        if youngest < cfg.young_days:
            green = cfg.young_green_below
    detail = f"{pct(rate, 2)} (green below {pct(green)}, red above {pct(red)}{age_txt})"
    if rate < green:
        return HealthBand("green", "Keep sending.", detail, green, red)
    if rate <= red:
        return HealthBand("yellow", "Pause new batches and find the cause.", detail, green, red)
    return HealthBand("red", "Fix the causes below before sending to new leads.", detail, green, red)


def inbox_outliers(kept: list[Bounce], ctx: WorkspaceContext) -> list[Note]:
    """Inboxes bouncing at >= outlier_lift x the workspace rate, whether or not a verdict fired."""
    total = ctx.total_sends
    if not total or not kept:
        return []
    ws_rate = len(kept) / total
    sends: Counter[str] = Counter()
    for s in ctx.sends:
        sends[s.sender_email] += s.sends
    bounces = Counter(b.sender_email for b in kept)
    notes = []
    for inbox in sorted(bounces):
        n, s = bounces[inbox], sends[inbox]
        if n >= ctx.th.min_bounces and s and n / s >= ctx.th.outlier_lift * ws_rate:
            notes.append(Note("inbox_outlier", "Inbox outlier", n,
                              f"on {inbox}: {pct(n / s, 2)} of {s:,} sends, {n / s / ws_rate:.1f}x the workspace "
                              f"rate ({pct(ws_rate, 2)})", key=inbox))
    return notes
