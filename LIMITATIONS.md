# Limitations and source rights

## Known gaps

- **Websites.** Only about 11% of the eligible universe lists a website in the official register.
  - Companies without one get a website only when an exact-orgnr NAV ad declares the employer's homepage.
  - No search engine is used, so otherwise the website is `not_available`.
  - A search-API connector (e.g. Brave) could raise coverage but needs an evaluator-supplied key. It would be used for candidate generation only.
- **Jobs.** Only NAV's national job feed is used, covering ads modified in the last 45 days (about 10k active ads).
  - Jobs posted only on LinkedIn, Finn or company career portals are not captured, except as a `careers_page` claim on a verified site.
  - Candidate matching is by normalized employer name; publication is by exact `employer.orgnr`. An ad whose employer name differs from the legal and subunit names is missed.
- **Accounts.** The Regnskapsregisteret API frequently returns HTTP 503 from its load balancer (about 40–50% of requests in our tests).
  - Mitigations: a narrow retry lane with 8 attempts, a second sweep during the NAV walk, and carry-forward of the last supported value on refresh.
  - Companies still unresolved after that are `failed` with reason `HTTP 503`, which is a shared-source failure.
- **JavaScript-only websites** are not rendered, since no browser is used. The identity gate can then fail and the site becomes `ambiguous`.
- **Dated news** comes only from structured markup (JSON-LD articles and `<time datetime>` elements) on up to 5 pages of a verified site. Dates are never guessed from free text.
- **Sentiment and reviews** are not produced. We found no permitted, durable source that ties them to an exact organisation number.

## Source rights

| Source | Rights basis |
|---|---|
| Brønnøysundregistrene APIs | open data under NLOD 2.0 |
| NAV pam-stilling-feed | public token; use follows https://arbeidsplassen.nav.no/vilkar-api. Inactive ads are reported only as `closed_job` changes with their title; contact details are never stored. |
| Company websites | fetched only when robots.txt allows our user agent (`signalpost-agent/1.0`); 4xx robots means no restrictions, 5xx means disallow. At most about 7 requests per site. Only the company's own facts are extracted. |

## Safety

- **Outbound URLs.** Only public HTTP(S) hosts are fetched. Private, loopback, link-local and reserved IPs are blocked, and redirect targets are re-validated at each hop.
- **Secrets.** No secrets are needed. The NAV public token is fetched at run time from NAV's documented endpoint.
- **Personal data.** Personal data is limited to registered role holders' names as published by the official register. Birth dates are discarded.
