"""Claim-level refresh: stable keys, typed changes, carried-forward evidence, idempotent reruns.

Given the previous terminal envelope and the freshly built one:
- a claim key present in both with the same value hash is unchanged (first_seen is preserved);
- a new key is an addition, typed by family (``new_job``, ``new_role``, ``new_filing`` ...);
- a missing key is a removal only when the family's source was checked successfully this run;
  when the source failed or was blocked, the previous claim is carried forward as ``stale`` and no
  change is emitted (a failed refresh never erases the last supported value);
- a changed value emits ``changed_<field>`` with both values and evidence for both sides.
Change ids hash (claim key, type, old value, new value), so replaying the same snapshots can never
create duplicate or phantom changes.
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
    run = current.get("run") or {}
    now = run.get("started_at")
    if not previous:
        for claim in current["claims"]:
            claim["first_seen"] = now
            claim["last_seen"] = now
        current["refresh"] = {
            "previous_run_id": None,
            "mode": "initial",
            "changes_detected": 0,
            "carried_forward_claims": 0,
        }
        current["change_log"] = []
        return current

    prev_claims = {claim["claim_key"]: claim for claim in previous.get("claims") or []}
    prev_evidence = {item["evidence_id"]: item for item in previous.get("evidence") or []}
    curr_claims = {claim["claim_key"]: claim for claim in current["claims"]}
    curr_evidence_ids = {item["evidence_id"] for item in current["evidence"]}
    availability = current.get("availability") or {}
    changes: list[dict[str, Any]] = []
    carried = 0

    def keep_previous_evidence(evidence_ids: list[str]) -> None:
        for evidence_id in evidence_ids:
            if evidence_id in prev_evidence and evidence_id not in curr_evidence_ids:
                current["evidence"].append({**prev_evidence[evidence_id], "from_previous_run": previous.get("run", {}).get("run_id")})
                curr_evidence_ids.add(evidence_id)

    for key, claim in curr_claims.items():
        before = prev_claims.get(key)
        if before is None:
            claim["first_seen"] = now
            claim["last_seen"] = now
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
                "detected_at": now,
                "evidence_ids": {"previous": [], "current": claim["evidence_ids"]},
            })
            continue
        claim["first_seen"] = before.get("first_seen") or previous.get("run", {}).get("started_at")
        claim["last_seen"] = now
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
                "detected_at": now,
                "evidence_ids": {"previous": before.get("evidence_ids") or [], "current": claim["evidence_ids"]},
            })

    for key, before in prev_claims.items():
        if key in curr_claims:
            continue
        family = before.get("family")
        family_state = availability.get(family)
        source_checked = family_state in {"available", "not_available"}
        if family in NON_REMOVABLE or not source_checked or family not in REMOVED:
            # Keep the last supported value; expose it as not re-verified in this run.
            carried += 1
            marker = {"historical": True} if family in NON_REMOVABLE and source_checked else {"stale": True}
            carried_claim = {**before, **marker, "last_verified_run_id": before.get("last_verified_run_id") or previous.get("run", {}).get("run_id")}
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
            "previous_value": before.get("value") if family != "jobs" else {"title": (before.get("value") or {}).get("title")},
            "current_value": None,
            "material": _material(before, change_type),
            "detected_at": now,
            "evidence_ids": {"previous": before.get("evidence_ids") or [], "current": []},
        })

    # Change log = previous log + new changes, deduplicated by deterministic change id.
    log = {item["change_id"]: item for item in previous.get("change_log") or []}
    for change in changes:
        log.setdefault(change["change_id"], change)
    current["changes"] = changes
    current["change_log"] = sorted(log.values(), key=lambda item: (str(item.get("detected_at")), item["change_id"]))
    current["refresh"] = {
        "previous_run_id": previous.get("run", {}).get("run_id"),
        "previous_completed_at": previous.get("run", {}).get("completed_at"),
        "mode": "diff",
        "changes_detected": len(changes),
        "material_changes": sum(1 for change in changes if change["material"]),
        "carried_forward_claims": carried,
    }
    return current
