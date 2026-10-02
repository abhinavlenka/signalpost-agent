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
| 5 | The registered phone or address is on the site, and every distinguishing word of the legal name is on the homepage or spells the domain. Country words (Norway, Norge, Nordic and similar) are ignored. | sites declared to the register |
| 6 | The exact legal name is on the homepage and the registered address or phone is on the site | sites nobody declared: name-derived domains, search candidates |
| 7 | The employer with this organisation number declared the homepage in a NAV job ad, and the page has real content | NAV-declared homepages |

"Declared to the register" means the website field or the e-mail domain in the company's own register record.

Everything else is `ambiguous`: nothing is extracted, and a register-declared URL is reported only as a labelled, unverified site under public brand.

### Why contact details alone are not enough

Sister companies often share a switchboard and an address, and holding companies often list the operating company's site. Rule 5 therefore also requires the name. "WORK SYSTEM NORWAY AS" passes on a site that says "Work System" and carries its phone number; a property company that lists its owner's site does not.

### Other rejections

- Parked, for-sale and hosting placeholder pages.
- Bot-protection interstitials (reported as `blocked`).
- A company sports club (`BIL`) that points to the operating company's site without club evidence.

## Site scope

A verified site still gets a scope label that limits what is taken from it (see `DATA_SCHEMA.md`). Two scopes restrict extraction:

- **`page_on_third_party_site`**: a deep page on a domain not named after the company, such as a chain's member page. Only the URL is published.
- **`possibly_group_or_international`**: a non-`.no` site proven by name only, where the entity is not a public company and no registered address or phone is on the site. Only Norway-specific profiles are published, and no news.

## Social profiles

A profile is published when all of these hold:

1. It is linked from a verified site, or declared in the site's Organization `sameAs` markup or publisher meta tag.
2. The handle contains the legal name, or its single distinctive word, or most of its words.
3. It is a profile page, not a share button, post, event or group.

## Jobs

A NAV ad is published only when its `employer.orgnr` equals the organisation number or one of the company's registered sub-units. Name similarity only selects which ads to open.

## Group links

Parent and subsidiary links come from the register's group structure and are published as labelled relationships. Facts about a parent or subsidiary are never merged into the company's own profile.
