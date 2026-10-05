#!/usr/bin/env python3
"""Export published external claims for a manual wrong-company audit, plus coverage totals.

Usage: uv run python scripts/audit_sample.py --envelopes out/envelopes.jsonl --output out/audit.csv [--sample 40]
Review each row: does the URL/job/profile really belong to the organisation number? Mark verdicts in
the `verdict` column (ok / wrong_company / unsupported) and keep the file as the audit record.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from pathlib import Path

EXTERNAL_FAMILIES = {"official_website", "company_profiles", "jobs", "public_brand"}
EXTERNAL_FIELDS = {"dated_news", "job_posted"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample", type=int, default=0, help="random sample size (0 = all)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    rows, coverage, companies = [], Counter(), 0
    for line in Path(args.envelopes).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = json.loads(line)
        companies += 1
        evidence = {item["evidence_id"]: item for item in envelope.get("evidence") or []}
        for family, state in (envelope.get("availability") or {}).items():
            if state == "available":
                coverage[family] += 1
        for claim in envelope.get("claims") or []:
            if claim["family"] not in EXTERNAL_FAMILIES and claim["field"] not in EXTERNAL_FIELDS:
                continue
            if claim["family"] == "public_brand" and claim["field"] == "previous_legal_name":
                continue
            source = evidence.get((claim.get("evidence_ids") or [None])[0]) or {}
            rows.append({
                "organisation_number": envelope["organisation_number"],
                "legal_name": envelope.get("legal_name"),
                "family": claim["family"],
                "field": claim["field"],
                "value": json.dumps(claim["value"], ensure_ascii=False)[:300],
                "confidence": claim.get("confidence"),
                "proof": (claim.get("claim_span") or source.get("note") or "")[:300],
                "source_url": source.get("final_url") or source.get("source_url"),
                "source_class": source.get("source_class"),
                "verdict": "",
            })
    if args.sample and len(rows) > args.sample:
        rows = random.Random(args.seed).sample(rows, args.sample)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["organisation_number"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "companies": companies,
        "audit_rows": len(rows),
        "company_coverage_by_family": {family: f"{count}/{companies} ({count / companies:.0%})" for family, count in sorted(coverage.items())},
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
