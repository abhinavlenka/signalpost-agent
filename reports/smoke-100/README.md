# 100-company smoke test

Run on 2026-10-02 from a fresh clone at commit `1680953`, in an empty virtual environment, with only the declared install step:

```bash
uv sync --frozen
uv run python -m signalpost run --input data/batch-rehearsal-100.txt --out out/run1 --state state --run-id smoke100-run
uv run python -m signalpost run --input data/batch-rehearsal-100.txt --out out/run2 --state state --run-id smoke100-refresh
```

The batch is 100 organisation numbers drawn at random from the public universe. The second command is the same run again, to exercise refresh.

| | First run | Refresh run |
|---|---|---|
| Report | [`run-report.json`](run-report.json) | [`refresh-run-report.json`](refresh-run-report.json) |
| Envelopes returned | 100 of 100 | 100 of 100 |
| Envelope state | 100 `available` | 100 `available` |
| Claims | 4,743 | 4,743 |
| Changes reported | 0 (first observation) | 0 (nothing changed between runs) |
| Outbound requests | 1,037 of 1,900 allowed | 1,023 of 1,900 allowed |
| Wall-clock time | 5 min 39 s | 5 min 30 s |
| Third-party API cost | $0 | $0 |

[`envelopes.jsonl`](envelopes.jsonl) is the output of the refresh run. The same 100 profiles are browsable in the explorer under [`docs/`](../../docs/), which the run command writes to `out/site/`.

## Evidence completeness (refresh run)

| Of 4,743 published claims | Count |
|---|---|
| with a source URL and retrieval time | 4,743 |
| with a content hash | 4,743 |
| with a stored raw snapshot | 4,743 |
| with a locator | 4,743 |
| with a span | 4,743 |
| of which the span was found verbatim in the fetched source | 4,727 |
| accounts claims with a reporting period | 1,908 of 1,908 |

The raw snapshots themselves stay in the run's state directory and are not committed.

## Field states (identical in both runs)

| Field family | available | not_available | ambiguous | not_applicable |
|---|---|---|---|---|
| Legal identity | 100 |  |  |  |
| Annual accounts | 100 |  |  |  |
| Filing history | 100 |  |  |  |
| Leadership | 100 |  |  |  |
| Registered workplaces | 83 | 17 |  |  |
| Group relationships | 6 |  |  | 94 |
| Public brand | 40 | 60 |  |  |
| Official website | 11 | 84 | 5 |  |
| Company-owned profiles | 6 | 89 | 5 |  |
| Jobs | 3 | 97 |  |  |
| Dated activity | 100 |  |  |  |

## Published websites

The identity proof the agent recorded for each of the 11 published websites:

| Company | Published URL | Found via | Proof |
|---|---|---|---|
| ANLEGGSDELER AS | www.anleggsdeler.no | name-derived domain | org number on the site |
| DATA NOVA AS | www.datanova.no | listed in the register | legal name, `.no` domain |
| NORWEGIAN AERO SOLUTIONS AS | www.norwegianaerosolutions.no | name-derived domain | legal name, `.no` domain |
| GT EIENDOM AS | gt-eiendom.no | listed in the register | org number on the site |
| PLOREA HOLDING AS | plorea.com | listed in the register | org number on the site |
| ARENDAL GYNEKOLOGI AS | www.arendalgynekologi.no | name-derived domain | org number on the site |
| VILDE&INGA AS | vildeinga.com | listed in the register | legal name only, non-`.no` domain; labelled as a possible group site |
| OSLOFJORDENS FRILUFTSRÅD | www.oslofjorden.org | listed in the register | org number on the site |
| BRAUTEN EIENDOM AS | brauten-eiendom.no | name-derived domain | org number on the site |
| STYRBJØRN AS | www.styrbjorn.no | listed in the register | legal name, `.no` domain |
| MÅLSELV BYGG AS | www.systemhus.no/forhandlere/malselv-bygg-as/om-oss | register e-mail domain | page on a third-party site; URL only |

The 5 `ambiguous` websites are register-listed sites that failed the exact-entity gate, for example a housing-association manager's site listed by a borettslag. They are reported as register-declared sites under public brand, and nothing is extracted from them.
