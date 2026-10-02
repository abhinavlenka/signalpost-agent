"""Static Signalpost explorer (GitHub Pages ready, and openable straight from disk) built from terminal envelopes.

Output:
  <site>/index.html            single-page app (search, filters, compare, evidence drill-down)
  <site>/data/index.js         compact list rows
  <site>/data/c/<org>.js       full envelope per company (loaded on demand)
  <site>/manifest.txt          exact organisation-number manifest
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

TEMPLATE = Path(__file__).resolve().parent / "site_template.html"


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


def _js(value: Any) -> str:
    # "</" must not appear inside a <script>-loaded file's string literals.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def build_site(envelopes: Iterable[dict], site: str | Path) -> dict[str, Any]:
    """Write the explorer for these envelopes (later duplicates win). Returns {"site", "companies"}."""
    site = Path(site)
    by_org = {item["organisation_number"]: item for item in envelopes if len(str(item.get("organisation_number") or "")) == 9}
    (site / "data" / "c").mkdir(parents=True, exist_ok=True)
    rows = [row(envelope) for envelope in by_org.values()]
    rows.sort(key=lambda item: (-(item["k"] or 0), item["n"] or ""))
    (site / "data" / "index.js").write_text(f"window.SP_INDEX={_js(rows)};\n", encoding="utf-8")
    for org, envelope in by_org.items():
        (site / "data" / "c" / f"{org}.js").write_text(f'window.SP_COMPANY["{org}"]={_js(envelope)};\n', encoding="utf-8")
    (site / "manifest.txt").write_text("\n".join(sorted(by_org)) + "\n", encoding="utf-8")
    (site / "index.html").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    (site / ".nojekyll").write_text("", encoding="utf-8")
    return {"site": str(site), "companies": len(rows)}
