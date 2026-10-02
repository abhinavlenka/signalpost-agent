#!/usr/bin/env python3
"""Build the static Signalpost explorer from one or more envelopes.jsonl files (see norway_company_agent.site)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.site import build_site  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", nargs="+", required=True, help="one or more envelopes.jsonl files (later files win)")
    parser.add_argument("--site", default="site")
    args = parser.parse_args()
    envelopes = [json.loads(line) for path in args.envelopes for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    print(json.dumps(build_site(envelopes, args.site)))


if __name__ == "__main__":
    main()
