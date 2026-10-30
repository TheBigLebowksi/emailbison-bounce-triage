"""Webhook receiver: point EmailBison's 'Email Bounced' webhook at POST /webhook.

Each POST is appended to <state>/events.jsonl, the pipeline reruns and out/ is
rewritten. Valid JSON always gets a 200 (duplicates included) so EmailBison
stops retrying. Slack is posted only when a verdict is new or its confidence
changed, and only if SLACK_WEBHOOK_URL is set.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import report
from .ingest import normalise
from .models import RunResult
from .pipeline import triage


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    inputs: Path
    state: Path
    out: Path
    dns: str = "mock"
    token: str | None = None


def _workspace(event: Any) -> str | None:
    try:
        return normalise(event).workspace
    except (ValueError, TypeError, AttributeError):
        return None


class TriageState:
    """Event log + which verdicts have already been posted to Slack."""

    def __init__(self, cfg: ServerConfig) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        cfg.state.mkdir(parents=True, exist_ok=True)
        self.events_path = cfg.state / "events.jsonl"
        self.posted_path = cfg.state / "posted.json"

    def _posted(self) -> set[str]:
        try:
            return set(json.loads(self.posted_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()

    def receive(self, events: list[Any]) -> RunResult:
        with self.lock:
            with self.events_path.open("a", encoding="utf-8", newline="\n") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")
            result, messages = triage(self.events_path, self.cfg.inputs, self.cfg.out, self.cfg.dns, mode="serve")
            posted = self._posted()
            current = {f"{v.workspace}:{v.cause}:{v.confidence}"
                       for r in result.results for v in r.verdicts if v.cause != "noise"}
            changed = {k.split(":")[0] for k in current - posted}
            if changed:
                report.post_slack(messages, only=changed)
            self.posted_path.write_text(json.dumps(sorted(current | posted)), encoding="utf-8")
            return result


def make_handler(state: TriageState) -> type[BaseHTTPRequestHandler]:
    cfg = state.cfg

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            return not cfg.token or parse_qs(urlparse(self.path).query).get("token", [""])[0] == cfg.token

        def do_GET(self) -> None:
            if urlparse(self.path).path in ("/report", "/report.md"):
                if not self._authorised():
                    return self._send(401, {"error": "bad token"})
                path = cfg.out / "report.md"
                if path.exists():
                    return self._send(200, path.read_text(encoding="utf-8"), "text/markdown; charset=utf-8")
                return self._send(404, {"error": "no report yet - POST a bounce first"})
            return self._send(200, {"status": "ok", "post_webhooks_to": "/webhook", "report": "/report"})

        def do_POST(self) -> None:
            if not self._authorised():
                return self._send(401, {"error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
            except ValueError:  # bad Content-Length, not JSON, or not UTF-8
                return self._send(400, {"error": "body is not JSON"})
            events = body if isinstance(body, list) else [body]
            try:
                result = state.receive(events)
            except Exception as exc:  # the event is already stored: answer 200 so EmailBison stops retrying
                sys.stderr.write(f"[serve] pipeline error, event kept in {cfg.state}/events.jsonl: {exc!r}\n")
                return self._send(200, {"received": len(events), "error": "stored, but the rerun failed"})
            touched = {_workspace(e) for e in events}
            summary = [{"workspace": r.workspace, "bounce_rate": r.bounce_rate,
                        "verdicts": [(v.cause, v.confidence) for v in r.verdicts]}
                       for r in result.results if r.workspace in touched]
            return self._send(200, {"received": len(events), "clients": summary})

        def log_message(self, fmt: str, *args: Any) -> None:
            line = fmt % args
            if cfg.token:
                line = line.replace(cfg.token, "***")
            sys.stderr.write("[serve] " + line + "\n")

    return Handler


def serve(cfg: ServerConfig) -> None:
    server = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(TriageState(cfg)))
    print(f"Bounce Triage listening on http://{cfg.host}:{cfg.port}/webhook "
          f"(report at /report, state in {cfg.state}/, outputs in {cfg.out}/)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def config_from_env(**kwargs: Any) -> ServerConfig:
    return ServerConfig(token=os.environ.get("WEBHOOK_TOKEN") or None, **kwargs)
