from __future__ import annotations

import json
import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .budget import active_budget
from .evidence import evidence
from .http import USER_AGENT
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
)


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        active_budget().take(newurl, purpose="website_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def _open(request: urllib.request.Request, *, timeout: float, purpose: str = "website") -> Any:
    """Single choke point for website traffic: charge the batch budget, then open."""
    active_budget().take(request.full_url, purpose=purpose)
    return SAFE_OPENER.open(request, timeout=timeout)


_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _robots_allowed(url: str, timeout: float) -> bool:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    key = f"{parsed.scheme}://{parsed.netloc.lower()}"
    with _robots_lock:
        cached = key in _robots_cache
        parser = _robots_cache.get(key)
    if not cached:
        robots_url = key + "/robots.txt"
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
            with _open(request, timeout=timeout, purpose="robots") as response:
                parser.parse(response.read(500_000).decode("utf-8", errors="replace").splitlines())
        except urllib.error.HTTPError as exc:
            # RFC 9309: 4xx means no restrictions; 5xx means assume full disallow.
            parser = None if 400 <= exc.code < 500 else False  # type: ignore[assignment]
        except Exception:
            # Unreachable robots file: allow the bounded, polite crawl rather than guessing a ban.
            parser = None
        with _robots_lock:
            _robots_cache[key] = parser
    if parser is False:
        return False
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


PAGE_CATEGORIES = (
    ("about", ("om-oss", "om_oss", "omoss", "about", "hvem-er-vi", "selskapet", "bedriften")),
    ("contact", ("kontakt", "contact")),
    ("careers", ("karriere", "career", "jobb", "ledige-stillinger", "stilling", "jobs", "join-us", "bli-med")),
    ("news", ("nyheter", "news", "aktuelt", "presse", "press", "blogg", "blog", "artikler")),
    ("people", ("ledelse", "management", "ansatte", "team", "people", "medarbeidere", "vare-folk")),
    ("locations", ("locations", "lokasjoner", "avdelinger", "butikker", "kontorer", "finn-oss")),
)


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 5) -> list[str]:
    """Pick at most one link per page category, in category priority order."""
    base = urllib.parse.urlparse(base_url)
    base_host = base.netloc.lower().removeprefix("www.")
    best: dict[str, tuple[int, str]] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower().removeprefix("www.") != base_host:
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?)$", parsed.path, re.I):
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        path = parsed.path.casefold()
        label = anchor.get_text(" ", strip=True).casefold()
        for category, terms in PAGE_CATEGORIES:
            if any(term in path for term in terms):
                score = 0 + path.count("/")
            elif any(term.replace("-", " ") in label for term in terms):
                score = 5 + path.count("/")
            else:
                continue
            if category not in best or score < best[category][0]:
                best[category] = (score, clean)
            break
    ordered = [best[category][1] for category, _ in PAGE_CATEGORIES if category in best]
    return list(dict.fromkeys(ordered))[:limit]


def page_category(url: str) -> str | None:
    path = urllib.parse.urlparse(url).path.casefold()
    return next((category for category, terms in PAGE_CATEGORIES if any(term in path for term in terms)), None)


DATE_PATTERN = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


def _iso_date(value: Any) -> str | None:
    match = DATE_PATTERN.search(str(value or ""))
    if not match:
        return None
    try:
        parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None
    if parsed > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return parsed.date().isoformat()


