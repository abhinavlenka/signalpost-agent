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
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any, Callable

from .budget import BudgetExhausted, RequestBudget, active_budget, set_active_budget
from .claim_refresh import apply_refresh
from .envelope import build_envelope
from .evidence import evidence, utc_now
from .identity import apply_website_identity_gate
from .jobs_nav import NavJobIndex, match_company_jobs, raw_tokens
from .official import fetch_official_modules, set_accounts_lanes
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


CORE_MODULES = {"roles", "locations", "role_events"}


def research_core(profile: dict[str, Any]) -> None:
    """Phase 1: registry identity, roles, workplaces, role events and group links (no accounts)."""
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
    modules = set(CORE_MODULES)
    if value.get("in_group"):
        modules.add("group")
    records, _ = fetch_official_modules(org, modules)
    profile["evidence"].update(records)


def research_accounts(profile: dict[str, Any]) -> None:
    records, _ = fetch_official_modules(profile["organisation_number"], {"financials"})
    profile["evidence"].update(records)


def research_official(profile: dict[str, Any]) -> None:
    research_core(profile)
    research_accounts(profile)


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


GENERIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no", "msn.com",
    "yahoo.com", "yahoo.no", "icloud.com", "me.com", "mac.com", "online.no", "broadpark.no", "getmail.no", "frisurf.no",
    "c2i.net", "start.no", "lyse.net", "altibox.no", "mail.com", "protonmail.com", "proton.me", "haugnett.no", "tele2.no",
    "enivest.net", "ebnett.no", "telia.no", "netcom.no", "bluezone.no", "tussa.com", "nextgentel.com", "ngt.no", "sf-nett.no",
    "kvinnherad.net", "vikenfiber.no", "neasenett.no", "hotmail.se", "gmx.com", "gmx.net", "aol.com", "tdcadsl.no",
}
SKIP_GUESS_FORMS = {"BRL", "ESEK", "BBL", "SAM", "KIRK", "PRE", "VPFO", "ANNA"}


ENTITY_PREFIXES = {"stiftelsen", "sameiet", "borettslag", "foreningen", "the"}


def _merge_initials(tokens: list[str]) -> list[str]:
    """Merge runs of single-letter initials: ["p", "e", "gaarud"] → ["pe", "gaarud"]."""
    merged: list[str] = []
    in_run = False
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            if in_run:
                merged[-1] += token
            else:
                merged.append(token)
            in_run = True
        else:
            merged.append(token)
            in_run = False
    return merged


def domain_candidates(name: str | None) -> list[str]:
    """Name-derived domain candidates (DNS-checked before any HTTP request).

    Norwegian letters get both common spellings (å→aa/a, ø→o/oe, æ→ae); single-letter initials are
    merged ("P.E. GAARUD" → pe-gaarud); entity prefixes (stiftelsen, sameiet, the ...) are also tried
    without; .com is tried after .no; dropping a trailing (often geographic) token comes last.
    """
    spellings: list[list[str]] = []
    for table in ({"å": "aa", "ø": "o", "æ": "ae"}, {"å": "a", "ø": "oe", "æ": "ae"}):
        variant = "".join(table.get(char, char) for char in str(name or "").casefold())
        tokens = _merge_initials([token for token in raw_tokens(variant) if token not in {"og", "and"}])
        for option in (tokens, [token for token in tokens if token not in ENTITY_PREFIXES]):
            if option and option not in spellings:
                spellings.append(option)
    primary: list[str] = []
    secondary: list[str] = []
    for tokens in spellings:
        joined = "".join(tokens)
        if len(tokens) > 4 or (len(tokens) == 1 and len(joined) < 5) or len(joined) > 40:
            continue
        primary.append(f"{joined}.no")
        if len(tokens) > 1:
            primary.append(f"{'-'.join(tokens)}.no")
        secondary.append(f"{joined}.com")
        if len(tokens) >= 2 and len(tokens[0]) >= 5 and tokens[0] not in ENTITY_PREFIXES:
            secondary.append(f"{''.join(tokens[:-1])}.no")
    return list(dict.fromkeys(primary + secondary))


