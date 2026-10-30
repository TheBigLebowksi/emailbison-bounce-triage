"""Turn verdicts into EmailBison API steps. Always a dry run: nothing is sent to EmailBison.

Operation names match EmailBison's public API reference. The HTTP paths below
were NOT tested against a live instance: confirm them once with a real key
before wiring them up. They are all in ENDPOINTS so there is one place to fix them.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .diagnose import WorkspaceContext
from .models import Action, Bounce, Verdict

ENDPOINTS = {
    "pause_campaign": ("PATCH", "/api/campaigns/{campaign_id}/pause"),
    "toggle_variant": ("PATCH", "/api/campaigns/v1.1/sequence-steps/{sequence_step_id}/activate-or-deactivate"),
    "remove_sender_emails": ("DELETE", "/api/campaigns/{campaign_id}/remove-sender-emails"),
    "bulk_update_daily_limits": ("PATCH", "/api/sender-emails/daily-limits/bulk"),
    "create_tag": ("POST", "/api/tags"),
    "attach_tags_to_leads": ("POST", "/api/tags/attach-to-leads"),
    "attach_tags_to_sender_emails": ("POST", "/api/tags/attach-to-sender-emails"),
    "attach_tags_to_campaigns": ("POST", "/api/tags/attach-to-campaigns"),
    "create_blacklisted_email": ("POST", "/api/blacklisted-emails"),
    "campaign_stats": ("POST", "/api/campaigns/{campaign_id}/stats"),
}
RANK = {"Low": 1, "Medium": 2, "High": 3}
SAFE_AUTO = {"create_blacklisted_email", "create_tag", "attach_tags_to_leads", "attach_tags_to_sender_emails",
             "attach_tags_to_campaigns"}  # reversible and stop nothing: the only steps that may run without a person
PLACE_ACTIONS = {"hold_list_source", "remove_domain_senders", "remove_sender", "tag_senders", "lower_daily_limit",
                 "pause_variant", "tag_campaign"}  # act on the hot keys, so they assume the expected dimension


def _api(op: str, why: str, body: dict[str, Any] | None = None, items: list[dict[str, Any]] | None = None,
         **path_args: Any) -> Action:
    method, path = ENDPOINTS[op]
    return Action(kind="api", operation=op, why=why, method=method, path=path.format(**path_args),
                  body=body, items=items)


def _manual(why: str) -> Action:
    return Action(kind="manual", operation="manual", why=why)


def _tag(tag: str, op: str, why: str, ids_field: str, ids: list[Any]) -> list[Action]:
    """Create the cause tag, then attach it. The tag id is only known once the tag exists."""
    return [_api("create_tag", "Make the cause filterable in EmailBison.", body={"name": tag}),
            _api(op, why, body={"tag_ids": [f"<id of {tag}>"], ids_field: ids})]


def build_actions(verdict: Verdict, ctx: WorkspaceContext) -> list[Action]:
    """Map a verdict's action kinds onto concrete calls."""
    cause, rows = verdict.cause, verdict.rows
    tag = f"bounce:{cause.replace('_', '-')}"
    hot_keys = [h.key for h in verdict.hot]
    out: list[Action] = []
    kinds = verdict.action_kinds
    if verdict.unexpected:  # the hot keys are not the kind of segment these actions target: a person checks
        kinds = [k for k in kinds if k not in PLACE_ACTIONS]
        out.append(_manual(f"These bounces pile up on {', '.join(f'`{h.label}`' for h in verdict.hot)}, not where "
                           f"this cause usually shows up. Check why before acting on that place."))
    for kind in kinds:
        if kind == "blocklist_bounced":
            # Ambiguous codes (Microsoft 5.4.1 'Access denied') may be real people behind a company filter.
            emails = sorted({b.lead_email for b in rows if b.lead_email and not b.decoded.ambiguous})
            unsure = sorted({b.lead_email for b in rows if b.lead_email and b.decoded.ambiguous} - set(emails))
            out.append(_api("create_blacklisted_email", "Never retry an address that does not exist.",
                            items=[{"email": e} for e in emails]))
            if unsure:
                out.append(_manual(f"{len(unsure)} addresses bounced with an ambiguous code (5.4.1 'Access "
                                   f"denied'): they may be real people behind a company filter. Re-verify them "
                                   f"before blocking."))
        elif kind == "tag_leads":
            lead_ids = sorted({b.lead_id for b in rows if b.lead_id is not None}, key=str)
            out += _tag(tag, "attach_tags_to_leads", f"Tag bounced leads `{tag}`.", "lead_ids", lead_ids)
        elif kind == "hold_list_source":
            out += [_manual(f"Stop uploads from list source `{src}` and re-verify its un-contacted leads "
                            f"(no list-source object in the API; done in the enrichment tool).") for src in hot_keys]
        elif kind in ("remove_domain_senders", "remove_sender"):
            inboxes = [s for s in ctx.senders if
                       (kind == "remove_sender" and s in hot_keys) or
                       (kind == "remove_domain_senders" and s.split("@")[-1] in hot_keys)]
            ids = [ctx.senders[s].sender_email_id for s in inboxes]
            for cid in sorted({s.campaign_id for s in ctx.sends if s.sender_email in inboxes}, key=str):
                out.append(_api("remove_sender_emails", f"Pull {', '.join(inboxes)} out of campaign {cid} "
                                f"(EmailBison has no domain-level pause).", campaign_id=cid,
                                body={"sender_email_ids": ids}))
        elif kind == "tag_senders":
            inboxes = [s for s in ctx.senders if s in hot_keys or s.split("@")[-1] in hot_keys]
            out += _tag(tag, "attach_tags_to_sender_emails", f"Tag affected inboxes `{tag}`.", "sender_email_ids",
                        [ctx.senders[s].sender_email_id for s in inboxes])
        elif kind == "lower_daily_limit":
            ids = [ctx.senders[s].sender_email_id for s in hot_keys if s in ctx.senders]
            out.append(_api("bulk_update_daily_limits", "Bring hot inboxes back under the safe cap.",
                            body={"sender_email_ids": ids, "daily_limit": ctx.th.safe_daily_cap}))
        elif kind == "pause_variant":
            for key in hot_keys:
                step_id = ctx.step_ids.get(tuple(key.split("|")))
                label = ctx.labels.get("step_variant", {}).get(key, key)
                out.append(_api("toggle_variant", f"Deactivate `{label}`.",
                                sequence_step_id=step_id if step_id is not None else "<unknown>",
                                body={"active": False}))
        elif kind == "tag_campaign":
            cids = sorted({k.split("|")[0] for k in hot_keys})
            out += _tag(tag, "attach_tags_to_campaigns", f"Tag campaign(s) `{tag}`.", "campaign_ids", cids)

    auto = RANK[verdict.confidence] >= RANK[ctx.th.auto_action_confidence]
    for i, act in enumerate(out, 1):
        act.id = f"{verdict.workspace}:{cause}:{i}"
        act.workspace, act.cause, act.confidence = verdict.workspace, cause, verdict.confidence
        act.auto = auto and act.operation in SAFE_AUTO
        act.status = "dry_run" if act.kind == "api" else "manual"
    return out


def repeat_soft_bounce_actions(workspace: str, kept: list[Bounce]) -> list[Action]:
    """Suppress addresses whose mailbox was full (x.2.2) on two or more separate sends. Needs approval."""
    full = Counter(b.lead_email for b in kept if b.lead_email and b.decoded.enhanced_code[1:] == ".2.2"
                   and b.category != "sender_mailbox")  # Microsoft 5.2.2 'Submission quota' is our inbox, not theirs
    emails = sorted(e for e, n in full.items() if n >= 2)
    if not emails:
        return []
    act = _api("create_blacklisted_email", "Mailbox full on 2+ separate sends: stop retrying these addresses.",
               items=[{"email": e} for e in emails])
    act.id, act.workspace, act.cause, act.status = f"{workspace}:repeat_soft:1", workspace, "repeat_soft", "dry_run"
    return [act]
