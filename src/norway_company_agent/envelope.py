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
from typing import Any, Iterable

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
LEADERSHIP_ROLE_CODES = {"DAGL", "LEDE", "NEST", "MEDL", "VARA", "KONT", "DTPR", "DTSO", "INNH", "BEST", "KOMP", "FFØR", "REPR", "SAM"}
SERVICE_ROLE_CODES = {"REVI", "REGN"}


def _hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(json.dumps(part, sort_keys=True, ensure_ascii=False, default=str) for part in parts).encode()).hexdigest()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


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
    ) -> str:
        evidence_id = "ev-" + _hash(source_url, content_sha256, extraction_method)[:16]
        if evidence_id not in self.evidence:
            self.evidence[evidence_id] = {
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
            "claim_span": span,
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


def _official_evidence(builder: EnvelopeBuilder, record: dict[str, Any], method: str, reporting_period: str | None = None) -> str:
    return builder.add_evidence(
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
        mapping = [
            ("legal_name", value.get("name"), "$.navn"),
            ("organisation_number", value.get("organisation_number"), "$.organisasjonsnummer"),
            ("legal_form", value.get("legal_form"), "$.organisasjonsform.kode"),
            ("legal_form_label", value.get("legal_form_label"), "$.organisasjonsform.beskrivelse"),
            ("business_address", _address(value.get("business_address")), "$.forretningsadresse"),
            ("postal_address", _address(value.get("postal_address")), "$.postadresse"),
            ("municipality", (value.get("business_address") or {}).get("kommune"), "$.forretningsadresse.kommune"),
            ("industry", value.get("industry"), "$.naeringskode1"),
            ("founded_date", value.get("founded_date"), "$.stiftelsesdato"),
            ("registered_date", value.get("registered_date"), "$.registreringsdatoEnhetsregisteret"),
            ("vat_registered", value.get("vat_registered"), "$.registrertIMvaregisteret"),
            ("bankrupt", value.get("bankrupt"), "$.konkurs"),
            ("under_liquidation", value.get("liquidating"), "$.underAvvikling"),
            ("forced_dissolution", value.get("forced_dissolution"), "$.underTvangsavviklingEllerTvangsopplosning"),
            ("stated_activity", " ".join(value.get("activity") or []) or None, "$.aktivitet"),
            ("statutory_purpose", " ".join(value.get("statutory_purpose") or []) or None, "$.vedtektsfestetFormaal"),
            ("share_capital", (value.get("share_capital") or {}).get("belop"), "$.kapital.belop"),
        ]
        for field, field_value, locator in mapping:
            builder.claim("legal_identity", field, field_value, eid, identity=field, locator=locator)
        # Registered employees: only claim when the registry actually reports a count.
        if value.get("has_registered_employees") and value.get("employees") is not None:
            builder.claim("legal_identity", "registered_employees", value.get("employees"), eid, identity="registered_employees", locator="$.antallAnsatte")
        for index, previous in enumerate(value.get("historical_names") or []):
            if isinstance(previous, dict):
                previous = {"name": previous.get("navn"), "from": str(previous.get("fraDato") or "")[:10] or None, "to": str(previous.get("tilDato") or "")[:10] or None}
            builder.claim("public_brand", "previous_legal_name", previous, eid, identity=["previous_legal_name", previous.get("name") if isinstance(previous, dict) else previous],
                          locator=f"$.historiskeNavn[{index}]", effective_date=previous.get("to") if isinstance(previous, dict) else None)
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
                builder.claim("group_relationships", link["relation"], link, eid, identity=[link["relation"], link["organisation_number"]], locator="$..children", effective_date=link.get("as_of"))
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
        builder.claim("filing_history", "latest_submitted_accounts_year", str(latest), eid, identity="latest_submitted_accounts_year", locator="$.sisteInnsendteAarsregnskap")
    history = ev.get("financial_history")
    if history and history.get("status") == "available":
        eid = _official_evidence(builder, history, "brreg_regnskap_copy_years_v1")
        for year in (history.get("value") or {}).get("years") or []:
            builder.claim("filing_history", "filed_annual_account_copy", year, eid, locator="$[*]")


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
            builder.claim("leadership", field, value, eid, identity=[role.get("role_code"), _norm(role.get("name")), role.get("organisation_number")], locator="$.rollegrupper[*].roller[*]", effective_date=role.get("last_changed"))
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
        for index, location in enumerate(locations):
            value = {
                "organisation_number": location.get("organisation_number"),
                "name": location.get("name"),
                "address": _address(location.get("address")),
                "municipality": (location.get("address") or {}).get("kommune"),
                "industry": (location.get("industry") or {}).get("beskrivelse") if isinstance(location.get("industry"), dict) else location.get("industry"),
                "registered_employees": location.get("employees"),
            }
            builder.claim("registered_workplaces", "registered_workplace", value, eid, identity=location.get("organisation_number"), locator=f"$._embedded.underenheter[{index}]")
    elif l_state == "available":
        builder.state("registered_workplaces", "not_available", "no registered subunits")
    else:
        builder.state("registered_workplaces", l_state, l_reason)


def build_web_presence(builder: EnvelopeBuilder, profile: dict[str, Any]) -> None:
    ev = profile.get("evidence", {})
    website = ev.get("website")
    state, reason = record_state(website)
    value = (website or {}).get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if state == "available" and assessment.get("publishable"):
        eid = builder.add_evidence(
            source_url=value.get("requested_url") or website.get("source_url"),
            final_url=value.get("final_url"),
            source_class=website.get("source_type") or "company_website",
            retrieved_at=website.get("retrieved_at"),
            content_sha256=value.get("content_sha256"),
            extraction_method=assessment.get("method") or "identity_gate",
            note="; ".join(assessment.get("reasons") or []),
        )
        builder.claim("official_website", "official_website", value.get("final_url"), eid, identity="official_website",
                      span="; ".join(assessment.get("reasons") or []), confidence=float(assessment.get("score") or 0.9))
        brand = value.get("site_name") or value.get("title")
        if brand:
            builder.claim("public_brand", "website_brand_title", brand[:200], eid, identity="website_brand_title", locator="html>head>title")
        if value.get("description"):
            builder.claim("public_brand", "self_description", value["description"][:600], eid, identity="self_description", locator='meta[name="description"]')
        builder.state("official_website", "available", None, discovery=value.get("discovery_method") or "registry_listed")
        social = value.get("social_links") or []
        for link in social:
            builder.claim("company_profiles", link["platform"], link["url"], eid, identity=[link["platform"], link["url"]], locator="a[href]",
                          span="profile linked from the verified company website")
        if not social:
            builder.state("company_profiles", "not_available", "verified website links no company-owned social profiles")
        for page in value.get("careers_pages") or []:
            builder.claim("jobs", "careers_page", page, eid, identity=["careers_page", page], locator="a[href]",
                          span="careers page on the verified company website")
        for item in value.get("news_items") or []:
            builder.claim("dated_activity", "website_news", {"date": item.get("date"), "title": item.get("title"), "url": item.get("url")},
                          eid, identity=["website_news", item.get("url")], effective_date=item.get("date"), locator=item.get("locator"))
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
            builder.claim("public_brand", "registry_declared_site", {"url": value.get("final_url"), "relation": "declared in the official register; exact-entity identity not verified (possible brand, parent or franchise site)"},
                          reg_eid, identity="registry_declared_site", locator="$.hjemmeside", confidence=0.6)
        builder.state("company_profiles", "ambiguous", "no verified website to anchor company-owned profiles")
    else:
        if state == "not_available":
            reason = reason or "no registry-listed website and no verified candidate"
        builder.state("official_website", state, reason)
        builder.state("company_profiles", "not_available" if state == "not_available" else state, "no verified website to anchor company-owned profiles")


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
                          eid, identity=["job_posted", job.get("uuid")], effective_date=str(job.get("published") or "")[:10] or None)
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
                builder.claim("dated_activity", "registered_role_change", {"date": date, "title": "Registered roles updated in Brønnøysund"}, eid,
                              identity=["registered_role_change", date], effective_date=date, locator="$[*].time")
    live = ev.get("registry_live") or {}
    live_value = live.get("value") or {}
    latest = live_value.get("latest_submitted_accounts")
    if latest:
        eid = _official_evidence(builder, live, "brreg_entity_json_v1")
        builder.claim("dated_activity", "annual_accounts_filed", {"date": None, "year": str(latest), "title": f"Annual accounts for {latest} submitted"}, eid,
                      identity=["annual_accounts_filed", str(latest)], reporting_period=str(latest), locator="$.sisteInnsendteAarsregnskap")


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