def dated_items(html: str, page_url: str, soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Dated news/article items from JSON-LD and <time datetime> markup (no free-text date guessing)."""
    items: dict[str, dict[str, Any]] = {}
    try:
        structured = extruct.extract(html, base_url=page_url, syntaxes=["json-ld"]).get("json-ld", [])
    except Exception:
        structured = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kinds = node.get("@type")
            kinds = set(kinds if isinstance(kinds, list) else [kinds])
            if kinds & {"NewsArticle", "BlogPosting", "Article", "PressRelease", "Report"}:
                date = _iso_date(node.get("datePublished") or node.get("dateCreated"))
                title = node.get("headline") or node.get("name")
                url = node.get("url") or node.get("mainEntityOfPage") or page_url
                if isinstance(url, dict):
                    url = url.get("@id") or page_url
                if date and title and isinstance(title, str):
                    items[str(url) + date] = {"date": date, "title": title.strip()[:200], "url": str(url), "locator": "script[type='application/ld+json']"}
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(structured)
    for time_node in soup.select("time[datetime]")[:60]:
        date = _iso_date(time_node.get("datetime"))
        if not date:
            continue
        container = time_node.find_parent(["article", "li"]) or time_node.parent
        heading = container.find(["h1", "h2", "h3", "h4"]) if container else None
        link = (heading.find("a", href=True) if heading else None) or (container.find("a", href=True) if container else None)
        title = (heading or link).get_text(" ", strip=True) if (heading or link) else ""
        if not title or len(title) < 8:
            continue
        url = urllib.parse.urljoin(page_url, link.get("href")) if link else page_url
        items.setdefault(url + date, {"date": date, "title": title[:200], "url": url, "locator": "time[datetime]"})
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items.values():
        item["url"] = str(item["url"]).split("#", 1)[0]
        unique.setdefault((item["title"].casefold(), item["date"]), item)
    return sorted(unique.values(), key=lambda item: item["date"], reverse=True)[:12]


def identity_markers(html: str, identity: dict[str, Any] | None) -> list[str]:
    """Exact registry markers present in raw HTML (footers included): orgnr, phone, address."""
    if not identity:
        return []
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&#160;|\u00a0", " ", text)
    digits_only = re.sub(r"(?<=\d)[\s.\-](?=\d)", "", text)
    found = []
    org = str(identity.get("organisation_number") or "")
    if len(org) == 9 and org in digits_only:
        found.append("organisation_number")
    for phone in identity.get("phones") or []:
        phone = re.sub(r"\D", "", str(phone))[-8:]
        if len(phone) == 8 and phone in digits_only:
            found.append("phone")
            break
    postal, street = str(identity.get("postal_code") or ""), str(identity.get("street") or "").casefold()
    lowered = text.casefold()
    if postal and street and re.search(rf"\b{re.escape(postal)}\b", text) and street in lowered:
        found.append("address")
    return found


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int, identity: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    if not _robots_allowed(url, timeout):
        return None, [], 1, 0, 0, "robots.txt disallows page"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with _open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            final_url = response.geturl()
            if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                return None, [], 2, len(raw), elapsed, "unsupported or oversized page"
            if _registered_domain(final_url) != homepage_domain:
                return None, [], 2, len(raw), elapsed, "redirected outside registered domain"
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        page = {
            "identity_markers": identity_markers(page_html, identity),
            "category": page_category(url),
            "dated_items": dated_items(page_html, final_url, page_soup),
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        }
        return page, _social_links(final_url, page_soup), 2, len(raw), elapsed, None
    except Exception as exc:
        return None, [], 2, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def fetch_website(
    url: str | None,
    *,
    timeout: float = 15.0,
    max_bytes: int = 2_000_000,
    identity: dict[str, Any] | None = None,
    max_pages: int = 5,
) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    if not _robots_allowed(normalized, timeout):
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": 1, "bytes": 0, "latencies_ms": []}
    started = time.monotonic()
    request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with _open(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            elapsed = int((time.monotonic() - started) * 1000)
            if len(raw) > max_bytes:
                return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            if "html" not in content_type.lower():
                return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
            final_url = response.geturl()
            assert_public_url(final_url)
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "social_links": _social_links(final_url, soup),
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
            "site_name": next((str(node.get("content")).strip() for node in soup.select('meta[property="og:site_name"]') if node.get("content")), None),
        }
        pages = [{"url": final_url, "title": title[:500], "main_text_excerpt": text[:5000], "content_sha256": value["content_sha256"]}]
        social = value["social_links"]
        crawl_errors = []
        requests = 2
        bytes_received = len(raw)
        page_latencies = [elapsed]
        homepage_domain = value["registered_domain"]
        value["identity_markers"] = {final_url: identity_markers(html, identity)} if identity else {}
        for page_url in _priority_links(final_url, soup, limit=max_pages):
            page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                page_url,
                homepage_domain=homepage_domain,
                timeout=timeout,
                max_bytes=min(max_bytes, 1_000_000),
                identity=identity,
            )
            requests += page_requests
            bytes_received += page_bytes
            if page_elapsed:
                page_latencies.append(page_elapsed)
            if page:
                pages.append(page)
                if page.get("identity_markers"):
                    value["identity_markers"][page["url"]] = page["identity_markers"]
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        news: dict[str, dict[str, Any]] = {}
        for item in dated_items(html, final_url, soup) + [item for page in pages[1:] for item in page.get("dated_items") or []]:
            news.setdefault(item["title"].casefold() + item["date"], item)
        value["news_items"] = sorted(news.values(), key=lambda item: item["date"], reverse=True)[:12]
        value["careers_pages"] = [page["url"] for page in pages[1:] if page.get("category") == "careers"]
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://"):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes, identity=identity, max_pages=max_pages)
            metrics["requests"] += 2
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": 2, "bytes": 0, "latencies_ms": [elapsed]}
