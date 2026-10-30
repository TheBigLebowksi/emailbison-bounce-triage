"""Step 1: read inputs, normalise EmailBison webhook payloads, keep only real bounces.

EmailBison's 'Email Bounced' webhook sends {event: {...}, data: {reply, lead,
campaign, scheduled_email, sender_email}}. Integrations re-shape it, so camelCase
keys and a flattened variant (lead_email, sender_email, ...) are accepted too.
Field names follow EmailBison's API spec and two skills written against the live API
(see README); no live payload has been seen yet: replay a test webhook first.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .models import Bounce, Sender, SendCount


# --- files ------------------------------------------------------------------

def read_events(path: Path) -> tuple[list[Any], list[str]]:
    """Read JSONL (or a JSON array). Lines that aren't JSON become error messages, not crashes."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        try:
            return json.loads(text), []
        except json.JSONDecodeError:
            pass  # a JSONL file whose first event is itself a list: read it line by line
    events, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name} line {n}: not valid JSON ({exc.msg})")
    return events, errors


def read_sends(path: Path) -> list[SendCount]:
    with path.open(encoding="utf-8", newline="") as f:
        return [SendCount(**{**row, "sender_email": row["sender_email"].lower(), "sends": int(row["sends"])})
                for row in csv.DictReader(f)]


