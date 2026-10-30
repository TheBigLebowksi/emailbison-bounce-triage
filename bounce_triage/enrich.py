"""Step 3: enrich with DNS facts that do not come from the bounce itself.

- recipient provider from the lead domain's MX
- SPF / DKIM / DMARC on each sending domain
- Spamhaus DBL on sending domains and on link domains in the copy

Every value carries a source: 'live', 'mock' or 'unavailable'. Mock answers
are used with --dns mock (the default) or when a live lookup fails.
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .models import DblResult, DomainCheck

PROVIDER_MX = [
    ("google", re.compile(r"google\.com$|googlemail\.com$", re.I)),
    ("microsoft", re.compile(r"outlook\.com$|hotmail\.com$", re.I)),
    ("yahoo", re.compile(r"yahoodns\.net$|yahoo\.com$|aol\.com$", re.I)),
    ("proofpoint", re.compile(r"pphosted\.com$|ppe-hosted\.com$", re.I)),
    ("mimecast", re.compile(r"mimecast\.com$|mimecast-offshore\.com$", re.I)),
    ("barracuda", re.compile(r"barracudanetworks\.com$|barracuda\.com$", re.I)),
]
# DKIM selectors are guessed; production should read s= from a sent message's DKIM-Signature.
DKIM_SELECTORS = ("google", "selector1", "selector2", "default", "k1", "s1", "mail")
DBL_LISTINGS = {
    "127.0.1.2": "spam domain", "127.0.1.4": "phish domain", "127.0.1.5": "malware domain",
    "127.0.1.6": "botnet C&C domain", "127.0.1.102": "abused legit spam",
    "127.0.1.103": "abused spammed redirector", "127.0.1.104": "abused legit phish",
    "127.0.1.105": "abused legit malware", "127.0.1.106": "abused legit botnet C&C",
}
DBL_ERRORS = {
    "127.255.255.252": "typo in DNSBL name",
    "127.255.255.254": "query via public/open resolver - not a listing",
    "127.255.255.255": "excessive number of queries",
}
DOH_URL = "https://cloudflare-dns.com/dns-query"
RTYPE_CODES = {"A": 1, "MX": 15, "TXT": 16}
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.I)


def provider_from_mx(hosts: list[str]) -> str:
    for host in hosts:
        name = host.split()[-1].rstrip(".")
        for provider, pattern in PROVIDER_MX:
            if pattern.search(name):
                return provider
    return "other" if hosts else "unknown"


class Resolver:
    """DNS and DBL lookups: dnspython if installed, else DNS-over-HTTPS, else the mock files."""

    def __init__(self, mode: str = "mock", dns_mock: dict[str, Any] | None = None,
                 dbl_mock: dict[str, Any] | None = None) -> None:
        self.mode = mode
        self.dns_mock = dns_mock or {}
        self.dbl_mock = dbl_mock or {}
        self._cache: dict[tuple[str, str], Any] = {}

    @classmethod
    def from_dir(cls, mode: str, directory: Path) -> Resolver:
        def load(name: str) -> dict[str, Any] | None:
            path = directory / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        return cls(mode, load("dns_mock.json"), load("dbl_mock.json"))

    def knows(self, domain: str) -> bool:
        """Live mode can look anything up; mock mode only knows domains in dns_mock.json."""
        domain = domain.lower()
        return self.mode == "live" or any(n == domain or n.endswith("." + domain)
                                          for table in self.dns_mock.values() for n in table)

    def lookup(self, name: str, rtype: str) -> tuple[list[str], str]:
        """Return (answers, source). NXDOMAIN and NoAnswer are an empty list."""
        key = (name.lower(), rtype)
        if key not in self._cache:
            self._cache[key] = self._lookup(name.lower(), rtype)
        return self._cache[key]

    def _lookup(self, name: str, rtype: str) -> tuple[list[str], str]:
        if self.mode == "live":
            try:
                return _live_lookup(name, rtype), "live"
            except OSError:
                pass  # offline or blocked: fall back to the mock answer
        table = self.dns_mock.get(rtype, {})
        if name in table:
            return table[name], "mock"
        return [], "mock" if self.mode == "mock" else "unavailable"

    def dbl(self, domain: str) -> DblResult:
        key = (domain.lower(), "DBL")
        if key not in self._cache:
            self._cache[key] = self._dbl(domain.lower())
        return self._cache[key]

    def _dbl(self, domain: str) -> DblResult:
        if self.mode == "live":
            dqs_key = os.environ.get("SPAMHAUS_DQS_KEY")
            zone = f"{dqs_key}.dbl.dq.spamhaus.net" if dqs_key else "dbl.spamhaus.org"
            try:
                return dbl_result(socket.gethostbyname_ex(f"{domain}.{zone}")[2],
                                  "live" if dqs_key else "live:public-mirror")
            except socket.gaierror:
                return dbl_result([], "live")  # NXDOMAIN = not listed
            except OSError:
                pass  # offline: fall back to the mock answer
        code = self.dbl_mock.get("listed", {}).get(domain)
        return dbl_result([code] if code else [], "mock")


def _live_lookup(name: str, rtype: str) -> list[str]:
    try:
        import dns.resolver  # optional extra
    except ImportError:
        return _doh_lookup(name, rtype)
    try:
        answers = dns.resolver.resolve(name, rtype, lifetime=4)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as exc:
        raise OSError(str(exc)) from exc
    if rtype == "TXT":
        return [b"".join(r.strings).decode("utf-8", "replace") for r in answers]
    if rtype == "MX":
        return [f"{r.preference} {r.exchange.to_text()}" for r in answers]
    return [r.to_text() for r in answers]


def _doh_lookup(name: str, rtype: str) -> list[str]:
    """Standard-library fallback: DNS-over-HTTPS JSON API."""
    url = f"{DOH_URL}?{urllib.parse.urlencode({'name': name, 'type': rtype})}"
    req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.load(resp)
    return [a["data"].strip('"').replace('" "', "") for a in body.get("Answer", [])
            if a.get("type") == RTYPE_CODES[rtype]]


def dbl_result(ips: list[str], source: str) -> DblResult:
    """Interpret Spamhaus DBL answers. 127.255.255.x is an error code, never a listing."""
    for ip in ips:
        if ip in DBL_ERRORS:
            return DblResult(None, ip, DBL_ERRORS[ip], source)
        if ip.startswith("127.0.1."):
            return DblResult(True, ip, DBL_LISTINGS.get(ip, "listed"), source)
    return DblResult(False, "", "not listed", source)


def recipient_provider(domain: str, resolver: Resolver) -> tuple[str, str, str]:
    """(provider, top MX hosts, source) for a lead domain."""
    if not domain:
        return "unknown", "", "unavailable"
    hosts, source = resolver.lookup(domain, "MX")
    return provider_from_mx(hosts), " ".join(h.split()[-1] for h in hosts[:2]), source


def check_sending_domain(domain: str, resolver: Resolver) -> DomainCheck:
    """SPF, DKIM (common selectors), DMARC and DBL for one sending domain."""
    if not resolver.knows(domain):
        return DomainCheck(domain=domain, spf=False, dkim_selectors=(), dmarc=False, dmarc_policy="", problems=(),
                           dbl=resolver.dbl(domain), source="no mock data", checked=False)
    txt, src_spf = resolver.lookup(domain, "TXT")
    spf = [t for t in txt if t.lower().startswith("v=spf1")]
    dmarc_txt, src_dmarc = resolver.lookup(f"_dmarc.{domain}", "TXT")
    dmarc = [t for t in dmarc_txt if t.lower().startswith("v=dmarc1")]
    policy_match = re.search(r"\bp\s*=\s*(\w+)", dmarc[0], re.I) if dmarc else None
    sources, dkim = {src_spf, src_dmarc}, []
    for selector in DKIM_SELECTORS:
        records, src = resolver.lookup(f"{selector}._domainkey.{domain}", "TXT")
        sources.add(src)
        if any(re.search(r"\bp=[A-Za-z0-9+/]{20,}", r) for r in records):
            dkim.append(selector)
    problems = []
    if len(spf) != 1:
        problems.append("multiple SPF records" if spf else "SPF missing")
    if not dkim:
        problems.append("DKIM missing on common selectors")
    if not dmarc:
        problems.append("DMARC missing")
    elif not policy_match:
        problems.append("DMARC has no p= policy")
    return DomainCheck(
        domain=domain, spf=bool(spf), dkim_selectors=tuple(dkim), dmarc=bool(dmarc),
        dmarc_policy=policy_match.group(1).lower() if policy_match else "", problems=tuple(problems),
        dbl=resolver.dbl(domain), source="+".join(sorted(sources)),
    )


def link_domains(text: str) -> list[str]:
    return sorted({m.group(1).lower() for m in URL_RE.finditer(text or "")})


def variant_link_domains(campaigns: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[str]]:
    """Sequence-step copy -> {(campaign_id, step, variant): link domains}."""
    return {(str(c["id"]), str(s["order"]), str(s["variant"])): link_domains(f"{s.get('subject', '')} {s.get('body', '')}")
            for c in campaigns for s in c.get("steps", [])}
