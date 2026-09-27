#!/usr/bin/env python3
"""Proxy score against Builderr's public 100-company sample (reference/benchmark/builderr-sample-100.json).

The official score uses a hidden pooled reference; this public sample (Builderr's own crawler output) is
the closest stand-in. Per field family: coverage = 70% company recall + 30% claim recall, as in the brief.
Also lists our extra finds (possible pool additions) and disagreements to audit for wrong-company risk.

Usage: uv run python scripts/benchmark_vs_builderr.py --envelopes out/bench/envelopes.jsonl
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
from pathlib import Path

import tldextract


def domain(url: str | None) -> str | None:
    if not url:
        return None
    host = urllib.parse.urlparse(url if "://" in url else "https://" + url).hostname or ""
    return tldextract.extract(host).top_domain_under_public_suffix or None


def person(value) -> str:
    return (" ".join(str(part) for part in value) if isinstance(value, list) else str(value or "")).casefold()


def handle(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    return f"{host}{parsed.path.rstrip('/')}".casefold()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--envelopes", required=True)
    parser.add_argument("--reference", default="reference/benchmark/builderr-sample-100.json")
    args = parser.parse_args()
    reference = {row["org"]: row for row in json.loads(Path(args.reference).read_text(encoding="utf-8"))}
    ours = {}
    for line in Path(args.envelopes).read_text(encoding="utf-8").splitlines():
        if line.strip():
            envelope = json.loads(line)
            ours[envelope["organisation_number"]] = envelope

    families: dict[str, dict[str, set]] = {}

    def add(family: str, side: str, org: str, items: set) -> None:
        families.setdefault(family, {"ref": {}, "ours": {}})[side][org] = items

    extras, disagreements = [], []
    for org, ref in reference.items():
        env = ours.get(org) or {"claims": []}
        claims = [claim for claim in env["claims"] if not claim.get("stale")]
        by = lambda family, field=None: [c for c in claims if c["family"] == family and (field is None or c["field"] == field)]  # noqa: E731

        web = ref.get("web") or {}
        assessment = ((web.get("value") or {}).get("identity_assessment") or {}) if isinstance(web.get("value"), dict) else {}
        ref_site = {domain((web.get("value") or {}).get("final_url") or web.get("source"))} if web.get("status") == "available" and assessment.get("status") == "exact" else set()
        our_site = {domain(c["value"]) for c in by("official_website", "official_website")}
        add("official_website", "ref", org, ref_site - {None})
        add("official_website", "ours", org, our_site - {None})
        if our_site and ref_site and not (our_site & ref_site):
            disagreements.append((org, ref["name"], "website", sorted(ref_site), sorted(our_site)))
        if our_site and not ref_site:
            extras.append((org, ref["name"], "website", sorted(our_site)))

        ref_handles = {handle(item["url"]) for item in (ref.get("external") or {}).get("handles") or []}
        our_handles = {handle(c["value"]) for c in by("company_profiles")}
        add("company_profiles", "ref", org, ref_handles)
        add("company_profiles", "ours", org, our_handles)
        for item in sorted(our_handles - ref_handles):
            extras.append((org, ref["name"], "profile", item))

        records = (ref.get("financial") or {}).get("records") or []
        ref_fin = {(field, str((record.get("period") or {}).get("tilDato"))) for record in records[:1]
                   for field in ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt") if record.get(field) is not None}
        our_fin = {(c["field"], (c.get("reporting_period") or "").split("..")[-1]) for c in by("annual_accounts")}
        add("annual_accounts", "ref", org, ref_fin)
        add("annual_accounts", "ours", org, our_fin)

        ref_roles = {(item.get("role_code"), person(item.get("name"))) for item in (ref.get("roles") or {}).get("items") or [] if not item.get("inactive")}
        our_roles = {((c["value"] or {}).get("role_code"), person((c["value"] or {}).get("name"))) for c in by("leadership")}
        add("leadership", "ref", org, ref_roles)
        add("leadership", "ours", org, our_roles)

        ref_locs = {item.get("organisation_number") for item in (ref.get("locations") or {}).get("items") or []}
        our_locs = {(c["value"] or {}).get("organisation_number") for c in by("registered_workplaces")}
        add("registered_workplaces", "ref", org, ref_locs - {None})
        add("registered_workplaces", "ours", org, our_locs - {None})

        linkedin = (ref.get("external") or {}).get("linkedin") or {}
        add("linkedin_profile_data", "ref", org, {"linkedin"} if linkedin.get("available") else set())
        add("linkedin_profile_data", "ours", org, set())

    print(f"{'family':<24} {'ref cos':>7} {'co recall':>9} {'claim recall':>12} {'coverage':>8}  {'our cos':>7}")
    total = 0.0
    for family, sides in families.items():
        ref_cos = [org for org, items in sides["ref"].items() if items]
        hit_cos = [org for org in ref_cos if sides["ours"].get(org, set()) & sides["ref"][org]]
        ref_claims = sum(len(sides["ref"][org]) for org in ref_cos)
        hit_claims = sum(len(sides["ours"].get(org, set()) & sides["ref"][org]) for org in ref_cos)
        company_recall = len(hit_cos) / len(ref_cos) if ref_cos else 1.0
        claim_recall = hit_claims / ref_claims if ref_claims else 1.0
        coverage = 0.7 * company_recall + 0.3 * claim_recall
        total += coverage
        our_cos = sum(1 for items in sides["ours"].values() if items)
        print(f"{family:<24} {len(ref_cos):>7} {company_recall:>9.0%} {claim_recall:>12.0%} {coverage:>8.0%}  {our_cos:>7}")
    print(f"\nunweighted mean coverage across families: {total / len(families):.0%}")
    print(f"\nOUR EXTRA FINDS not in Builderr's sample ({len(extras)}) — verify, these could add to the pool:")
    for row in extras[:40]:
        print("  ", row)
    print(f"\nWEBSITE DISAGREEMENTS ({len(disagreements)}) — audit for wrong-company risk:")
    for row in disagreements:
        print("  ", row)


if __name__ == "__main__":
    main()
