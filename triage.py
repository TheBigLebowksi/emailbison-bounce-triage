"""Bounce Triage: tells you WHY bounces went up in EmailBison, and what to do about it.

  python triage.py replay samples/            # reproduce the sample run into out/
  python triage.py serve --port 8080          # receive EmailBison 'Email Bounced' webhooks live
  python -m unittest                          # tests

See README.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Bounce Triage needs Python 3.10 or newer.")

from bounce_triage import report  # noqa: E402
from bounce_triage.pipeline import triage  # noqa: E402
from bounce_triage.server import config_from_env, serve  # noqa: E402


def cmd_replay(args: argparse.Namespace) -> None:
    samples, out = Path(args.samples), Path(args.out)
    result, messages = triage(samples / "bounces.jsonl", samples, out, args.dns, mode="replay")
    print(report.terminal_summary(result))
    sent = report.post_slack(messages)
    print(f"\nWrote {out.as_posix()}/bounces_decoded.csv, report.md, actions.json, slack.md"
          + (f"; posted {sent} Slack message(s)" if sent else ""))


def cmd_serve(args: argparse.Namespace) -> None:
    serve(config_from_env(host=args.host, port=args.port, inputs=Path(args.inputs), state=Path(args.state),
                          out=Path(args.out), dns=args.dns))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="run over a folder of sample inputs")
    rp.add_argument("samples", help="folder with bounces.jsonl, sends.csv (+ optional senders.csv, "
                                    "campaigns.json, dns_mock.json, dbl_mock.json)")
    rp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("serve", help="receive EmailBison webhooks and rewrite outputs on each one")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--inputs", default="samples", help="folder with sends.csv etc. (mocked API data)")
    sp.add_argument("--state", default="state", help="where received webhook events are stored")
    sp.set_defaults(func=cmd_serve)

    for sp_ in (rp, sp):
        sp_.add_argument("--out", default="out")
        sp_.add_argument("--dns", choices=["mock", "live"], default="mock",
                         help="mock (default, reproducible) or live DNS/DBL lookups with mock fallback")
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
