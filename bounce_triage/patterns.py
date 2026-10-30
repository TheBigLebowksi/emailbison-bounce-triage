"""Step 4: where do a cause's bounces concentrate?

Bounce rate is always bounces / sends for the same segment. Lift is
(share of this cause's bounces) / (share of sends): 1.0 means the segment
bounces like everyone else, 3.0 means three times its fair share.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from typing import Any

from .config import Thresholds
from .models import Segment, SendCount

DIMENSIONS = {  # key: (singular, plural)
    "sender_inbox": ("inbox", "inboxes"),
    "sender_domain": ("sending domain", "sending domains"),
    "step_variant": ("step/variant", "step/variants"),
    "list_source": ("list source", "list sources"),
    "recipient_provider": ("recipient provider", "recipient providers"),
}


def dim_key(row: Any, dim: str) -> str:
    """Segment key of a Bounce or SendCount on one dimension."""
    if dim == "sender_inbox":
        return row.sender_email or "unknown"
    if dim == "step_variant":
        return f"{row.campaign_id}|{row.step}|{row.variant}"
    return str(getattr(row, dim) or "unknown")


def segments(bounces: Iterable[Any], sends: list[SendCount], dim: str, th: Thresholds,
             labels: dict[str, str] | None = None) -> list[Segment]:
    """Rate and lift per segment, most bounces first (ties by key, so output is stable)."""
    bounces = list(bounces)
    total_sends = sum(s.sends for s in sends) or 1
    sends_by: Counter[str] = Counter()
    for s in sends:
        sends_by[dim_key(s, dim)] += s.sends
    bounces_by = Counter(dim_key(b, dim) for b in bounces)
    n = len(bounces) or 1
    rows = []
    for key in sorted(set(sends_by) | set(bounces_by)):
        s, b = sends_by[key], bounces_by[key]
        share_s, share_b = s / total_sends, b / n
        lift = share_b / share_s if share_s else None
        hot = b >= th.min_bounces and s >= th.min_sends and lift is not None and lift >= th.min_lift
        rows.append(Segment(dim, key, (labels or {}).get(key, key), s, b, b / s if s else None,
                            share_s, share_b, lift, hot))
    return sorted(rows, key=lambda r: (-r.bounces, r.key))


def surprise(segs: list[Segment], n: int) -> float:
    """How far these segments' bounces sit above their fair share, in standard deviations (binomial z-score).

    Lift says how big a pile-up is; this says how unlikely it is by chance. Looking in every dimension
    means more chances for a coincidence, so pile-ups outside a cause's usual place must pass both.
    """
    p = sum(s.share_sends for s in segs)
    if not n or not 0 < p < 1:
        return 0.0
    return (sum(s.bounces for s in segs) - n * p) / math.sqrt(n * p * (1 - p))


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{digits}f}%"
