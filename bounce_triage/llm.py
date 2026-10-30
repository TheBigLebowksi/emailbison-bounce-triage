"""Optional LLM step. Off unless ANTHROPIC_API_KEY is set.

Two jobs only:
  (a) suggest a category for bounces the code book could not classify
      (JSON schema-constrained output; stored as a suggestion, never used
      in the verdict, so the same evidence still gives the same answer)
  (b) write the plain-English summary paragraph.
Raw HTTP via urllib to keep the tool standard-library only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any

MODEL = "claude-sonnet-5-5"
API = "https://api.anthropic.com/v1/messages"
CATEGORIES = ["bad_data", "auth", "domain_reputation", "content", "recipient_gateway", "throttling",
              "sender_mailbox", "noise", "unknown"]


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _post(payload: dict[str, Any]) -> str:
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST", headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2026-10-21",
        "anthropic-beta": "server-side-fallback-2026-07-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        msg = json.load(resp)
    if msg.get("stop_reason") == "refusal":
        raise RuntimeError("model refused")
    return "".join(block.get("text", "") for block in msg.get("content", []) if block.get("type") == "text")


def classify_unknowns(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """rows: [{'id', 'diagnostic'}] -> {id: {'category', 'reason'}}. Empty dict when off or on error."""
    if not enabled() or not rows:
        return {}
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "category": {"type": "string", "enum": CATEGORIES},
                           "reason": {"type": "string"}},
            "required": ["id", "category", "reason"], "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False,
    }
    prompt = (
        "You label email bounce responses for a cold-email deliverability tool. For each item, pick the "
        "single category that best explains why the receiving server rejected the message:\n"
        "bad_data = the address does not exist; auth = SPF/DKIM/DMARC failure; domain_reputation = sender "
        "domain/IP blocked or listed; content = message content or links flagged; recipient_gateway = a "
        "recipient-side security gateway or admin policy; throttling = rate limits/deferrals; sender_mailbox = "
        "the sending account itself is suspended or over its limit; noise = mailbox full, loops, expiry; "
        "unknown = not enough information. Use unknown rather than guess. Keep reason under 20 words.\n\n"
        + json.dumps(rows[:40], ensure_ascii=False))
    try:
        text = _post({"model": MODEL, "max_tokens": 4000, "fallbacks": "default",
                      "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                      "messages": [{"role": "user", "content": prompt}]})
        return {item["id"]: item for item in json.loads(text)["items"]}
    except Exception as exc:
        print(f"  LLM classify skipped: {exc}", file=sys.stderr)
        return {}


def summarise(ws: str, facts: dict[str, Any], template: str) -> str:
    """Plain-English summary; falls back to the deterministic template."""
    if not enabled():
        return template
    prompt = (
        "Write a 3-4 sentence summary for a client-services lead about why this client's cold email bounces "
        "went up and what is being done. Use only the facts given; keep every number exactly as given; no "
        "headings, no bullet points, plain sentences.\n\nFacts:\n" + json.dumps(facts, ensure_ascii=False))
    try:
        text = _post({"model": MODEL, "max_tokens": 800, "fallbacks": "default",
                      "output_config": {"effort": "low"},
                      "messages": [{"role": "user", "content": prompt}]}).strip()
        return text or template
    except Exception as exc:
        print(f"  LLM summary skipped for {ws}: {exc}", file=sys.stderr)
        return template

