"""NAV arbeidsplassen job-ad connector (pam-stilling-feed).

Source: https://navikt.github.io/pam-stilling-feed/ — Norway's public employment service feed.
Access: the documented public token endpoint; terms at https://arbeidsplassen.nav.no/vilkar-api.

Flow per batch:
1. One background walk of the feed pages modified in the last ``since_days`` (≈90 pages / 45 days),
   keeping the latest status per ad. Feed items carry only ``businessName``.
2. Candidate ads are those whose normalized business name equals a batch company's normalized legal
   name (or one of its registered subunit names).
3. Each candidate's feed entry is fetched and published only when ``employer.orgnr`` is the exact
   company or one of its own registered subunits. Name equality alone never publishes a job.

Contact persons, e-mails and phone numbers in ads are never stored.
"""
from __future__ import annotations

import re
import threading
import unicodedata
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Iterable

from .budget import active_budget
from .http import FetchResult, fetch_json, fetch_text
from .identity import _tokens

FEED_BASE = "https://pam-stilling-feed.nav.no"
PUBLIC_TOKEN_URL = FEED_BASE + "/api/publicToken"
PUBLIC_AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"


GENERIC_TOKENS = {
    "holding", "holdings", "invest", "investering", "investment", "eiendom", "eiendommer", "eiendomsselskap", "bygg",
    "drift", "service", "services", "gruppen", "group", "norge", "norway", "norsk", "consult", "consulting", "fritid",
    "klinikken", "profil", "solutions", "partner", "partners", "capital", "management", "utvikling", "handel", "og", "i",
    "of", "og", "vs", "kommune", "senter", "senteret", "systems", "system", "company", "nordic", "scandinavia",
}


def name_key(value: Any) -> str:
    return " ".join(_tokens(value))


def raw_tokens(value: Any) -> list[str]:
    """Tokens including single-letter initials (so "J.A. Invest" never reduces to "invest")."""
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"})).casefold()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    legal = {"as", "asa", "ans", "da", "enk", "sa", "nuf", "ab", "ltd", "avd", "avdeling"}
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in legal]


def token_set_eligible(tokens: list[str]) -> bool:
    return any(len(token) >= 5 and token not in GENERIC_TOKENS and not token.isdigit() for token in tokens)


