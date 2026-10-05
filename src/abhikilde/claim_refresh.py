"""Claim-level refresh: stable keys, typed changes, carried-forward evidence, idempotent reruns.

Given the previous terminal envelope and the freshly built one:
- a claim key present in both with the same value hash is unchanged;
- a new key is an addition, typed by family (``new_job``, ``new_role``, ``new_filing`` ...);
- a missing key is a removal only when the family's source was checked successfully this run;
  when the source failed or was blocked, the previous claim is carried forward as ``stale`` and no
  change is emitted (a failed refresh never erases the last supported value);
- a changed value emits ``changed_<field>`` with both values and evidence for both sides.
Change ids hash (claim key, type, old value, new value), so replaying the same snapshots can never
create duplicate or phantom changes.

Nothing written here depends on when the run happened or what it was called: the same pair of
snapshots always gives the same claims, changes and counts, and a first run with nothing to compare
against gives the same record as a refresh that found nothing changed. What a run was compared
against (previous run id and time, initial or diff) is a fact about the run, not about the company:
it is written once to the run report (``pipeline.previous_run_link``), never into an envelope.
"""
from __future__ import annotations

import hashlib
from typing import Any

ADDED = {
    "jobs": "new_job",
    "leadership": "new_role",
    "registered_workplaces": "new_location",
    "annual_accounts": "new_filing",
    "filing_history": "new_filing",
    "official_website": "new_website",
    "company_profiles": "new_company_profile",
    "dated_activity": "new_activity",
    "group_relationships": "new_group_link",
    "public_brand": "new_brand_signal",
    "legal_identity": "new_identity_fact",
}
REMOVED = {
    "jobs": "closed_job",
    "leadership": "removed_role",
    "registered_workplaces": "closed_location",
    "official_website": "removed_website",
    "company_profiles": "removed_company_profile",
    "group_relationships": "removed_group_link",
}
MATERIAL_FAMILIES = {"legal_identity", "annual_accounts", "leadership", "official_website", "group_relationships", "jobs"}
NON_REMOVABLE = {"dated_activity", "filing_history"}  # history does not "disappear"; windows simply move
# Website-derived facts flap when a site is slow or a page times out. A removal is reported only after
# two consecutive runs miss the fact; the first miss carries it forward as stale.
DEBOUNCED_FAMILIES = {"official_website", "company_profiles", "public_brand"}
DEBOUNCED_FIELDS = {"dated_news"}


def _website_derived(claim: dict[str, Any]) -> bool:
    careers_page = claim.get("field") == "hiring_signal" and claim.get("signal") == "careers_page"
    return claim.get("family") in DEBOUNCED_FAMILIES or claim.get("field") in DEBOUNCED_FIELDS or careers_page


def _shadows_open_job(claim: dict[str, Any]) -> bool:
    """A NAV ad is published twice: in detail (open_job) and as a plain hiring signal. Only the first reports changes."""
    return claim.get("field") == "hiring_signal" and claim.get("signal") == "job_ad"


def _change_id(*parts: Any) -> str:
    return "chg-" + hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:16]


def _material(claim: dict[str, Any], change_type: str) -> bool:
    family = claim.get("family")
    if family == "legal_identity":
        return claim.get("field") in {"legal_name", "legal_form", "business_address", "bankrupt", "under_liquidation", "forced_dissolution", "registered_employees", "industry"}
    if family == "leadership":
        role_code = (claim.get("value") or {}).get("role_code") if isinstance(claim.get("value"), dict) else None
        return role_code in {"DAGL", "LEDE", "INNH", "REVI"} or change_type in {"new_role", "removed_role"}
    return family in MATERIAL_FAMILIES


