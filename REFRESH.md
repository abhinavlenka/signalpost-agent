# Refresh semantics

Each run diffs every freshly built envelope against the previous envelope for the same organisation number. The previous envelope comes from `--previous`, or from `state/latest/<org>.json` by default. The logic lives in `src/norway_company_agent/claim_refresh.py`.

## Rules

| Situation | Result |
|---|---|
| Same claim key, same value hash | unchanged; `first_seen` preserved, `last_seen` updated |
| New claim key | typed addition: `new_job`, `new_role`, `new_location`, `new_filing`, `new_website`, `new_company_profile`, `new_activity`, `new_group_link` … |
| Same key, different value | `changed_<field>`, e.g. `changed_revenue`, `changed_business_address`, carrying both values and evidence for both sides |
| Key disappeared **and** the family's source was checked successfully this run | typed removal: `closed_job`, `removed_role`, `closed_location`, `removed_website` … |
| Key disappeared but the source failed or was blocked this run | **no change**; the previous claim is carried forward as `stale: true` with its original evidence. A failed refresh never erases the last supported value. |
| Dated history item aged out of the source window | kept as `historical: true`; not a removal |
| Claim appears in a family whose source failed or was blocked last run | `backfilled: true`, **not** reported as a change. The world didn't change; the source recovered. |

## Idempotency

- Claim keys are deterministic, built from organisation number, family, field and a normalized identity.
- Change ids are `sha256(claim_key, change_type, old_value_hash, new_value_hash)`. Replaying the same pair of snapshots always yields identical change ids.
- `change_log` merges the previous log with new changes by change id, so reruns never duplicate entries.
- Retrieval timestamps are not part of value hashes, so re-fetching unchanged sources produces zero changes.

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

Every run writes an immutable `state/snapshots/<run_id>/envelopes.jsonl`. Evidence records from the previous run that support a change or a carried-forward claim are copied into the new envelope, marked `from_previous_run`.

## Tests

`tests/test_signalpost_agent.py` covers:
- idempotent reruns
- typed role changes
- financial restatements
- stale carry-forward on source failure
- no duplicate change-log entries
- recovered sources treated as backfill rather than change
