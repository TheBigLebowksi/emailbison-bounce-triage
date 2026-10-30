"""Write out/: bounces_decoded.csv, report.md, actions.json, slack.md (+ optional Slack post).

Outputs are deterministic: no timestamps, LF line endings, stable ordering.
The same inputs give byte-identical files on every OS.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import sys
import urllib.request
from collections import Counter
from pathlib import Path

from .diagnose import code_breakdown
from .models import Action, Bounce, Decoded, RunResult, Segment, Verdict, WorkspaceResult
from .patterns import pct

BODY_FIELDS = {"text_body", "html_body", "raw_body", "decoded"}  # raw text lives in `diagnostic`
CSV_FIELDS = ([f.name for f in dataclasses.fields(Bounce) if f.name not in BODY_FIELDS] + ["sender_domain"]
              + [f.name for f in dataclasses.fields(Decoded)])
CONFIDENCE_NOTE = {"High": "", "Medium": " (probable cause)", "Low": " (possible cause: check before acting)"}
BAND_ICON = {"green": ":large_green_circle:", "yellow": ":large_yellow_circle:", "red": ":red_circle:"}
SlackMessage = tuple[str, str]  # (workspace, text)
PLACE = {"sender_inbox": ("the inbox", "the inboxes"), "sender_domain": ("the domain", "the domains"),
         "step_variant": ("the email", "the emails"), "list_source": ("the list", "the lists"),
         "recipient_provider": ("emails to", "emails to")}  # plain words for a segment's dimension


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _pts(n: int, sends: int) -> str:
    return f"{100 * n / sends:.2f}" if sends else "n/a"


def _classes(r: WorkspaceResult) -> str:
    return f"hard {pct(r.class_rate('hard'), 2)}, soft {pct(r.class_rate('soft'), 2)}, block {pct(r.class_rate('block'), 2)}"


def _band(r: WorkspaceResult) -> str:
    return r.health.band.upper() if r.health else "n/a"


def _by_rate(run: RunResult) -> list[WorkspaceResult]:
    return sorted(run.results, key=lambda r: (-(r.bounce_rate or 0), r.workspace))


def write_all(run: RunResult, out_dir: Path, mode: str = "replay") -> list[SlackMessage]:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(run, out_dir / "bounces_decoded.csv")
    _write(out_dir / "report.md", render_report(run, mode))
    actions = [a.to_dict() for r in run.results for a in r.actions]
    _write(out_dir / "actions.json", json.dumps(
        {"dry_run": True, "note": "Paths unverified against a live EmailBison instance; "
         "see ENDPOINTS in bounce_triage/actions.py.", "actions": actions}, indent=2, default=str) + "\n")
    messages = slack_messages(run)
    _write(out_dir / "slack.md", "\n\n---\n\n".join(m for _, m in messages) + "\n")
    return messages


def write_csv(run: RunResult, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for b in run.bounces:
            row = {k: v for k, v in dataclasses.asdict(b).items() if k not in BODY_FIELDS}
            decoded = dataclasses.asdict(b.decoded) if b.kept else {}  # dropped events are never decoded
            w.writerow({**row, "sender_domain": b.sender_domain, **decoded})


def _actions_md(actions: list[Action]) -> list[str]:
    lines = []
    for a in actions:
        if a.kind == "manual":
            lines.append(f"- [manual] {a.why}")
            continue
        size = f" x{len(a.items)}" if a.items is not None else ""
        body = ""
        if a.body is not None:
            text = json.dumps(a.body, default=str)
            body = f" `{text if len(text) < 110 else text[:107] + '...'}`"
        gate = "auto" if a.auto else "needs approval"
        lines.append(f"- [{gate}] `{a.method} {a.path}`{size}{body} - {a.why}")
    return lines or ["- none"]


def render_report(run: RunResult, mode: str = "replay") -> str:
    dns_note = " (values from samples/*_mock.json)" if run.dns_mode == "mock" else ""
    out = ["# Bounce Triage report", "",
           f"Mode: `{mode}`. DNS/DBL: **{run.dns_mode}**{dns_note}. Send counts: **mocked** (samples/sends.csv). "
           f"LLM: **{'on' if run.llm_on else 'off (template summary)'}**. "
           "Actions: **dry run**.", ""]
    if run.input_errors:
        out += [f"**Input errors ({len(run.input_errors)} events skipped, kept in the CSV where possible):**", ""]
        out += [f"- {e}" for e in run.input_errors[:20]] + [""]

    order = _by_rate(run)
    out += ["| Client | Health | Sends | Real bounces | Bounce rate | Hard | Soft | Causes found |",
            "|---|---|---:|---:|---:|---:|---:|---|"]
    for r in order:
        causes = ", ".join(f"{v.cause} ({v.confidence})" for v in r.verdicts if v.cause != "noise")
        out.append(f"| {r.workspace} | {_band(r)} | {r.total_sends:,} | {r.kept} | {pct(r.bounce_rate, 2)} | "
                   f"{pct(r.class_rate('hard'), 2)} | {pct(r.class_rate('soft'), 2)} | {causes or 'none - healthy'} |")
    out.append("")
    for r in order:
        out += _workspace_md(r)
    return "\n".join(out) + "\n"


def _workspace_md(r: WorkspaceResult) -> list[str]:
    drops = r.drops
    out = [f"## {r.workspace}", ""]
    if r.health:
        out += [f"**Health: {r.health.band.upper()}** - {r.health.detail}. {r.health.action}", ""]
    out += [f"**Note:** {n}" for n in r.notes] + ([""] if r.notes else [])
    out += [r.summary, "", f"Bounce class (share of sends): {_classes(r)}.", "",
           f"Input: {r.events} webhook events -> {drops.get('duplicate_webhook', 0)} duplicate retries, "
           f"{drops.get('auto_reply', 0)} auto-replies, {drops.get('delay_notice', 0)} delay notices, "
           f"{drops.get('not_bounced_type', 0)} non-bounce events dropped -> **{r.kept} real bounces**.", ""]
    if r.verdicts:
        out += ["| # | Cause | Confidence | Bounces | Pts of rate | Hard pts | Soft pts | Where |",
                "|---|---|---|---:|---:|---:|---:|---|"]
        for i, v in enumerate(r.verdicts, 1):
            where = (", ".join(f"`{h.label}`" for h in v.hot) or "spread") + (" (unexpected)" if v.unexpected else "")
            out.append(f"| {i} | {v.short_title} | {v.confidence} | {v.count} | {100 * v.rate:.2f} | "
                       f"{_pts(v.class_count('hard'), r.total_sends)} | {_pts(v.class_count('soft'), r.total_sends)} | "
                       f"{where} |")
        out.append("")
    for i, v in enumerate(r.verdicts, 1):
        sig_txt = ", ".join(f"{k} {'yes' if ok else 'no'}" for k, ok in v.signals.items() if ok is not None)
        out += [f"### {i}. {v.title} - {v.confidence} confidence", "", f"Signals: {sig_txt}.", "", "**Evidence**"]
        out += [f"- {e}" for e in v.evidence]
        out += ["", "**Fix**"] + [f"- {x}" for x in v.fix]
        out += ["", "**Do NOT**"] + [f"- {x}" for x in v.do_not]
        out += ["", "**Actions (dry run)**"] + _actions_md([a for a in r.actions if a.cause == v.cause]) + [""]
    other = [a for a in r.actions if a.cause not in {v.cause for v in r.verdicts}]
    if other:
        out += ["### Other actions", ""] + _actions_md(other) + [""]
    if r.watch:
        out += ["### Watch list (below minimum sample, no verdict yet)", ""]
        out += [f"- {w.title.split(':')[0]}: {w.bounces} bounces {w.detail}" for w in r.watch] + [""]
    if r.baseline:
        out += ["### Baseline (spread out and below the materiality bar)", ""]
        out += [f"- {b.title.split(':')[0]}: {b.bounces} bounces {b.detail}" for b in r.baseline] + [""]
    if r.unknown:
        out += [f"### Unknown queue ({len(r.unknown)} bounces kept with raw text)", "",
                "Add a code-book entry for any pattern that repeats. LLM suggestions are shown, never used in "
                "the verdict.", ""]
        for u in r.unknown[:8]:
            sug = f" - LLM suggests `{u.llm_suggestion}`" if u.llm_suggestion else ""
            out.append(f"- `{u.decoded.diagnostic[:140]}`{sug}")
        out.append("")
    out += ["### Sending domain checks", "", "| Domain | SPF | DKIM | DMARC | DBL | Source |",
            "|---|---|---|---|---|---|"]
    for d, c in r.domain_checks.items():
        if not c.checked:
            out.append(f"| {d} | not checked | not checked | not checked | - | {c.source} |")
            continue
        dbl = "listed" if c.dbl.listed else ("error" if c.dbl.listed is None else "clean")
        out.append(f"| {d} | {'yes' if c.spf else 'MISSING'} | {'/'.join(c.dkim_selectors) or 'MISSING'} | "
                   f"{c.dmarc_policy or 'MISSING'} | {dbl} | {c.source} / {c.dbl.source} |")
    out.append("")
    return out


def _fill(template: str, values: dict[str, str]) -> str | None:
    """A rules.json Slack line with its blanks filled in, or None when a value it needs is missing."""
    try:
        return template.format_map(values)
    except KeyError:
        return None


def _where(segs: list[Segment], named: bool = False) -> str:
    """`a`, `b` and `c`; with `named`, says what kind of place it is ("the inbox `a`")."""
    labels = [h.label.capitalize() if h.dimension == "recipient_provider" else f"`{h.label}`" for h in segs]
    text = f"{', '.join(labels[:-1])} and {labels[-1]}" if len(labels) > 1 else "".join(labels)
    if named and segs and segs[0].dimension in PLACE:
        text = f"{PLACE[segs[0].dimension][len(segs) > 1]} {text}"
    return text or "several places"


def _codes(v: Verdict) -> str:
    c = Counter(b.decoded.enhanced_code or b.decoded.basic_code for b in v.rows)
    return ", ".join(code for code, _ in c.most_common(3) if code) or "no SMTP code"


def _slack_head(r: WorkspaceResult) -> str:
    head = f"*{r.workspace}: {pct(r.bounce_rate, 2)} of emails bounced* ({r.kept:,} of {r.total_sends:,} sent)."
    if not r.health:
        return head
    h = r.health
    green, red = (pct(x).replace(".0%", "%") for x in (h.green_below, h.red_above))
    verdict = {"green": f"Healthy (under {green}): keep sending.",
               "yellow": f"Above the healthy line of {green}: hold new batches until the cause is clear.",
               "red": f"Above the {red} danger line: fix the problems below before sending to new leads."}
    return f"{BAND_ICON[h.band]} {head} {verdict[h.band]}"


def _slack_cause(i: int, v: Verdict) -> list[str]:
    sure = {b.lead_email for b in v.rows if b.lead_email and not b.decoded.ambiguous}
    unsure = len({b.lead_email for b in v.rows if b.lead_email and b.decoded.ambiguous} - sure)
    values = {"where": _where(v.hot, named=v.unexpected), "n": f"{v.count:,}", "codes": _codes(v),
              "n_sure": f"{len(sure):,}", "unsure": f" Re-verify the {unsure} 'Access denied' (5.4.1) ones first: "
              f"they may be real people behind a company filter." if unsure else "", **v.facts}
    s = v.slack
    headline = _fill(s.get("headline_elsewhere" if v.unexpected else "headline", ""), values) or v.short_title
    share = v.rate * v.placed / v.count if v.count else 0.0
    out = [f"*{i}. {headline}*{CONFIDENCE_NOTE[v.confidence]}: {v.placed:,} bounces, {pct(share)} of emails sent"
           + (f". Another {v.count - v.placed:,} are spread out elsewhere" if v.placed < v.count else "")]
    if v.unexpected:
        out.append("That's not where this problem usually shows up, so check it before acting.")
    for key in ("why", "ruled_out"):  # first template whose blanks can all be filled
        line = next((t for t in (_fill(x, values) for x in s.get(key, [])) if t), None)
        out += [line] if line else []
    if v.also:
        out.append(f"They also pile up on {_where(v.also, named=True)}.")
    do = [t for t in (_fill(x, values) for x in s.get("do", [])) if t] or v.fix[:1]
    out += [f"→ {t}" for t in do]
    out.append(f"_{s.get('dont') or v.do_not[0]}_")
    return out


def slack_messages(run: RunResult) -> list[SlackMessage]:
    """One plain-English message per client: how bad, what is causing it, what to do."""
    msgs = []
    for r in _by_rate(run):
        real = [v for v in r.verdicts if v.cause != "noise"]
        lines = [_slack_head(r)]
        if real and r.total_sends:
            # only the bounces on the named places go away; the spread-out rest is normal background
            after = (r.kept - sum(v.placed for v in real if v.confidence != "Low")) / r.total_sends
            lines.append(f"{'1 problem is' if len(real) == 1 else f'{len(real)} separate problems are'} causing this. "
                         f"Fixing {'it' if len(real) == 1 else 'them'} could bring it down to about {pct(after)}.")
        elif r.health and r.health.band != "green":
            lines.append("No single cause stands out yet: see the full report.")
        for i, v in enumerate(real, 1):
            lines += [""] + _slack_cause(i, v)
        outliers = [w for w in r.watch if w.cause == "inbox_outlier"]
        lines += [f"`{w.key}` bounces at 3x+ the client's overall rate ({w.bounces} bounces): worth a look."
                  for w in outliers]
        if real:
            lines += ["", f"_The full report has the evidence and {len(r.actions)} suggested EmailBison steps. "
                          f"Nothing has been changed in EmailBison._"]
        msgs.append((r.workspace, "\n".join(lines)))
    return msgs


def post_slack(messages: list[SlackMessage], only: set[str] | None = None) -> int:
    """Post to the reviewer's own incoming webhook. Does nothing unless SLACK_WEBHOOK_URL is set."""
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return 0
    sent = 0
    for ws, text in messages:
        if only is not None and ws not in only:
            continue
        req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).close()
            sent += 1
        except OSError as exc:
            print(f"  Slack post failed for {ws}: {exc}", file=sys.stderr)
    return sent


