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
| `state/latest/<org>.json` | latest envelope per company (the previous-run input for the next refresh) |
| `state/snapshots/<run_id>/envelopes.jsonl` | immutable per-run snapshot |

Run the same command again and it refreshes: every envelope is diffed against `state/latest`. To diff against an explicit previous output, pass `--previous path/to/envelopes.jsonl`.

The frozen company universe (`data/signalpost-universe.jsonl.gz`, SHA-256 `1c89710e…0384`) is used as a fallback identity anchor. If the file is absent, the agent still runs from the live registry.

Useful flags:
- `--out`, `--state` choose the output and state directories.
- `--max-requests` defaults to 1900, below the 2,000 cap.
- `--deadline-minutes` defaults to 38, below the 45-minute limit.
- `--workers` defaults to 12.
- `--nav-days` defaults to 45.
- `--no-nav` skips the jobs connector.

## Budget guarantees

- **Requests.** Every outbound HTTP attempt goes through one thread-safe governor (`budget.py`) before it is sent, including redirects, retries and robots.txt. The hard cap defaults to 1,900. Once the cap is reached, sources return `failed` with reason `budget` instead of fetching.
- **Time.** Fetching stops at the deadline, and envelopes are always written.
- **Envelopes.** Exactly one per input. A crash in one company, one section or the whole pipeline produces `failed` envelopes, never missing rows. Invalid input rows also get a `failed` row.
- **Cost.** $0 third-party API spend. All sources are free and public, and no API keys are needed.

A measured random 100-company batch took about 5 minutes and about 950 requests.

## Sources (source ladder)

| Tier | Source | Used for | Access / licence |
|---|---|---|---|
| 1 | Brønnøysund Enhetsregisteret API: entity, roles, subunits, group structure, role-update log | identity, leadership, workplaces, group links, dated role changes | open API, [NLOD 2.0](https://data.norge.no/nlod/en/2.0) |
| 1 | Regnskapsregisteret API | latest filed annual accounts | open API, NLOD 2.0 |
| 1 | NAV arbeidsplassen `pam-stilling-feed` | job postings | public token, [API terms](https://arbeidsplassen.nav.no/vilkar-api) |
| 2 | Company website: listed in the official register, declared by the employer in a NAV ad, the company's registry e-mail domain, or a name-derived `.no` domain | verified website, company-owned social profiles, careers page, dated news, self-description | robots.txt respected; small bounded crawl |

**Not used:** search engines, LinkedIn, Meta, Glassdoor, Indeed, Google, or any unofficial scrapers.

Job-ad contact persons, e-mails and phone numbers are never stored. Personal birth dates from the roles register are discarded.

## How identity is protected

The organisation number is the anchor throughout:

1. **Registry facts** come from the live registry record for that number.
2. **Websites.** A registry-listed site is published only when it passes an exact-entity gate: the org number appears anywhere in the site's raw HTML, or the full legal name appears in the homepage identity markup, or the listed domain spells the full legal name.
   - **Discovered sites.** Sites found from the registry e-mail domain or a name-derived domain face a stricter rule: the org number on the site, or the exact legal name **plus** the registered street address or phone.
   - **Failed gate.** Registry-listed sites that fail the gate are marked `ambiguous` and labelled as a registry-declared site under *public brand*. Nothing is extracted from them.
   - **Possible group sites.** A non-`.no` site proven only by name is labelled `possibly_group_or_international`. Only Norway-specific social handles are published from it, and no news.
3. **Social profiles** are published only when a verified site links them and the handle matches the legal name.
4. **Jobs** are published only when the NAV ad's `employer.orgnr` is the company or one of its own registered subunits. Name similarity only nominates candidates.

## Output contract

See [`DATA_SCHEMA.md`](DATA_SCHEMA.md) for the envelope format, [`REFRESH.md`](REFRESH.md) for refresh semantics and [`LIMITATIONS.md`](LIMITATIONS.md) for known gaps.

## Explorer (desktop and mobile)

```bash
uv run python scripts/build_site.py --envelopes out/envelopes.jsonl --site site
```

This builds a static site (GitHub Pages ready) with:
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
