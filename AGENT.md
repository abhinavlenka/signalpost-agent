# Research and abstention policy

The agent researches each organisation number in a fixed order and publishes a fact only when it can show where the fact came from and that it belongs to this exact legal entity.

## Research order

1. **Official records.** Entity record, roles, sub-units, group links and the role-update log from Brønnøysund. Annual accounts run in parallel because that service is slow.
2. **Declared website.** The site the company itself listed in the register.
3. **Discovered website.** In order: the site verified in the previous run, the register e-mail domain, name-derived domains, and (only with a key) search-API candidates.
4. **Jobs.** NAV's national job feed, matched on the employer's organisation number.
5. **Employer-declared homepage.** A homepage stated in an exact-number NAV ad, for companies still without a site.

Each phase stops starting new companies as the deadline approaches. A company a phase did not reach gets `failed` with the reason.

## When the agent abstains

| Situation | What is published |
|---|---|
| A site cannot be tied to the entity | nothing from the site; state `ambiguous`; the register-declared URL is reported under public brand |
| A site is a page on a chain, directory or platform | the URL only, labelled `page_on_third_party_site` |
| A non-`.no` site is proven by name only | the URL and Norway-specific profiles; no news; labelled `possibly_group_or_international` |
| A source refuses access or shows a bot challenge | nothing; state `blocked` |
| A source errors or the budget runs out | nothing new; state `failed`; the last supported value is carried forward as stale on refresh |
| A figure is absent from the accounts | no claim. Absence is never turned into zero. |
| A social handle does not match the legal name | not published |

## What the agent never does

- It never publishes a fact from a search result, a directory or a third-party profile.
- It never infers a date from free text.
- It never stores contact persons, e-mail addresses or phone numbers from job ads, or birth dates from the roles register.
- It uses no model: the summary is a template over published claims, and every sentence lists the claims it rests on.
