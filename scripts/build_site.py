#!/usr/bin/env python3
"""Build the static Signalpost explorer (GitHub Pages ready) from terminal envelopes.

Output:
  <site>/index.html            single-page app (search, filters, compare, evidence drill-down)
  <site>/data/index.json       compact list rows
  <site>/data/c/<org>.json     full envelope per company (loaded on demand)
  <site>/manifest.txt          exact organisation-number manifest
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def amount(claims: list[dict], field: str):
    values = [claim for claim in claims if claim.get("family") == "annual_accounts" and claim.get("field") == field and not claim.get("stale")]
    if not values:
        return None
    latest = max(values, key=lambda claim: claim.get("reporting_period") or "")
    return (latest.get("value") or {}).get("amount")


def row(envelope: dict) -> dict:
    claims = envelope.get("claims") or []
    by = lambda family, field: next((claim.get("value") for claim in claims if claim.get("family") == family and claim.get("field") == field), None)  # noqa: E731
    industry = by("legal_identity", "industry")
    return {
        "o": envelope["organisation_number"],
        "n": envelope.get("legal_name") or by("legal_identity", "legal_name"),
        "f": by("legal_identity", "legal_form"),
        "m": by("legal_identity", "municipality"),
        "i": (industry or {}).get("beskrivelse") if isinstance(industry, dict) else None,
        "e": by("legal_identity", "registered_employees"),
        "r": amount(claims, "revenue"),
        "p": amount(claims, "annual_result"),
        "w": by("official_website", "official_website"),
        "j": sum(1 for claim in claims if claim.get("field") == "open_job" and not claim.get("stale")),
        "c": len(envelope.get("changes") or []),
        "s": envelope.get("state"),
        "a": envelope.get("availability") or {},
        "k": len(claims),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", nargs="+", required=True, help="one or more envelopes.jsonl files (later files win)")
    parser.add_argument("--site", default="site")
    args = parser.parse_args()
    envelopes: dict[str, dict] = {}
    for path in args.envelopes:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if len(str(item.get("organisation_number") or "")) == 9:
                    envelopes[item["organisation_number"]] = item
    site = Path(args.site)
    (site / "data" / "c").mkdir(parents=True, exist_ok=True)
    rows = [row(envelope) for envelope in envelopes.values()]
    rows.sort(key=lambda item: (-(item["k"] or 0), item["n"] or ""))
    (site / "data" / "index.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for org, envelope in envelopes.items():
        (site / "data" / "c" / f"{org}.json").write_text(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (site / "manifest.txt").write_text("\n".join(sorted(envelopes)) + "\n", encoding="utf-8")
    template = (Path(__file__).resolve().parent / "site_template.html").read_text(encoding="utf-8")
    (site / "index.html").write_text(template, encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    print(json.dumps({"site": str(site), "companies": len(rows)}))


if __name__ == "__main__":
    main()
