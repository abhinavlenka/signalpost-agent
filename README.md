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

## Budget guarantees

- **Priority order.** Work runs in phases, so a tight budget cuts optional enrichment first, never the basics:
  1. core registry facts for **every** company (accounts run concurrently from the start)
  2. registry-listed websites
  3. website discovery
  4. NAV job matching
  Each phase stops starting new companies as the deadline approaches. Companies it didn't reach get an explicit `failed` state with the reason.
- **Requests.** Every outbound HTTP attempt goes through one thread-safe governor (`budget.py`) before it is sent, including redirects, retries and robots.txt.
- **Envelopes.** Exactly one per input. A crash in one company, one section or the whole pipeline produces `failed` envelopes, never missing rows. Invalid input rows also get a `failed` row.
- **Cost.** $0 third-party API spend. All sources are free and public, and no API keys are needed.

## Sources (source ladder)

| Tier | Source | Used for | Access / licence |
|---|---|---|---|
| 1 | Brønnøysund Enhetsregisteret API: entity, roles, subunits, group structure, role-update log | identity, leadership, workplaces, group links, dated role changes | open API, [NLOD 2.0](https://data.norge.no/nlod/en/2.0) |
| 1 | Regnskapsregisteret API | latest filed annual accounts | open API, NLOD 2.0 |
| 1 | NAV arbeidsplassen `pam-stilling-feed` | job postings | public token, [API terms](https://arbeidsplassen.nav.no/vilkar-api) |
| 2 | Company website: listed in the official register, declared by the employer in a NAV ad, the company's registry e-mail domain, or a name-derived `.no` domain | verified website, company-owned social profiles, careers page, dated news, self-description | robots.txt respected; small bounded crawl |

**Not used:** search engines, LinkedIn, Meta, Glassdoor, Indeed, Google, or any unofficial scrapers.

`scripts/` still holds the starter kit's experimental connectors (LinkedIn guest pages, Google Maps, Brave, YouTube and others), kept unchanged because the starter kit's tests cover them. The run command never imports or calls them.

Job-ad contact persons, e-mails and phone numbers are never stored. Personal birth dates from the roles register are discarded.

## How identity is protected

The organisation number is the anchor throughout:

1. **Registry facts** come from the live registry record for that number.
2. **Websites.** A registry-listed site is published only when it passes an exact-entity gate: the org number appears anywhere in the site's raw HTML, or the full legal name appears in the homepage identity markup, or the listed domain spells the full legal name.
   - **Discovered sites.** A site found from a name-derived domain faces a stricter rule: the org number on the site, or the exact legal name **plus** the registered street address or phone. A site on the company's registry e-mail domain is company-declared to the register, so it faces the same gate as a registry-listed site.
   - **Failed gate.** Registry-listed sites that fail the gate are marked `ambiguous` and labelled as a registry-declared site under *public brand*. Nothing is extracted from them.
   - **Pages on someone else's site.** A deep page on a domain not named after the company (a chain's member page, a directory, a platform) is labelled `page_on_third_party_site`. Only the URL is published: no description, profiles, careers page or news, because those belong to the site owner.
   - **Possible group sites.** A non-`.no` site proven only by name is labelled `possibly_group_or_international`. Only Norway-specific social handles are published from it, and no news.
3. **Social profiles** are published only when a verified site links them and the handle matches the legal name.
4. **Jobs** are published only when the NAV ad's `employer.orgnr` is the company or one of its own registered subunits. Name similarity only nominates candidates.

## Output contract

See [`DATA_SCHEMA.md`](DATA_SCHEMA.md) for the envelope format, [`REFRESH.md`](REFRESH.md) for refresh semantics and [`LIMITATIONS.md`](LIMITATIONS.md) for known gaps.

## Explorer (desktop and mobile)

Every run writes the explorer to `out/site/`. To build it elsewhere, or from several runs:

```bash
uv run python scripts/build_site.py --envelopes out/envelopes.jsonl --site site
```

It is a static site (GitHub Pages ready, and it also opens from disk) with:
- search and filters
- side-by-side comparison of up to 4 companies
- per-company pages where every fact links to its evidence, retrieval time, content hash and locator
- summary sentences with clickable claim citations
- the change log and explicit unknowns

## Tests

```bash
uv run --with pytest pytest -q
```

## Models, APIs, licences

- **Models:** none. The summary is a deterministic, evidence-bounded template, and every sentence cites claim ids.
- **Third-party paid APIs:** none. Expected cost per 100-company batch is **$0**.
- **Code:** built on the Builderr Signalpost starter kit. Python dependencies are pinned in `uv.lock`.