def _resolves(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    except (socket.gaierror, UnicodeError, OSError):
        return False


_DNS_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="dns")


def resolve_many(hosts: list[str], timeout: float = 4.0) -> set[str]:
    """Resolve candidate hosts in parallel; slow or failed lookups count as unresolved."""
    futures = {host: _DNS_POOL.submit(_resolves, host) for host in dict.fromkeys(hosts)}
    done, _ = wait(list(futures.values()), timeout=timeout)
    return {host for host, future in futures.items() if future in done and future.result()}


def discover_by_domain_guess(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Website discovery from the registry e-mail domain and name-derived domains.

    - registry e-mail domain: company-declared to the official register, so it faces the same identity
      gate as a registry-listed website;
    - name-derived domains: published only with the org number on the site, or the exact legal name
      plus the registered street address or phone.
    Stage 1 fetches only the homepage; the bounded crawl runs only for promising candidates.
    """
    live = ((profile["evidence"].get("registry_live") or {}).get("value")) or {}
    if str(profile.get("legal_form") or "").upper() in SKIP_GUESS_FORMS or live.get("bankrupt") or live.get("liquidating"):
        return {"skipped": "legal form or status unlikely to have an own website"}
    if active_budget().remaining < 300:
        return {"skipped": "request budget reserved for mandatory sources"}
    candidates: list[tuple[str, str]] = []
    email_domain = live.get("_email_domain")
    if email_domain and email_domain not in GENERIC_MAIL_DOMAINS and "." in email_domain:
        candidates.append((email_domain, "registry_email_domain"))
    candidates += [(domain, "name_derived_domain") for domain in domain_candidates(profile.get("name")) if domain != email_domain]
    identity = registry_identity(profile)
    tried: list[dict[str, Any]] = []
    probes = crawls = 0
    resolved = resolve_many([host for domain, _ in candidates for host in (f"www.{domain}", domain)])
    for domain, discovery in candidates:
        if probes >= 4 or crawls >= 2:
            break
        hosts = [host for host in (f"www.{domain}", domain) if host in resolved]
        if not hosts:
            tried.append({"domain": domain, "result": "no DNS"})
            continue
        record = None
        for host in hosts:  # a broken www certificate should not hide a working bare domain
            probes += 1
            record, _ = fetch_website(host, identity=identity, max_pages=0)
            if record.get("status") == "available":
                break
        if not record or record.get("status") != "available":
            tried.append({"domain": domain, "result": (record or {}).get("note") or (record or {}).get("status")})
            continue
        record["value"]["registry_listed"] = discovery == "registry_email_domain"
        probe = apply_website_identity_gate(profile, record)["website"]
        probe_value = probe.get("value") or {}
        probe_markers = {marker for items in (probe_value.get("identity_markers") or {}).values() for marker in items}
        if "organisation_number" not in probe_markers and (probe_value.get("identity_assessment") or {}).get("score", 0) < 0.85:
            tried.append({"domain": domain, "result": "homepage does not name the company"})
            continue
        crawls += 1
        record, _ = fetch_website(probe_value.get("final_url") or host, identity=identity, max_pages=3)
        if record.get("status") != "available":
            record = probe
        record["source_type"] = record["source_class"] = discovery
        record["value"]["registry_listed"] = discovery == "registry_email_domain"
        gated = apply_website_identity_gate(profile, record)["website"]
        value = gated.get("value") or {}
        assessment = value.get("identity_assessment") or {}
        markers = {marker for items in (value.get("identity_markers") or {}).values() for marker in items}
        name_exact = assessment.get("score", 0) >= 0.95 and "legal-name" in " ".join(assessment.get("reasons") or [])
        if "organisation_number" in markers or (name_exact and markers & {"address", "phone"}):
            verdict = f"verified by registry markers: {sorted(markers)}"
            score = 1.0 if "organisation_number" in markers else 0.95
        elif discovery == "registry_email_domain" and assessment.get("publishable"):
            verdict = "company-declared e-mail domain in the official register, and the site passes the exact-entity gate"
            score = min(0.93, float(assessment.get("score") or 0.93))
        else:
            tried.append({"domain": domain, "result": "identity not verified", "markers": sorted(markers)})
            continue
        assessment.update({
            "status": "exact", "publishable": True, "method": f"{discovery}_v2", "score": score,
            "reasons": [*assessment.get("reasons", []), f"{discovery.replace('_', ' ')} {verdict}"],
        })
        value["social_links"] = [{"platform": item["platform"], "url": item["url"]} for item in value.get("social_link_assessments") or [] if item.get("publishable")]
        value["discovery_method"] = discovery
        profile["evidence"]["website"] = gated
        tried.append({"domain": domain, "result": "verified"})
        return {"tried": tried}
    return {"tried": tried}


def previous_website(envelope: dict[str, Any]) -> dict[str, Any] | None:
    evidence_by_id = {item["evidence_id"]: item for item in envelope.get("evidence") or []}
    for claim in envelope.get("claims") or []:
        if claim.get("field") == "official_website" and claim.get("value"):
            source = evidence_by_id.get((claim.get("evidence_ids") or [None])[0]) or {}
            return {"url": claim["value"], "source_class": source.get("source_class"), "discovery": source.get("source_class")}
    return None


def research_registry_website(profile: dict[str, Any]) -> None:
    """Phase 3: the registry-listed website through the exact-entity gate."""
    if profile.get("website"):
        research_website(profile, profile["website"], discovery="registry_listed")
    else:
        profile["evidence"]["website"] = evidence(
            "website", "not_found", "registry_linked_company_website",
            f"https://data.brreg.no/enhetsregisteret/api/enheter/{profile['organisation_number']}",
            note="registry lists no website",
        )


def research_discovery(profile: dict[str, Any]) -> None:
    """Phase 4: re-verify last run's website, else e-mail-domain and name-derived discovery."""
    website = profile["evidence"].get("website") or {}
    if ((website.get("value") or {}).get("identity_assessment") or {}).get("publishable"):
        return
    known = profile.get("known_website")
    if known and known.get("url"):
        # Refresh: re-check the previously verified site at its URL instead of rediscovering it.
        record, _ = fetch_website(known["url"], identity=registry_identity(profile), max_pages=3)
        if record.get("status") == "available":
            record["source_type"] = record["source_class"] = known.get("source_class") or "previously_verified_website"
            record["value"]["registry_listed"] = known.get("source_class") in {"registry_linked_company_website", "registry_email_domain"}
            gated = apply_website_identity_gate(profile, record)["website"]
            value = gated.get("value") or {}
            assessment = value.get("identity_assessment") or {}
            markers = {marker for items in (value.get("identity_markers") or {}).values() for marker in items}
            if assessment.get("publishable") or "organisation_number" in markers or markers & {"address", "phone"}:
                assessment.update({"status": "exact", "publishable": True, "method": "previously_verified_recheck_v1",
                                   "score": max(float(assessment.get("score") or 0), 0.93),
                                   "reasons": [*assessment.get("reasons", []), f"re-verified site from previous run (markers: {sorted(markers)})"]})
                value["social_links"] = [{"platform": item["platform"], "url": item["url"]} for item in value.get("social_link_assessments") or [] if item.get("publishable")]
                value["discovery_method"] = known.get("discovery") or "previously_verified"
                profile["evidence"]["website"] = gated
                return
        elif record.get("status") in {"source_error", "blocked"}:
            # Could not re-check: keep the website state failed so refresh carries the last value forward.
            profile["evidence"]["website"] = record
            return
    profile["domain_guess"] = discover_by_domain_guess(profile)


def research_company(profile: dict[str, Any]) -> dict[str, Any]:
    """All phases for one company (used by tests and ad-hoc runs; batches use run_phase)."""
    for step in (research_core, research_accounts, research_registry_website, research_discovery):
        _guarded(step, profile)
    return profile


def _guarded(step: Callable[[dict[str, Any]], None], profile: dict[str, Any]) -> None:
    started = time.monotonic()
    try:
        step(profile)
    except BudgetExhausted as exc:
        profile["errors"].append({"source": step.__name__, "error": f"budget_exhausted: {exc}"})
    except Exception as exc:
        profile["errors"].append({"source": step.__name__, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)})
    profile["runtime_ms"] = int(profile.get("runtime_ms") or 0) + int((time.monotonic() - started) * 1000)


