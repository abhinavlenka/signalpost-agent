# Limitations and source rights

## Known gaps

- **Websites.** Only about 11% of the eligible universe lists a website in the official register.
  - Companies without one are covered by three free discovery paths: an employer homepage declared in an exact-orgnr NAV ad, the company's registry e-mail domain, and name-derived `.no` domains. The last two require the org number, or the exact name plus address or phone, on the site.
  - Without a search key, websites under unrelated brand names are missed and stay `not_available`.
  - The optional Brave Search connector (`BRAVE_SEARCH_API_KEY`) generates candidates for those companies. It was tested against saved responses only, not live, because no key was available during development. Only a domain named after the company is fetched, so sites under unrelated brand names are still missed. A candidate is published only with the org number on the site, or the exact legal name plus the registered address or phone.
- **Jobs.** Only NAV's national job feed is used, covering ads modified in the last 45 days (about 10k active ads).
  - Jobs posted only on LinkedIn, Finn or company career portals are not captured, except as a `hiring_signal` claim for the careers page of a verified site.
  - Candidate matching is by normalized employer name; publication is by exact `employer.orgnr`. An ad whose employer name differs from the legal and subunit names is missed.
- **Accounts.** The Regnskapsregisteret API frequently returns HTTP 503 from its load balancer (about 40–50% of requests in our tests).
  - Mitigations: a narrow retry lane with 8 attempts, a second sweep during the NAV walk, and carry-forward of the last supported value on refresh.
  - Companies still unresolved after that are `failed` with reason `HTTP 503`, which is a shared-source failure.
- **JavaScript-only websites** are not rendered, since no browser is used. The identity gate can then fail and the site becomes `ambiguous`, unless the registered phone or address is in the raw HTML.
- **Bot-protection pages** (for example a Cloudflare challenge) are reported as `blocked`; nothing is published from them.
- **Social profiles** are published only when the handle matches the legal name. A profile under a brand name or a numeric id is not published even when the verified site links it.
- **Dated news** comes only from structured sources on a verified site: its declared RSS/Atom feed, JSON-LD articles and `<time datetime>` elements on up to 5 pages. When those give fewer than 4 items, up to 4 article pages are read, found through news links on the site and its sitemap, and each is dated by its own JSON-LD, publication meta tag or `<time datetime>` in the article. Dates are never guessed from free text, so a site that prints dates only as text yields no news.
  - Homepage, about, contact and careers pages are skipped even when the site marks them as articles.
  - Product or information pages that a site marks as articles can still appear as news.
- **Pages that change on every request.** Some sites put a build time, a token or a rotating block in every response. Two live runs then store different bytes and record a different `content_sha256`, and so a different evidence id, even though every claim read from the page is the same. On frozen inputs the record is identical.
- **Sentiment and reviews** are not produced. We found no permitted, durable source that ties them to an exact organisation number.

## Source rights

| Source | Rights basis |
|---|---|
| Brønnøysundregistrene APIs | open data under NLOD 2.0 |
| NAV pam-stilling-feed | public token; use follows https://arbeidsplassen.nav.no/vilkar-api. Inactive ads are reported only as `closed_job` changes with their title; contact details are never stored. |
| Company websites | fetched only when robots.txt allows our user agent (`abhikilde/1.0`); 4xx robots means no restrictions, 5xx means disallow. At most 14 requests per site plus redirects: the homepage, robots.txt, up to 5 priority pages, the feed, up to 2 sitemap requests and up to 4 article pages. Only the company's own facts are extracted. |

## Safety

- **Outbound URLs.** Only public HTTP(S) hosts are fetched. Private, loopback, link-local and reserved IPs are blocked, and redirect targets are re-validated at each hop.
- **Secrets.** No secrets are needed. The NAV public token is fetched at run time from NAV's documented endpoint.
- **Personal data.** Personal data is limited to registered role holders' names as published by the official register. Birth dates are discarded.
