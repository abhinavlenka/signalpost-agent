"""Terminal company envelope: seven required sections, claim-level evidence and explicit states.

Rules enforced here:
- every published claim has at least one evidence record (URL, retrieval time, content hash, locator);
- absent values are never turned into zero or empty strings — they are simply not claimed, and the
  field family carries an explicit state (available / not_available / blocked / not_applicable /
  ambiguous / failed) plus a reason;
- claim keys are stable across runs so refresh can diff and upsert idempotently.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any, Iterable

import tldextract

from .identity import _tokens as name_tokens
from .rawstore import snapshot_path

STATES = ("available", "not_available", "blocked", "not_applicable", "ambiguous", "failed")

SECTIONS = {
    "legal_identity": "1. Legal identity and public brand",
    "annual_accounts": "2. Latest annual accounts and available history",
    "leadership_workplaces": "3. Leadership and registered workplaces",
    "web_presence": "4. Verified official website and company-owned profiles",
    "hiring_activity": "5. Hiring and dated public activity",
}

# field family -> section
FIELD_FAMILIES = {
    "legal_identity": "legal_identity",
    "public_brand": "legal_identity",
    "group_relationships": "legal_identity",
    "annual_accounts": "annual_accounts",
    "filing_history": "annual_accounts",
    "leadership": "leadership_workplaces",
    "registered_workplaces": "leadership_workplaces",
    "official_website": "web_presence",
    "company_profiles": "web_presence",
    "jobs": "hiring_activity",
    "dated_activity": "hiring_activity",
}

FINANCIAL_FIELDS = ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt")
# our field -> the key the accounts register uses, for quoting the figure as filed
ACCOUNT_SOURCE_KEYS = {"revenue": "sumDriftsinntekter", "operating_result": "driftsresultat", "profit_before_tax": "ordinaertResultatFoerSkattekostnad",
                       "annual_result": "aarsresultat", "assets": "sumEiendeler", "equity": "sumEgenkapital", "debt": "sumGjeld"}
LEADERSHIP_ROLE_CODES = {"DAGL", "LEDE", "NEST", "MEDL", "VARA", "KONT", "DTPR", "DTSO", "INNH", "BEST", "KOMP", "FFØR", "REPR", "SAM"}
SERVICE_ROLE_CODES = {"REVI", "REGN"}


def _hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(json.dumps(part, sort_keys=True, ensure_ascii=False, default=str) for part in parts).encode()).hexdigest()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


_ANY = object()
# A scalar never starts with a brace, bracket or quote: otherwise a nested object would be swallowed
# as a "scalar" and hide an inner key of the same name in compact JSON.
_JSON_VALUE = r'(\{[^{}]*\}|\[[^\[\]]*\]|"(?:[^"\\]|\\.)*"|[^,\s}\]{\["]+)'


class Quote(str):
    """A span with its provenance: ``exact`` when the text was found in the stored source, not rendered from parsed data."""

    exact: bool

    def __new__(cls, text: str, exact: bool = False) -> "Quote":
        item = super().__new__(cls, text)
        item.exact = exact
        return item


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _same_json(text: str, expected: Any) -> bool:
    try:
        actual = json.loads(text)
    except ValueError:
        return False
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return float(actual) == float(expected)
    return actual == expected


def quote_json(raw_text: str | None, key: str, expected: Any = _ANY, fallback: Any = None, suffix: str = "") -> Quote:
    """The ``"key" : value`` text exactly as the source sent it (``exact``); a rendering of the parsed value otherwise."""
    if raw_text:
        for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*{_JSON_VALUE}', raw_text):
            if expected is _ANY or _same_json(match.group(1), expected):
                return Quote(_clip(match.group(0)) + suffix, exact=True)
    shown = fallback if fallback is not None else (None if expected is _ANY else expected)
    return Quote(_clip(f'"{key}": {json.dumps(shown, ensure_ascii=False, default=str)}') + suffix, exact=False)


def index_quotes(raw_text: str | None, key: str) -> dict[str, str]:
    """One pass over a large response: every ``"key" : "value"`` occurrence by value (first wins)."""
    found: dict[str, str] = {}
    for match in re.finditer(rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text or ""):
        found.setdefault(match.group(1), " ".join(match.group(0).split()))
    return found


RIGHTS_BY_SOURCE = {
    "official_job_register_nav": "NAV pam-stilling-feed public API, used under https://arbeidsplassen.nav.no/vilkar-api",
    "frozen_universe_snapshot": "Builderr Signalpost public universe file",
}


def source_rights(source_class: str | None) -> str:
    source_class = str(source_class or "")
    if source_class in RIGHTS_BY_SOURCE:
        return RIGHTS_BY_SOURCE[source_class]
    if source_class.startswith("official_"):
        return "Brønnøysund Register Centre open data, NLOD 2.0"
    return "public page on the company's own site; fetched only where robots.txt allows this user agent"


class EnvelopeBuilder:
    def __init__(self, organisation_number: str) -> None:
        self.org = organisation_number
        self.claims: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}
        self.fields: dict[str, dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []
        self._claim_keys: set[str] = set()

    # -- evidence and claims -------------------------------------------------------------------
    def add_evidence(
        self,
        *,
        source_url: str,
        source_class: str,
        retrieved_at: str | None,
        content_sha256: str | None,
        extraction_method: str,
        final_url: str | None = None,
        http_status: int | None = 200,
        reporting_period: str | None = None,
        note: str | None = None,
        span: str | None = None,
    ) -> str:
        evidence_id = "ev-" + _hash(source_url, content_sha256, extraction_method)[:16]
        if evidence_id not in self.evidence:
            self.evidence[evidence_id] = {
                "id": evidence_id,  # the name used by the published output contract
                "evidence_id": evidence_id,
                "source_url": source_url,
                "final_url": final_url or source_url,
                "source_class": source_class,
                "retrieved_at": retrieved_at,
                "http_status": http_status,
                "content_sha256": content_sha256,
                "extraction_method": extraction_method,
                "reporting_period": reporting_period,
                "note": note,
                "claim_span": _clip(span or note or f"source record for organisation number {self.org}"),
                "snapshot_path": snapshot_path(content_sha256),
                "rights": source_rights(source_class),
            }
        return evidence_id

    def claim(
        self,
        family: str,
        field: str,
        value: Any,
        evidence_id: str,
        *,
        identity: Any = None,
        locator: str | None = None,
        span: str | None = None,
        reporting_period: str | None = None,
        effective_date: str | None = None,
        confidence: float = 1.0,
    ) -> None:
        if value is None or value == "" or value == [] or value == {}:
            return  # absence is a field state, never a claim
        key_part = identity if identity is not None else value
        claim_key = f"{self.org}|{family}|{field}|{_norm(json.dumps(key_part, sort_keys=True, ensure_ascii=False, default=str))}"
        if reporting_period and identity is None:
            claim_key = f"{self.org}|{family}|{field}|{reporting_period}"
        if claim_key in self._claim_keys:
            for existing in self.claims:
                if existing["claim_key"] == claim_key and evidence_id not in existing["evidence_ids"]:
                    existing["evidence_ids"].append(evidence_id)
            return
        self._claim_keys.add(claim_key)
        self.claims.append({
            "claim_id": "cl-" + hashlib.sha256(claim_key.encode()).hexdigest()[:16],
            "claim_key": claim_key,
            "section": FIELD_FAMILIES[family],
            "family": family,
            "field": field,
            "value": value,
            "availability": "available",
            "confidence": confidence,
            "reporting_period": reporting_period,
            "effective_date": effective_date,
            "evidence_ids": [evidence_id],
            "locator": locator,
            "claim_span": _clip(span) if span else _clip(f'"{field}": {json.dumps(value, ensure_ascii=False, default=str)}'),
            # source_text: found verbatim in the fetched source; rendered_value: written out from the parsed value
            "span_kind": "source_text" if getattr(span, "exact", False) else "rendered_value",
            "value_hash": _hash(value)[:16],
        })

    def state(self, family: str, state: str, reason: str | None = None, **extra: Any) -> None:
        assert state in STATES, state
        self.fields[family] = {"state": state, "reason": reason, **extra}

    def finalize_states(self) -> None:
        counts: dict[str, int] = {}
        for claim in self.claims:
            counts[claim["family"]] = counts.get(claim["family"], 0) + 1
        for family in FIELD_FAMILIES:
            current = self.fields.get(family)
            if counts.get(family):
                if not current or current["state"] != "available":
                    self.fields[family] = {**(current or {}), "state": "available", "reason": (current or {}).get("reason")}
                self.fields[family]["claims"] = counts[family]
            elif not current:
                self.fields[family] = {"state": "not_available", "reason": "no supported value found in checked sources", "claims": 0}
            else:
                current.setdefault("claims", 0)


# -- helpers mapping raw evidence records ---------------------------------------------------------
def record_state(record: dict[str, Any] | None) -> tuple[str, str | None]:
    """Map a kit evidence record status to a contract availability state."""
    if not record:
        return "failed", "module not executed"
    status = record.get("status")
    note = str(record.get("note") or "")
    if "budget_exhausted" in note or "BudgetExhausted" in note:
        return "failed", "run budget exhausted before this source was checked"
    if status == "available":
        return "available", None
    if status == "not_found":
        return "not_available", note or "source returned no record"
    if status == "not_applicable":
        return "not_applicable", note or None
    if status == "blocked" or any(code in note for code in ("HTTP 401", "HTTP 403", "HTTP 429", "HTTP 451")):
        return "blocked", note or "source refused access"
    return "failed", note or "source error"


def _anchor_quote(record: dict[str, Any], org: str) -> str | None:
    """Source text showing that the record is about this organisation number."""
    raw_text = record.get("raw_text")
    if raw_text:
        match = re.search(rf'"[A-Za-z_]+"\s*:\s*"[^"\n]*{re.escape(org)}[^"\n]*"', raw_text)
        if match:
            return _clip(match.group(0))
    return None


def _official_evidence(builder: EnvelopeBuilder, record: dict[str, Any], method: str, reporting_period: str | None = None) -> str:
    return builder.add_evidence(
        span=_anchor_quote(record, builder.org) or f"record for organisation number {builder.org} returned by {record.get('source_url')}",
        source_url=record.get("source_url"),
        source_class=record.get("source_class") or record.get("source_type") or "official_registry",
        retrieved_at=record.get("retrieved_at"),
        content_sha256=record.get("content_sha256"),
        extraction_method=method,
        reporting_period=reporting_period,
    )


def _address(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [", ".join(value.get("adresse") or []), " ".join(filter(None, [value.get("postnummer"), value.get("poststed")])), value.get("land")]
    text = ", ".join(part for part in parts if part)
    return text or None


def build_identity(builder: EnvelopeBuilder, profile: dict[str, Any]) -> None:
    ev = profile.get("evidence", {})
    live = ev.get("registry_live") or {}
    state, reason = record_state(live)
    value = live.get("value") if state == "available" else None
    if value:
        eid = _official_evidence(builder, live, "brreg_entity_json_v1")
        raw_text = live.get("raw_text")
        # (field, value, locator, whether the source value is a scalar that the quote must equal)
        mapping = [
            ("legal_name", value.get("name"), "$.navn", True),
            ("organisation_number", value.get("organisation_number"), "$.organisasjonsnummer", True),
            ("legal_form", value.get("legal_form"), "$.organisasjonsform.kode", True),
            ("legal_form_label", value.get("legal_form_label"), "$.organisasjonsform.beskrivelse", True),
            ("business_address", _address(value.get("business_address")), "$.forretningsadresse", False),
            ("postal_address", _address(value.get("postal_address")), "$.postadresse", False),
            ("municipality", (value.get("business_address") or {}).get("kommune"), "$.forretningsadresse.kommune", True),
            ("industry", value.get("industry"), "$.naeringskode1", False),
            ("founded_date", value.get("founded_date"), "$.stiftelsesdato", True),
            ("registered_date", value.get("registered_date"), "$.registreringsdatoEnhetsregisteret", True),
            ("vat_registered", value.get("vat_registered"), "$.registrertIMvaregisteret", True),
            ("bankrupt", value.get("bankrupt"), "$.konkurs", True),
            ("under_liquidation", value.get("liquidating"), "$.underAvvikling", True),
            ("forced_dissolution", value.get("forced_dissolution"), "$.underTvangsavviklingEllerTvangsopplosning", True),
            ("stated_activity", " ".join(value.get("activity") or []) or None, "$.aktivitet", False),
            ("statutory_purpose", " ".join(value.get("statutory_purpose") or []) or None, "$.vedtektsfestetFormaal", False),
            ("share_capital", (value.get("share_capital") or {}).get("belop"), "$.kapital.belop", True),
        ]
        for field, field_value, locator, scalar in mapping:
            key = locator.rsplit(".", 1)[-1]
            builder.claim("legal_identity", field, field_value, eid, identity=field, locator=locator,
                          span=quote_json(raw_text, key, field_value if scalar else _ANY, fallback=field_value))
        # Registered employees: only claim when the registry actually reports a count.
        if value.get("has_registered_employees") and value.get("employees") is not None:
            builder.claim("legal_identity", "registered_employees", value.get("employees"), eid, identity="registered_employees", locator="$.antallAnsatte",
                          span=quote_json(raw_text, "antallAnsatte", value.get("employees")))
        for index, previous in enumerate(value.get("historical_names") or []):
            if isinstance(previous, dict):
                previous = {"name": previous.get("navn"), "from": str(previous.get("fraDato") or "")[:10] or None, "to": str(previous.get("tilDato") or "")[:10] or None}
            builder.claim("public_brand", "previous_legal_name", previous, eid, identity=["previous_legal_name", previous.get("name") if isinstance(previous, dict) else previous],
                          locator=f"$.historiskeNavn[{index}]", effective_date=previous.get("to") if isinstance(previous, dict) else None,
                          span=quote_json(raw_text, "navn", previous.get("name") if isinstance(previous, dict) else previous))
        builder.state("legal_identity", "available")
    else:
        registry = ev.get("registry") or {}
        raw = registry.get("value") or {}
        if raw:
            eid = _official_evidence(builder, registry, "frozen_universe_row_v1")
            builder.claim("legal_identity", "legal_name", profile.get("name"), eid, identity="legal_name", locator="$.name")
            builder.claim("legal_identity", "organisation_number", profile.get("organisation_number"), eid, identity="organisation_number", locator="$.organisation_number")
            builder.claim("legal_identity", "legal_form", profile.get("legal_form"), eid, identity="legal_form", locator="$.legal_form")
            builder.claim("legal_identity", "municipality", profile.get("municipality"), eid, identity="municipality", locator="$.municipality")
            builder.state("legal_identity", "available", f"live registry unavailable ({reason}); frozen snapshot used")
        else:
            builder.state("legal_identity", state if state != "available" else "failed", reason)

    group = ev.get("group")
    if group:
        g_state, g_reason = record_state(group)
        links = ((group.get("value") or {}).get("links") or []) if g_state == "available" else []
        if links:
            eid = _official_evidence(builder, group, "brreg_group_structure_v1")
            for link in links:
                builder.claim("group_relationships", link["relation"], link, eid, identity=[link["relation"], link["organisation_number"]], locator="$..children", effective_date=link.get("as_of"),
                              span=quote_json(group.get("raw_text"), "organisasjonsnummer", link["organisation_number"], suffix=f" ({link['relation']}: {link.get('name')})"))
        else:
            builder.state("group_relationships", "not_available" if g_state == "available" else g_state, g_reason or "no group links returned")
    elif value is not None and not value.get("in_group"):
        builder.state("group_relationships", "not_applicable", "registry reports the entity is not part of a group")


def build_accounts(builder: EnvelopeBuilder, profile: dict[str, Any]) -> None:
    ev = profile.get("evidence", {})
    record = ev.get("financials")
    state, reason = record_state(record)
    records = ((record or {}).get("value") or {}).get("records") or []
    if state == "available" and records:
        for index, item in enumerate(records):
            period = item.get("period") or {}
            period_label = f"{period.get('fraDato')}..{period.get('tilDato')}" if period else None
            eid = _official_evidence(builder, record, "brreg_regnskap_json_v1", reporting_period=period_label)
            for field in FINANCIAL_FIELDS:
                amount = item.get(field)
                if amount is None:
                    continue  # never convert an absent figure to zero
                builder.claim(
                    "annual_accounts", field, {"amount": amount, "currency": item.get("currency"), "account_type": item.get("account_type")},
                    eid, identity=[field, period_label, item.get("account_type")], locator=f"$[{index}].{field}",
                    reporting_period=period_label, effective_date=period.get("tilDato"),
                    span=quote_json(record.get("raw_text"), ACCOUNT_SOURCE_KEYS[field], amount, suffix=f" (regnskapsperiode {period_label})"),
                )
        builder.state("annual_accounts", "available")
    elif state == "available":
        builder.state("annual_accounts", "not_available", "accounts register returned no normalized accounts")
    else:
        builder.state("annual_accounts", state, reason)
        if record and state == "failed":
            builder.errors.append({"source": "financials", "error": reason})

    live = (ev.get("registry_live") or {})
    latest = (live.get("value") or {}).get("latest_submitted_accounts") if live.get("status") == "available" else profile.get("latest_submitted_accounts")
    if latest:
        eid = _official_evidence(builder, live if live.get("status") == "available" else ev.get("registry") or {}, "brreg_entity_json_v1")
        builder.claim("filing_history", "latest_submitted_accounts_year", str(latest), eid, identity="latest_submitted_accounts_year", locator="$.sisteInnsendteAarsregnskap",
                      span=quote_json(live.get("raw_text") if live.get("status") == "available" else None, "sisteInnsendteAarsregnskap", str(latest)))
    history = ev.get("financial_history")
    if history and history.get("status") == "available":
        eid = _official_evidence(builder, history, "brreg_regnskap_copy_years_v1")
        for year in (history.get("value") or {}).get("years") or []:
            builder.claim("filing_history", "filed_annual_account_copy", year, eid, locator="$[*]")


def _role_quote(raw_text: str | None, role: dict[str, Any]) -> Quote:
    """Quote the role holder as the roles register names them, followed by the role."""
    name, suffix = " ".join(str(role.get("name") or "").casefold().split()), f" · rolle: {role.get('role')} ({role.get('role_code')})"
    if raw_text and name:
        for match in re.finditer(r'"etternavn"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text):
            surname = " ".join(match.group(1).casefold().split())
            if surname and (name == surname or name.endswith(" " + surname)):  # whole words only: "Lindberg" is not "Berg"
                return Quote(_clip(match.group(0)) + suffix, exact=True)
    if role.get("organisation_number"):
        return quote_json(raw_text, "organisasjonsnummer", role.get("organisation_number"), suffix=suffix)
    return Quote(f'"navn": {json.dumps(str(role.get("name") or ""), ensure_ascii=False)}' + suffix, exact=False)


def build_leadership(builder: EnvelopeBuilder, profile: dict[str, Any]) -> None:
    ev = profile.get("evidence", {})
    roles_record = ev.get("roles")
    state, reason = record_state(roles_record)
    roles = ((roles_record or {}).get("value") or {}).get("roles") or []
    active = [role for role in roles if not role.get("inactive")]
    if state == "available" and active:
        eid = _official_evidence(builder, roles_record, "brreg_roles_json_v1")
        for role in active:
            if role.get("role_code") in SERVICE_ROLE_CODES:
                field = "auditor" if role.get("role_code") == "REVI" else "accountant"
            else:
                field = "role"
            value = {
                "role": role.get("role"),
                "role_code": role.get("role_code"),
                "name": role.get("name"),
                "organisation_number": role.get("organisation_number"),
                "group_last_changed": role.get("last_changed"),
            }
            builder.claim("leadership", field, value, eid, identity=[role.get("role_code"), _norm(role.get("name")), role.get("organisation_number")], locator="$.rollegrupper[*].roller[*]", effective_date=role.get("last_changed"),
                          span=_role_quote(roles_record.get("raw_text"), role))
        builder.state("leadership", "available")
    elif state == "available":
        builder.state("leadership", "not_available", "no active registered roles")
    else:
        builder.state("leadership", state, reason)

    loc_record = ev.get("locations")
    l_state, l_reason = record_state(loc_record)
    locations = ((loc_record or {}).get("value") or {}).get("locations") or []
    if l_state == "available" and locations:
        eid = _official_evidence(builder, loc_record, "brreg_subunits_json_v1")
        numbers = index_quotes(loc_record.get("raw_text"), "organisasjonsnummer")  # a chain can have a thousand sub-units: scan once
        for index, location in enumerate(locations):
            value = {
                "organisation_number": location.get("organisation_number"),
                "name": location.get("name"),
                "address": _address(location.get("address")),
                "municipality": (location.get("address") or {}).get("kommune"),
                "industry": (location.get("industry") or {}).get("beskrivelse") if isinstance(location.get("industry"), dict) else location.get("industry"),
                "registered_employees": location.get("employees"),
            }
            builder.claim("registered_workplaces", "registered_workplace", value, eid, identity=location.get("organisation_number"), locator=f"$._embedded.underenheter[{index}]",
                          span=Quote(f"{numbers[location.get('organisation_number')]} ({location.get('name')})", exact=True) if location.get("organisation_number") in numbers
                          else Quote(f'"organisasjonsnummer": "{location.get("organisation_number")}" ({location.get("name")})', exact=False))
    elif l_state == "available":
        builder.state("registered_workplaces", "not_available", "no registered subunits")
    else:
        builder.state("registered_workplaces", l_state, l_reason)


def _without_default_port(url: str | None) -> str | None:
    """Drop an explicit :443/:80 that some servers add on redirect; the evidence keeps the fetched URL."""
    if not url:
        return url
    parsed = urllib.parse.urlsplit(url)
    if parsed.port != {"https": 443, "http": 80}.get(parsed.scheme):
        return url
    return urllib.parse.urlunsplit(parsed._replace(netloc=parsed.netloc.rsplit(":", 1)[0]))


def build_web_presence(builder: EnvelopeBuilder, profile: dict[str, Any]) -> None:
    ev = profile.get("evidence", {})
    website = ev.get("website")
    state, reason = record_state(website)
    value = (website or {}).get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if state == "available" and assessment.get("publishable"):
        proof = _site_proof(value)
        eid = builder.add_evidence(
            source_url=value.get("requested_url") or website.get("source_url"),
            final_url=value.get("final_url"),
            source_class=website.get("source_type") or "company_website",
            retrieved_at=website.get("retrieved_at"),
            content_sha256=value.get("content_sha256"),
            extraction_method=assessment.get("method") or "identity_gate",
            note="; ".join(assessment.get("reasons") or []),
            span=proof or value.get("title") or "; ".join(assessment.get("reasons") or []),
        )
        scope = website_scope(value, profile.get("name"), profile.get("legal_form"))
        third_party = scope == "page_on_third_party_site"
        span = "; ".join(assessment.get("reasons") or [])
        if proof:
            span = Quote(span + f"; on the site: “{proof}”", exact=True)
        if scope == "possibly_group_or_international":
            span += "; site scope: possibly a group or international site (no organisation number on site, non-.no domain)"
        if third_party:
            span += "; site scope: a page naming this company on a site under another name (chain, directory or platform); only the URL is published"
        builder.claim("official_website", "official_website", _without_default_port(value.get("final_url")), eid, identity="official_website",
                      locator="html (identity gate)", span=span, confidence=float(assessment.get("score") or 0.9) if scope not in {"possibly_group_or_international", "page_on_third_party_site"} else 0.8)
        builder.claim("public_brand", "website_scope", scope, eid, identity="website_scope", locator="identity_markers",
                      span=Quote(proof, exact=True) if proof else f"{urllib.parse.urlparse(value.get('final_url') or '').hostname}: {'; '.join(assessment.get('reasons') or [])}")
        brand = None if third_party else value.get("site_name") or value.get("title")
        if brand:
            builder.claim("public_brand", "website_brand_title", brand[:200], eid, identity="website_brand_title", locator="html>head>title", span=Quote(brand[:200], exact=True))
        if value.get("description") and not third_party:
            builder.claim("public_brand", "self_description", value["description"][:600], eid, identity="self_description", locator='meta[name="description"]',
                          span=Quote(value["description"], exact=True))
        builder.state("official_website", "available", None, discovery=value.get("discovery_method") or "registry_listed")
        social = [] if third_party else value.get("social_links") or []
        if scope == "possibly_group_or_international":
            social = [link for link in social if NORWAY_HANDLE.search(link["url"].rsplit("/", 1)[-1])]
        for link in social:
            declared_in = link.get("declared_in") or "a[href]"
            builder.claim("company_profiles", link["platform"], link["url"], eid, identity=[link["platform"], link["url"]], locator=declared_in,
                          span=f"{declared_in} on the verified company website: {link['url']}")
        if not social:
            builder.state("company_profiles", "not_available", "verified website links no company-owned social profiles")
        group_site = scope in {"possibly_group_or_international", "page_on_third_party_site"}
        for page in [] if group_site else value.get("careers_pages") or []:
            builder.claim("jobs", "careers_page", page, eid, identity=["careers_page", page], locator="a[href]",
                          span=f"careers page linked from the verified company website: {page}")
        for item in [] if group_site else value.get("news_items") or []:
            # cite the page or feed the item was read from, not the homepage
            found_in = item.get("found_in") or {}
            news_eid = eid if not found_in.get("content_sha256") or found_in.get("content_sha256") == value.get("content_sha256") else builder.add_evidence(
                source_url=found_in.get("url"), source_class=website.get("source_type") or "company_website", retrieved_at=found_in.get("retrieved_at") or website.get("retrieved_at"),
                content_sha256=found_in.get("content_sha256"), extraction_method="site_feed_v1" if str(item.get("locator") or "").startswith(("rss:", "atom:")) else "site_page_markup_v1",
                span=f"dated items on the verified site {urllib.parse.urlparse(value.get('final_url') or '').hostname}")
            builder.claim("dated_activity", "website_news", {"date": item.get("date"), "title": item.get("title"), "url": item.get("url")},
                          news_eid, identity=["website_news", item.get("url")], effective_date=item.get("date"), locator=item.get("locator"),
                          span=Quote(f"{item.get('title')} ({item.get('locator')}: {item.get('date')})", exact=True))
    elif state == "available":
        builder.state("official_website", "ambiguous", "candidate site failed exact-entity identity gate: " + "; ".join(assessment.get("reasons") or []),
                      candidate=value.get("final_url"))
        if value.get("discovery_method") == "registry_listed" and value.get("final_url"):
            # True, labelled fact: the register lists this site. It may be a brand, parent or franchise
            # site, so it is not published as the official website and nothing is extracted from it.
            reg = ev.get("registry_live") or {}
            reg_eid = _official_evidence(builder, reg, "brreg_entity_json_v1") if reg.get("status") == "available" else builder.add_evidence(
                source_url=value.get("requested_url") or website.get("source_url"), final_url=value.get("final_url"), source_class="registry_linked_company_website",
                retrieved_at=website.get("retrieved_at"), content_sha256=value.get("content_sha256"), extraction_method="registry_hjemmeside_v1")
            builder.claim("public_brand", "registry_declared_site", {"url": _without_default_port(value.get("final_url")), "relation": "declared in the official register; exact-entity identity not verified (possible brand, parent or franchise site)"},
                          reg_eid, identity="registry_declared_site", locator="$.hjemmeside", confidence=0.6,
                          span=quote_json(reg.get("raw_text"), "hjemmeside", fallback=value.get("requested_url") or value.get("final_url")))
        builder.state("company_profiles", "ambiguous", "no verified website to anchor company-owned profiles")
    else:
        if state == "not_available":
            reason = "the register lists no website and none was verified" if not reason or reason == "registry lists no website" else reason
        builder.state("official_website", state, reason)
        builder.state("company_profiles", "not_available" if state == "not_available" else state, "no verified website to anchor company-owned profiles")


def _site_proof(value: dict[str, Any]) -> str | None:
    """The strongest text on the site that ties it to the registered entity: org number, then address, then phone."""
    snippets = value.get("identity_snippets") or {}
    for marker in ("organisation_number", "address", "phone"):
        for page_url, found in snippets.items():
            if found.get(marker):
                return _clip(f"{found[marker]} ({page_url})", 260)
    return None


def _own_domain(host: str, legal_name: str) -> bool:
    """Whether the registrable domain is named after the company.

    A deep page on a domain under another name is a member/listing page on someone else's site
    (chain, directory, platform). One shared trade word is not enough ("HÅST RØR AS" on rorkjop.no):
    the domain must contain the first name word, or at least two name words.
    """
    label = re.sub(r"[^a-z0-9]", "", tldextract.extract(host).domain.lower())
    spellings = [name_tokens(legal_name), name_tokens(legal_name.translate(AA_SPELLING))]
    if not spellings[0]:
        return True  # nothing distinctive to compare
    if any(tokens and tokens[0] in label for tokens in spellings):
        return True
    return len({token for tokens in spellings for token in tokens if len(token) >= 3 and token in label}) >= 2


AA_SPELLING = str.maketrans({"å": "aa", "Å": "aa", "ø": "oe", "Ø": "oe"})
NORWAY_HANDLE = re.compile(r"(norge|norway|norsk|[-_.]no$|[-_.]no[-_.])", re.I)


def website_scope(value: dict[str, Any], legal_name: str | None = None, legal_form: str | None = None) -> str:
    markers = {marker for items in (value.get("identity_markers") or {}).values() for marker in items}
    if "organisation_number" in markers:
        return "exact_entity_verified_by_organisation_number"
    parsed = urllib.parse.urlparse(value.get("final_url") or "")
    host = parsed.hostname or ""
    if parsed.path.strip("/") and legal_name and not _own_domain(host, legal_name):
        return "page_on_third_party_site"
    if host.endswith(".no"):
        return "norwegian_domain"  # stable between runs: it must not depend on which secondary pages answered
    # On an international domain only the homepage counts: a group's contact page lists every
    # subsidiary's office, so an address there does not make the site the subsidiary's own.
    if {"address", "phone"} & set((value.get("identity_markers") or {}).get(value.get("final_url")) or []):
        return "verified_by_registered_address_or_phone"
    if str(legal_form or "").upper() == "ASA":
        # A Norwegian public limited company is the listed parent: its corporate site is its own, whatever the domain.
        return "public_company_own_site"
    return "possibly_group_or_international"


def build_activity(builder: EnvelopeBuilder, profile: dict[str, Any]) -> None:
    ev = profile.get("evidence", {})
    jobs_record = ev.get("jobs")
    if jobs_record is None:
        builder.state("jobs", "failed", "jobs source not checked")
    elif jobs_record.get("status") == "available":
        jobs = (jobs_record.get("value") or {}).get("jobs") or []
        for job in jobs:
            eid = builder.add_evidence(
                source_url=job["public_url"], final_url=job["source_url"], source_class="official_job_register_nav",
                retrieved_at=job.get("retrieved_at"), content_sha256=job.get("content_sha256"),
                extraction_method="nav_feed_entry_json_v1", note=f"employer.orgnr={job.get('employer_orgnr')} ({job.get('matched_via')})",
            )
            value = {key: job.get(key) for key in ("title", "job_title", "published", "expires", "application_due", "engagement_type", "extent", "positions", "work_locations", "public_url")}
            builder.claim("jobs", "open_job", value, eid, identity=["nav", job.get("uuid")], effective_date=str(job.get("published") or "")[:10] or None,
                          locator="$.ad_content", span=f"employer orgnr {job.get('employer_orgnr')}")
            builder.claim("dated_activity", "job_posted", {"date": str(job.get("published") or "")[:10], "title": job.get("title"), "url": job.get("public_url")},
                          eid, identity=["job_posted", job.get("uuid")], effective_date=str(job.get("published") or "")[:10] or None, locator="$.ad_content.published",
                          span=f"{job.get('title')} (published {str(job.get('published') or '')[:10]}; employer orgnr {job.get('employer_orgnr')})")
        if not jobs:
            builder.state("jobs", "not_available", "no active NAV job ads with this organisation number in the checked window",
                          checked_window_days=(jobs_record.get("value") or {}).get("window_days"))
    else:
        state, reason = record_state(jobs_record)
        builder.state("jobs", state, reason)

    events = ev.get("role_events")
    if events and events.get("status") == "available":
        dates = (events.get("value") or {}).get("role_change_dates") or []
        if dates:
            eid = _official_evidence(builder, events, "brreg_role_update_log_v1")
            for date in dates[-10:]:
                stamp = re.search(rf'"time"\s*:\s*"{re.escape(date)}[^"]*"', events.get("raw_text") or "")
                builder.claim("dated_activity", "registered_role_change", {"date": date, "title": "Registered roles updated in Brønnøysund"}, eid,
                              identity=["registered_role_change", date], effective_date=date, locator="$[*].time",
                              span=_clip(stamp.group(0)) if stamp else f'"time": "{date}"')
    live = ev.get("registry_live") or {}
    live_value = live.get("value") or {}
    latest = live_value.get("latest_submitted_accounts")
    if latest:
        eid = _official_evidence(builder, live, "brreg_entity_json_v1")
        builder.claim("dated_activity", "annual_accounts_filed", {"date": None, "year": str(latest), "title": f"Annual accounts for {latest} submitted"}, eid,
                      identity=["annual_accounts_filed", str(latest)], reporting_period=str(latest), locator="$.sisteInnsendteAarsregnskap",
                      span=quote_json(live.get("raw_text"), "sisteInnsendteAarsregnskap", str(latest)))


def build_envelope(
    profile: dict[str, Any],
    *,
    run: dict[str, Any],
    changes: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
    operations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    builder = EnvelopeBuilder(profile["organisation_number"])
    fatal = profile.get("fatal_error")
    if not fatal:
        for step in (build_identity, build_accounts, build_leadership, build_web_presence, build_activity):
            try:
                step(builder, profile)
            except Exception as exc:  # one broken section must not lose the others
                builder.errors.append({"source": step.__name__, "error": f"{type(exc).__name__}: {exc}"})
    builder.finalize_states()
    if fatal:
        for family in FIELD_FAMILIES:
            builder.fields[family] = {"state": "failed", "reason": fatal, "claims": 0}
        builder.errors.append({"source": "pipeline", "error": fatal})
    states = [item["state"] for item in builder.fields.values()]
    identity_ok = builder.fields.get("legal_identity", {}).get("state") == "available"
    overall = "available" if identity_ok and "available" in states else "failed" if fatal or not identity_ok else "not_available"
    sections = {section: {"title": title, "fields": {}} for section, title in SECTIONS.items()}
    for family, section in FIELD_FAMILIES.items():
        sections[section]["fields"][family] = builder.fields[family]
    return {
        "schema_version": "signalpost-envelope/1.0",
        "organisation_number": profile["organisation_number"],
        "legal_name": profile.get("name"),
        "state": overall,
        "run": run,
        "sections": sections,
        "claims": builder.claims,
        "evidence": list(builder.evidence.values()),
        "availability": {family: item["state"] for family, item in builder.fields.items()},
        "changes": changes or [],
        "summary": summary,
        "errors": builder.errors + list(profile.get("errors") or []),
        "operations": operations or {},
    }
