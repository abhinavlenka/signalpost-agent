"""Signalpost batch pipeline: N organisation numbers in, exactly N terminal envelopes out."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .budget import BudgetExhausted, RequestBudget, set_active_budget
from .claim_refresh import apply_refresh
from .envelope import build_envelope
from .evidence import evidence, utc_now
from .identity import apply_website_identity_gate
from .jobs_nav import NavJobIndex, match_company_jobs, raw_tokens
from .official import fetch_official_modules
from .summary import build_summary
from .website import fetch_website

AGENT_VERSION = "signalpost-agent/1.0.0"
UNIVERSE_URL = "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"
OFFICIAL_MODULES = {"registry_live", "roles", "locations", "financials", "role_events"}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# -- inputs --------------------------------------------------------------------------------------
def read_input_orgs(path: str | Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Accept txt / csv / json / jsonl. Invalid rows are reported, never silently dropped."""
    text = Path(path).read_text(encoding="utf-8-sig")
    values: list[Any] = []
    stripped = text.strip()
    if stripped.startswith("["):
        values = json.loads(stripped)
    elif stripped.startswith("{") and "\n" not in stripped and not ('"organisation_number"' in stripped or '"orgnr"' in stripped):
        body = json.loads(stripped)
        values = body.get("organisation_numbers") or body.get("organisations") or []
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.lower().startswith(("organisation", "orgnr", "#")):
                continue
            if line.startswith("{"):
                values.append(json.loads(line))
            else:
                values.append(line.split(",")[0].split(";")[0])
    orgs: list[str] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        raw = value.get("organisation_number") or value.get("orgnr") if isinstance(value, dict) else value
        org = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(org) != 9:
            invalid.append({"input": raw, "error": "not a 9-digit organisation number"})
            continue
        if org in seen:
            continue
        seen.add(org)
        orgs.append(org)
    return orgs, invalid


