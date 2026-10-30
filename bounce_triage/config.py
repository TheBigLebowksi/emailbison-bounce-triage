"""Load the code book and rule book. All tunable numbers live in rules.json -> thresholds."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Thresholds:
    min_bounces: int          # bounces a segment needs before any verdict
    min_sends: int            # sends a segment needs before any verdict
    min_lift: float           # share of bounces / share of sends to call a segment hot
    min_share: float          # hot segments must hold this share of the cause's bounces
    code_share: float         # share decoded to a specific code-book entry for the code signal
    volume_jump: float        # now / previous daily volume that counts as a jump
    safe_daily_cap: int       # per-inbox daily sends we consider safe for cold email
    min_cause_rate: float     # a spread-out cause below this rate is baseline, not a verdict
    outlier_lift: float       # an inbox bouncing at this multiple of its workspace rate is an outlier
    place_z: float            # a pile-up outside a cause's usual place must sit this many std devs above fair share
    place_min_bounces: int    # ... and hold at least this many bounces (stricter than min_bounces)
    role_share_max: float     # role addresses (info@, sales@...) per batch we accept
    verify_max_age_days: int  # older verification than this before the send: re-verify
    auto_action_confidence: str


@dataclass(frozen=True)
class Health:
    """Bounce-rate bands. A young pool (youngest sending domain) gets a stricter green line."""

    green_below: float
    red_above: float
    young_days: int
    young_green_below: float


@dataclass(frozen=True)
class Config:
    codebook: dict[str, Any]
    causes: list[dict[str, Any]]
    dimensions: list[str]
    thresholds: Thresholds
    health: Health
    primary_domains: frozenset[str]


def load_config(root: Path = ROOT) -> Config:
    codebook = json.loads((root / "codebook.json").read_text(encoding="utf-8"))
    rules = json.loads((root / "rules.json").read_text(encoding="utf-8"))
    return Config(codebook=codebook, causes=rules["causes"], dimensions=rules["dimensions"],
                  thresholds=Thresholds(**rules["thresholds"]), health=Health(**rules["health"]),
                  primary_domains=frozenset(d.lower() for d in rules.get("primary_domains", [])))

