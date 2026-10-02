"""Evidence-bounded company summary.

Each sentence is generated only from published claims and lists the claim ids it rests on, so the
summary cannot introduce an unsupported fact. Unknowns are stated explicitly from field states.
"""
from __future__ import annotations

from typing import Any

LEGAL_FORM_EN = {
    "AS": "private limited company (AS)", "ASA": "public limited company (ASA)", "ENK": "sole proprietorship (ENK)",
    "ANS": "general partnership (ANS)", "DA": "partnership with shared liability (DA)", "NUF": "Norwegian branch of a foreign company (NUF)",
    "SA": "cooperative (SA)", "STI": "foundation", "BRL": "housing cooperative (BRL)", "ESEK": "owner-section condominium (sameie)",
    "FLI": "association (FLI)", "KS": "limited partnership (KS)", "IKS": "inter-municipal company (IKS)", "SPA": "savings bank",
}

FAMILY_LABELS = {
    "legal_identity": "legal identity",
    "public_brand": "public brand",
    "group_relationships": "group relationships",
    "annual_accounts": "annual accounts",
    "filing_history": "filing history",
    "leadership": "leadership",
    "registered_workplaces": "registered workplaces",
    "official_website": "official website",
    "company_profiles": "company-owned social profiles",
    "jobs": "job postings",
    "dated_activity": "dated public activity",
}


def _fmt_amount(value: Any) -> str:
    amount = value.get("amount") if isinstance(value, dict) else value
    currency = value.get("currency") if isinstance(value, dict) else None
    if not isinstance(amount, (int, float)):
        return str(amount)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    text = f"{amount / 1e9:.1f} bn" if amount >= 1e9 else f"{amount / 1e6:.1f} m" if amount >= 1e6 else f"{amount / 1e3:.0f} k" if amount >= 1e3 else f"{amount:.0f}"
    return f"{sign}{currency or 'NOK'} {text}"


