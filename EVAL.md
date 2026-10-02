# Local evaluation

The official score uses a hidden pooled reference. These are the local checks used to decide whether a change is kept.

## Corpora

| File | Use |
|---|---|
| `data/batch-rehearsal-100.txt` | 100 random companies from the public universe; the smoke test in `reports/smoke-100/` |
| `data/batch-rehearsal-1000.txt` | 1,000 random companies; timing and budget rehearsal |
| `data/builderr-sample-100.txt` | the 100 companies in Builderr's public product sample; recall benchmark |
| `data/dev30.txt`, `data/smoke10.txt` | quick development runs |

## Checks

| Check | Command | What it shows |
|---|---|---|
| Unit and contract tests | `uv run --with pytest pytest -q` | gates, states, refresh idempotency, spans, budget cap |
| Recall benchmark | `uv run python scripts/benchmark_vs_builderr.py --envelopes out/envelopes.jsonl` | per field family: 70% company recall plus 30% claim recall against Builderr's public sample, plus our extra finds and any website disagreements |
| Audit sample | `uv run python scripts/audit_sample.py` | a CSV of published external claims with their proof, for hand review |
| Run report | `out/run-report.json` | envelopes returned, requests by purpose, runtime, p50/p95, field states, evidence completeness, cost |
| Refresh | run the same batch twice with the same `--state` | changes on the second run should be zero unless a source changed |

## Order of judgement

1. Wrong-company publications must not increase.
2. Every published claim keeps a source, retrieval time, content hash and quoted span.
3. Coverage and recall on the benchmark.
4. False changes on refresh.
5. Requests, runtime and cost.

A change that finds more data by weakening the identity gate is dropped.

## Evidence completeness in the run report

`run-report.json` has an `evidence` block counting, over all published claims: those with a source and retrieval time, with a content hash, with any span, with a span found verbatim in the fetched source (`with_quoted_span`), with a locator, with a stored raw snapshot, and accounts claims with a reporting period.