class NavJobIndex:
    def __init__(self, *, since_days: int = 45, max_pages: int = 140, page_timeout: float = 30.0) -> None:
        self.since_days = since_days
        self.max_pages = max_pages
        self.page_timeout = page_timeout
        self.ads: dict[str, dict[str, Any]] = {}
        self.pages = 0
        self.error: str | None = None
        self.token: str | None = None
        self.started_at: str | None = None
        self.completed_at: str | None = None
        self.complete = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- feed walk -----------------------------------------------------------------------------
    def start(self) -> "NavJobIndex":
        self._thread = threading.Thread(target=self._run, name="nav-feed", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None) -> bool:
        return self._ready.wait(timeout)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _run(self) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            token = fetch_text(PUBLIC_TOKEN_URL, purpose="nav_token")
            match = re.search(r"eyJ[\w.-]+", str(token.body or ""))
            if token.status != 200 or not match:
                self.error = f"public token unavailable: {token.error or token.status}"
                return
            self.token = match.group(0)
            since = datetime.now(timezone.utc) - timedelta(days=self.since_days)
            headers = {**self._headers(), "If-Modified-Since": format_datetime(since, usegmt=True)}
            page = fetch_json(FEED_BASE + "/api/v1/feed", headers=headers, timeout=self.page_timeout, purpose="nav_feed")
            while True:
                if page.status != 200 or not isinstance(page.body, dict):
                    self.error = f"feed page failed: {page.error or page.status}"
                    return
                self.pages += 1
                for item in page.body.get("items") or []:
                    entry = item.get("_feed_entry") or {}
                    self.ads[item["id"]] = {
                        "uuid": item["id"],
                        "status": entry.get("status"),
                        "title": entry.get("title") or item.get("title"),
                        "business_name": entry.get("businessName"),
                        "municipal": entry.get("municipal"),
                        "modified": item.get("date_modified"),
                        "entry_path": item.get("url"),
                    }
                next_url = page.body.get("next_url")
                if not next_url or self._stop.is_set() or self.pages >= self.max_pages:
                    self.complete = not next_url
                    return
                page = fetch_json(FEED_BASE + next_url, headers=self._headers(), timeout=self.page_timeout, purpose="nav_feed")
        except Exception as exc:  # the batch must never die because of this connector
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self._ready.set()

    # -- matching ------------------------------------------------------------------------------
    def build_name_index(self) -> dict[str, Any]:
        return build_name_index(self.ads.values())

    def fetch_entry(self, ad: dict[str, Any]) -> FetchResult:
        path = ad.get("entry_path") or f"/api/v1/feedentry/{ad['uuid']}"
        return fetch_json(FEED_BASE + path, headers=self._headers(), timeout=self.page_timeout, purpose="nav_entry")

    def report(self) -> dict[str, Any]:
        return {
            "source": "nav_pam_stilling_feed",
            "pages": self.pages,
            "ads_indexed": len(self.ads),
            "active_ads": sum(ad.get("status") == "ACTIVE" for ad in self.ads.values()),
            "complete": self.complete,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def build_name_index(ads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    exact: dict[str, list[dict[str, Any]]] = {}
    by_token: dict[str, list[dict[str, Any]]] = {}
    for ad in ads:
        if ad.get("status") != "ACTIVE":
            continue
        key = name_key(ad.get("business_name"))
        if key:
            exact.setdefault(key, []).append(ad)
        for token in set(raw_tokens(ad.get("business_name"))):
            by_token.setdefault(token, []).append(ad)
    return {"exact": exact, "tokens": by_token}


def candidate_ads(name_index: dict[str, Any], names: Iterable[str], *, token_limit: int = 4) -> list[dict[str, Any]]:
    """Exact normalized-name matches first, then token-set matches (all name tokens present)."""
    exact: dict[str, dict[str, Any]] = {}
    loose: dict[str, dict[str, Any]] = {}
    for name in names:
        for ad in name_index["exact"].get(name_key(name), []):
            exact[ad["uuid"]] = ad
        tokens = raw_tokens(name)
        if not tokens or not token_set_eligible(tokens):
            continue
        pools = [{ad["uuid"]: ad for ad in name_index["tokens"].get(token, [])} for token in set(tokens)]
        shared = set.intersection(*[set(pool) for pool in pools]) if pools else set()
        for uuid in shared:
            if uuid not in exact:
                loose[uuid] = pools[0][uuid]
    # Ties on the modified time are broken by ad id, so the selection never depends on set order.
    newest = lambda ads: sorted(ads, key=lambda ad: (str(ad.get("modified") or ""), str(ad.get("uuid") or "")), reverse=True)  # noqa: E731
    return newest(exact.values()) + newest(loose.values())[:token_limit]


def normalize_ad(entry: dict[str, Any], retrieved: FetchResult) -> dict[str, Any]:
    ad = entry.get("ad_content") or {}
    employer = ad.get("employer") or {}
    locations = [
        {key: location.get(key) for key in ("city", "municipal", "county", "postalCode", "country") if location.get(key)}
        for location in ad.get("workLocations") or []
    ]
    return {
        "uuid": ad.get("uuid") or entry.get("uuid"),
        "title": ad.get("title"),
        "job_title": ad.get("jobtitle"),
        "published": ad.get("published"),
        "expires": ad.get("expires"),
        "updated": ad.get("updated"),
        "application_due": ad.get("applicationDue"),
        "engagement_type": ad.get("engagementtype"),
        "extent": ad.get("extent"),
        "positions": ad.get("positioncount"),
        "work_locations": locations,
        "employer_name": employer.get("name"),
        "employer_orgnr": employer.get("orgnr"),
        "employer_homepage": employer.get("homepage"),
        "public_url": PUBLIC_AD_URL.format(uuid=ad.get("uuid") or entry.get("uuid")),
        "source_url": retrieved.url,
        "retrieved_at": retrieved.retrieved_at,
        "content_sha256": retrieved.content_sha256,
    }


def match_company_jobs(
    index: NavJobIndex,
    name_index: dict[str, Any],
    *,
    organisation_number: str,
    names: Iterable[str],
    subunit_orgs: Iterable[str],
    max_entries: int = 8,
) -> dict[str, Any]:
    """Return verified jobs plus rejected candidates for one company."""
    allowed = {organisation_number, *[org for org in subunit_orgs if org]}
    ordered = candidate_ads(name_index, names)
    candidates = {ad["uuid"]: ad for ad in ordered}
    jobs, rejected, errors = [], [], []
    for ad in ordered[:max_entries]:
        if active_budget().remaining <= 0:
            errors.append({"uuid": ad["uuid"], "error": "budget_exhausted"})
            break
        result = index.fetch_entry(ad)
        if result.status != 200 or not isinstance(result.body, dict):
            errors.append({"uuid": ad["uuid"], "error": result.error or f"HTTP {result.status}"})
            continue
        normalized = normalize_ad(result.body, result)
        if normalized["employer_orgnr"] in allowed:
            normalized["matched_via"] = "exact_organisation_number" if normalized["employer_orgnr"] == organisation_number else "registered_subunit"
            jobs.append(normalized)
        else:
            rejected.append({"uuid": ad["uuid"], "employer_orgnr": normalized["employer_orgnr"], "reason": "employer organisation number differs"})
    return {
        "candidates": len(candidates),
        "checked": min(len(ordered), max_entries),
        "jobs": sorted(jobs, key=lambda job: (str(job.get("published") or ""), str(job.get("uuid") or "")), reverse=True),
        "rejected": rejected,
        "errors": errors,
        "truncated": len(ordered) > max_entries,
    }
