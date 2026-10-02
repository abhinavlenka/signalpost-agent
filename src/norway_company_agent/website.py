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
from .rawstore import save_raw
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


def page_social_links(html: str, base_url: str, soup: BeautifulSoup | None = None) -> list[dict[str, str]]:
    """Profiles a page links to or declares as its own: anchors, Organization ``sameAs`` and publisher meta."""
    soup = soup or BeautifulSoup(html, "lxml")
    found: dict[tuple[str, str], dict[str, str]] = {}

    def add(items: list[dict[str, str]], declared_in: str) -> None:
        for item in items:
            found.setdefault((item["platform"], item["url"].casefold()), {**item, "declared_in": declared_in})

    add(_social_links(base_url, soup), "a[href]")
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text() or "null")
        except (ValueError, TypeError):
            continue
        # only what the site says about the organisation itself: an article author's or a product's sameAs is someone else's profile
        for organisation in _jsonld_organisations({"json-ld": data}):
            add(structured_social_links({"sameAs": organisation.get("sameAs")}), "ld+json:sameAs")
    for selector in ('meta[property="article:publisher"]', 'meta[property="og:see_also"]'):
        for node in soup.select(selector):
            normalized = normalize_social_url(str(node.get("content") or "").strip())
            if normalized:
                add([normalized], selector)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


BOT_CHALLENGE_TITLES = (
    "just a moment", "verifying", "attention required", "access denied", "please wait", "checking your browser",
    "ddos-guard", "are you a robot", "pardon our interruption", "security check", "one moment, please",
)


def is_bot_challenge(title: str, text: str) -> bool:
    """A bot-protection interstitial instead of the site: a stock title and next to no content."""
    lowered = str(title or "").casefold().strip().rstrip(".!… ").strip()
    return len(str(text or "").strip()) < 200 and any(lowered == stock or lowered.startswith(stock + " |") or lowered.startswith(stock + " -") or lowered.startswith(stock + "!")
                                                      for stock in BOT_CHALLENGE_TITLES)


def declared_feed_url(html: str, page_url: str, soup: BeautifulSoup | None = None) -> str | None:
    """The site's own RSS/Atom feed as declared in the page head (same registered domain, not a comments feed)."""
    soup = soup or BeautifulSoup(html, "lxml")
    domain = _registered_domain(page_url)
    for node in soup.select('link[rel~="alternate"][href]'):
        kind = str(node.get("type") or "").casefold()
        if "rss+xml" not in kind and "atom+xml" not in kind:
            continue
        url = urllib.parse.urljoin(page_url, str(node.get("href")).strip())
        if "comment" in url.casefold() or "comment" in str(node.get("title") or "").casefold():
            continue
        if urllib.parse.urlparse(url).scheme in {"http", "https"} and _registered_domain(url) == domain:
            return url
    return None


def feed_items(xml: bytes | str, feed_url: str, limit: int = 12) -> list[dict[str, Any]]:
    """Dated posts from an RSS or Atom feed. Only items with an explicit date and a link on the feed's own domain.

    Pass the bytes as received: the parser then honours the encoding the feed declares.
    """
    from email.utils import parsedate_to_datetime

    from lxml import etree

    if isinstance(xml, str):  # already decoded: drop the declared encoding so it is not applied a second time
        xml = re.sub(r'^(\s*<\?xml[^>]*?)\s+encoding=["\'][^"\']*["\']', r"\1", xml).encode("utf-8")
    try:
        root = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False))
    except (etree.XMLSyntaxError, ValueError):
        return []
    if root is None:
        return []
    domain = _registered_domain(feed_url)
    local = lambda node: etree.QName(node).localname if isinstance(node.tag, str) else ""  # noqa: E731
    child_text = lambda node, *names: next((" ".join((child.text or "").split()) for child in node if local(child) in names and (child.text or "").strip()), "")  # noqa: E731
    items: list[dict[str, Any]] = []
    for node in root.iter():
        kind = local(node)
        if kind not in {"item", "entry"}:
            continue
        title = child_text(node, "title")
        if kind == "item":
            link, raw_date, locator = child_text(node, "link"), child_text(node, "pubDate", "date"), "rss:item/pubDate"
            try:
                date = parsedate_to_datetime(raw_date).date().isoformat() if raw_date and not DATE_PATTERN.match(raw_date) else _iso_date(raw_date)
            except (TypeError, ValueError):
                date = None
        else:
            links = [child for child in node if local(child) == "link" and child.get("href")]
            link = next((child.get("href") for child in links if child.get("rel") in (None, "alternate")), links[0].get("href") if links else "")
            date, locator = _iso_date(child_text(node, "published", "updated")), "atom:entry/published"
        date = _iso_date(date)  # also rejects dates in the future
        url = urllib.parse.urljoin(feed_url, link) if link else ""
        if not (title and date and url) or _registered_domain(url) != domain:
            continue
        items.append({"date": date, "title": title[:200], "url": url.split("#", 1)[0], "locator": locator})
    return sorted(items, key=lambda item: item["date"], reverse=True)[:limit]


