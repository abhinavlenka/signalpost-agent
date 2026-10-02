# 100-company smoke test

Run on 2026-10-02 from a fresh clone at commit `9bc7bb8`, in an empty virtual environment, with only the declared install step:

```bash
uv sync --frozen
uv run python -m signalpost run --input data/batch-rehearsal-100.txt --out out/run1 --state state --run-id smoke100-run
uv run python -m signalpost run --input data/batch-rehearsal-100.txt --out out/run2 --state state --run-id smoke100-refresh
```

Commits after `9bc7bb8` add only this report, the hosted explorer under `docs/` and an explorer CSS fix; the agent's output is unchanged.

The batch is 100 organisation numbers drawn at random from the public universe. The second command is the same run again, to exercise refresh.

| | First run | Refresh run |
|---|---|---|
| Report | [`run-report.json`](run-report.json) | [`refresh-run-report.json`](refresh-run-report.json) |
| Envelopes returned | 100 of 100 | 100 of 100 |
| Envelope state | 100 `available` | 100 `available` |
| Claims | 4,723 | 4,723 |
| Changes reported | 0 (first observation) | 0 (nothing changed between runs) |
| Outbound requests | 1,037 of 1,900 allowed | 1,018 of 1,900 allowed |
| Wall-clock time | 6 min 27 s | 5 min 32 s |
| Third-party API cost | $0 | $0 |

[`envelopes.jsonl`](envelopes.jsonl) is the output of the refresh run. The same 100 profiles are browsable in the explorer under [`docs/`](../../docs/), which the run command writes to `out/site/`.

## Field states (identical in both runs)

| Field family | available | not_available | ambiguous | not_applicable |
|---|---|---|---|---|
| Legal identity | 100 | | | |
| Annual accounts | 100 | | | |
| Filing history | 100 | | | |
| Leadership | 100 | | | |
| Registered workplaces | 83 | 17 | | |
| Group relationships | 6 | | | 94 |
| Public brand | 40 | 60 | | |
| Official website | 11 | 84 | 5 | |
| Company-owned profiles | 6 | 89 | 5 | |
| Jobs | 3 | 97 | | |
| Dated activity | 100 | | | |

## Website audit

The identity proof the agent recorded for each of the 11 published websites:

| Company | Published URL | Proof |
|---|---|---|
| ANLEGGSDELER AS | anleggsdeler.no | org number on site |
| GT EIENDOM AS | gt-eiendom.no | org number on site |
| PLOREA HOLDING AS | plorea.com | org number on site |
| ARENDAL GYNEKOLOGI AS | arendalgynekologi.no | org number on site |
| OSLOFJORDENS FRILUFTSRÅD | oslofjorden.org | org number on site |
| BRAUTEN EIENDOM AS | brauten-eiendom.no | org number on site |
| VILDE&INGA AS | vildeinga.com | listed in the register; legal name on homepage; non-`.no` domain, so labelled `possibly_group_or_international` |
| DATA NOVA AS | datanova.no | listed in the register; legal name on homepage |
| STYRBJØRN AS | styrbjorn.no | listed in the register; domain equals legal name |
| NORWEGIAN AERO SOLUTIONS AS | norwegianaerosolutions.no | legal name plus registered address on site |
| MÅLSELV BYGG AS | systemhus.no/forhandlere/malselv-bygg-as/om-oss | dealer page on a chain's site; labelled `page_on_third_party_site`, URL only |

The 5 `ambiguous` websites are registry-listed sites that failed the exact-entity gate, for example a housing-association manager's site listed by a borettslag. They are reported as registry-declared sites under public brand, and nothing is extracted from them.
