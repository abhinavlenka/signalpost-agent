from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

from .evidence import evidence
from .http import FetchResult, fetch_json

ACCOUNTING_OBLIGATION_SOURCE = "https://www.brreg.no/en/submission-of-annual-accounts/reporting-obligations-to-the-register-of-company-accounts/who-has-an-accounting-obligation/"
ACCOUNTING_RULESET_VERSION = "brreg_accounting_obligation_rules_2024-08-09_v1"
ALWAYS_ACCOUNTING_OBLIGED_FORMS = {"AS", "ASA", "BRL", "BBL", "STI", "SF", "VPFO"}
THRESHOLD_OR_ACTIVITY_FORMS = {"ENK", "ANS", "DA", "SA", "FLI", "ESEK", "NUF", "UTLA", "ORGL", "SAM", "SPA", "KS", "BO"}

BRREG_ENTITY = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
BRREG_ROLES = BRREG_ENTITY + "/roller"
BRREG_GROUP = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"
BRREG_SUBUNITS = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}&size=1000"
BRREG_ACCOUNTS = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
BRREG_ACCOUNT_YEARS = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar"
BRREG_ACCOUNT_PDF = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/{year}"
BRREG_ROLE_EVENTS = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/roller?organisasjonsnummer={org}&afterTime=2000-01-01T00:00:00.000Z&size=200"
# The accounts service answers ~half of requests with bare load-balancer 503s in bursts of a few
# seconds, independent of the organisation. Concurrency makes it worse, so accounts calls go through a
# narrow lane with short, steady, jittered retries instead of exponential backoff.
ACCOUNTS_LANE = threading.BoundedSemaphore(2)


def set_accounts_lanes(lanes: int) -> None:
    global ACCOUNTS_LANE
    ACCOUNTS_LANE = threading.BoundedSemaphore(max(1, lanes))
ACCOUNTS_ATTEMPTS = 8
FLAKY_MODULE_RETRIES = {"financial_history": {"attempts": 4, "backoff": 1.5}}


def fetch_accounts(url: str, purpose: str) -> FetchResult:
    result: FetchResult | None = None
    for attempt in range(ACCOUNTS_ATTEMPTS):
        with ACCOUNTS_LANE:
            result = fetch_json(url, attempts=1, purpose=purpose)
        if result.status in {200, 404, 410} or "budget_exhausted" in str(result.error or ""):
            break
        time.sleep(1.5 + random.uniform(0, 2.0))
    assert result is not None
    result.attempts = attempt + 1
    return result

_history_lock = threading.Lock()
_history_last_request = 0.0


def accounting_obligation_assessment(profile: dict[str, Any]) -> dict[str, Any]:
    """Classify the rule path without inventing a definitive exemption.

    Brreg's public rule is categorical for some forms and threshold/activity dependent for others.
    Registry employee counts are not treated as equivalent to statutory man-years or asset/revenue tests.
    """
    legal_form = str(profile.get("legal_form") or "").upper()
    latest = profile.get("latest_submitted_accounts")
    if latest:
        classification = "filing_observed"
        reason = "The registry snapshot reports a latest submitted annual-account year."
    elif legal_form in ALWAYS_ACCOUNTING_OBLIGED_FORMS:
        classification = "required_by_legal_form"
        reason = f"Brreg lists organisation form {legal_form} in an always-obliged category."
    elif legal_form in THRESHOLD_OR_ACTIVITY_FORMS:
        classification = "threshold_or_activity_dependent"
        reason = "Obligation depends on statutory size, partner, activity, tax or supervision tests not fully present in the open entity row."
    else:
        classification = "special_rule_or_review_required"
        reason = "The open entity row is insufficient for a definitive accounting-obligation decision."
    ruleset_hash = __import__("hashlib").sha256(ACCOUNTING_RULESET_VERSION.encode()).hexdigest()
    return evidence(
        "accounting_obligation",
        "available",
        "official_rule_interpretation",
        ACCOUNTING_OBLIGATION_SOURCE,
        value={
            "classification": classification,
            "legal_form": legal_form or None,
            "latest_submitted_accounts": latest or None,
            "reason": reason,
            "ruleset_version": ACCOUNTING_RULESET_VERSION,
        },
        as_of="2024-08-09",
        content_sha256=ruleset_hash,
        source_row_key=str(profile.get("organisation_number") or "") or None,
    )