def _fetch_feed(url: str, *, timeout: float) -> tuple[list[dict[str, Any]], int]:
    """One bounded request for the declared feed; any failure simply yields no items."""
    try:
        assert_public_url(url)  # a feed may sit on another host of the same domain: check it like any other target
    except ValueError:
        return [], 0
    try:
        if not _robots_allowed(url, timeout):
            return [], 1
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.5"})
        with _open(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000 or _registered_domain(response.geturl()) != _registered_domain(url):
                return [], 2
        digest = __import__("hashlib").sha256(raw).hexdigest()
        save_raw(digest, raw)
        found_in = {"url": url, "content_sha256": digest, "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        return [{**item, "found_in": found_in} for item in feed_items(raw, url)], 2
    except Exception:
        return [], 2


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


# A careers page is published as a hiring signal, so it is matched on whole words: "bestilling" (an order)
# and "utstilling" (an exhibition) contain "stilling", and "jobber" is what a craftsman calls finished projects.
CAREERS_PATH = re.compile(
    r"(?<![a-z0-9æøå])(?:karriere|careers?|jobs?|jobbe?(?:[-_](?:hos|i|med)[-_][a-z0-9æøå_-]+)?|ledige?[-_]?stilling(?:er|ar|ane)?"
    r"|stilling(?:er|ar)?(?:[-_]ledige?)?|vacanc(?:y|ies)|join[-_]us|bli[-_]med|work[-_]with[-_]us)(?![a-z0-9æøå])")
CAREERS_LABEL = re.compile(
    r"(?<![a-z0-9æøå])(?:karriere|careers?|ledige? stilling(?:er|ar)?|stilling(?:er|ar)? ledige?|jobbe? hos oss|jobb i \w+|vacanc(?:y|ies)|join us|work with us|bli med på laget)(?![a-z0-9æøå])|^jobb$|^jobs$")


def _matches_category(category: str, terms: tuple[str, ...], path: str, label: str | None = None) -> str | None:
    """'path' when the URL path names the category, 'label' when only the link text does, else None."""
    if category == "careers":
        return "path" if CAREERS_PATH.search(path) else "label" if label is not None and CAREERS_LABEL.search(label) else None
    if any(term in path for term in terms):
        return "path"
    return "label" if label is not None and any(term.replace("-", " ") in label for term in terms) else None


def _same_page(first: str, second: str) -> bool:
    a, b = urllib.parse.urlparse(first), urllib.parse.urlparse(second)
    return (a.netloc.lower().removeprefix("www."), a.path.rstrip("/")) == (b.netloc.lower().removeprefix("www."), b.path.rstrip("/"))


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
        if _same_page(clean, base_url):
            continue
        path = parsed.path.casefold()
        label = anchor.get_text(" ", strip=True).casefold()
        for category, terms in PAGE_CATEGORIES:
            matched = _matches_category(category, terms, path, label)
            if matched == "path":
                score = 0 + path.count("/")
            elif matched == "label":
                score = 5 + path.count("/")
            else:
                continue
            if category not in best or score < best[category][0]:
                best[category] = (score, clean)
            break
    ordered = [best[category][1] for category, _ in PAGE_CATEGORIES if category in best]
    return list(dict.fromkeys(ordered))[:limit]


CAREERS_HOSTS = {"career", "careers", "karriere", "jobb", "jobs", "job"}


def page_category(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.casefold()
    if CAREERS_PATH.search(path) or (parsed.hostname or "").casefold().split(".")[0] in CAREERS_HOSTS:  # career.example.com
        return "careers"
    return next((category for category, terms in PAGE_CATEGORIES if category != "careers" and _matches_category(category, terms, path)), None)


STATIC_PAGE_SLUG = re.compile(
    r"^(om|om[-_]oss.*|about.*|kontakt.*|contact.*|karriere.*|careers?|jobb.*|ledige?[-_]stilling.*|butikker|ansatte|team|personvern.*|privacy.*|hjem|home|forside|index(\.\w+)?)$")


def is_static_page(url: str) -> bool:
    """Homepage, about, contact and similar pages: many CMSs mark them as schema.org Article, but they are not news."""
    segments = [segment for segment in urllib.parse.urlparse(url).path.casefold().split("/") if segment]
    return not segments or bool(STATIC_PAGE_SLUG.match(segments[-1])) or (len(segments) == 1 and segments[0].startswith(("om-", "om_")))


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
                if date and title and isinstance(title, str) and not is_static_page(str(url)):
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


def marker_snippets(html: str, identity: dict[str, Any] | None) -> dict[str, str]:
    """The page text around each registry marker, so a reader can see the proof without opening the page."""
    if not identity:
        return {}
    present = set(identity_markers(html, identity))  # a snippet is proof only for a marker that actually holds
    if not present:
        return {}
    text = " ".join(re.sub(r"&nbsp;|&#160;|\u00a0", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html))).split())
    snippets: dict[str, str] = {}

    def around(match: re.Match[str] | None, marker: str) -> None:
        if match and marker in present and marker not in snippets:
            snippets[marker] = text[max(0, match.start() - 60):match.end() + 40].strip()

    spaced = lambda digits: r"[\s.\-]?".join(re.escape(char) for char in digits)  # noqa: E731
    org = str(identity.get("organisation_number") or "")
    if len(org) == 9:
        around(re.search(spaced(org), text), "organisation_number")
    for phone in identity.get("phones") or []:
        phone = re.sub(r"\D", "", str(phone))[-8:]
        if len(phone) == 8:
            around(re.search(spaced(phone), text), "phone")
    postal, street = str(identity.get("postal_code") or ""), str(identity.get("street") or "")
    if postal and street:
        around(re.search(re.escape(street), text, re.I), "address")
    return snippets


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
        save_raw(__import__("hashlib").sha256(raw).hexdigest(), raw)
        page = {
            "identity_markers": identity_markers(page_html, identity),
            "identity_snippets": marker_snippets(page_html, identity),
            "category": page_category(final_url),  # where the server actually took us, not what the link promised
            "dated_items": dated_items(page_html, final_url, page_soup),
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        }
        for item in page["dated_items"]:
            item["found_in"] = {"url": final_url, "content_sha256": page["content_sha256"]}
        return page, page_social_links(page_html, final_url, page_soup), 2, len(raw), elapsed, None
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
        if is_bot_challenge(title, text):
            return evidence("website", "blocked", "registry_linked_company_website", normalized, note=f"bot-protection page instead of the site (title: {title[:60]})"), {"requests": 2, "bytes": len(raw), "latencies_ms": [elapsed]}
        save_raw(__import__("hashlib").sha256(raw).hexdigest(), raw)
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "social_links": page_social_links(html, final_url, soup),
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
        value["identity_snippets"] = {final_url: marker_snippets(html, identity)} if identity else {}
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
                    value["identity_snippets"][page["url"]] = page.get("identity_snippets") or {}
                social.extend(page_social)
            elif page_error:
                crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        feed_news: list[dict[str, Any]] = []
        try:  # the feed is a bonus: a malformed link or a flaky feed host must never cost the site itself
            feed_url = declared_feed_url(html, final_url, soup) if max_pages else None
            if feed_url:
                feed_news, feed_requests = _fetch_feed(feed_url, timeout=timeout)
                requests += feed_requests
                value["feed_url"] = feed_url
        except Exception as exc:
            crawl_errors.append({"url": "feed", "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
        home_news = [{**item, "found_in": {"url": final_url, "content_sha256": value["content_sha256"]}} for item in dated_items(html, final_url, soup)]
        news: dict[str, dict[str, Any]] = {}
        for item in feed_news + home_news + [item for page in pages[1:] for item in page.get("dated_items") or []]:
            news.setdefault(item["title"].casefold() + item["date"], item)
        value["news_items"] = sorted(news.values(), key=lambda item: item["date"], reverse=True)[:12]
        value["careers_pages"] = [page["url"] for page in pages[1:] if page.get("category") == "careers" and not _same_page(page["url"], final_url)]
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
