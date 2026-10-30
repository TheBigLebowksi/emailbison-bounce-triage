"""Run the five steps over a batch of webhook payloads, one workspace (client) at a time.

  1 ingest    normalise payloads, drop auto-replies / delays / retries
  2 decode    SMTP codes, who rejected it, code-book category
  3 enrich    recipient MX, sender SPF/DKIM/DMARC, Spamhaus DBL
  4 patterns  bounce rate and lift per segment
  5 diagnose  rule book -> cause, confidence, fix, do-not, actions
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import llm, report
from .actions import build_actions, repeat_soft_bounce_actions
from .config import Config, load_config
from .decode import decode
from .diagnose import WorkspaceContext, diagnose_workspace, health_band, inbox_outliers
from .enrich import Resolver, check_sending_domain, recipient_provider, variant_link_domains
from .ingest import filter_bounce, normalise, read_campaigns, read_events, read_senders, read_sends
from .models import Bounce, RunResult, Sender, SendCount, WorkspaceResult
from .patterns import dim_key, pct

MALFORMED = "malformed"


@dataclass
class Inputs:
    """Mocks of EmailBison API data: send counts (required), sender stats and sequence copy (optional)."""

    sends: list[SendCount]
    senders: dict[str, dict[str, Sender]] = field(default_factory=dict)
    campaigns: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dir(cls, directory: Path) -> Inputs:
        return cls(read_sends(directory / "sends.csv"), read_senders(directory / "senders.csv"),
                   read_campaigns(directory / "campaigns.json"))


def process_events(payloads: list[Any], codebook: dict[str, Any], resolver: Resolver,
) -> list[Bounce]:
    """Steps 1-3 for every event. Returns every event (kept and dropped) as a Bounce."""
    rows, seen = [], set()
    for i, payload in enumerate(payloads):
        try:
            b = normalise(payload, i)
        except (ValueError, TypeError, AttributeError) as exc:  # never lose an event: keep it as a row
            rows.append(Bounce(event_index=i, workspace="", drop_reason=f"{MALFORMED}: {exc}"))
            continue
        keep, reason = filter_bounce(b)
        # Dedupe after filtering: a delay notice for the same scheduled email must not hide the final bounce.
        if keep and b.dedupe_key in seen:
            keep, reason = False, "duplicate_webhook"
        if keep:
            seen.add(b.dedupe_key)
        b.kept, b.drop_reason = keep, reason
        if keep:
            b.recipient_provider, b.recipient_mx, b.recipient_provider_source = recipient_provider(b.lead_domain,
                                                                                                  resolver)
            b.decoded = decode(b, codebook)
            if b.decoded.rejecting_provider == "unknown" and b.recipient_provider not in ("unknown", "other"):
                b.decoded = decode(b, codebook, provider_hint=b.recipient_provider)
        rows.append(b)
    return rows


def _context(ws: str, kept: list[Bounce], inputs: Inputs, config: Config, resolver: Resolver) -> WorkspaceContext:
    sends = [s for s in inputs.sends if s.workspace == ws]
    labels = {"step_variant": {dim_key(s, "step_variant"): f"{s.campaign_name} / step {s.step} variant {s.variant}"
                               for s in sends}}
    domains = sorted(({s.sender_domain for s in sends} | {b.sender_domain for b in kept}) - {"unknown"})
    return WorkspaceContext(
        th=config.thresholds, sends=sends, senders=inputs.senders.get(ws, {}), resolver=resolver,
        domain_checks={d: check_sending_domain(d, resolver) for d in domains},
        variant_links=variant_link_domains(inputs.campaigns),
        step_ids={(str(c["id"]), str(s["order"]), str(s["variant"])): s.get("sequence_step_id")
                  for c in inputs.campaigns for s in c.get("steps", [])},
        labels=labels,
    )


def run(payloads: list[Any], inputs: Inputs, config: Config, resolver: Resolver,
        input_errors: list[str] | None = None) -> RunResult:
    rows = process_events(payloads, config.codebook, resolver)
    errors = list(input_errors or []) + [f"event {b.event_index}: {b.drop_reason}" for b in rows
                                         if b.drop_reason.startswith(MALFORMED)]
    parsed = [b for b in rows if not b.drop_reason.startswith(MALFORMED)]
    # a workspace with only dropped events (e.g. a test webhook) is not a client
    workspaces = sorted({s.workspace for s in inputs.sends} | {b.workspace for b in parsed if b.kept})
    results = []
    for ws in workspaces:
        ws_rows = [b for b in parsed if b.workspace == ws]
        kept = [b for b in ws_rows if b.kept]
        ctx = _context(ws, kept, inputs, config, resolver)
        verdicts, watch, baseline = diagnose_workspace(ws, kept, ctx, config) if ctx.sends else ([], [], [])
        watch += inbox_outliers(kept, ctx)
        primary = sorted(set(ctx.domain_checks) & config.primary_domains)
        notes = [f"Cold email is sent from the primary domain {', '.join(primary)}: move cold email off it, "
                 f"so a reputation hit cannot reach the company's own mail."] if primary else []

        unknown = [b for b in kept if b.category == "unknown"]
        suggestions = llm.classify_unknowns([{"id": b.dedupe_key, "diagnostic": b.decoded.diagnostic}
                                             for b in unknown])
        for b in kept:
            b.llm_suggestion = suggestions.get(b.dedupe_key, {}).get("category", "")

        res = WorkspaceResult(
            workspace=ws, events=len(ws_rows), drops=dict(Counter(b.drop_reason for b in ws_rows if not b.kept)),
            kept=len(kept), total_sends=ctx.total_sends, verdicts=verdicts, watch=watch, baseline=baseline,
            unknown=unknown,
            actions=[a for v in verdicts for a in build_actions(v, ctx)] + repeat_soft_bounce_actions(ws, kept),
            domain_checks=ctx.domain_checks, class_counts=dict(Counter(b.decoded.bounce_class for b in kept)),
            notes=notes,
        )
        res.health = health_band(res.bounce_rate, ctx.senders, config.health)
        res.summary = llm.summarise(ws, summary_facts(res), summary_template(res))
        results.append(res)
    return RunResult(results=results, bounces=rows, input_errors=errors, dns_mode=resolver.mode,
                     llm_on=llm.enabled())


def triage(events_path: Path, inputs_dir: Path, out_dir: Path, dns_mode: str = "mock",
           mode: str = "replay") -> tuple[RunResult, list[report.SlackMessage]]:
    """Read files, run the five steps, write out/."""
    payloads, errors = read_events(events_path)
    result = run(payloads, Inputs.from_dir(inputs_dir), load_config(), Resolver.from_dir(dns_mode, inputs_dir),
                 input_errors=errors)
    return result, report.write_all(result, out_dir, mode)


def summary_facts(res: WorkspaceResult) -> dict[str, Any]:
    return {"client": res.workspace, "sends": res.total_sends, "real_bounces": res.kept,
            "bounce_rate": pct(res.bounce_rate, 2), "health": res.health.band if res.health else None,
            "hard_bounce_rate": pct(res.class_rate("hard"), 2), "soft_bounce_rate": pct(res.class_rate("soft"), 2),
            "causes": [{"cause": v.title, "confidence": v.confidence, "bounces": v.count,
                        "points_of_bounce_rate": round(100 * v.rate, 2), "where": [h.label for h in v.hot]}
                       for v in res.verdicts],
            "automatable_actions": sum(1 for a in res.actions if a.auto),
            "actions_needing_review": sum(1 for a in res.actions if not a.auto)}


def summary_template(res: WorkspaceResult) -> str:
    ws, rate = res.workspace, pct(res.bounce_rate, 2)
    real = [v for v in res.verdicts if v.cause != "noise"]
    if not res.total_sends:
        return f"{ws}: bounces received but no send counts, so no rates can be computed."
    outliers = [w.key for w in res.watch if w.cause == "inbox_outlier"]
    if not real:
        tail = (f"but {', '.join(outliers)} {'bounces' if len(outliers) == 1 else 'bounce'} at 3x+ the workspace "
                f"rate: check {'that inbox' if len(outliers) == 1 else 'those inboxes'}." if outliers
                else "Healthy; no action.")
        return (f"{ws}: {res.kept} real bounces on {res.total_sends:,} sends ({rate}). "
                f"No cause passes the evidence bar{', ' if outliers else '. '}{tail}")
    parts = [f"{v.short_title} ({v.confidence}, {100 * v.rate:.2f} pts)" for v in real]
    # only the bounces on the named places go away; the spread-out rest is normal background (same as Slack)
    fixed = sum(v.placed for v in real if v.confidence != "Low")
    after = (res.kept - fixed) / res.total_sends
    auto = sum(1 for a in res.actions if a.auto)
    review = len(res.actions) - auto
    count = "1 cause" if len(real) == 1 else f"{len(real)} separate causes, not one"
    review_txt = f"{review} {'needs' if review == 1 else 'need'} a person" if review else "none need a person"
    return (f"{ws}: {res.kept} real bounces on {res.total_sends:,} sends ({rate}). {count}: {'; '.join(parts)}. "
            f"Fixing the High/Medium ones could bring the rate down to about {pct(after, 1)}. "
            f"Of the {len(res.actions)} suggested EmailBison steps (all dry run), {auto} are safe to run on their "
            f"own; {review_txt}.")
