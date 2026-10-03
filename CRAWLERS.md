# Connectors, budgets and fallback rules

Every outbound request, including redirects, retries and robots.txt, passes one thread-safe governor (`src/abhikilde/budget.py`). The run stops fetching when the request cap or the deadline is reached.

## Connectors

| Connector | Source | Requests per company | Retries and pacing |
|---|---|---|---|
| Entity, roles, sub-units, role log | `data.brreg.no/enhetsregisteret` | 4, plus 1 for group members | 3 attempts with backoff |
| Annual accounts | `data.brreg.no/regnskapsregisteret` | 1 to 8 | narrow lane, steady jittered retries, then a second sweep for failures |
| Company website | the company's own site | at most 8 plus redirects: robots.txt, homepage, up to 5 priority pages, the declared feed | none; a failed page is skipped |
| Website discovery | register e-mail domain and name-derived domains | at most 4 homepage probes and 2 bounded crawls | none |
| Search candidates (optional) | Brave Search API | 1 query and at most 3 probes of domains named after the company | 2 attempts; capped per run |
| Jobs | NAV `pam-stilling-feed` | about 97 feed pages per run, shared by all companies, plus 1 per candidate ad | 3 attempts |

## Website crawl rules

- Only public HTTP(S) hosts are fetched. Private, loopback, link-local and reserved addresses are refused, and every redirect target is checked again.
- robots.txt is honoured for the user agent `abhikilde/1.0`. A 4xx robots response means no restrictions; a 5xx means disallow.
- Static HTML only. No browser, no JavaScript rendering.
- Priority pages are chosen one per category: about, contact, careers, news, people, locations.
- A homepage over 2 MB, a page over 1 MB or a feed over 1 MB is not read.

## Fallback order for a missing website

1. Re-check the site verified in the previous run.
2. The register e-mail domain, when it is not a public mail provider.
3. Name-derived domains (`.no` first, then `.com`), after a DNS check.
4. Search-API candidates, only when `BRAVE_SEARCH_API_KEY` is set.
5. A homepage declared by the employer in an exact-number NAV ad.

A candidate found in steps 3 to 5 is published only after the fetched site passes the gate in [`IDENTITY_RESOLUTION.md`](IDENTITY_RESOLUTION.md).

## Budget order

When time or requests run short, optional work is cut first: official records for every company, then declared websites, then discovery, then jobs. The defaults are 19 requests per input company and a 40-minute deadline; both can be set by flag or environment variable (see the README).

## What is stored

The raw response behind each evidence record is kept under `<state>/raw/` by content hash. NAV feed pages and search responses are not kept, because they are not claim evidence.
