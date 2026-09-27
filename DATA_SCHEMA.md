# Envelope schema (`signalpost-envelope/1.0`)

`out/envelopes.jsonl` holds one JSON object per input organisation number.

```jsonc
{
  "schema_version": "signalpost-envelope/1.0",
  "organisation_number": "888567232",
  "legal_name": "AAS ELEKTRONIKK AS",
  "state": "available",                    // overall terminal state
  "run": {"run_id": "...", "agent_version": "...", "started_at": "...", "completed_at": "...", "terminal_status": "completed"},
  "sections": {                             // required sections 1-5, each with per-family states
    "legal_identity":        {"title": "1. Legal identity and public brand", "fields": {"legal_identity": {...}, "public_brand": {...}, "group_relationships": {...}}},
    "annual_accounts":       {"title": "2. Latest annual accounts and available history", "fields": {"annual_accounts": {...}, "filing_history": {...}}},
    "leadership_workplaces": {"title": "3. Leadership and registered workplaces", "fields": {"leadership": {...}, "registered_workplaces": {...}}},
    "web_presence":          {"title": "4. Verified official website and company-owned profiles", "fields": {"official_website": {...}, "company_profiles": {...}}},
    "hiring_activity":       {"title": "5. Hiring and dated public activity", "fields": {"jobs": {...}, "dated_activity": {...}}}
  },
  "claims": [ /* section 6: claim-level evidence */ ],
  "evidence": [ /* evidence records referenced by claims */ ],
  "availability": {"legal_identity": "available", "jobs": "not_available", "...": "..."},
  "refresh": { /* section 7: refresh metadata */ },
  "changes": [ /* material changes since the previous run */ ],
  "change_log": [ /* all changes across runs, deduplicated by change_id */ ],
  "summary": {"text": "...", "sentences": [{"text": "...", "claim_ids": ["cl-..."]}], "unknowns": [...], "changes_text": "..."},
  "errors": [],
  "operations": {"runtime_ms": 8120, "third_party_cost_usd": 0}
}
```

## States

Every field family has exactly one state:

| State | Meaning |
|---|---|
| `available` | at least one supported claim was published |
| `not_available` | the source was checked successfully and returned nothing (e.g. no active NAV ads, no registered subunits) |
| `blocked` | the source refused access (robots.txt, HTTP 401/403/429) |
| `not_applicable` | the question does not apply (e.g. the entity is not part of a group) |
| `ambiguous` | a candidate exists but its exact-entity match could not be verified; nothing is published from it |
| `failed` | the source errored, or the run budget or deadline was reached before it was checked |

The state always comes with a `reason`. **Absence is never converted to zero.** For example, an unreported employee count or financial line item is simply not claimed.

## Claim

```jsonc
{
  "claim_id": "cl-5f0c…",                  // sha256 of claim_key
  "claim_key": "888567232|leadership|role|[\"dagl\",\"tor ivar aas\",null]",   // stable across runs
  "section": "leadership_workplaces",
  "family": "leadership",
  "field": "role",
  "value": {"role": "Daglig leder", "role_code": "DAGL", "name": "Tor Ivar Aas", ...},
  "availability": "available",
  "confidence": 1.0,
  "reporting_period": "2025-01-01..2025-12-31",   // financial claims
  "effective_date": "2025-12-31",
  "evidence_ids": ["ev-3b96…"],
  "locator": "$.rollegrupper[*].roller[*]",       // JSON path, CSS selector or markup locator
  "claim_span": "…",                              // proof text where relevant
  "value_hash": "…",                              // used by refresh
  "first_seen": "…", "last_seen": "…",
  "stale": true,        // only when carried forward because the source failed this run
  "historical": true,   // only for dated history that aged out of the source window
  "backfilled": true    // first observed after a previously failed source recovered (not a change)
}
```

## Evidence

```jsonc
{
  "evidence_id": "ev-…",
  "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/888567232",
  "final_url": "…",                     // after redirects
  "source_class": "official_registry_live | official_annual_accounts | official_roles | official_subunits | official_group_structure | official_role_update_log | official_job_register_nav | registry_linked_company_website | employer_declared_homepage | frozen_universe_snapshot",
  "retrieved_at": "2026-09-27T00:31:21Z",
  "http_status": 200,
  "content_sha256": "…",                // hash of the exact response body
  "extraction_method": "brreg_entity_json_v1 | deterministic_name_org_evidence_v2 | nav_feed_entry_json_v1 | …",
  "reporting_period": "2025-01-01..2025-12-31",
  "note": "…",
  "from_previous_run": "run-id"         // evidence carried from the previous snapshot
}
```

## Field families

| Family | Typical fields |
|---|---|
| legal_identity | legal_name, organisation_number, legal_form, business/postal address, municipality, industry, founded/registered dates, VAT, bankruptcy/liquidation flags, stated activity, statutory purpose, share capital, registered employees |
| public_brand | previous legal names, website title, self-description, registry-declared (unverified) site |
| group_relationships | parent / subsidiary links with ownership basis |
| annual_accounts | revenue, operating result, profit before tax, net result, assets, equity, debt (per reporting period, with currency) |
| filing_history | latest submitted accounts year, filed annual-account copies |
| leadership | registered roles (CEO, chair, board, deputies, owners), auditor, accountant |
| registered_workplaces | subunits with address, industry, registered employees |
| official_website | verified website plus discovery method |
| company_profiles | social profiles linked from the verified site |
| jobs | active NAV job ads (exact orgnr), careers page |
| dated_activity | website news items, job postings, registered role changes, accounts filed |