def apply_refresh(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if not previous:
        # Same keys, in the same order, as the diff below writes them.
        current["changes"] = []
        current["change_log"] = []
        current["refresh"] = {"changes_detected": 0, "material_changes": 0, "carried_forward_claims": 0, "backfilled_claims": 0}
        return current

    prev_claims = {claim["claim_key"]: claim for claim in previous.get("claims") or []}
    prev_evidence = {item["evidence_id"]: item for item in previous.get("evidence") or []}
    curr_claims = {claim["claim_key"]: claim for claim in current["claims"]}
    curr_evidence_ids = {item["evidence_id"] for item in current["evidence"]}
    availability = current.get("availability") or {}
    previous_availability = previous.get("availability") or {}
    changes: list[dict[str, Any]] = []
    carried = 0
    backfilled = 0

    def keep_previous_evidence(evidence_ids: list[str]) -> None:
        for evidence_id in evidence_ids:
            if evidence_id in prev_evidence and evidence_id not in curr_evidence_ids:
                current["evidence"].append({**prev_evidence[evidence_id], "id": evidence_id, "from_previous_run": True})
                curr_evidence_ids.add(evidence_id)

    for key, claim in curr_claims.items():
        before = prev_claims.get(key)
        if before is None:
            if _shadows_open_job(claim):
                continue
            website_derived = _website_derived(claim)
            if previous_availability.get(claim["family"]) in {None, "failed", "blocked"} or website_derived:
                # Either the source was not checked last run, or this is a website-derived fact that
                # discovery may simply have missed before: a first observation, not a change in the world.
                # The source was not successfully checked last run: this is a first observation, not a
                # change in the world. Reporting it as new would be a false change.
                claim["backfilled"] = True
                backfilled += 1
                continue
            change_type = ADDED.get(claim["family"], "added")
            changes.append({
                "change_id": _change_id(key, change_type, None, claim["value_hash"]),
                "change_type": change_type,
                "claim_key": key,
                "family": claim["family"],
                "field": claim["field"],
                "previous_value": None,
                "current_value": claim["value"],
                "material": _material(claim, change_type),
                "evidence_ids": {"previous": [], "current": claim["evidence_ids"]},
            })
            continue
        if before.get("value_hash") != claim.get("value_hash"):
            change_type = f"changed_{claim['field']}"
            keep_previous_evidence(before.get("evidence_ids") or [])
            changes.append({
                "change_id": _change_id(key, change_type, before.get("value_hash"), claim["value_hash"]),
                "change_type": change_type,
                "claim_key": key,
                "family": claim["family"],
                "field": claim["field"],
                "previous_value": before.get("value"),
                "current_value": claim["value"],
                "material": _material(claim, change_type),
                "evidence_ids": {"previous": before.get("evidence_ids") or [], "current": claim["evidence_ids"]},
            })

    for key, before in prev_claims.items():
        if key in curr_claims:
            continue
        family = before.get("family")
        family_state = availability.get(family)
        source_checked = family_state in {"available", "not_available"}
        debounced = _website_derived(before)
        if source_checked and _shadows_open_job(before):
            continue
        first_miss = debounced and not before.get("stale")
        if family in NON_REMOVABLE or not source_checked or family not in REMOVED or first_miss:
            # Keep the last supported value; expose it as not re-verified in this run.
            carried += 1
            marker = {"historical": True} if family in NON_REMOVABLE and source_checked else {"stale": True}
            carried_claim = {**before, **marker}
            current["claims"].append(carried_claim)
            keep_previous_evidence(before.get("evidence_ids") or [])
            continue
        change_type = REMOVED[family]
        keep_previous_evidence(before.get("evidence_ids") or [])
        changes.append({
            "change_id": _change_id(key, change_type, before.get("value_hash"), None),
            "change_type": change_type,
            "claim_key": key,
            "family": family,
            "field": before.get("field"),
            "previous_value": {"title": before["value"].get("title")} if family == "jobs" and isinstance(before.get("value"), dict) else before.get("value"),
            "current_value": None,
            "material": _material(before, change_type),
            "evidence_ids": {"previous": before.get("evidence_ids") or [], "current": []},
        })

    # Change log = previous log, oldest first, then this run's changes; deduplicated by deterministic change id.
    changes.sort(key=lambda item: item["change_id"])
    log = {item["change_id"]: item for item in previous.get("change_log") or []}
    for change in changes:
        log.setdefault(change["change_id"], change)
    current["changes"] = changes
    current["change_log"] = list(log.values())
    current["refresh"] = {
        "changes_detected": len(changes),
        "material_changes": sum(1 for change in changes if change["material"]),
        "carried_forward_claims": carried,
        "backfilled_claims": backfilled,
    }
    return current
