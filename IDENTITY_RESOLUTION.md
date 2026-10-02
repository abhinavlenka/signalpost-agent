# Identity resolution

The organisation number is the only key. A website, a profile or a job ad is a candidate until it is tied back to that number.

## Website gate

A fetched site is published as the official website when one of these holds, checked in this order:

| # | Proof | Applies to |
|---|---|---|
| 1 | The organisation number appears in the site's HTML (homepage or one of the crawled pages) | any candidate |
| 2 | Every word of the legal name appears together in the homepage title, description, structured data or host name | sites declared to the register |
| 3 | A one-word legal name appears in the homepage identity text, and the homepage has real content | sites declared to the register |
| 4 | The declared domain spells the full legal name (at least 5 characters) | sites declared to the register |
| 5 | The registered phone or address is on the site, and the site is named after the company: the domain spells the legal name with country words (Norway, Norge, Nordic and similar) set aside, or a one-word legal name is in the homepage title, description or structured data. | sites declared to the register |
| 6 | The exact legal name is on the homepage and the registered address or phone is on the site | sites nobody declared: name-derived domains, search candidates |
| 7 | The employer with this organisation number declared the homepage in a NAV job ad, and the page has real content | NAV-declared homepages |

"Declared to the register" means the website field or the e-mail domain in the company's own register record.

Everything else is `ambiguous`: nothing is extracted, and a register-declared URL is reported only as a labelled, unverified site under public brand.

### Why contact details alone are not enough

Sister companies often share a switchboard and an address, their names share words, and holding companies often list the operating company's site. Rule 5 therefore requires the site itself to be named after the company. "WORK SYSTEM NORWAY AS" passes on `worksystem.no` with its phone number on the site. "HANSEN EIENDOM AS" does not pass on `hansen-bygg.no`, even if both words appear somewhere on the page, and a property company that lists its owner's site does not pass either.

### Other rejections

- Parked, for-sale and hosting placeholder pages.
- Bot-protection interstitials (reported as `blocked`).
- A company sports club (`BIL`) that points to the operating company's site without club evidence.

## Site scope

A verified site still gets a scope label that limits what is taken from it (see `DATA_SCHEMA.md`). Two scopes restrict extraction:

- **`page_on_third_party_site`**: a deep page on a domain not named after the company, such as a chain's member page. Only the URL is published.
- **`possibly_group_or_international`**: a non-`.no` site proven by name only, where the entity is not a public company and the homepage does not carry the registered address or phone. Only Norway-specific profiles are published, and no news. An address on a contact page does not count: a group's contact page lists every subsidiary's office.

## Social profiles

A profile is published when all of these hold:

1. It is linked from a verified site, or declared in the `sameAs` of the site's Organization markup or in its publisher meta tag. `sameAs` on other nodes, such as an article's author, is ignored.
2. The handle contains the legal name, or its single distinctive word, or most of its words.
3. It is a profile page, not a share button, post, event or group.

## Search candidates

With a search key, results are used only to find domains to look at. Directories and registries print every company's name and organisation number, so a number on such a page proves nothing. A result is fetched only when its registrable domain is named after the company (it contains the first word of the legal name, or two of its words), and only from the domain root. The fetched site must then pass rule 1 or rule 6.

## Jobs

A NAV ad is published only when its `employer.orgnr` equals the organisation number or one of the company's registered sub-units. Name similarity only selects which ads to open.

## Group links

Parent and subsidiary links come from the register's group structure and are published as labelled relationships. Facts about a parent or subsidiary are never merged into the company's own profile.
