"""Evidence-bounded company summary.

Each sentence is generated only from published claims and lists the claim ids it rests on, so the
summary cannot introduce an unsupported fact. Derived statements (a trend, a ratio) cite every claim
they are computed from. Unknowns are stated explicitly from field states, with the reason.
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

SECTION_TITLES = {
    "identity": "Who it is",
    "business": "What it does",
    "finances": "Finances",
    "people": "People and locations",
    "presence": "Web presence and hiring",
}


def _fmt_amount(value: Any) -> str:
    amount = value.get("amount") if isinstance(value, dict) else value
    currency = value.get("currency") if isinstance(value, dict) else None
    if not isinstance(amount, (int, float)):
        return str(amount)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    text = f"{amount / 1e9:.1f} bn" if amount >= 1e9 else f"{amount / 1e6:.1f} m" if amount >= 1e6 else f"{amount / 1e3:.0f} k" if amount >= 1e3 else f"{amount:.0f}"
    return f"{currency or 'NOK'} {sign}{text}"


def _amount(claim: dict[str, Any] | None) -> float | None:
    value = (claim or {}).get("value")
    amount = value.get("amount") if isinstance(value, dict) else None
    return float(amount) if isinstance(amount, (int, float)) else None


def _result_phrase(claim: dict[str, Any]) -> str:
    amount = _amount(claim) or 0.0
    return f"a loss of {_fmt_amount({**claim['value'], 'amount': abs(amount)})}" if amount < 0 else _fmt_amount(claim["value"])


def _year(period: str | None) -> str:
    return str(period or "").split("..")[-1][:4]


def build_summary(envelope: dict[str, Any]) -> dict[str, Any]:
    claims = [claim for claim in envelope.get("claims") or [] if not claim.get("stale")]
    by_field: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in claims:
        by_field.setdefault((claim["family"], claim["field"]), []).append(claim)

    def first(family: str, field: str) -> dict[str, Any] | None:
        items = by_field.get((family, field))
        return items[0] if items else None

    sentences: list[dict[str, Any]] = []

    def say(section: str, text: str, *support: dict[str, Any] | None) -> None:
        ids = [claim["claim_id"] for claim in support if claim]
        if ids:
            sentences.append({"text": text, "claim_ids": ids, "section": section})

    # ---- who it is
    name = first("legal_identity", "legal_name")
    form = first("legal_identity", "legal_form")
    municipality = first("legal_identity", "municipality")
    founded = first("legal_identity", "founded_date")
    if name:
        parts = [f"{name['value']}"]
        if form:
            parts.append(f"is a {LEGAL_FORM_EN.get(str(form['value']), 'registered entity of type ' + str(form['value']))}")
        if municipality:
            parts.append(f"registered in {str(municipality['value']).title()}")
        if founded:
            parts.append(f"founded {founded['value']}")
        say("identity", " ".join(parts[:1]) + " " + ", ".join(parts[1:]) + "." if len(parts) > 1 else f"{parts[0]}.", name, form, municipality, founded)
    flags = [first("legal_identity", flag) for flag in ("bankrupt", "under_liquidation", "forced_dissolution")]
    raised = [(claim, text) for claim, text in zip(flags, ("The register flags the company as bankrupt.", "The company is under liquidation.", "The company is under forced dissolution.")) if claim and claim["value"] is True]
    for claim, text in raised:
        say("identity", text, claim)
    if not raised and all(claim and claim["value"] is False for claim in flags):
        say("identity", "The register shows no bankruptcy, liquidation or forced dissolution.", *flags)
    parents = by_field.get(("group_relationships", "parent"), [])
    subsidiaries = by_field.get(("group_relationships", "subsidiary"), [])
    if parents:
        parent = parents[0]["value"] or {}
        basis = f" ({parent.get('ownership_basis')})" if parent.get("ownership_basis") else ""
        say("identity", f"Its registered parent is {parent.get('name')}{basis}.", parents[0])
    if subsidiaries:
        names = [str((claim["value"] or {}).get("name")) for claim in subsidiaries[:3]]
        say("identity", f"It has {len(subsidiaries)} registered subsidiar{'y' if len(subsidiaries) == 1 else 'ies'}" + (f", including {', '.join(names)}." if len(subsidiaries) > 1 else f": {names[0]}."), *subsidiaries[:3])

    # ---- what it does
    industry = first("legal_identity", "industry")
    industry_label = (industry or {}).get("value", {}).get("beskrivelse") if isinstance((industry or {}).get("value"), dict) else None
    activity = first("legal_identity", "stated_activity")
    if industry_label:
        say("business", f"Its registered industry is {industry_label.lower()}.", industry)
    if activity:
        say("business", f"The registry describes its activity as: “{str(activity['value'])[:240]}”.", activity)
    brand = first("public_brand", "self_description")
    if brand:
        say("business", f"Its own website describes it as: “{str(brand['value'])[:240]}” (company-reported).", brand)

    # ---- finances: latest filed year, then the change from the year before
    def by_period(field: str) -> dict[str, dict[str, Any]]:
        return {claim.get("reporting_period") or "": claim for claim in by_field.get(("annual_accounts", field), [])}

    revenue, result, equity, assets = by_period("revenue"), by_period("annual_result"), by_period("equity"), by_period("assets")
    periods = sorted({period for series in (revenue, result, equity, assets) for period in series if period}, reverse=True)
    if periods:
        latest, previous = periods[0], periods[1] if len(periods) > 1 else None
        rev, res = revenue.get(latest), result.get(latest)
        pieces = []
        if rev:
            pieces.append(f"revenue of {_fmt_amount(rev['value'])}")
        if res:
            pieces.append(f"a net result of {_fmt_amount(res['value'])}" if (_amount(res) or 0) >= 0 else f"a net loss of {_fmt_amount({**res['value'], 'amount': abs(_amount(res) or 0)})}")
        if pieces:
            say("finances", f"Filed accounts for the period ending {latest.split('..')[-1]} show " + " and ".join(pieces) + ".", rev, res)
        old_rev, old_res = (revenue.get(previous), result.get(previous)) if previous else (None, None)
        if rev and old_rev and _amount(rev) is not None and _amount(old_rev) is not None:
            new, old = _amount(rev), _amount(old_rev)
            if old > 0 and new != old:
                say("finances", f"Revenue {'rose' if new > old else 'fell'} {abs(new - old) / old * 100:.1f}% from {_fmt_amount(old_rev['value'])} in {_year(previous)} to {_fmt_amount(rev['value'])} in {_year(latest)}.", old_rev, rev)
            elif new == old:
                say("finances", f"Revenue was unchanged at {_fmt_amount(rev['value'])} from {_year(previous)} to {_year(latest)}.", old_rev, rev)
        if res and old_res and _amount(res) is not None and _amount(old_res) is not None:
            new, old = _amount(res), _amount(old_res)
            if new != old:
                say("finances", f"The net result {'rose' if new > old else 'fell'} from {_result_phrase(old_res)} to {_result_phrase(res)}.", old_res, res)
        eq, total = equity.get(latest), assets.get(latest)
        if eq and _amount(eq) is not None:
            if _amount(eq) < 0:
                say("finances", f"Equity was negative ({_fmt_amount(eq['value'])}) at the end of {_year(latest)}.", eq)
            elif total and (_amount(total) or 0) > 0:
                say("finances", f"Equity was {_fmt_amount(eq['value'])} at the end of {_year(latest)}, {_amount(eq) / _amount(total) * 100:.0f}% of total assets.", eq, total)
            else:
                say("finances", f"Equity was {_fmt_amount(eq['value'])} at the end of {_year(latest)}.", eq)

    # ---- people and locations
    employees = first("legal_identity", "registered_employees")
    if employees:
        say("people", f"The registry reports {employees['value']} employees.", employees)
    roles = by_field.get(("leadership", "role"), [])
    leaders = [claim for claim in roles if (claim["value"] or {}).get("role_code") in {"DAGL", "LEDE", "INNH"}]
    if leaders:
        described = "; ".join(f"{(claim['value'] or {}).get('role')}: {(claim['value'] or {}).get('name')}" for claim in leaders[:3])
        say("people", f"Key registered roles — {described}.", *leaders[:3])
    board = [claim for claim in roles if (claim["value"] or {}).get("role_code") in {"MEDL", "NEST"}]
    if board:
        say("people", f"The board has {len(board) + sum(1 for claim in leaders if (claim['value'] or {}).get('role_code') == 'LEDE')} registered members including the chair.", *board)
    auditor, accountant = first("leadership", "auditor"), first("leadership", "accountant")
    if auditor or accountant:
        services = [f"its {label} is {(claim['value'] or {}).get('name')}" for label, claim in (("auditor", auditor), ("accountant", accountant)) if claim]
        text = "; ".join(services) + "."
        say("people", text[0].upper() + text[1:], auditor, accountant)
    workplaces = by_field.get(("registered_workplaces", "registered_workplace"), [])
    if len(workplaces) > 1:
        cities = sorted({str((claim["value"] or {}).get("municipality") or "").title() for claim in workplaces if (claim["value"] or {}).get("municipality")})
        say("people", f"It has {len(workplaces)} registered workplaces" + (f" in {', '.join(cities[:5])}." if cities else "."), *workplaces)
    role_changes = sorted(by_field.get(("dated_activity", "registered_role_change"), []), key=lambda claim: str(claim.get("effective_date") or ""))
    if role_changes:
        say("people", f"The register last recorded a change to its roles on {role_changes[-1].get('effective_date')}.", role_changes[-1])

    # ---- web presence and hiring
    website = first("official_website", "official_website")
    if website:
        scope = first("public_brand", "website_scope")
        if scope and scope["value"] == "page_on_third_party_site":
            say("presence", f"It has a page on a third-party site: {website['value']}.", website, scope)
        else:
            say("presence", f"Its verified website is {website['value']}.", website)
    profiles = [claim for (family, _), items in by_field.items() if family == "company_profiles" for claim in items]
    if profiles:
        say("presence", "Company-owned profiles linked from that site: " + ", ".join(sorted({claim["field"] for claim in profiles})) + ".", *profiles)
    jobs = by_field.get(("jobs", "open_job"), [])
    if jobs:
        titles = "; ".join(str((claim["value"] or {}).get("title") or "")[:80] for claim in jobs[:3])
        say("presence", f"It appears to be hiring: {len(jobs)} active NAV job ad(s), e.g. {titles}.", *jobs[:3])
    careers = first("jobs", "careers_page")
    if careers and not jobs:
        say("presence", f"Its site has a careers page: {careers['value']}.", careers)
    news = sorted(by_field.get(("dated_activity", "website_news"), []), key=lambda claim: str(claim.get("effective_date") or ""))
    if news:
        latest_news = news[-1]["value"] or {}
        say("presence", f"The latest dated item on its site is “{str(latest_news.get('title'))[:120]}” ({latest_news.get('date')}).", news[-1])

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
    unknown_text = ("Not established from checked sources: " + "; ".join(f"{item['label']} ({item['reason'] or item['state'].replace('_', ' ')})" for item in unknowns) + ".") if unknowns else None
    sections = [
        {"key": key, "title": title, "text": " ".join(sentence["text"] for sentence in sentences if sentence["section"] == key)}
        for key, title in SECTION_TITLES.items() if any(sentence["section"] == key for sentence in sentences)
    ]
    return {
        "method": "deterministic_template_v2",
        "sentences": sentences,
        "sections": sections,
        "text": " ".join(sentence["text"] for sentence in sentences),
        "changes_text": change_text,
        "unknowns": unknowns,
        "unknowns_text": unknown_text,
    }


def section_of(family: str) -> str:
    from .envelope import FIELD_FAMILIES

    return FIELD_FAMILIES.get(family, "")