def build_summary(envelope: dict[str, Any]) -> dict[str, Any]:
    claims = [claim for claim in envelope.get("claims") or [] if not claim.get("stale")]
    by_field: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in claims:
        by_field.setdefault((claim["family"], claim["field"]), []).append(claim)

    def first(family: str, field: str) -> dict[str, Any] | None:
        items = by_field.get((family, field))
        return items[0] if items else None

    sentences: list[dict[str, Any]] = []

    def say(text: str, *support: dict[str, Any] | None) -> None:
        ids = [claim["claim_id"] for claim in support if claim]
        if ids:
            sentences.append({"text": text, "claim_ids": ids})

    name = first("legal_identity", "legal_name")
    form = first("legal_identity", "legal_form")
    municipality = first("legal_identity", "municipality")
    founded = first("legal_identity", "founded_date")
    industry = first("legal_identity", "industry")
    if name:
        parts = [f"{name['value']}"]
        if form:
            parts.append(f"is a {LEGAL_FORM_EN.get(str(form['value']), 'registered entity of type ' + str(form['value']))}")
        if municipality:
            parts.append(f"registered in {str(municipality['value']).title()}")
        if founded:
            parts.append(f"founded {founded['value']}")
        say(" ".join(parts[:1]) + " " + ", ".join(parts[1:]) + "." if len(parts) > 1 else f"{parts[0]}.", name, form, municipality, founded)
    industry_label = (industry or {}).get("value", {}).get("beskrivelse") if isinstance((industry or {}).get("value"), dict) else None
    activity = first("legal_identity", "stated_activity")
    if industry_label:
        say(f"Its registered industry is {industry_label.lower()}.", industry)
    if activity:
        say(f"The registry describes its activity as: “{str(activity['value'])[:240]}”.", activity)
    brand = first("public_brand", "self_description")
    if brand:
        say(f"Its own website describes it as: “{str(brand['value'])[:240]}” (company-reported).", brand)
    employees = first("legal_identity", "registered_employees")
    if employees:
        say(f"The registry reports {employees['value']} employees.", employees)
    for flag, text in (("bankrupt", "The registry flags the company as bankrupt."), ("under_liquidation", "The company is under liquidation."), ("forced_dissolution", "The company is under forced dissolution.")):
        claim = first("legal_identity", flag)
        if claim and claim["value"] is True:
            say(text, claim)

    leaders = [claim for claim in by_field.get(("leadership", "role"), []) if (claim["value"] or {}).get("role_code") in {"DAGL", "LEDE", "INNH"}]
    if leaders:
        described = "; ".join(f"{(claim['value'] or {}).get('role')}: {(claim['value'] or {}).get('name')}" for claim in leaders[:3])
        say(f"Key registered roles — {described}.", *leaders[:3])
    board = [claim for claim in by_field.get(("leadership", "role"), []) if (claim["value"] or {}).get("role_code") in {"MEDL", "NEST"}]
    if board:
        say(f"The board has {len(board) + sum(1 for claim in leaders if (claim['value'] or {}).get('role_code') == 'LEDE')} registered members including the chair.", *board)
    workplaces = by_field.get(("registered_workplaces", "registered_workplace"), [])
    if len(workplaces) > 1:
        cities = sorted({str((claim["value"] or {}).get("municipality") or "").title() for claim in workplaces if (claim["value"] or {}).get("municipality")})
        say(f"It has {len(workplaces)} registered workplaces" + (f" in {', '.join(cities[:5])}." if cities else "."), *workplaces)

    revenue = [claim for claim in by_field.get(("annual_accounts", "revenue"), [])]
    result = [claim for claim in by_field.get(("annual_accounts", "annual_result"), [])]
    if revenue or result:
        latest_period = max([claim.get("reporting_period") or "" for claim in revenue + result])
        rev = next((claim for claim in revenue if claim.get("reporting_period") == latest_period), None)
        res = next((claim for claim in result if claim.get("reporting_period") == latest_period), None)
        pieces = []
        if rev:
            pieces.append(f"revenue of {_fmt_amount(rev['value'])}")
        if res:
            pieces.append(f"a net result of {_fmt_amount(res['value'])}")
        period_end = latest_period.split("..")[-1] if latest_period else "the latest period"
        say(f"Filed accounts for the period ending {period_end} show " + " and ".join(pieces) + ".", rev, res)

    website = first("official_website", "official_website")
    if website:
        scope = first("public_brand", "website_scope")
        if scope and scope["value"] == "page_on_third_party_site":
            say(f"It has a page on a third-party site: {website['value']}.", website, scope)
        else:
            say(f"Its verified website is {website['value']}.", website)
    profiles = [claim for (family, _), items in by_field.items() if family == "company_profiles" for claim in items]
    if profiles:
        say("Company-owned profiles linked from that site: " + ", ".join(sorted({claim["field"] for claim in profiles})) + ".", *profiles)
    jobs = by_field.get(("jobs", "open_job"), [])
    if jobs:
        titles = "; ".join(str((claim["value"] or {}).get("title") or "")[:80] for claim in jobs[:3])
        say(f"It appears to be hiring: {len(jobs)} active NAV job ad(s), e.g. {titles}.", *jobs[:3])

    changes = envelope.get("changes") or []
    material = [change for change in changes if change.get("material")]
    change_text = None
    if envelope.get("refresh", {}).get("mode") == "diff":
        if changes:
            kinds: dict[str, int] = {}
            for change in changes:
                kinds[change["change_type"]] = kinds.get(change["change_type"], 0) + 1
            change_text = f"Since the previous run: {len(changes)} change(s) ({', '.join(f'{count}× {kind}' for kind, count in sorted(kinds.items()))}); {len(material)} material."
        else:
            change_text = "No changes since the previous run."

    availability = envelope.get("availability") or {}
    unknowns = [
        {"family": family, "label": FAMILY_LABELS.get(family, family), "state": state, "reason": (envelope.get("sections") or {}).get(section_of(family), {}).get("fields", {}).get(family, {}).get("reason")}
        for family, state in availability.items() if state not in {"available", "not_applicable"}
    ]
    unknown_text = ("Not established from checked sources: " + ", ".join(f"{item['label']} ({item['state']})" for item in unknowns) + ".") if unknowns else None
    return {
        "method": "deterministic_template_v1",
        "sentences": sentences,
        "text": " ".join(sentence["text"] for sentence in sentences),
        "changes_text": change_text,
        "unknowns": unknowns,
        "unknowns_text": unknown_text,
    }


def section_of(family: str) -> str:
    from .envelope import FIELD_FAMILIES

    return FIELD_FAMILIES.get(family, "")
