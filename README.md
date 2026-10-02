# Signalpost agent

A company-intelligence agent for Builderr's Signalpost challenge. You give it Norwegian organisation numbers. For each one it returns a terminal envelope:
- claims with sources, retrieval times, content hashes and locators
- an explicit availability state for every field family
- a claim-level refresh diff against the previous run

## Run it (one command)

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --frozen
uv run python -m signalpost run --input batch.txt
```

`batch.txt` may be a text, CSV, JSON or JSONL list of organisation numbers. The command writes:

| Path | Content |
|---|---|
| `out/envelopes.jsonl` | exactly one terminal envelope per input number |
| `out/run-report.json` | request count by purpose, runtime, p50/p95, field-state totals, validation |
| `out/site/index.html` | the explorer for this run; open it straight from disk, no web server needed |
| `state/latest/<org>.json` | latest envelope per company (the previous-run input for the next refresh) |
| `state/snapshots/<run_id>/envelopes.jsonl` | immutable per-run snapshot |
| `state/raw/<aa>/<sha256>.gz` | the raw response behind each evidence record, stored once by content hash |

Run the same command again and it refreshes: every envelope is diffed against `state/latest`. To diff against an explicit previous output, pass `--previous path/to/envelopes.jsonl`.

The frozen company universe (`data/signalpost-universe.jsonl.gz`, SHA-256 `1c89710e…0384`) is used as a fallback identity anchor. If the file is absent, the agent still runs from the live registry.

Limits are set by flags or environment variables, so the evaluator can match its budget without code changes:

| Flag | Env var | Default |
|---|---|---|
| `--max-requests` | `SIGNALPOST_MAX_REQUESTS` | 19 × number of input companies |
| `--deadline-minutes` | `SIGNALPOST_DEADLINE_MINUTES` | 40 |
| `--workers` | `SIGNALPOST_WORKERS` | 16 |
| `--accounts-lanes` | `SIGNALPOST_ACCOUNTS_LANES` | 6 |

Other flags: `--out`, `--state`, `--previous`, `--nav-days` (45), `--no-nav`.

Optional search discovery is off unless a key is supplied:

| Env var | Default | Effect |
|---|---|---|
| `BRAVE_SEARCH_API_KEY` | unset | turns on search-API candidate discovery for companies with no verified website |
| `SIGNALPOST_SEARCH_MAX_QUERIES` | one per input company | hard cap on paid queries per run |
| `SIGNALPOST_SEARCH_COST_PER_QUERY` | 0.005 | USD per query, used for the cost figures in the run report |

## Budget guarantees

- **Priority order.** Work runs in phases, so a tight budget cuts optional enrichment first, never the basics:
  1. core registry facts for **every** company (accounts run concurrently from the start)
  2. registry-listed websites
  3. website discovery
  4. NAV job matching
  Each phase stops starting new companies as the deadline approaches. Companies it didn't reach get an explicit `failed` state with the reason.
- **Requests.** Every outbound HTTP attempt goes through one thread-safe governor (`budget.py`) before it is sent, including redirects, retries and robots.txt.
- **Envelopes.** Exactly one per input. A crash in one company, one section or the whole pipeline produces `failed` envelopes, never missing rows. Invalid input rows also get a `failed` row.
- **Cost.** $0 third-party API spend by default: all sources are free and public, and no API keys are needed. With the optional search key, cost is at most one query per input company, and the run report states the queries used and their cost.

## Sources (source ladder)

| Tier | Source | Used for | Access / licence |
|---|---|---|---|
| 1 | Brønnøysund Enhetsregisteret API: entity, roles, subunits, group structure, role-update log | identity, leadership, workplaces, group links, dated role changes | open API, [NLOD 2.0](https://data.norge.no/nlod/en/2.0) |
| 1 | Regnskapsregisteret API | latest filed annual accounts | open API, NLOD 2.0 |
| 1 | NAV arbeidsplassen `pam-stilling-feed` | job postings | public token, [API terms](https://arbeidsplassen.nav.no/vilkar-api) |
| 2 | Company website: listed in the official register, declared by the employer in a NAV ad, the company's registry e-mail domain, or a name-derived `.no` domain | verified website, company-owned social profiles, careers page, dated news (page markup and the site's own RSS/Atom feed), self-description | robots.txt respected; small bounded crawl |
| 5 | Brave Search API (optional, key required) | website candidates only; nothing from a search result is published | paid API; only a domain named after the company is fetched, and it must pass the same gate as any undeclared site |

**Not used:** LinkedIn, Meta, Glassdoor, Indeed, Google, scraped search-engine pages, or any unofficial scrapers.

`scripts/` still holds the starter kit's experimental connectors (LinkedIn guest pages, Google Maps, Brave, YouTube and others), kept unchanged because the starter kit's tests cover them. The run command never imports or calls them.

Job-ad contact persons, e-mails and phone numbers are never stored. Personal birth dates from the roles register are discarded.

## How identity is protected

The organisation number is the anchor throughout:

1. **Registry facts** come from the live registry record for that number.
2. **Websites.** A registry-listed site is published only when it passes an exact-entity gate: the org number appears anywhere in the site's raw HTML, or the full legal name appears in the homepage identity markup, or the listed domain spells the full legal name.
   - **Contact-detail proof.** A site the company declared to the register also passes when its registered phone or address is on the site and the site is named after the company: the domain spells the legal name (country words such as "Norway" aside), or a one-word name is in the homepage title. A sister company that shares a switchboard still fails.
   - **Discovered sites.** A site found from a name-derived domain faces a stricter rule: the org number on the site, or the exact legal name **plus** the registered street address or phone. A site on the company's registry e-mail domain is company-declared to the register, so it faces the same gate as a registry-listed site.
   - **Failed gate.** Registry-listed sites that fail the gate are marked `ambiguous` and labelled as a registry-declared site under *public brand*. Nothing is extracted from them.
   - **Pages on someone else's site.** A deep page on a domain not named after the company (a chain's member page, a directory, a platform) is labelled `page_on_third_party_site`. Only the URL is published: no description, profiles, careers page or news, because those belong to the site owner.
   - **Possible group sites.** A non-`.no` site proven only by name is labelled `possibly_group_or_international`. Only Norway-specific social handles are published from it, and no news. A site whose homepage carries the registered address or phone, or the site of a Norwegian public company (ASA), is treated as the company's own. An address on a contact page does not count, because a group's contact page lists every subsidiary's office.
3. **Social profiles** are published only when a verified site links them, or declares them in the `sameAs` of its Organization markup, and the handle matches the legal name. An author's or product's `sameAs` is ignored.
4. **Jobs** are published only when the NAV ad's `employer.orgnr` is the company or one of its own registered subunits. Name similarity only nominates candidates.

## Output contract

| Document | Content |
|---|---|
| [`DATA_SCHEMA.md`](DATA_SCHEMA.md) | envelope, claim and evidence format |
| [`REFRESH.md`](REFRESH.md) | refresh semantics and idempotency |
| [`AGENT.md`](AGENT.md) | research order and when the agent abstains |
| [`CRAWLERS.md`](CRAWLERS.md) | connectors, request budgets and fallback rules |
| [`IDENTITY_RESOLUTION.md`](IDENTITY_RESOLUTION.md) | candidate and publication gates |
| [`EVAL.md`](EVAL.md) | how the agent is measured locally |
| [`LIMITATIONS.md`](LIMITATIONS.md) | known gaps and source rights |

## Explorer (desktop and mobile)

Every run writes the explorer to `out/site/`. To build it elsewhere, or from several runs:

```bash
uv run python scripts/build_site.py --envelopes out/envelopes.jsonl --site site
```

It is a static site (GitHub Pages ready, and it also opens from disk) with no dependencies. A hosted copy of the 100-company smoke test is at https://abhinavlenka.github.io/signalpost-agent/.

- **Find.** Search by name, organisation number, place, industry, website or leader. Filter by verified website, hiring signal, company profiles, news, revenue, profitability and changes; each filter shows its count. More filters cover municipality, legal form, minimum employees and minimum revenue. Companies can be pinned and filtered by pin. Sort by coverage, name, revenue, result or employees. The search, filters and sort live in the URL, so a view can be shared. Export the current list as CSV.
- **Compare.** Pick up to 4 companies from the list or a company page and compare identity, latest accounts, people and web presence side by side. The highest figure in each row is marked.
- **Verify.** Every fact shows its source, retrieval time, content hash, locator and the quoted source text, and has its own link. A company's facts can be exported with their evidence as CSV, or the full profile as JSON. Summary sentences cite their facts. Accounts are shown as a year-by-year table whose figures link to their evidence. Each company page links to the official register.
- **Desktop and mobile.** Card layout on phones, light and dark themes, keyboard access, visible focus, touch-sized controls, and status shown by text and shape as well as colour.

## 100-company smoke test

[`reports/smoke-100/`](reports/smoke-100/) holds the run report, the refresh run report and the envelopes from a clean-clone run on 100 random companies: 100 of 100 envelopes, 1,037 requests, 6.5 minutes, and 0 changes on the refresh run. The same profiles are browsable in the explorer under [`docs/`](docs/).

## Tests

```bash
uv run --with pytest pytest -q
```

## Models, APIs, licences

- **Models:** none. The summary is a deterministic, evidence-bounded template, and every sentence cites claim ids.
- **Third-party paid APIs:** none by default; expected cost per official run is **$0**. Optional: Brave Search API when `BRAVE_SEARCH_API_KEY` is set, at most one query per input company (about $6 for 1,200 companies at $0.005 per query).
- **Code:** built on the Builderr Signalpost starter kit. Python dependencies are pinned in `uv.lock`.