def read_senders(path: Path) -> dict[str, dict[str, Sender]]:
    """workspace -> inbox -> Sender. Optional file."""
    out: dict[str, dict[str, Sender]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            sid = row["sender_email_id"]
            sender = Sender(workspace=row["workspace"], sender_email=row["sender_email"].lower(),
                            sender_email_id=int(sid) if sid.isdigit() else sid, account_type=row["account_type"],
                            daily_limit=float(row["daily_limit"]),
                            avg_daily_sent_prev=float(row["avg_daily_sent_prev"]),
                            avg_daily_sent_now=float(row["avg_daily_sent_now"]),
                            domain_age_days=int(row["domain_age_days"]) if row.get("domain_age_days") else None)
            out.setdefault(sender.workspace, {})[sender.sender_email] = sender
    return out


def read_campaigns(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["campaigns"] if path.exists() else []


# --- payloads ---------------------------------------------------------------

def _snake(key: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def _snake_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {_snake(k): _snake_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_snake_keys(v) for v in obj]
    return obj


def _custom_vars(raw: Any) -> dict[str, Any]:
    """custom_variables arrives as [{name, value}] or as a plain dict."""
    if isinstance(raw, dict):
        return {str(k).lower(): v for k, v in raw.items()}
    if not isinstance(raw, list):  # anything else is unusable, but must not cost the bounce
        return {}
    return {str(i["name"]).lower(): i.get("value") for i in raw if isinstance(i, dict) and "name" in i}


def _first(*values: Any) -> Any:
    return next((v for v in values if v not in (None, "")), None)


def _obj(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def normalise(payload: Any, index: int = 0) -> Bounce:
    """Turn any accepted payload shape into a Bounce. Raises ValueError on a non-object payload."""
    if not isinstance(payload, dict):
        raise ValueError(f"payload is {type(payload).__name__}, expected a JSON object")
    p = _snake_keys(payload)
    event = _obj(p, "event")
    data = _obj(p, "data") or p
    reply, lead, campaign = _obj(data, "reply"), _obj(data, "lead"), _obj(data, "campaign")
    sched, sender = _obj(data, "scheduled_email"), _obj(data, "sender_email")
    cv = _custom_vars(_first(lead.get("custom_variables"), data.get("custom_variables")))
    flat_type = _str(data, "type")

    b = Bounce(
        event_index=index,
        workspace=str(_first(event.get("workspace_name"), data.get("workspace_name"), event.get("workspace_id"),
                             data.get("workspace_id"), "unknown")),
        workspace_id=_first(event.get("workspace_id"), data.get("workspace_id")),
        event_type=str(_first(event.get("type"), _str(p, "event"),
                              flat_type if flat_type and flat_type.upper().startswith("EMAIL_") else None, "")),
        reply_id=_first(reply.get("id"), data.get("reply_id")),
        reply_type=str(_first(reply.get("reply_type"), reply.get("type"), data.get("reply_type"),
                              flat_type if flat_type and not flat_type.upper().startswith("EMAIL_") else None) or ""),
        folder=str(_first(reply.get("folder"), data.get("folder")) or ""),
        automated_reply=bool(_first(reply.get("automated_reply"), data.get("automated_reply"), False)),
        subject=str(_first(reply.get("email_subject"), reply.get("subject"), data.get("email_subject")) or ""),
        text_body=str(_first(reply.get("text_body"), data.get("text_body")) or ""),
        html_body=str(_first(reply.get("html_body"), data.get("html_body")) or ""),
        raw_body=str(_first(reply.get("raw_body"), data.get("raw_body")) or ""),
        from_email=str(_first(reply.get("from_email_address"), data.get("from_email_address")) or ""),
        date_received=str(_first(reply.get("date_received"), data.get("date_received")) or ""),
        lead_id=_first(lead.get("id"), data.get("lead_id")),
        lead_email=str(_first(lead.get("email"), data.get("lead_email")) or "").lower(),
        list_source=str(_first(cv.get("list_source"), data.get("list_source"), "unknown")),
        verifier_result=str(_first(cv.get("verifier_result"), data.get("verifier_result")) or "").lower(),
        verified_at=str(_first(cv.get("verified_at"), data.get("verified_at")) or ""),
        campaign_id=_first(campaign.get("id"), data.get("campaign_id")),
        campaign_name=str(_first(campaign.get("name"), data.get("campaign_name")) or ""),
        scheduled_email_id=_first(sched.get("id"), data.get("scheduled_email_id")),
        step=_first(sched.get("sequence_step_order"), data.get("sequence_step_order")),
        variant=_first(sched.get("sequence_step_variant"), data.get("sequence_step_variant")),
        sequence_step_id=_first(sched.get("sequence_step_id"), data.get("sequence_step_id")),
        raw_message_id=str(_first(sched.get("raw_message_id"), reply.get("raw_message_id"),
                                  data.get("raw_message_id")) or ""),
        sent_at=str(_first(sched.get("sent_at"), data.get("sent_at")) or ""),
        sender_email_id=_first(sender.get("id"), data.get("sender_email_id")),
        sender_email=str(_first(sender.get("email"), _str(data, "sender_email")) or "").lower(),
        account_type=str(_first(sender.get("account_type"), data.get("account_type")) or ""),
    )
    b.dedupe_key = dedupe_key(b)
    return b


def dedupe_key(b: Bounce) -> str:
    """EmailBison retries a webhook up to 5 times in 24h: one bounce per scheduled email."""
    if b.scheduled_email_id is not None:
        return f"se:{b.workspace}:{b.scheduled_email_id}"
    if b.raw_message_id:
        return f"mid:{b.raw_message_id}"
    basis = "|".join(str(x) for x in (b.workspace, b.lead_email, b.sender_email, b.campaign_id, b.step))
    return "h:" + hashlib.sha1(basis.encode()).hexdigest()[:16]


# --- filter -----------------------------------------------------------------

DELAY = re.compile(
    r"Delivery incomplete|Action:\s*delayed|will retry|will continue to retry|still being retried|"
    r"Delivery has been delayed|Delayed Mail|delivery is delayed|Delivery Status Notification \(Delay\)", re.I)
AUTO_REPLY = re.compile(
    r"out of (the )?office|automatic reply|auto[- ]?reply|autoreply|I am (currently )?(away|out)|"
    r"on (annual )?leave|on vacation|limited access to (my )?email|Abwesenheit|Autosvar|"
    r"r[eé]ponse automatique|respuesta autom[aá]tica|no longer with", re.I)
DSN_SIGNAL = re.compile(
    r"\b[245]\d\d[ -][245]\.\d{1,3}\.\d{1,3}\b|mailer-daemon|postmaster|Mail Delivery (Subsystem|System)|"
    r"Undeliverable|delivery-status|Diagnostic-Code|Delivery Status Notification|Address not found|"
    r"Message not delivered|Remote Server returned|could not be delivered|couldn't be delivered", re.I)


def filter_bounce(b: Bounce) -> tuple[bool, str]:
    """Return (keep, drop_reason). Counting auto-replies and delays as bounces inflates the rate."""
    labels = [x.lower() for x in (b.reply_type, b.folder) if x]
    if labels:  # EmailBison stores a bounce as type "Bounced" AND folder "Bounced"; OOO is "Tracked Reply"
        is_bounce = all(x == "bounced" for x in labels)
    else:  # flattened payload with only the event name
        is_bounce = b.event_type.upper() == "EMAIL_BOUNCED"
    if not is_bounce:
        return False, "not_bounced_type"
    if not b.lead_email and not (b.text_body or b.raw_body or b.html_body):  # e.g. a 'Send test webhook' ping
        return False, "empty_event"
    blob = " ".join([b.subject, b.from_email, b.text_body[:4000], b.raw_body[:6000], b.html_body[:4000]])
    if DELAY.search(blob):
        return False, "delay_notice"
    if not DSN_SIGNAL.search(blob) and (b.automated_reply or AUTO_REPLY.search(blob)):
        return False, "auto_reply"
    return True, ""