def load_universe_rows(path: Path, orgs: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    wanted = set(orgs)
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows, {"path": str(path), "available": False}
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for line in handle:
            digest.update(line)
            if len(rows) < len(wanted):
                row = json.loads(line)
                if row.get("organisation_number") in wanted:
                    rows[row["organisation_number"]] = row
    return rows, {"path": str(path), "available": True, "content_sha256": digest.hexdigest(), "matched": len(rows)}


def load_previous(previous: str | None, state_dir: Path, orgs: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    wanted = set(orgs)
    if previous:
        source = Path(previous)
        files = [source / "envelopes.jsonl"] if source.is_dir() else [source]
        for file in files:
            if file.exists():
                for line in file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        item = json.loads(line)
                        if item.get("organisation_number") in wanted:
                            found[item["organisation_number"]] = item
        return found
    latest = state_dir / "latest"
    for org in orgs:
        file = latest / f"{org}.json"
        if file.exists():
            found[org] = json.loads(file.read_text(encoding="utf-8"))
    return found


# -- per-company research ------------------------------------------------------------------------
def base_profile(org: str, row: dict[str, Any] | None, universe_meta: dict[str, Any]) -> dict[str, Any]:
    row = row or {}
    profile = {
        "organisation_number": org,
        "name": row.get("name"),
        "legal_form": row.get("legal_form"),
        "municipality": row.get("municipality"),
        "employees": row.get("employees"),
        "website": row.get("website") or None,
        "latest_submitted_accounts": row.get("latest_submitted_accounts"),
        "evidence": {},
        "errors": [],
    }
    if row:
        profile["evidence"]["registry"] = evidence(
            "registry", "available", "frozen_universe_snapshot", UNIVERSE_URL, value=row,
            content_sha256=universe_meta.get("content_sha256"), source_row_key=org,
        )
    return profile


def research_official(profile: dict[str, Any]) -> None:
    org = profile["organisation_number"]
    records, _ = fetch_official_modules(org, {"registry_live"})
    profile["evidence"].update(records)
    live = records.get("registry_live") or {}
    value = live.get("value") or {}
    if live.get("status") == "available":
        profile["name"] = value.get("name") or profile.get("name")
        profile["legal_form"] = value.get("legal_form") or profile.get("legal_form")
        profile["website"] = value.get("website") or profile.get("website")
        profile["latest_submitted_accounts"] = value.get("latest_submitted_accounts") or profile.get("latest_submitted_accounts")
    modules = set(OFFICIAL_MODULES) - {"registry_live"}
    if value.get("in_group"):
        modules.add("group")
    records, _ = fetch_official_modules(org, modules)
    profile["evidence"].update(records)


def registry_identity(profile: dict[str, Any]) -> dict[str, Any]:
    live = ((profile["evidence"].get("registry_live") or {}).get("value")) or {}
    address = live.get("business_address") or {}
    first_line = (address.get("adresse") or [""])[0] if isinstance(address, dict) else ""
    street = re.split(r"\s+\d", str(first_line or ""), maxsplit=1)[0].strip()
    return {
        "organisation_number": profile["organisation_number"],
        "phones": live.get("_phones") or [],
        "postal_code": address.get("postnummer") if isinstance(address, dict) else None,
        "street": street if len(street) >= 4 else None,
    }


def research_website(profile: dict[str, Any], url: str | None, *, discovery: str, declared_by: dict[str, Any] | None = None, max_pages: int = 5) -> bool:
    record, _ = fetch_website(url, identity=registry_identity(profile), max_pages=max_pages)
    if isinstance(record.get("value"), dict):
        record["value"]["registry_listed"] = discovery == "registry_listed"
    if declared_by and record.get("status") == "available":
        record["source_type"] = record["source_class"] = "employer_declared_homepage"
    gated = apply_website_identity_gate(profile, record)["website"]
    value = gated.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if declared_by and value and not assessment.get("publishable"):
        parked = any("parked" in reason for reason in assessment.get("reasons") or [])
        if not parked and len(str(value.get("main_text_excerpt") or "")) >= 200:
            # The official job register ties this homepage to the exact organisation number.
            assessment.update({
                "status": "exact", "publishable": True, "score": 0.92, "method": "nav_employer_declared_homepage_v1",
                "reasons": [*assessment.get("reasons", []), f"homepage declared by employer orgnr {declared_by.get('employer_orgnr')} in NAV ad {declared_by.get('uuid')}"],
            })
            value["social_links"] = [
                {"platform": item["platform"], "url": item["url"]}
                for item in value.get("social_link_assessments") or [] if item.get("publishable")
            ]
    if value:
        value["discovery_method"] = discovery
    publishable = bool(assessment.get("publishable"))
    previous = profile["evidence"].get("website")
    if publishable or not previous or previous.get("status") != "available":
        profile["evidence"]["website"] = gated
    return publishable


SKIP_GUESS_FORMS = {"BRL", "ESEK", "BBL", "SAM", "KIRK", "PRE", "VPFO", "ANNA"}


def domain_candidates(name: str | None) -> list[str]:
    tokens = [token for token in raw_tokens(name) if token not in {"og", "and", "the"}]
    if not tokens or len(tokens) > 4:
        return []
    joined = "".join(tokens)
    if len(tokens) == 1 and len(joined) < 5 or len(joined) > 40:
        return []
    candidates = [f"{joined}.no"]
    if len(tokens) > 1:
        candidates.append(f"{'-'.join(tokens)}.no")
    return candidates


def _resolves(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    except (socket.gaierror, UnicodeError, OSError):
        return False


def discover_by_domain_guess(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Name-derived .no domains, published only with orgnr on site or exact name + registered address/phone."""
    live = ((profile["evidence"].get("registry_live") or {}).get("value")) or {}
    if str(profile.get("legal_form") or "").upper() in SKIP_GUESS_FORMS or live.get("bankrupt") or live.get("liquidating"):
        return {"skipped": "legal form or status unlikely to have an own website"}
    records = (((profile["evidence"].get("financials") or {}).get("value")) or {}).get("records") or []
    revenue = max([record.get("revenue") or 0 for record in records] or [0])
    if not (live.get("employees") or 0) >= 1 and revenue < 1_000_000:
        return {"skipped": "no registered employees and revenue below NOK 1m"}
    tried = []
    for domain in domain_candidates(profile.get("name")):
        if len(tried) >= 2:
            break
        if not _resolves(domain):
            tried.append({"domain": domain, "result": "no DNS"})
            continue
        record, _ = fetch_website("https://" + domain, identity=registry_identity(profile), max_pages=3)
        if record.get("status") != "available":
            tried.append({"domain": domain, "result": record.get("note") or record.get("status")})
            continue
        record["source_type"] = record["source_class"] = "name_derived_domain"
        gated = apply_website_identity_gate(profile, record)["website"]
        value = gated.get("value") or {}
        assessment = value.get("identity_assessment") or {}
        markers = {marker for items in (value.get("identity_markers") or {}).values() for marker in items}
        name_exact = assessment.get("score", 0) >= 0.95 and "legal-name" in " ".join(assessment.get("reasons") or [])
        if "organisation_number" in markers or (name_exact and markers & {"address", "phone"}):
            assessment.update({
                "status": "exact", "publishable": True, "method": "name_derived_domain_strict_v1",
                "score": 1.0 if "organisation_number" in markers else 0.95,
                "reasons": [*assessment.get("reasons", []), f"name-derived domain verified by registry markers: {sorted(markers)}"],
            })
            value["social_links"] = [{"platform": item["platform"], "url": item["url"]} for item in value.get("social_link_assessments") or [] if item.get("publishable")]
            value["discovery_method"] = "name_derived_domain"
            value["registry_listed"] = False
            profile["evidence"]["website"] = gated
            tried.append({"domain": domain, "result": "verified"})
            return {"tried": tried}
        tried.append({"domain": domain, "result": "identity not verified", "markers": sorted(markers)})
    return {"tried": tried}


def research_company(profile: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        research_official(profile)
        if profile.get("website"):
            research_website(profile, profile["website"], discovery="registry_listed")
        else:
            profile["evidence"]["website"] = evidence(
                "website", "not_found", "registry_linked_company_website",
                f"https://data.brreg.no/enhetsregisteret/api/enheter/{profile['organisation_number']}",
                note="registry lists no website",
            )
        website = profile["evidence"].get("website") or {}
        if not ((website.get("value") or {}).get("identity_assessment") or {}).get("publishable"):
            profile["domain_guess"] = discover_by_domain_guess(profile)
    except BudgetExhausted as exc:
        profile["errors"].append({"source": "pipeline", "error": f"budget_exhausted: {exc}"})
    except Exception as exc:
        profile["errors"].append({"source": "pipeline", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)})
    profile["runtime_ms"] = int((time.monotonic() - started) * 1000)
    return profile


def retry_failed_accounts(profiles: dict[str, dict[str, Any]], budget: RequestBudget, *, max_seconds: float, max_requests: int) -> dict[str, Any]:
    """Second pass for the accounts register, whose 503 bursts can outlast per-company retries."""
    pending = [org for org, profile in profiles.items() if (profile["evidence"].get("financials") or {}).get("status") == "source_error"
               and "budget_exhausted" not in str((profile["evidence"].get("financials") or {}).get("note") or "")]
    started, start_used, recovered, rounds = time.monotonic(), budget.used, 0, 0
    while pending and time.monotonic() - started < max_seconds and budget.used - start_used < max_requests and budget.remaining > 100:
        rounds += 1
        still = []
        for org in pending:
            if time.monotonic() - started >= max_seconds or budget.used - start_used >= max_requests:
                still.append(org)
                continue
            records, _ = fetch_official_modules(org, {"financials"})
            if records["financials"].get("status") in {"available", "not_found"}:
                profiles[org]["evidence"]["financials"] = records["financials"]
                recovered += 1
            else:
                still.append(org)
        pending = still
        if pending:
            time.sleep(5)
    return {"recovered": recovered, "still_failed": len(pending), "rounds": rounds, "requests": budget.used - start_used}


def attach_jobs(profile: dict[str, Any], index: NavJobIndex, name_index: dict[str, Any]) -> None:
    ev = profile["evidence"]
    if index.error and not index.ads:
        ev["jobs"] = evidence("jobs", "source_error", "official_job_register_nav", "https://pam-stilling-feed.nav.no/api/v1/feed", note=index.error)
        return
    locations = ((ev.get("locations") or {}).get("value") or {}).get("locations") or []
    names = [profile.get("name"), *[item.get("name") for item in locations]]
    live_value = (ev.get("registry_live") or {}).get("value") or {}
    names.extend(live_value.get("historical_names") or [])
    result = match_company_jobs(
        index, name_index,
        organisation_number=profile["organisation_number"],
        names=[name for name in names if name],
        subunit_orgs=[item.get("organisation_number") for item in locations],
    )
    result["window_days"] = index.since_days
    result["feed_complete"] = index.complete
    status = "available" if result["jobs"] or not result["errors"] else "source_error"
    ev["jobs"] = evidence(
        "jobs", status, "official_job_register_nav", "https://pam-stilling-feed.nav.no/api/v1/feed",
        value=result, note=None if status == "available" else "; ".join(item["error"] for item in result["errors"][:3]),
    )


def discover_websites(profile: dict[str, Any]) -> None:
    """Use employer-declared homepages from exact-orgnr NAV ads when no website is verified yet."""
    website = profile["evidence"].get("website") or {}
    if ((website.get("value") or {}).get("identity_assessment") or {}).get("publishable"):
        return
    jobs = (((profile["evidence"].get("jobs") or {}).get("value") or {}).get("jobs")) or []
    tried: set[str] = set()
    for job in jobs:
        homepage = str(job.get("employer_homepage") or "").strip()
        if not homepage or homepage in tried or "arbeidsplassen" in homepage:
            continue
        tried.add(homepage)
        if research_website(profile, homepage, discovery="nav_employer_homepage", declared_by=job):
            return
        if len(tried) >= 2:
            return


# -- batch ---------------------------------------------------------------------------------------
def run_batch(
    input_path: str,
    *,
    out_dir: str = "out",
    state_dir: str = "state",
    universe_path: str = "data/signalpost-universe.jsonl.gz",
    previous: str | None = None,
    run_id: str | None = None,
    max_requests: int = 1900,
    deadline_minutes: float = 38.0,
    workers: int = 12,
    nav_days: int = 45,
    use_nav: bool = True,
) -> dict[str, Any]:
    started_at = utc_now()
    run_id = run_id or "run-" + started_at.replace(":", "").replace("-", "")[:15]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state = Path(state_dir)
    budget = set_active_budget(RequestBudget(max_requests=max_requests, deadline_seconds=deadline_minutes * 60, reserve=40))
    orgs, invalid = read_input_orgs(input_path)
    log(f"{run_id}: {len(orgs)} organisation numbers ({len(invalid)} invalid rows)")
    profiles: dict[str, dict[str, Any]] = {}
    previous_envelopes: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"run_id": run_id, "agent_version": AGENT_VERSION, "started_at": started_at, "input": str(input_path), "invalid_inputs": invalid}
    try:
        universe_rows, universe_meta = load_universe_rows(Path(universe_path), orgs)
        report["universe"] = universe_meta
        previous_envelopes = load_previous(previous, state, orgs)
        report["previous_envelopes"] = len(previous_envelopes)
        profiles = {org: base_profile(org, universe_rows.get(org), universe_meta) for org in orgs}

        nav = NavJobIndex(since_days=nav_days).start() if use_nav else None
        log("researching official registers and registry-listed websites")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(research_company, profiles[org]): org for org in orgs}
            for done, future in enumerate(as_completed(futures), 1):
                future.result()
                if done % 20 == 0 or done == len(orgs):
                    log(f"  {done}/{len(orgs)} companies, {budget.used} requests")

        sweep_seconds = min(600.0, max(0.0, (budget.time_left() or 600) - 600))
        report["accounts_retry"] = retry_failed_accounts(profiles, budget, max_seconds=sweep_seconds, max_requests=max(0, budget.remaining - 500))
        log(f"accounts retry sweep: {report['accounts_retry']}")

        if nav:
            remaining = max(5.0, (budget.time_left() or 600) - 240)
            log(f"waiting for NAV job feed (up to {remaining:.0f}s)")
            if not nav.wait(remaining):
                nav.stop()
                nav.wait(60)
            report["nav"] = nav.report()
            log(f"NAV feed: {report['nav']['pages']} pages, {report['nav']['active_ads']} active ads, error={report['nav']['error']}")
            name_index = nav.build_name_index()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda org: _safe(attach_jobs, profiles[org], nav, name_index), orgs))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda org: _safe(discover_websites, profiles[org]), orgs))
        else:
            for org in orgs:
                profiles[org]["evidence"]["jobs"] = evidence("jobs", "blocked", "official_job_register_nav", "https://pam-stilling-feed.nav.no", note="jobs connector disabled for this run")
    except Exception as exc:
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        report["fatal_trace"] = traceback.format_exc(limit=5)
        log(f"FATAL: {report['fatal_error']} — emitting failed envelopes for unresearched companies")
        for org in orgs:
            profiles.setdefault(org, {"organisation_number": org, "evidence": {}, "errors": []})
            if not profiles[org].get("evidence"):
                profiles[org]["fatal_error"] = report["fatal_error"]

    completed_at = utc_now()
    envelopes = []
    for org in orgs:
        profile = profiles.get(org) or {"organisation_number": org, "evidence": {}, "errors": [], "fatal_error": "not researched"}
        run_meta = {"run_id": run_id, "agent_version": AGENT_VERSION, "started_at": started_at, "completed_at": completed_at, "terminal_status": "completed"}
        try:
            envelope = build_envelope(profile, run=run_meta, operations={"runtime_ms": profile.get("runtime_ms"), "third_party_cost_usd": 0})
            envelope = apply_refresh(previous_envelopes.get(org), envelope)
            envelope["summary"] = build_summary(envelope)
        except Exception as exc:
            envelope = build_envelope({"organisation_number": org, "name": profile.get("name"), "fatal_error": f"envelope build failed: {type(exc).__name__}: {exc}"}, run=run_meta)
            envelope["refresh"] = {"mode": "failed"}
        envelopes.append(envelope)

    # Invalid inputs still get a terminal row so nothing is silently dropped.
    for item in invalid:
        envelopes.append({"organisation_number": str(item["input"]), "state": "failed", "run": {"run_id": run_id, "started_at": started_at, "completed_at": completed_at, "terminal_status": "completed"},
                          "claims": [], "evidence": [], "availability": {}, "changes": [], "errors": [item]})

    write_outputs(out, state, run_id, envelopes)
    states: dict[str, int] = {}
    family_states: dict[str, dict[str, int]] = {}
    for envelope in envelopes:
        states[envelope["state"]] = states.get(envelope["state"], 0) + 1
        for family, value in (envelope.get("availability") or {}).items():
            family_states.setdefault(family, {}).setdefault(value, 0)
            family_states[family][value] += 1
    report.update({
        "completed_at": completed_at,
        "expected": len(orgs) + len(invalid),
        "emitted_envelopes": len(envelopes),
        "envelope_states": states,
        "field_states": family_states,
        "claims": sum(len(envelope.get("claims") or []) for envelope in envelopes),
        "changes": sum(len(envelope.get("changes") or []) for envelope in envelopes),
        "budget": budget.report(),
        "third_party_cost_usd": 0.0,
        "validation": {
            "exact_count": len(envelopes) == len(orgs) + len(invalid),
            "unique": len({envelope["organisation_number"] for envelope in envelopes}) == len(envelopes),
            "within_request_cap": budget.used <= max_requests,
        },
    })
    runtimes = sorted(profile.get("runtime_ms") or 0 for profile in profiles.values())
    if runtimes:
        report["latency_ms"] = {"p50": runtimes[len(runtimes) // 2], "p95": runtimes[min(len(runtimes) - 1, int(len(runtimes) * 0.95))]}
    (out / "run-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"done: {len(envelopes)} envelopes, {budget.used} requests, states={states}")
    return report


def _safe(function: Callable[..., Any], profile: dict[str, Any], *args: Any) -> None:
    try:
        function(profile, *args)
    except Exception as exc:
        profile.setdefault("errors", []).append({"source": function.__name__, "error": f"{type(exc).__name__}: {exc}"})


def write_outputs(out: Path, state: Path, run_id: str, envelopes: list[dict[str, Any]]) -> None:
    lines = "".join(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n" for envelope in envelopes)
    temporary = out / "envelopes.jsonl.tmp"
    temporary.write_text(lines, encoding="utf-8")
    temporary.replace(out / "envelopes.jsonl")
    snapshot_dir = state / "snapshots" / run_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "envelopes.jsonl").write_text(lines, encoding="utf-8")
    latest = state / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    for envelope in envelopes:
        if envelope.get("claims") is not None and len(envelope["organisation_number"]) == 9:
            (latest / f"{envelope['organisation_number']}.json").write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