def run_phase(
    name: str,
    step: Callable[[dict[str, Any]], None],
    profiles: list[dict[str, Any]],
    budget: RequestBudget,
    *,
    workers: int,
    stop_when_seconds_left: float,
    on_skip: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one phase over many companies; stop starting new work when the deadline approaches."""
    started, start_requests = time.monotonic(), budget.used
    done = skipped = 0
    lock = threading.Lock()

    def task(profile: dict[str, Any]) -> None:
        nonlocal done, skipped
        left = budget.time_left()
        if (left is not None and left < stop_when_seconds_left) or budget.remaining <= 0:
            if on_skip:
                on_skip(profile)
            with lock:
                skipped += 1
            return
        _guarded(step, profile)
        with lock:
            done += 1
            if done % 200 == 0:
                log(f"  {name}: {done}/{len(profiles)} companies, {budget.used} requests")

    if profiles:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(task, profiles))
    summary = {"companies": len(profiles), "done": done, "skipped_for_deadline_or_budget": skipped,
               "seconds": round(time.monotonic() - started, 1), "requests": budget.used - start_requests}
    log(f"phase {name}: {summary}")
    return summary


def _deadline_evidence(field: str, source_type: str, url: str) -> Callable[[dict[str, Any]], None]:
    def mark(profile: dict[str, Any]) -> None:
        profile["evidence"].setdefault(field, evidence(field, "source_error", source_type, url.format(org=profile["organisation_number"]),
                                                       note="budget_exhausted: run deadline or request budget reached before this source was checked"))
    return mark


def retry_failed_accounts(profiles: dict[str, dict[str, Any]], budget: RequestBudget, *, max_seconds: float, max_requests: int, max_rounds: int = 3) -> dict[str, Any]:
    """Second pass for the accounts register, whose 503 bursts can outlast per-company retries."""
    pending = [org for org, profile in profiles.items() if (profile["evidence"].get("financials") or {}).get("status") == "source_error"
               and "budget_exhausted" not in str((profile["evidence"].get("financials") or {}).get("note") or "")]
    started, start_used, recovered, rounds = time.monotonic(), budget.used, 0, 0
    while pending and rounds < max_rounds and time.monotonic() - started < max_seconds and budget.used - start_used < max_requests and budget.remaining > 100:
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
    names.extend(item.get("navn") if isinstance(item, dict) else item for item in live_value.get("historical_names") or [])
    result = match_company_jobs(
        index, name_index,
        organisation_number=profile["organisation_number"],
        names=[name for name in names if name],
        subunit_orgs=[item.get("organisation_number") for item in locations],
    )
    result["window_days"] = index.since_days
    result["feed_complete"] = index.complete
    if result["jobs"] or (not result["errors"] and index.complete):
        status, note = "available", None
    elif result["errors"]:
        status, note = "source_error", "; ".join(item["error"] for item in result["errors"][:3])
    else:
        status, note = "source_error", f"job feed only partially read ({index.pages} pages): absence of ads is not established"
    ev["jobs"] = evidence("jobs", status, "official_job_register_nav", "https://pam-stilling-feed.nav.no/api/v1/feed", value=result, note=note)


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
    max_requests: int | None = None,
    requests_per_company: float = 19.0,
    deadline_minutes: float = 40.0,
    workers: int = 16,
    accounts_lanes: int = 6,
    nav_days: int = 45,
    use_nav: bool = True,
) -> dict[str, Any]:
    started_at = utc_now()
    run_id = run_id or "run-" + started_at.replace(":", "").replace("-", "")[:15]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    state = Path(state_dir)
    orgs, invalid = read_input_orgs(input_path)
    if max_requests is None:
        # Scale the request budget with the batch (old contract: 2,000 per 100 companies), with margin.
        max_requests = max(200, int(requests_per_company * max(1, len(orgs))))
    set_accounts_lanes(accounts_lanes)
    budget = set_active_budget(RequestBudget(max_requests=max_requests, deadline_seconds=deadline_minutes * 60, reserve=40))
    report_limits = {"max_requests": max_requests, "deadline_minutes": deadline_minutes, "workers": workers, "accounts_lanes": accounts_lanes}
    log(f"{run_id}: {len(orgs)} organisation numbers ({len(invalid)} invalid rows)")
    profiles: dict[str, dict[str, Any]] = {}
    previous_envelopes: dict[str, dict[str, Any]] = {}
    report: dict[str, Any] = {"run_id": run_id, "agent_version": AGENT_VERSION, "started_at": started_at, "input": str(input_path), "invalid_inputs": invalid, "limits": report_limits}
    try:
        universe_rows, universe_meta = load_universe_rows(Path(universe_path), orgs)
        report["universe"] = universe_meta
        previous_envelopes = load_previous(previous, state, orgs)
        report["previous_envelopes"] = len(previous_envelopes)
        profiles = {org: base_profile(org, universe_rows.get(org), universe_meta) for org in orgs}
        for org, previous_envelope in previous_envelopes.items():
            known = previous_website(previous_envelope)
            if known:
                profiles[org]["known_website"] = known

        nav = NavJobIndex(since_days=nav_days).start() if use_nav else None
        ordered = [profiles[org] for org in orgs]
        report["phases"] = {}

        # Accounts run concurrently from the start: the accounts API is the slowest, flakiest source.
        accounts_result: dict[str, Any] = {}
        accounts_thread = threading.Thread(target=lambda: accounts_result.update(run_phase(
            "accounts", research_accounts, ordered, budget, workers=max(2, accounts_lanes * 2), stop_when_seconds_left=240,
            on_skip=_deadline_evidence("financials", "official_annual_accounts", "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"))),
            name="accounts", daemon=True)
        accounts_thread.start()

        report["phases"]["core_registry"] = run_phase(
            "core_registry", research_core, ordered, budget, workers=workers, stop_when_seconds_left=120,
            on_skip=_deadline_evidence("registry_live", "official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"))
        report["phases"]["registry_websites"] = run_phase(
            "registry_websites", research_registry_website, ordered, budget, workers=workers, stop_when_seconds_left=480,
            on_skip=_deadline_evidence("website", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"))
        report["phases"]["website_discovery"] = run_phase(
            "website_discovery", research_discovery, [profile for profile in ordered if profile.get("legal_form") not in SKIP_GUESS_FORMS],
            budget, workers=workers * 2, stop_when_seconds_left=420)

        accounts_thread.join(timeout=max(1.0, (budget.time_left() or 600) - 200))
        report["phases"]["accounts"] = dict(accounts_result)
        sweep_seconds = min(600.0, max(0.0, (budget.time_left() or 600) - 300))
        report["accounts_retry"] = retry_failed_accounts(profiles, budget, max_seconds=sweep_seconds, max_requests=max(0, budget.remaining - 200))
        log(f"accounts retry sweep: {report['accounts_retry']}")

        if nav:
            remaining = max(5.0, (budget.time_left() or 600) - 150)
            log(f"waiting for NAV job feed (up to {remaining:.0f}s)")
            if not nav.wait(remaining):
                nav.stop()
                nav.wait(30)
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
