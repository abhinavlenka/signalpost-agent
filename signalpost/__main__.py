"""One-command entry point: ``uv run python -m signalpost run --input batch.txt``."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.pipeline import run_batch  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(prog="signalpost", description="Signalpost company-intelligence agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="research a batch of organisation numbers")
    run.add_argument("--input", required=True, help="txt/csv/json/jsonl file of organisation numbers")
    run.add_argument("--out", default="out", help="output directory (envelopes.jsonl, run-report.json)")
    run.add_argument("--state", default="state", help="refresh state directory (latest envelopes + snapshots)")
    run.add_argument("--previous", help="previous envelopes.jsonl (or its directory); defaults to --state/latest")
    run.add_argument("--universe", default=str(ROOT / "data" / "signalpost-universe.jsonl.gz"))
    run.add_argument("--run-id")
    run.add_argument("--max-requests", type=int, default=1900)
    run.add_argument("--deadline-minutes", type=float, default=38.0)
    run.add_argument("--workers", type=int, default=12)
    run.add_argument("--nav-days", type=int, default=45)
    run.add_argument("--no-nav", action="store_true", help="skip the NAV job-feed connector")
    args = parser.parse_args()
    report = run_batch(
        args.input, out_dir=args.out, state_dir=args.state, universe_path=args.universe, previous=args.previous,
        run_id=args.run_id, max_requests=args.max_requests, deadline_minutes=args.deadline_minutes,
        workers=args.workers, nav_days=args.nav_days, use_nav=not args.no_nav,
    )
    sys.exit(0 if all(report["validation"].values()) else 1)


if __name__ == "__main__":
    main()
