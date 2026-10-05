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
  "refresh": {"changes_detected": 0, "material_changes": 0, "carried_forward_claims": 0, "backfilled_claims": 0},   // section 7: counts only
  "changes": [ /* material changes since the previous run */ ],
  "change_log": [ /* all changes across runs, deduplicated by change_id */ ],
  "summary": {"text": "...", "sentences": [{"text": "...", "claim_ids": ["cl-..."], "section": "finances"}],
              "sections": [{"key": "finances", "title": "Finances", "text": "..."}], "unknowns": [...], "unknowns_text": "...", "changes_text": "..."},
  "errors": [],                            // {"source": "...", "error": "..."}, sorted; no tracebacks or file paths
  "operations": {"requests": 9, "runtime_ms": 8120, "third_party_cost_usd": 0}
}
```

Run-scoped values are confined to `run`, `operations` and `evidence[].retrieved_at`. Every other field depends only on the source content, so the same sources give the same record on every run, whether it is a first run or a refresh; see [`REFRESH.md`](REFRESH.md#determinism). What the run was compared against is in `run-report.json`, not in the envelope.

External facts (`official_website`, `company_profile`, `social_profile`, `hiring_signal`, `dated_news`, `dated_activity`) always carry a plain string as their value; details sit in keys beside the value. A claim with `alias_of` repeats a fact published under another field name: it is the same fact, counted once in the summary and the explorer, and it reports no changes of its own on refresh.

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
  "claim_span": "\"etternavn\" : \"Aas\" · rolle: Daglig leder (DAGL)",   // the source text the claim rests on
  "span_kind": "source_text",                     // source_text: found verbatim in the fetched source; rendered_value: written out from the parsed value
  "value_hash": "…",                              // used by refresh
  "stale": true,        // only when carried forward because the source failed this run
  "historical": true,   // only for dated history that aged out of the source window
  "backfilled": true    // first observed after a previously failed source recovered (not a change)
}
```

## Evidence

```jsonc
{
  "id": "ev-…",                         // the key name used by the published output contract
  "evidence_id": "ev-…",                // same value; kept for earlier consumers
  "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/888567232",
  "final_url": "…",                     // after redirects
  "source_class": "official_registry_live | official_annual_accounts | official_roles | official_subunits | official_group_structure | official_role_update_log | official_job_register_nav | registry_linked_company_website | registry_email_domain | name_derived_domain | employer_declared_homepage | search_discovered_website | frozen_universe_snapshot",
  "retrieved_at": "2026-09-27T00:31:21Z",
  "http_status": 200,
  "content_sha256": "…",                // hash of the exact response body
  "extraction_method": "brreg_entity_json_v1 | deterministic_name_org_evidence_v2 | nav_feed_entry_json_v1 | …",
  "reporting_period": "2025-01-01..2025-12-31",
  "note": "…",
  "claim_span": "\"organisasjonsnummer\" : \"888567232\"",   // source text showing the record is about this entity
  "snapshot_path": "raw/5d/5d3ef6…c57a.gz",   // the stored response, relative to the state directory
  "rights": "Brønnøysund Register Centre open data, NLOD 2.0",
  "from_previous_run": true             // only on evidence carried from the previous snapshot
}
```

### Spans and snapshots

- Every claim has a `claim_span` and a `span_kind`. `source_text` means the span was found verbatim in the fetched source: for register data the `"key" : value` text exactly as the API returned it, for website data the page text. `rendered_value` means the exact text could not be located (or the source is carried from an earlier run), and the span is the parsed value written out. The run report counts both.
- News items cite the page or feed they were read from as their own evidence record, not the homepage.
- Every response that becomes evidence is stored once, gzip-compressed, at `<state>/raw/<first two hex chars>/<sha256>.gz`. `content_sha256` is the hash of those bytes, so a claim can be checked against what was fetched.
- The NAV feed pages and search-API responses are not stored: they are used to find candidates, not as claim evidence.

### Website scope

The `website_scope` claim says how the published site is tied to the entity:

| Value | Meaning |
|---|---|
| `exact_entity_verified_by_organisation_number` | the organisation number is on the site |
| `norwegian_domain` | verified on a `.no` domain without the organisation number on the site |
| `verified_by_registered_address_or_phone` | a non-`.no` domain whose homepage carries the registered address or phone |
| `public_company_own_site` | verified by legal name; the entity is a Norwegian public company (ASA) |
| `possibly_group_or_international` | a non-`.no` domain without that proof on its homepage, where the entity is not a public company; only Norway-specific profiles are published, and no news |
| `page_on_third_party_site` | a page about the company on a chain, directory or platform; only the URL is published |

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
| company_profiles | `company_profile`: one claim per profile linked from the verified site or declared in its Organization markup; the value is the profile URL and `platform` names the network. The same fact is also published as `social_profile`, marked `alias_of: company_profile` |
| jobs | `hiring_signal`: one claim per signal, the value is its URL and `signal` is `careers_page` (cited to the careers page itself) or `job_ad` (`alias_of: open_job`); `open_job`: the full NAV ad (exact orgnr) |
| dated_activity | `dated_news`: one claim per news item on the verified site; the value is the string `Title (published time)`, with `title`, `date`, `published_at` and `url` as keys beside it, and the same fact also published as `dated_activity`, marked `alias_of: dated_news`; `job_posted`; registered role changes; accounts filed |
