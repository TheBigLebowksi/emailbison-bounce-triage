"""Data types shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Decoded:
    """What the bounce text says: codes, who rejected it, and the code-book category."""

    generator: str = "unknown"            # who wrote the NDR: google | microsoft | postfix | unknown
    rejecting_provider: str = "unknown"   # who refused the mail, read from the remote response
    basic_code: str = ""                  # 550
    enhanced_code: str = ""               # 5.1.1
    provider_tags: str = ""               # Yahoo [TSS04] etc.
    diagnostic: str = ""                  # the remote server's response, whitespace squashed
    decode_source: str = ""               # dsn | text_body | html_body | raw_body
    codebook_id: str = ""
    match_type: str = "none"              # code | keyword | class | none
    meaning: str = ""
    category: str = "unknown"
    fault_side: str = "unknown"
    ambiguous: bool = False
    bounce_class: str = "other"           # hard | soft | block | other


@dataclass
class Bounce:
    """One webhook event, normalised. Dropped events are kept too, with a reason."""

    event_index: int
    workspace: str = "unknown"
    workspace_id: Any = None
    event_type: str = ""
    reply_id: Any = None
    reply_type: str = ""
    folder: str = ""
    automated_reply: bool = False
    subject: str = ""
    text_body: str = ""
    html_body: str = ""
    raw_body: str = ""
    from_email: str = ""
    date_received: str = ""
    lead_id: Any = None
    lead_email: str = ""
    list_source: str = "unknown"
    verifier_result: str = ""
    verified_at: str = ""
    campaign_id: Any = None
    campaign_name: str = ""
    scheduled_email_id: Any = None
    step: Any = None
    variant: Any = None
    sequence_step_id: Any = None
    raw_message_id: str = ""
    sent_at: str = ""
    sender_email_id: Any = None
    sender_email: str = ""
    account_type: str = ""
    dedupe_key: str = ""
    kept: bool = False
    drop_reason: str = ""
    recipient_provider: str = "unknown"
    recipient_mx: str = ""
    recipient_provider_source: str = ""
    llm_suggestion: str = ""
    decoded: Decoded = field(default_factory=Decoded)

    @property
    def sender_domain(self) -> str:
        return self.sender_email.split("@")[-1] if "@" in self.sender_email else "unknown"

    @property
    def lead_domain(self) -> str:
        return self.lead_email.split("@")[-1] if "@" in self.lead_email else ""

    @property
    def category(self) -> str:
        return self.decoded.category


@dataclass(frozen=True)
class SendCount:
    """Sends for one segment (mock of EmailBison campaign/sender stats)."""

    workspace: str
    sender_email: str
    sender_domain: str
    campaign_id: str
    campaign_name: str
    step: str
    variant: str
    list_source: str
    recipient_provider: str
    sends: int


@dataclass(frozen=True)
class Sender:
    """One inbox's limits and volume (mock of EmailBison sender emails)."""

    workspace: str
    sender_email: str
    sender_email_id: int | str
    account_type: str
    daily_limit: float
    avg_daily_sent_prev: float
    avg_daily_sent_now: float
    domain_age_days: int | None = None


@dataclass(frozen=True)
class DblResult:
    listed: bool | None   # None = lookup error (e.g. 127.255.255.254), never a listing
    code: str
    meaning: str
    source: str


@dataclass(frozen=True)
class DomainCheck:
    domain: str
    spf: bool
    dkim_selectors: tuple[str, ...]
    dmarc: bool
    dmarc_policy: str
    problems: tuple[str, ...]
    dbl: DblResult
    source: str
    checked: bool = True  # False: no DNS data for this domain (mock mode), so nothing is claimed about it

    @property
    def auth_ok(self) -> bool:
        return self.checked and not self.problems


@dataclass(frozen=True)
class Segment:
    """Bounce rate and lift for one value of one dimension, for one cause."""

    dimension: str
    key: str
    label: str
    sends: int
    bounces: int
    rate: float | None
    share_sends: float
    share_bounces: float
    lift: float | None
    hot: bool


@dataclass
class Verdict:
    workspace: str
    cause: str
    title: str
    confidence: str
    signals: dict[str, bool | None]
    rate: float
    hot: list[Segment]
    evidence: list[str]
    fix: list[str]
    do_not: list[str]
    action_kinds: list[str]
    rows: list[Bounce] = field(repr=False, default_factory=list)
    slack: dict[str, Any] = field(default_factory=dict)   # plain-English templates from rules.json
    facts: dict[str, str] = field(default_factory=dict)   # values for them, e.g. {"cap": "30"}
    unexpected: bool = False                               # hot is outside the dimension the rule expects
    also: list[Segment] = field(default_factory=list)      # a second place the hot bounces pile up on
    placed: int = 0                                        # bounces on the hot segments (all of them if none)

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def short_title(self) -> str:
        return self.title.split(":")[0]

    def class_count(self, bounce_class: str) -> int:
        return sum(1 for b in self.rows if b.decoded.bounce_class == bounce_class)


@dataclass
class Action:
    """One EmailBison API call (or a manual step). Always a dry run."""

    kind: str                  # api | manual
    operation: str
    why: str
    method: str = ""
    path: str = ""
    body: dict[str, Any] | None = None
    items: list[dict[str, Any]] | None = None
    id: str = ""
    workspace: str = ""
    cause: str = ""
    confidence: str = ""
    auto: bool = False
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if v not in (None, "")}
        if self.items is not None:
            out["count"] = len(self.items)
        return out


@dataclass
class Note:
    """A cause that did not become a verdict (watch list or baseline)."""

    cause: str
    title: str
    bounces: int
    detail: str
    key: str = ""      # the segment it is about, e.g. the outlier inbox


@dataclass(frozen=True)
class HealthBand:
    """Traffic light for one client's bounce rate."""

    band: str      # green | yellow | red
    action: str
    detail: str
    green_below: float = 0.0
    red_above: float = 0.0


@dataclass
class WorkspaceResult:
    workspace: str
    events: int
    drops: dict[str, int]
    kept: int
    total_sends: int
    verdicts: list[Verdict]
    watch: list[Note]
    baseline: list[Note]
    unknown: list[Bounce]
    actions: list[Action]
    domain_checks: dict[str, DomainCheck]
    class_counts: dict[str, int] = field(default_factory=dict)   # hard | soft | block | other
    health: HealthBand | None = None
    notes: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def bounce_rate(self) -> float | None:
        return self.kept / self.total_sends if self.total_sends else None

    def class_rate(self, bounce_class: str) -> float | None:
        return self.class_counts.get(bounce_class, 0) / self.total_sends if self.total_sends else None


@dataclass
class RunResult:
    results: list[WorkspaceResult]
    bounces: list[Bounce]
    input_errors: list[str]
    dns_mode: str
    llm_on: bool
