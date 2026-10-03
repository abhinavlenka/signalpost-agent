"""One-command entry point: ``uv run python -m signalpost run --input batch.txt``."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from abhikilde.pipeline import run_batch  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="signalpost", description="abhikilde: company-intelligence agent for Builderr's Signalpost challenge")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="research a batch of organisation numbers")
    run.add_argument("--input", required=True, help="txt/csv/json/jsonl file of organisation numbers")
    run.add_argument("--out", default="out", help="output directory (envelopes.jsonl, run-report.json)")
    run.add_argument("--state", default="state", help="refresh state directory (latest envelopes + snapshots)")
    run.add_argument("--previous", help="previous envelopes.jsonl (or its directory); defaults to --state/latest")
    run.add_argument("--universe", default=str(ROOT / "data" / "signalpost-universe.jsonl.gz"))
    run.add_argument("--run-id")
    env = os.environ.get
    run.add_argument("--max-requests", type=int, default=int(env("SIGNALPOST_MAX_REQUESTS", 0)) or None,
                     help="hard cap on outbound requests (default: 19 per input company; env SIGNALPOST_MAX_REQUESTS)")
    run.add_argument("--requests-per-company", type=float, default=float(env("SIGNALPOST_REQUESTS_PER_COMPANY", 19)))
    run.add_argument("--deadline-minutes", type=float, default=float(env("SIGNALPOST_DEADLINE_MINUTES", 40)),
                     help="stop fetching and write results by this wall-clock budget (env SIGNALPOST_DEADLINE_MINUTES)")
    run.add_argument("--workers", type=int, default=int(env("SIGNALPOST_WORKERS", 16)))
    run.add_argument("--accounts-lanes", type=int, default=int(env("SIGNALPOST_ACCOUNTS_LANES", 6)))
    run.add_argument("--nav-days", type=int, default=45)
    run.add_argument("--no-nav", action="store_true", help="skip the NAV job-feed connector")
    args = parser.parse_args()
    report = run_batch(
        args.input, out_dir=args.out, state_dir=args.state, universe_path=args.universe, previous=args.previous,
        run_id=args.run_id, max_requests=args.max_requests, requests_per_company=args.requests_per_company,
        deadline_minutes=args.deadline_minutes, workers=args.workers, accounts_lanes=args.accounts_lanes,
        nav_days=args.nav_days, use_nav=not args.no_nav,
    )
    sys.exit(0 if all(report["validation"].values()) else 1)


if __name__ == "__main__":
    main()