def _get(value: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def normalize_financials(body: Any) -> dict[str, Any]:
    records = body if isinstance(body, list) else []
    if not records:
        return {"records": []}
    normalized = []
    for item in records[:3]:
        normalized.append({
            "record_id": item.get("id"),
            "account_type": item.get("regnskapstype"),
            "period": item.get("regnskapsperiode"),
            "currency": item.get("valuta"),
            "revenue": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"),
            "operating_result": _get(item, "resultatregnskapResultat", "driftsresultat", "driftsresultat"),
            "profit_before_tax": _get(item, "resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"),
            "annual_result": _get(item, "resultatregnskapResultat", "aarsresultat"),
            "assets": _get(item, "eiendeler", "sumEiendeler"),
            "equity": _get(item, "egenkapitalGjeld", "egenkapital", "sumEgenkapital"),
            "debt": _get(item, "egenkapitalGjeld", "gjeldOversikt", "sumGjeld"),
        })
    return {"records": normalized}


def normalize_financial_history(body: Any, org: str) -> dict[str, Any]:
    years = sorted({str(year) for year in body if str(year).isdigit()}) if isinstance(body, list) else []
    return {
        "years": years,
        "pdfs": [
            {"year": year, "url": BRREG_ACCOUNT_PDF.format(org=org, year=year)}
            for year in reversed(years)
        ],
    }


def _reserve_history_slot(clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Reserve starts at least 2.1 seconds apart without serializing response time."""
    global _history_last_request
    with _history_lock:
        delay = 2.1 - (clock() - _history_last_request)
        if delay > 0:
            sleeper(delay)
        _history_last_request = clock()


def _fetch_history(url: str) -> FetchResult:
    """Keep this endpoint below its observed 30-request-starts/minute allowance."""
    _reserve_history_slot()
    return fetch_json(url)


def normalize_roles(body: Any) -> dict[str, Any]:
    roles = []
    for group in body.get("rollegrupper", []) if isinstance(body, dict) else []:
        changed = group.get("sistEndret")
        for item in group.get("roller", []):
            person = item.get("person") or {}
            name = person.get("navn") or {}
            entity = item.get("enhet") or {}
            entity_name = entity.get("navn")
            if isinstance(entity_name, list):
                entity_name = " ".join(str(part) for part in entity_name if part)
            display_name = " ".join(filter(None, [name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")])) or entity_name
            roles.append({
                "name": display_name or None,
                "organisation_number": entity.get("organisasjonsnummer"),
                "role_code": _get(item, "type", "kode"),
                "role": _get(item, "type", "beskrivelse"),
                "group_code": _get(group, "type", "kode"),
                "group": _get(group, "type", "beskrivelse"),
                "last_changed": changed,
                "inactive": bool(item.get("avregistrert")),
            })
    return {"roles": roles}


def normalize_locations(body: Any) -> dict[str, Any]:
    rows = _get(body, "_embedded", "underenheter") or []
    return {"locations": [{
        "organisation_number": item.get("organisasjonsnummer"),
        "name": item.get("navn"),
        "address": item.get("beliggenhetsadresse") or item.get("postadresse"),
        "industry": item.get("naeringskode1"),
        "employees": item.get("antallAnsatte"),
    } for item in rows]}


def normalize_entity(body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    return {
        "organisation_number": body.get("organisasjonsnummer"),
        "name": body.get("navn"),
        "legal_form": _get(body, "organisasjonsform", "kode"),
        "legal_form_label": _get(body, "organisasjonsform", "beskrivelse"),
        "employees": body.get("antallAnsatte"),
        "has_registered_employees": body.get("harRegistrertAntallAnsatte"),
        "bankrupt": body.get("konkurs"),
        "liquidating": body.get("underAvvikling"),
        "forced_dissolution": body.get("underTvangsavviklingEllerTvangsopplosning"),
        "website": body.get("hjemmeside"),
        "industry": body.get("naeringskode1"),
        "industry_secondary": [code for code in (body.get("naeringskode2"), body.get("naeringskode3")) if code],
        "business_address": body.get("forretningsadresse"),
        "postal_address": body.get("postadresse"),
        "latest_submitted_accounts": body.get("sisteInnsendteAarsregnskap"),
        "founded_date": body.get("stiftelsesdato"),
        "registered_date": body.get("registreringsdatoEnhetsregisteret"),
        "business_register_date": body.get("registreringsdatoForetaksregisteret"),
        "vat_registered": body.get("registrertIMvaregisteret"),
        "in_group": body.get("erIKonsern"),
        "activity": body.get("aktivitet") or [],
        "statutory_purpose": body.get("vedtektsfestetFormaal") or [],
        "historical_names": body.get("historiskeNavn") or [],
        "share_capital": body.get("kapital"),
        "parent_unit": body.get("overordnetEnhet"),
        "deleted_date": body.get("slettedato"),
        # verification-only (never published): used to confirm website identity
        "_phones": [phone for phone in (body.get("telefon"), body.get("mobil")) if phone],
        "_email_domain": str(body.get("epostadresse") or "").rsplit("@", 1)[-1].strip().lower() if "@" in str(body.get("epostadresse") or "") else None,
    }


def normalize_group(body: Any, org: str) -> dict[str, Any]:
    """Flatten Brreg's group tree into labelled parent/child links touching this entity."""
    links: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("organisasjonsnummer") and node.get("parentOrganisasjonsnummer"):
                child, parent = node["organisasjonsnummer"], node["parentOrganisasjonsnummer"]
                if org in {child, parent}:
                    links.append({
                        "relation": "parent" if child == org else "subsidiary",
                        "organisation_number": parent if child == org else child,
                        "name": node.get("parentNavn") if child == org else node.get("navn"),
                        "ownership_basis": node.get("grunnlag"),
                        "link_type": _get(node, "knytningsform", "beskrivelse"),
                        "as_of": node.get("dato"),
                    })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    unique = {(item["relation"], item["organisation_number"]): item for item in links}
    return {"links": sorted(unique.values(), key=lambda item: (item["relation"], item["organisation_number"]))[:200]}


def normalize_role_events(body: Any) -> dict[str, Any]:
    rows = body if isinstance(body, list) else []
    dates = sorted({str(item.get("time") or "")[:10] for item in rows if item.get("time")})
    return {"role_change_dates": dates}


def _classified(field: str, source_type: str, result: FetchResult, value: Any = None) -> dict[str, Any]:
    if result.status == 200:
        return evidence(field, "available", source_type, result.url, value=result.body if value is None else value, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)
    if result.status in {404, 410}:
        return evidence(field, "not_found", source_type, result.url, note=result.error, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)
    return evidence(field, "source_error", source_type, result.url, note=result.error, content_sha256=result.content_sha256, retrieved_at=result.retrieved_at, effective_at=result.effective_at)


def fetch_official_modules(org: str, modules: set[str], fetcher: Callable[[str], FetchResult] = fetch_json) -> tuple[dict[str, Any], list[FetchResult]]:
    records: dict[str, Any] = {}
    metrics: list[FetchResult] = []
    endpoints = {
        "registry_live": (BRREG_ENTITY.format(org=org), "official_registry_live"),
        "financials": (BRREG_ACCOUNTS.format(org=org), "official_annual_accounts"),
        "financial_history": (BRREG_ACCOUNT_YEARS.format(org=org), "official_annual_account_copies"),
        "roles": (BRREG_ROLES.format(org=org), "official_roles"),
        "group": (BRREG_GROUP.format(org=org), "official_group_structure"),
        "locations": (BRREG_SUBUNITS.format(org=org), "official_subunits"),
        "role_events": (BRREG_ROLE_EVENTS.format(org=org), "official_role_update_log"),
    }
    for module, (url, source_type) in endpoints.items():
        if module not in modules:
            continue
        if fetcher is fetch_json:
            options = {"purpose": f"brreg_{module}", **FLAKY_MODULE_RETRIES.get(module, {})}
            if module == "financial_history":
                _reserve_history_slot()
            result = fetch_accounts(url, options["purpose"]) if module == "financials" else fetch_json(url, **options)
        else:
            result = fetcher(url)
        metrics.append(result)
        normalized = None
        if result.status == 200:
            normalizers = {
                "registry_live": normalize_entity,
                "financials": normalize_financials,
                "financial_history": lambda body: normalize_financial_history(body, org),
                "roles": normalize_roles,
                "locations": normalize_locations,
                "group": lambda body: normalize_group(body, org),
                "role_events": normalize_role_events,
            }
            normalized = normalizers[module](result.body) if module in normalizers else result.body
        records[module] = _classified(module, source_type, result, value=normalized)
    return records, metrics
