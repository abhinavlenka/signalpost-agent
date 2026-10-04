# Refresh semantics

Each run diffs every freshly built envelope against the previous envelope for the same organisation number. The previous envelope comes from `--previous`, or from `state/latest/<org>.json` by default. The logic lives in `src/abhikilde/claim_refresh.py`.

## Rules

| Situation | Result |
|---|---|
| Same claim key, same value hash | unchanged; the claim is identical to the previous run's |
| New claim key | typed addition: `new_job`, `new_role`, `new_location`, `new_filing`, `new_website`, `new_company_profile`, `new_activity`, `new_group_link` … |
| Same key, different value | `changed_<field>`, e.g. `changed_revenue`, `changed_business_address`, carrying both values and evidence for both sides |
| Key disappeared **and** the family's source was checked successfully this run | typed removal: `closed_job`, `removed_role`, `closed_location`, `removed_website` … |
| Key disappeared but the source failed or was blocked this run | **no change**; the previous claim is carried forward as `stale: true` with its original evidence. A failed refresh never erases the last supported value. |
| Dated history item aged out of the source window | kept as `historical: true`; not a removal |
| Claim appears in a family whose source failed or was blocked last run | `backfilled: true`, **not** reported as a change. The world didn't change; the source recovered. |
| Website-derived fact (website, profiles, brand, careers page, website news) appears | `backfilled: true` first observation, **not** a change. Discovery may simply have missed it last run. |
| Website-derived fact missing once | carried forward as `stale: true`. A removal is reported only after a **second** consecutive miss. |

## Idempotency

- Claim keys are deterministic, built from organisation number, family, field and a normalized identity.
- Change ids are `sha256(claim_key, change_type, old_value_hash, new_value_hash)`. Replaying the same pair of snapshots always yields identical change ids.
- `change_log` merges the previous log with new changes by change id, so reruns never duplicate entries.
- Retrieval timestamps are not part of value hashes, so re-fetching unchanged sources produces zero changes.

## Determinism

The same source content gives the same company record: on a first run, on a rerun from an empty state directory, and on a refresh over the previous run's state.

- Everything that belongs to one execution lives in three contract fields only: `run` (`run_id`, `started_at`, `completed_at`), `operations` (`requests`, `runtime_ms`, cost) and `evidence[].retrieved_at`. No other key name is used for a value that changes between runs, so a comparison that drops volatile values by key name reaches the same verdict as one that drops those three fields whole.
- No other field holds a run timestamp, a run id, a file path or a counter that depends on timing. Claims carry no first-seen or last-seen time, changes carry no detection time, and `refresh` holds counts only, with the same keys on a first run and on a refresh.
- What a run was compared against (initial or diff, the previous run's id and completion time) is a fact about the run, not about a company. It is written once, to `refresh` in `run-report.json`.
- A first run and a refresh that finds nothing changed give the same record, down to the wording of the summary and the order of the keys.
- A refresh finds a company's website the same way a first run does. The previous run's URL is not used as a shortcut, so the route to the site, and with it the evidence record, does not depend on what state exists.
- Lists and keys are ordered by content, never by timing or hashing: job-ad candidates and jobs break ties on the ad id, changes are ordered by change id, the change log keeps earlier changes first, errors are sorted, and `availability` lists the field families in one fixed order.
- Errors in an envelope say what failed and why. Tracebacks, which name file paths on the machine that ran the batch, go to `error_traces` in `run-report.json`.

Two scripts check this:

| Script | What it does |
|---|---|
| `scripts/check_determinism.py out/run1 out/run2` | compares two runs by organisation number, leaving out the three run-scoped fields, and exits non-zero on any other difference in value or key order |
| `scripts/replay_check.py --input batch.txt` | records one live run, replays it twice with the network sealed (an empty state directory, then a refresh over it, with different hash seeds) and compares all three |

What can still differ between two live runs is the sources themselves: a site that changes its HTML on every request, a job ad published in between, or a source that fails once. Those are real differences in the inputs, and the refresh rules above report them or carry the last value forward.

## Materiality

A change is marked `material` when it affects one of these:
- the legal name or form
- the address
- bankruptcy, liquidation or forced-dissolution status
- the registered employee count or industry
- accounts
- CEO, chair, owner or auditor roles, or any role added or removed
- the official website
- group links
- jobs

## Snapshots

Every run writes an immutable `state/snapshots/<run_id>/envelopes.jsonl`. Evidence records from the previous run that support a change or a carried-forward claim are copied into the new envelope, marked `from_previous_run: true`.

## Tests

`tests/test_abhikilde.py` covers:
- idempotent reruns
- identical records on a first run, a fresh rerun and a refresh
- typed role changes
- financial restatements
- stale carry-forward on source failure
- no duplicate change-log entries
- recovered sources treated as backfill rather than change