def terminal_summary(run: RunResult) -> str:
    lines = []
    for r in _by_rate(run):
        lines.append(f"{r.workspace} [{_band(r)}]: {r.kept} real bounces / {r.total_sends:,} sends = "
                     f"{pct(r.bounce_rate, 2)}  ({_classes(r)}; dropped: {sum(r.drops.values())})")
        for v in r.verdicts:
            where = (", ".join(h.label for h in v.hot) or "spread") + (" (unexpected)" if v.unexpected else "")
            where += "".join(f"; also {h.label}" for h in v.also)
            lines.append(f"  - {v.cause:<18} {v.confidence:<7} {v.count:>4} bounces {100 * v.rate:>5.2f} pts  {where}")
            lines.append(f"      codes: {code_breakdown(v.rows, top=3)}")
        watch = [w for w in r.watch if w.cause != "inbox_outlier"]
        if watch:
            lines.append("  watch: " + ", ".join(f"{w.cause} ({w.bounces})" for w in watch))
        lines += [f"  outlier: {w.key} ({w.bounces} bounces)" for w in r.watch if w.cause == "inbox_outlier"]
        if r.unknown:
            lines.append(f"  unknown queue: {len(r.unknown)}")
    if run.input_errors:
        lines.append(f"input errors: {len(run.input_errors)} (see report.md)")
    return "\n".join(lines)
