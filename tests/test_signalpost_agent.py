from __future__ import annotations

import copy
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.budget import BudgetExhausted, RequestBudget, set_active_budget  # noqa: E402
from norway_company_agent.claim_refresh import apply_refresh  # noqa: E402
from norway_company_agent.envelope import STATES, build_envelope  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.jobs_nav import build_name_index, candidate_ads, match_company_jobs  # noqa: E402
from norway_company_agent.http import FetchResult  # noqa: E402
from norway_company_agent.pipeline import read_input_orgs  # noqa: E402
from norway_company_agent.summary import build_summary  # noqa: E402

RUN = {"run_id": "r1", "started_at": "2026-09-27T00:00:00Z", "completed_at": "2026-09-27T00:01:00Z", "terminal_status": "completed"}
RUN2 = {"run_id": "r2", "started_at": "2026-09-28T00:00:00Z", "completed_at": "2026-09-28T00:01:00Z", "terminal_status": "completed"}


def profile(**overrides):
    org = "888567232"
    base = {
        "organisation_number": org,
        "name": "AAS ELEKTRONIKK AS",
        "evidence": {
            "registry_live": evidence("registry_live", "available", "official_registry_live", f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                                      value={"name": "AAS ELEKTRONIKK AS", "organisation_number": org, "legal_form": "AS", "employees": None,
                                             "has_registered_employees": False, "latest_submitted_accounts": "2025", "in_group": False,
                                             "business_address": {"adresse": ["Natvigveien 17"], "postnummer": "4823", "poststed": "NEDENES", "kommune": "ARENDAL"}},
                                      content_sha256="a" * 64, retrieved_at="2026-09-27T00:00:10Z"),
            "financials": evidence("financials", "available", "official_annual_accounts", f"https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
                                   value={"records": [{"period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "currency": "NOK", "account_type": "SELSKAP",
                                                       "revenue": 1425713.0, "annual_result": None, "assets": 0.0}]},
                                   content_sha256="b" * 64, retrieved_at="2026-09-27T00:00:11Z"),
            "roles": evidence("roles", "available", "official_roles", f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller",
                              value={"roles": [{"name": "Tor Ivar Aas", "role_code": "DAGL", "role": "Daglig leder", "inactive": False}]},
                              content_sha256="c" * 64, retrieved_at="2026-09-27T00:00:12Z"),
            "locations": evidence("locations", "available", "official_subunits", "https://data.brreg.no/x", value={"locations": []}, content_sha256="d" * 64),
            "website": evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/x", note="registry lists no website"),
            "jobs": evidence("jobs", "available", "official_job_register_nav", "https://pam-stilling-feed.nav.no", value={"jobs": [], "window_days": 45}),
        },
        "errors": [],
    }
    base.update(overrides)
    return base


def test_budget_never_exceeds_cap_under_concurrency():
    budget = RequestBudget(max_requests=50)
    taken = []

    def worker():
        for _ in range(20):
            try:
                budget.take("https://example.no/")
                taken.append(1)
            except BudgetExhausted:
                pass

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(taken) == 50 and budget.used == 50


def test_budget_reserve_only_for_essential():
    budget = RequestBudget(max_requests=3, reserve=1)
    budget.take("https://a.no")
    budget.take("https://a.no")
    with pytest.raises(BudgetExhausted):
        budget.take("https://a.no")
    budget.take("https://a.no", essential=True)


def test_envelope_states_and_no_zero_fill():
    envelope = build_envelope(profile(), run=RUN)
    assert set(envelope["availability"].values()) <= set(STATES)
    fields = {(claim["family"], claim["field"]) for claim in envelope["claims"]}
    assert ("annual_accounts", "revenue") in fields
    assert ("annual_accounts", "annual_result") not in fields  # None stays absent
    assert ("annual_accounts", "assets") in fields  # a real reported zero is kept
    assert ("legal_identity", "registered_employees") not in fields  # unknown employees never become 0
    for claim in envelope["claims"]:
        assert claim["evidence_ids"]
        for evidence_id in claim["evidence_ids"]:
            record = next(item for item in envelope["evidence"] if item["evidence_id"] == evidence_id)
            assert record["source_url"] and record["retrieved_at"]
    assert envelope["availability"]["jobs"] == "not_available"
    assert envelope["availability"]["official_website"] == "not_available"


def test_fatal_profile_yields_failed_envelope():
    envelope = build_envelope({"organisation_number": "123456789", "fatal_error": "boom"}, run=RUN)
    assert envelope["state"] == "failed"
    assert set(envelope["availability"].values()) == {"failed"}


def test_budget_exhausted_source_maps_to_failed():
    p = profile()
    p["evidence"]["financials"] = evidence("financials", "source_error", "official_annual_accounts", "https://x", note="budget_exhausted: cap")
    envelope = build_envelope(p, run=RUN)
    assert envelope["availability"]["annual_accounts"] == "failed"


def test_refresh_is_idempotent_and_detects_real_changes():
    first = apply_refresh(None, build_envelope(profile(), run=RUN))
    same = apply_refresh(copy.deepcopy(first), build_envelope(profile(), run=RUN2))
    assert same["changes"] == [] and same["refresh"]["changes_detected"] == 0
    assert all(claim["first_seen"] == RUN["started_at"] for claim in same["claims"])

    changed = profile()
    changed["evidence"]["roles"]["value"] = {"roles": [{"name": "Kari Nordmann", "role_code": "DAGL", "role": "Daglig leder", "inactive": False}]}
    second = apply_refresh(copy.deepcopy(first), build_envelope(changed, run=RUN2))
    kinds = sorted(change["change_type"] for change in second["changes"])
    assert kinds == ["new_role", "removed_role"]
    assert all(change["material"] for change in second["changes"])
    # replaying the same pair gives identical change ids (no duplicates in the log)
    replay = apply_refresh(copy.deepcopy(first), build_envelope(changed, run=RUN2))
    assert [c["change_id"] for c in replay["changes"]] == [c["change_id"] for c in second["changes"]]
    third = apply_refresh(copy.deepcopy(second), build_envelope(changed, run=RUN2))
    assert third["changes"] == []
    assert len({c["change_id"] for c in third["change_log"]}) == len(third["change_log"]) == 2


def test_failed_refresh_keeps_last_supported_value():
    first = apply_refresh(None, build_envelope(profile(), run=RUN))
    broken = profile()
    broken["evidence"]["roles"] = evidence("roles", "source_error", "official_roles", "https://x", note="HTTP 503")
    second = apply_refresh(copy.deepcopy(first), build_envelope(broken, run=RUN2))
    assert second["changes"] == []
    carried = [claim for claim in second["claims"] if claim["family"] == "leadership"]
    assert carried and carried[0]["stale"] is True


def test_changed_financial_value_is_typed_change():
    first = apply_refresh(None, build_envelope(profile(), run=RUN))
    restated = profile()
    restated["evidence"]["financials"]["value"]["records"][0]["revenue"] = 1500000.0
    second = apply_refresh(copy.deepcopy(first), build_envelope(restated, run=RUN2))
    assert [change["change_type"] for change in second["changes"]] == ["changed_revenue"]
    assert second["changes"][0]["evidence_ids"]["previous"]


def test_summary_sentences_cite_claims():
    envelope = apply_refresh(None, build_envelope(profile(), run=RUN))
    summary = build_summary(envelope)
    ids = {claim["claim_id"] for claim in envelope["claims"]}
    assert summary["sentences"]
    for sentence in summary["sentences"]:
        assert sentence["claim_ids"] and set(sentence["claim_ids"]) <= ids
    assert "private limited company" in summary["text"]


def test_input_formats(tmp_path):
    (tmp_path / "a.txt").write_text("organisation_number\n888 567 232\n123\n888567232\n")
    orgs, invalid = read_input_orgs(tmp_path / "a.txt")
    assert orgs == ["888567232"] and len(invalid) == 1
    (tmp_path / "b.jsonl").write_text(json.dumps({"organisation_number": "888567232"}) + "\n")
    assert read_input_orgs(tmp_path / "b.jsonl")[0] == ["888567232"]
    (tmp_path / "c.json").write_text(json.dumps(["888567232", "923609016"]))
    assert read_input_orgs(tmp_path / "c.json")[0] == ["888567232", "923609016"]


class FakeIndex:
    def __init__(self, entries):
        self.entries = entries

    def fetch_entry(self, ad):
        return FetchResult(url="https://feed/" + ad["uuid"], status=200, elapsed_ms=1, bytes_received=1, body=self.entries[ad["uuid"]], content_sha256="e" * 64, retrieved_at="t")


def test_jobs_publish_only_on_exact_orgnr():
    set_active_budget(None)
    ads = {"u1": {"ad_content": {"uuid": "u1", "title": "Montør", "employer": {"orgnr": "888567232", "name": "Aas Elektronikk AS"}}},
           "u2": {"ad_content": {"uuid": "u2", "title": "Selger", "employer": {"orgnr": "999999999", "name": "Aas Elektronikk AS"}}},
           "u3": {"ad_content": {"uuid": "u3", "title": "Lager", "employer": {"orgnr": "985636230", "name": "Aas Elektronikk AS"}}}}
    name_index = build_name_index([{"uuid": uuid, "modified": uuid, "status": "ACTIVE", "business_name": "Aas Elektronikk AS"} for uuid in ads])
    result = match_company_jobs(FakeIndex(ads), name_index, organisation_number="888567232", names=["AAS ELEKTRONIKK AS"], subunit_orgs=["985636230"])
    assert sorted(job["uuid"] for job in result["jobs"]) == ["u1", "u3"]
    assert [item["uuid"] for item in result["rejected"]] == ["u2"]


def test_token_set_candidates_keep_initials_and_skip_generic_names():
    ads = [{"uuid": str(i), "status": "ACTIVE", "business_name": name, "modified": str(i)} for i, name in enumerate(
        ["Supermercado AS - Frukthagen Tåsen", "GP-Invest As", "Hakkespetten Barnehage", "Avdeling fritid, Porsgrunn kommune", "Slemdal skole"])]
    index = build_name_index(ads)
    names = lambda name: [ad["business_name"] for ad in candidate_ads(index, [name])]  # noqa: E731
    assert names("SUPERMERCADO AS") == ["Supermercado AS - Frukthagen Tåsen"]
    assert names("J.A. INVEST AS") == []
    assert names("FRITID AS") == []
    assert names("HAKKESPETTEN BARNEHAGE AS") == ["Hakkespetten Barnehage"]


def test_recovered_source_is_backfill_not_change():
    broken = profile()
    broken["evidence"]["financials"] = evidence("financials", "source_error", "official_annual_accounts", "https://x", note="HTTP 503")
    first = apply_refresh(None, build_envelope(broken, run=RUN))
    assert first["availability"]["annual_accounts"] == "failed"
    second = apply_refresh(copy.deepcopy(first), build_envelope(profile(), run=RUN2))
    assert second["changes"] == []
    assert second["refresh"]["backfilled_claims"] > 0


def test_identity_markers_find_orgnr_phone_address_in_footer():
    from norway_company_agent.website import identity_markers

    html = "<footer>Aas Elektronikk AS · Org.nr: 888 567&nbsp;232 MVA · Natvigveien 17, 4823 Nedenes · Tlf 900 49 299</footer>"
    identity = {"organisation_number": "888567232", "phones": ["90049299"], "postal_code": "4823", "street": "Natvigveien"}
    assert identity_markers(html, identity) == ["organisation_number", "phone", "address"]
    assert identity_markers("<p>Org.nr 888 567 231</p>", identity) == []


def test_domain_candidates():
    from norway_company_agent.pipeline import domain_candidates

    assert domain_candidates("SANDNES ELEKTRISKE AS")[:2] == ["sandneselektriske.no", "sandnes-elektriske.no"]
    assert "maalselvbygg.no" in domain_candidates("MÅLSELV BYGG AS") and "malselvbygg.no" in domain_candidates("MÅLSELV BYGG AS")
    assert domain_candidates("BØ AS") == []
    assert domain_candidates("AASEN & FARSTAD AS")[:2] == ["aasenfarstad.no", "aasen-farstad.no"]
    assert "pe-gaarud.no" in domain_candidates("P.E. GAARUD AS")
    assert "themiceguru.com" in domain_candidates("THE MICE GURU AS")
    assert "hammaren.no" in domain_candidates("STIFTELSEN HAMMAREN")
    assert "netsolution.no" in domain_candidates("NETSOLUTION VIKEN AS")
    assert domain_candidates("A B C D E AS") == ["abcde.no", "abcde.com"]


def test_group_site_profiles_require_norway_handle():
    p = profile()
    p["evidence"]["website"] = evidence("website", "available", "registry_linked_company_website", "https://bestseller.com/", value={
        "final_url": "https://bestseller.com/", "requested_url": "https://bestseller.com/", "content_sha256": "f" * 64, "identity_markers": {},
        "identity_assessment": {"publishable": True, "score": 0.95, "reasons": ["name"], "method": "m"},
        "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/bestseller"},
                         {"platform": "linkedin", "url": "https://linkedin.com/company/bestseller-norge"}]})
    envelope = build_envelope(p, run=RUN)
    profiles = [c["value"] for c in envelope["claims"] if c["family"] == "company_profiles"]
    assert profiles == ["https://linkedin.com/company/bestseller-norge"]
    scope = [c["value"] for c in envelope["claims"] if c["field"] == "website_scope"]
    assert scope == ["possibly_group_or_international"]


def test_website_facts_need_two_misses_and_string_job_claims_do_not_crash():
    p = profile()
    p["evidence"]["website"] = evidence("website", "available", "registry_linked_company_website", "https://aas.no/", value={
        "final_url": "https://aas.no/", "requested_url": "https://aas.no/", "content_sha256": "f" * 64, "identity_markers": {"https://aas.no/": ["organisation_number"]},
        "identity_assessment": {"publishable": True, "score": 1.0, "reasons": ["orgnr"], "method": "m"},
        "social_links": [{"platform": "facebook", "url": "https://facebook.com/aas"}], "careers_pages": ["https://aas.no/jobb"]})
    first = apply_refresh(None, build_envelope(p, run=RUN))
    gone = profile()
    gone["evidence"]["website"] = evidence("website", "not_found", "registry_linked_company_website", "https://x", note="registry lists no website")
    second = apply_refresh(copy.deepcopy(first), build_envelope(gone, run=RUN2))
    assert second["changes"] == []  # first miss: carried forward as stale
    assert any(c["field"] == "official_website" and c.get("stale") for c in second["claims"])
    third = apply_refresh(copy.deepcopy(second), build_envelope(gone, run=RUN2))
    kinds = sorted(c["change_type"] for c in third["changes"])
    assert "removed_website" in kinds and "removed_company_profile" in kinds and "closed_job" in kinds


def website_profile(final_url):
    p = profile()
    p["evidence"]["website"] = evidence("website", "available", "registry_linked_company_website", final_url, value={
        "final_url": final_url, "requested_url": "https://aas.no/", "content_sha256": "f" * 64, "identity_markers": {final_url: ["organisation_number"]},
        "identity_assessment": {"publishable": True, "score": 1.0, "reasons": ["orgnr"], "method": "m"}})
    return p


@pytest.mark.parametrize("final_url, published", [
    ("https://aas.no:443/bygg/", "https://aas.no/bygg/"),
    ("http://aas.no:80/", "http://aas.no/"),
    ("https://aas.no:8443/", "https://aas.no:8443/"),
    ("https://aas.no/", "https://aas.no/"),
])
def test_published_website_drops_default_port(final_url, published):
    envelope = build_envelope(website_profile(final_url), run=RUN)
    assert [c["value"] for c in envelope["claims"] if c["field"] == "official_website"] == [published]


def js_payload(path, prefix):
    text = path.read_text(encoding="utf-8")
    assert text.startswith(prefix) and text.endswith(";\n")
    return json.loads(text[len(prefix):-2])


def test_site_data_is_script_loadable_so_it_opens_without_a_server(tmp_path):
    from norway_company_agent.site import build_site

    envelope = apply_refresh(None, build_envelope(profile(), run=RUN))
    result = build_site([envelope], tmp_path / "site")
    assert result == {"site": str(tmp_path / "site"), "companies": 1}
    assert (tmp_path / "site" / "index.html").read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html>")
    rows = js_payload(tmp_path / "site" / "data" / "index.js", "window.SP_INDEX=")
    assert [(row["o"], row["n"], row["r"]) for row in rows] == [("888567232", "AAS ELEKTRONIKK AS", 1425713.0)]
    company = js_payload(tmp_path / "site" / "data" / "c" / "888567232.js", 'window.SP_COMPANY["888567232"]=')
    assert company["organisation_number"] == "888567232" and company["claims"]
    assert (tmp_path / "site" / "manifest.txt").read_text() == "888567232\n"


def test_run_outputs_include_explorer(tmp_path):
    from norway_company_agent.pipeline import write_outputs

    envelope = apply_refresh(None, build_envelope(profile(), run=RUN))
    (tmp_path / "out").mkdir()
    write_outputs(tmp_path / "out", tmp_path / "state", "r1", [envelope])
    assert (tmp_path / "out" / "site" / "index.html").is_file()
    assert (tmp_path / "out" / "site" / "data" / "c" / "888567232.js").is_file()


def test_explorer_failure_never_costs_the_envelopes(tmp_path):
    from norway_company_agent.pipeline import write_outputs

    out = tmp_path / "out"
    out.mkdir()
    (out / "site").write_text("in the way")  # site directory cannot be created
    envelope = apply_refresh(None, build_envelope(profile(), run=RUN))
    write_outputs(out, tmp_path / "state", "r1", [envelope])
    assert len((out / "envelopes.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (tmp_path / "state" / "latest" / "888567232.json").is_file()


def jsonld_page(kind, headline, url):
    node = {"@context": "https://schema.org", "@type": kind, "headline": headline, "datePublished": "2022-11-10T08:00:00+00:00", "url": url}
    return f'<html><head><script type="application/ld+json">{json.dumps(node)}</script></head><body></body></html>'


@pytest.mark.parametrize("headline, url, kept", [
    ("Ny rammeavtale med Bane NOR", "https://aas.no/nyheter/ny-rammeavtale/", True),
    ("Forside - Aas Elektronikk AS", "https://aas.no/", False),
    ("Kontakt - Aas Elektronikk AS", "https://aas.no/kontakt/", False),
    ("Om Oss - Aas Elektronikk AS", "https://aas.no/om-oss/", False),
    ("Karriere - Aas Elektronikk", "https://aas.no/om-aas/karriere/", False),
])
def test_static_pages_marked_as_articles_are_not_dated_news(headline, url, kept):
    from bs4 import BeautifulSoup
    from norway_company_agent.website import dated_items

    html = jsonld_page("Article", headline, url)
    titles = [item["title"] for item in dated_items(html, url, BeautifulSoup(html, "html.parser"))]
    assert titles == ([headline] if kept else [])


def chain_profile(final_url, markers=()):
    p = profile()
    p["evidence"]["website"] = evidence("website", "available", "registry_linked_company_website", final_url, value={
        "final_url": final_url, "requested_url": final_url, "content_sha256": "f" * 64, "identity_markers": {final_url: list(markers)},
        "identity_assessment": {"publishable": True, "score": 0.95, "reasons": ["name"], "method": "m"},
        "title": "AAS ELEKTRONIKK AS | Elkjeden - Ekte fagfolk", "description": "Elkjeden er en kjede av lokale elektrikere.",
        "social_links": [{"platform": "facebook", "url": "https://facebook.com/aaselektronikk"}], "careers_pages": ["https://www.elkjeden.no/jobb"],
        "news_items": [{"date": "2026-01-02", "title": "Elkjeden vokser", "url": "https://www.elkjeden.no/nyheter/vokser"}]})
    return p


def web_fields(envelope):
    return sorted(c["field"] for c in envelope["claims"] if c["family"] in {"official_website", "company_profiles", "jobs", "dated_activity", "public_brand"} and c["evidence_ids"] and c["field"] not in {"annual_accounts_filed"})


@pytest.mark.parametrize("url", [
    "https://www.elkjeden.no/finn-elektriker/agder/aas-elektronikk-as",
    "https://www.elektronikkjeden.no/finn-elektriker/agder/aas-elektronikk-as",  # chain name shares only the trade word
    "https://aas-elektronikk.kjedeportal.no/om",                                  # company subdomain on a platform
])
def test_member_page_on_a_chain_site_publishes_only_the_labelled_url(url):
    envelope = build_envelope(chain_profile(url), run=RUN)
    assert web_fields(envelope) == ["official_website", "website_scope"]
    assert [c["value"] for c in envelope["claims"] if c["field"] == "website_scope"] == ["page_on_third_party_site"]
    assert envelope["availability"]["official_website"] == "available"
    text = build_summary(apply_refresh(None, envelope))["text"]
    assert "describes it as" not in text and "verified website" not in text
    assert f"third-party site: {url}" in text


@pytest.mark.parametrize("final_url, markers", [
    ("https://www.aas.no/elektronikk/", ()),                       # own domain, deep path
    ("https://www.elektronikk-aas.no/om-oss/", ()),                # own domain, words in another order
    ("https://www.elkjeden.no/", ()),                              # brand domain root declared in the register
    ("https://www.elkjeden.no/agder/aas", ("organisation_number",)),  # org number on the page
])
def test_own_or_number_verified_sites_keep_their_content(final_url, markers):
    envelope = build_envelope(chain_profile(final_url, markers), run=RUN)
    assert web_fields(envelope) == ["careers_page", "facebook", "official_website", "self_description", "website_brand_title", "website_news", "website_scope"]


def test_site_index_row_carries_leaders_and_presence_counts_for_search_and_filters(tmp_path):
    from norway_company_agent.site import row

    p = chain_profile("https://www.aas.no/")
    p["evidence"]["roles"] = evidence("roles", "available", "official_roles", "https://data.brreg.no/enhetsregisteret/api/enheter/888567232/roller", value={
        "roles": [{"role": "Daglig leder", "role_code": "DAGL", "name": "Kari Nordmann"}, {"role": "Styrets leder", "role_code": "LEDE", "name": "Ola Hansen"},
                  {"role": "Styremedlem", "role_code": "MEDL", "name": "Per Olsen"}]}, content_sha256="b" * 64, retrieved_at="2026-09-27T00:00:10Z")
    index_row = row(apply_refresh(None, build_envelope(p, run=RUN)))
    assert index_row["l"] == ["Kari Nordmann", "Ola Hansen"]
    assert (index_row["x"], index_row["y"]) == (1, 1)  # one company profile, one website news item


# ---------- final pass: recall, contract and honest states

def test_profiles_declared_in_organisation_markup_and_publisher_meta_are_found():
    from norway_company_agent.website import page_social_links

    html = """<html><head>
      <meta property="article:publisher" content="https://www.facebook.com/AasElektronikk/" />
      <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization","name":"Aas Elektronikk AS",
        "sameAs":["https:\\/\\/www.facebook.com\\/AasElektronikk\\/","https:\\/\\/www.youtube.com\\/user\\/aaselektronikk"]}]}</script>
      </head><body><footer><a href="https://www.instagram.com/aaselektronikk/">Instagram</a>
      <a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a></footer></body></html>"""
    assert [(item["platform"], item["url"]) for item in page_social_links(html, "https://aas.no/")] == [
        ("facebook", "https://facebook.com/AasElektronikk"), ("instagram", "https://instagram.com/aaselektronikk"), ("youtube", "https://youtube.com/user/aaselektronikk")]


def gate_profile(name, *, title, text, markers, registry_listed=True, host="https://www.worksystem.no/"):
    return {"organisation_number": "913170296", "name": name, "evidence": {"website": {"status": "available", "source_url": host, "value": {
        "final_url": host, "title": title, "description": "", "main_text_excerpt": text, "registry_listed": registry_listed,
        "identity_markers": {host: list(markers)}, "pages": []}}}}


@pytest.mark.parametrize("name, title, text, markers, registry_listed, publishable", [
    # register-listed, country word missing from the page, registered phone on the site
    ("WORK SYSTEM NORWAY AS", "Hjemmeside", "Work System leverer innredning til varebiler og servicebiler over hele landet. " * 3, ["phone"], True, True),
    # one-word name in the title of a JavaScript-rendered homepage (almost no text), registered address on the site
    ("DIPS AS", "DIPS - Ledende leverandør av e-helse", "", ["address"], True, True),
    # same pages without the registered phone or address: still not proven
    ("WORK SYSTEM NORWAY AS", "Hjemmeside", "Work System leverer innredning til varebiler og servicebiler over hele landet. " * 3, [], True, False),
    # a sister company sharing the switchboard: the distinctive word of its own name is missing
    ("WORK SYSTEM EIENDOM AS", "Hjemmeside", "Work System leverer innredning til varebiler og servicebiler over hele landet. " * 3, ["phone", "address"], True, False),
    # an owner's site listed by a property company: none of its name is there
    ("DRAMMENSVEIEN 133 AS", "Klaveness Marine", "Klaveness Marine is a family-owned investment company. " * 3, ["phone", "address"], True, False),
    # a sister company whose own words all appear somewhere in the homepage text, sharing the switchboard
    ("HANSEN EIENDOM AS", "Hansen Bygg", "Hansen Bygg bygger bolig og eiendom i hele regionen. " * 3, ["phone"], True, False),
    # after dropping the country word only a generic trade word is left
    ("NORDIC BYGG AS", "Hansen Bygg", "Hansen Bygg bygger bolig og eiendom i hele regionen. " * 3, ["phone"], True, False),
    # not declared to the register: phone and partial name are not enough here (discovery has its own stricter rule)
    ("WORK SYSTEM NORWAY AS", "Hjemmeside", "Work System leverer innredning til varebiler og servicebiler over hele landet. " * 3, ["phone"], False, False),
])
def test_register_listed_site_is_proven_by_registered_contact_details_plus_name(name, title, text, markers, registry_listed, publishable):
    from norway_company_agent.identity import assess_website_identity

    host = {"DIPS AS": "https://www.dips.com/", "DRAMMENSVEIEN 133 AS": "https://www.klavenessmarine.com/", "HANSEN EIENDOM AS": "https://www.hansen-bygg.no/",
            "NORDIC BYGG AS": "https://www.hansen-bygg.no/"}.get(name, "https://www.worksystem.no/")
    assessment = assess_website_identity(gate_profile(name, title=title, text=text, markers=markers, registry_listed=registry_listed, host=host))
    assert assessment["publishable"] is publishable


def international_profile(legal_form="AS", home=(), contact=(), name="KITRON AS", url="https://www.kitron.com/"):
    p = profile()
    p["name"], p["legal_form"] = name, legal_form
    p["evidence"]["website"] = evidence("website", "available", "registry_linked_company_website", url, value={
        "final_url": url, "requested_url": url, "content_sha256": "f" * 64, "identity_markers": {url: list(home), url + "contact": list(contact)},
        "identity_assessment": {"publishable": True, "score": 0.95, "reasons": ["name"], "method": "m"},
        "social_links": [{"platform": "facebook", "url": "https://facebook.com/kitrongroup"}, {"platform": "linkedin", "url": "https://linkedin.com/company/kitron"}]})
    return p


@pytest.mark.parametrize("legal_form, home, contact, scope, profiles", [
    ("AS", (), (), "possibly_group_or_international", []),                                       # could be a foreign group's site
    ("AS", (), ("address", "phone"), "possibly_group_or_international", []),                     # a group's contact page lists every subsidiary's office
    ("AS", ("address",), (), "verified_by_registered_address_or_phone", ["facebook", "linkedin"]),  # registered address on the homepage itself
    ("AS", ("phone",), (), "verified_by_registered_address_or_phone", ["facebook", "linkedin"]),
    ("ASA", (), (), "public_company_own_site", ["facebook", "linkedin"]),                        # a Norwegian public company is the parent
])
def test_international_domain_is_only_a_group_site_without_norwegian_proof(legal_form, home, contact, scope, profiles):
    envelope = build_envelope(international_profile(legal_form, home, contact), run=RUN)
    assert [c["value"] for c in envelope["claims"] if c["field"] == "website_scope"] == [scope]
    assert sorted(c["field"] for c in envelope["claims"] if c["family"] == "company_profiles") == profiles


def test_norwegian_domain_scope_does_not_depend_on_which_pages_were_reachable():
    with_contact = build_envelope(international_profile("AS", (), ("address",), url="https://www.kitron.no/"), run=RUN)
    without = build_envelope(international_profile("AS", (), (), url="https://www.kitron.no/"), run=RUN)
    scope = lambda envelope: [c["value"] for c in envelope["claims"] if c["field"] == "website_scope"]  # noqa: E731
    assert scope(with_contact) == scope(without) == ["norwegian_domain"]


def test_envelope_follows_the_published_contract_names():
    envelope = build_envelope(website_profile("https://aas.no/"), run=RUN, operations={"requests": 7, "runtime_ms": 1200, "third_party_cost_usd": 0})
    evidence_ids = {item["id"] for item in envelope["evidence"]}
    assert evidence_ids and all(item["id"] == item["evidence_id"] for item in envelope["evidence"])
    assert all(set(claim["evidence_ids"]) <= evidence_ids for claim in envelope["claims"])
    assert all(isinstance(item.get("claim_span"), str) and item["claim_span"] for item in envelope["evidence"])
    assert all(claim["availability"] == "available" and isinstance(claim["confidence"], float) for claim in envelope["claims"])
    assert envelope["operations"]["requests"] == 7


@pytest.mark.parametrize("title, text, challenged", [
    ("Verifying...", "", True),
    ("Just a moment...", "Checking your browser before accessing the site.", True),
    ("Attention Required! | Cloudflare", "", True),
    ("Aas Elektronikk AS", "", False),
    ("Verifying deliveries for our customers", "Vi leverer elektronikk til industrien i hele Norge. " * 4, False),
])
def test_bot_challenge_pages_are_recognised(title, text, challenged):
    from norway_company_agent.website import is_bot_challenge

    assert is_bot_challenge(title, text) is challenged


def test_feed_link_is_taken_from_the_page_head_on_the_same_domain_only():
    from norway_company_agent.website import declared_feed_url

    head = lambda links: f"<html><head>{links}</head><body></body></html>"  # noqa: E731
    own = '<link rel="alternate" type="application/rss+xml" title="Aas &raquo; Feed" href="https://aas.no/feed/" />'
    comments = '<link rel="alternate" type="application/rss+xml" title="Aas &raquo; Comments Feed" href="https://aas.no/comments/feed/" />'
    foreign = '<link rel="alternate" type="application/atom+xml" href="https://feeds.example.com/aas" />'
    assert declared_feed_url(head(comments + own), "https://aas.no/") == "https://aas.no/feed/"
    assert declared_feed_url(head('<link rel="alternate" type="application/rss+xml" href="/nyheter/rss" />'), "https://www.aas.no/om/") == "https://www.aas.no/nyheter/rss"
    assert declared_feed_url(head(foreign + comments), "https://aas.no/") is None


def test_feed_items_are_dated_news_with_exact_dates():
    from norway_company_agent.website import feed_items

    rss = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Aas</title>
      <item><title>Ny rammeavtale med Bane NOR</title><link>https://aas.no/nyheter/ny-rammeavtale/</link><pubDate>Tue, 15 Sep 2026 08:30:00 +0000</pubDate></item>
      <item><title>Uten dato</title><link>https://aas.no/nyheter/uten-dato/</link></item>
      <item><title>Fra et annet nettsted</title><link>https://other.example.com/post</link><pubDate>Mon, 14 Sep 2026 08:30:00 +0000</pubDate></item>
    </channel></rss>"""
    atom = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Aas</title>
      <entry><title>Vi flytter til nye lokaler</title><link rel="alternate" href="https://aas.no/aktuelt/flytter/"/><published>2026-06-01T10:00:00Z</published></entry></feed>"""
    assert feed_items(rss, "https://aas.no/feed/") == [
        {"date": "2026-09-15", "title": "Ny rammeavtale med Bane NOR", "url": "https://aas.no/nyheter/ny-rammeavtale/", "locator": "rss:item/pubDate"}]
    assert feed_items(atom, "https://aas.no/feed.atom") == [
        {"date": "2026-06-01", "title": "Vi flytter til nye lokaler", "url": "https://aas.no/aktuelt/flytter/", "locator": "atom:entry/published"}]
    assert feed_items("<html>not a feed</html>", "https://aas.no/feed/") == []


# ---------- final pass: evidence spans, snapshots and rights

RAW_ENTITY = """{
  "organisasjonsnummer" : "888567232",
  "navn" : "AAS ELEKTRONIKK AS",
  "organisasjonsform" : {
    "kode" : "AS",
    "beskrivelse" : "Aksjeselskap"
  },
  "forretningsadresse" : {
    "land" : "Norge",
    "postnummer" : "4823",
    "poststed" : "NEDENES",
    "adresse" : [ "Natvigveien 17" ],
    "kommune" : "ARENDAL"
  },
  "sisteInnsendteAarsregnskap" : "2025"
}"""
RAW_ACCOUNTS = """[ {
  "id" : 1,
  "regnskapsperiode" : { "fraDato" : "2025-01-01", "tilDato" : "2025-12-31" },
  "virksomhet" : { "organisasjonsnummer" : "888567232" },
  "resultatregnskapResultat" : { "driftsresultat" : { "driftsinntekter" : { "sumDriftsinntekter" : 1425713.00 } } },
  "eiendeler" : { "sumEiendeler" : 0.00 }
} ]"""


def spans(envelope):
    return {(c["family"], c["field"]): c["claim_span"] for c in envelope["claims"]}


def test_registry_claims_quote_the_source_text_as_received():
    p = profile()
    p["evidence"]["registry_live"]["raw_text"] = RAW_ENTITY
    p["evidence"]["financials"]["raw_text"] = RAW_ACCOUNTS
    quoted = spans(build_envelope(p, run=RUN))
    assert quoted[("legal_identity", "legal_name")] == '"navn" : "AAS ELEKTRONIKK AS"'
    assert quoted[("legal_identity", "legal_form")] == '"kode" : "AS"'
    assert quoted[("legal_identity", "municipality")] == '"kommune" : "ARENDAL"'
    assert quoted[("legal_identity", "business_address")] == '"forretningsadresse" : { "land" : "Norge", "postnummer" : "4823", "poststed" : "NEDENES", "adresse" : [ "Natvigveien 17" ], "kommune" : "ARENDAL" }'
    assert quoted[("annual_accounts", "revenue")] == '"sumDriftsinntekter" : 1425713.00 (regnskapsperiode 2025-01-01..2025-12-31)'
    assert quoted[("annual_accounts", "assets")] == '"sumEiendeler" : 0.00 (regnskapsperiode 2025-01-01..2025-12-31)'
    assert quoted[("filing_history", "latest_submitted_accounts_year")] == '"sisteInnsendteAarsregnskap" : "2025"'


def test_every_claim_and_every_evidence_record_carries_a_span_even_without_raw_text():
    envelope = build_envelope(chain_profile("https://www.aas.no/"), run=RUN)
    assert [c["field"] for c in envelope["claims"] if not c["claim_span"]] == []
    assert [e["source_class"] for e in envelope["evidence"] if not e["claim_span"]] == []
    assert spans(envelope)[("legal_identity", "legal_name")] == '"navn": "AAS ELEKTRONIKK AS"'


def test_evidence_points_to_the_stored_snapshot_and_states_the_source_rights(tmp_path):
    from norway_company_agent.rawstore import save_raw, set_raw_store

    set_raw_store(tmp_path)
    try:
        save_raw("a" * 64, RAW_ENTITY.encode())
        envelope = build_envelope(profile(), run=RUN)
    finally:
        set_raw_store(None)
    by_class = {item["source_class"]: item for item in envelope["evidence"]}
    assert by_class["official_registry_live"]["snapshot_path"] == "raw/aa/" + "a" * 64 + ".gz"
    assert __import__("gzip").decompress((tmp_path / by_class["official_registry_live"]["snapshot_path"]).read_bytes()).decode() == RAW_ENTITY
    assert by_class["official_annual_accounts"]["snapshot_path"] is None  # nothing was stored for that hash
    assert "NLOD" in by_class["official_registry_live"]["rights"]


# ---------- final pass: per-company request counts, search discovery, summary

def test_requests_are_attributed_to_the_company_being_researched():
    from norway_company_agent.budget import active_budget
    from norway_company_agent.pipeline import _guarded

    set_active_budget(RequestBudget(max_requests=100))
    try:
        def step(company):
            active_budget().take("https://aas.no/")
            active_budget().take("https://aas.no/kontakt")

        first, second = {"errors": []}, {"errors": []}
        _guarded(step, first)
        _guarded(step, first)
        _guarded(step, second)
    finally:
        set_active_budget(None)
    assert (first["requests"], second["requests"]) == (4, 2)


def site_record(url, *, title, markers, text="Vi leverer elektronikk til industrien i hele Norge. " * 4):
    return evidence("website", "available", "registry_linked_company_website", url, value={
        "final_url": url, "requested_url": url, "title": title, "description": "", "main_text_excerpt": text, "content_sha256": "e" * 64,
        "identity_markers": {url: list(markers)}, "identity_snippets": {url: {m: f"proof of {m}" for m in markers}}, "pages": [], "social_links": []}, content_sha256="e" * 64), {"requests": 2}


SEARCH_RESULTS = [
    {"url": "https://www.proff.no/selskap/aas-elektronikk-as/888567232", "title": "Aas Elektronikk AS - Proff", "snippet": "Org.nr 888 567 232", "rank": 1, "provider": "brave_search_api", "query": "q"},
    # a directory that is not on the block list and prints the org number: must never become the "official website"
    {"url": "https://www.regnskapstall.no/informasjon-om-aas-elektronikk-as-888567232S0", "title": "Aas Elektronikk AS - Regnskapstall", "snippet": "Org.nr 888567232", "rank": 2, "provider": "brave_search_api", "query": "q"},
    {"url": "https://www.aas-elektronikk-agder.no/kontakt", "title": "Kontakt | Aas Elektronikk AS", "snippet": "Aas Elektronikk AS, Natvigveien 17, Nedenes", "rank": 3, "provider": "brave_search_api", "query": "q"},
]


def search_profile():
    p = profile()
    p["legal_form"] = "AS"
    return p


def test_search_discovery_is_off_without_a_key(monkeypatch):
    from norway_company_agent import pipeline

    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("SIGNALPOST_BRAVE_API_KEY", raising=False)
    calls = []
    assert pipeline.discover_by_search(search_profile(), search=lambda query, key: calls.append(query) or SEARCH_RESULTS) is None
    assert calls == []


def test_search_candidate_is_published_only_after_the_fetched_site_proves_the_entity(monkeypatch):
    from norway_company_agent import pipeline

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    pipeline.reset_search_quota(10)
    fetched = []

    def fake_fetch(url, **kwargs):
        fetched.append(url)
        return site_record(url, title="Elektro Sør", markers=["organisation_number"])

    monkeypatch.setattr(pipeline, "fetch_website", fake_fetch)
    monkeypatch.setattr(pipeline, "resolve_many", lambda hosts, timeout=4.0: set(hosts))
    p = search_profile()
    result = pipeline.discover_by_search(p, search=lambda query, key: SEARCH_RESULTS)
    assert result["query"] == '"AAS ELEKTRONIKK AS" 888567232'
    assert set(fetched) == {"https://www.aas-elektronikk-agder.no/"}  # only the domain named after the company, at its root; directories are never crawled
    envelope = build_envelope(p, run=RUN)
    assert [c["value"] for c in envelope["claims"] if c["field"] == "official_website"] == ["https://www.aas-elektronikk-agder.no/"]
    assert {e["source_class"] for e in envelope["evidence"] if "agder" in e["source_url"]} == {"search_discovered_website"}
    assert p["search_queries"] == 1


def test_search_candidate_without_registry_proof_on_the_site_is_dropped(monkeypatch):
    from norway_company_agent import pipeline

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    pipeline.reset_search_quota(10)
    monkeypatch.setattr(pipeline, "fetch_website", lambda url, **kwargs: site_record(url, title="Aas Elektronikk AS", markers=[]))
    monkeypatch.setattr(pipeline, "resolve_many", lambda hosts, timeout=4.0: set(hosts))
    p = search_profile()
    pipeline.discover_by_search(p, search=lambda query, key: SEARCH_RESULTS)
    assert build_envelope(p, run=RUN)["availability"]["official_website"] == "not_available"


def test_search_quota_caps_paid_queries(monkeypatch):
    from norway_company_agent import pipeline

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    pipeline.reset_search_quota(1)
    monkeypatch.setattr(pipeline, "fetch_website", lambda url, **kwargs: site_record(url, title="x", markers=[]))
    monkeypatch.setattr(pipeline, "resolve_many", lambda hosts, timeout=4.0: set(hosts))
    calls = []
    search = lambda query, key: calls.append(query) or SEARCH_RESULTS  # noqa: E731
    pipeline.discover_by_search(search_profile(), search=search)
    assert pipeline.discover_by_search(search_profile(), search=search) == {"skipped": "search query quota used"}
    assert len(calls) == 1


def accounts_profile(records):
    p = profile()
    p["evidence"]["registry_live"]["value"].update({"bankrupt": False, "liquidating": False, "forced_dissolution": False})
    p["evidence"]["financials"]["value"]["records"] = [
        {"period": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"}, "currency": "NOK", "account_type": "SELSKAP", **figures} for year, figures in records]
    return p


def summary_of(p, run=RUN):
    envelope = apply_refresh(None, build_envelope(p, run=run))
    return build_summary(envelope), envelope


def test_summary_explains_the_trend_between_the_two_latest_filed_years():
    summary, envelope = summary_of(accounts_profile([(2025, {"revenue": 1425713.0, "annual_result": -50000.0, "assets": 2000000.0, "equity": 500000.0}),
                                                     (2024, {"revenue": 1000000.0, "annual_result": 120000.0, "assets": 1800000.0, "equity": 550000.0})]))
    text = summary["text"]
    assert "Revenue rose 42.6% from NOK 1.0 m in 2024 to NOK 1.4 m in 2025." in text
    assert "The net result fell from NOK 120 k to a loss of NOK 50 k." in text
    assert "Equity was NOK 500 k at the end of 2025, 25% of total assets." in text
    by_id = {c["claim_id"]: c for c in envelope["claims"]}
    trend = next(s for s in summary["sentences"] if s["text"].startswith("Revenue rose"))
    assert sorted(by_id[i]["reporting_period"][:4] for i in trend["claim_ids"]) == ["2024", "2025"]


def test_summary_flags_negative_equity_and_states_clean_register_status():
    summary, _ = summary_of(accounts_profile([(2025, {"revenue": 900000.0, "annual_result": -400000.0, "assets": 300000.0, "equity": -150000.0})]))
    assert "Equity was negative (NOK -150 k) at the end of 2025." in summary["text"]
    assert "The register shows no bankruptcy, liquidation or forced dissolution." in summary["text"]


def test_summary_is_grouped_into_sections_and_says_why_things_are_unknown():
    summary, envelope = summary_of(accounts_profile([(2025, {"revenue": 900000.0, "annual_result": 10000.0, "assets": 300000.0, "equity": 100000.0})]))
    ids = {c["claim_id"] for c in envelope["claims"]}
    assert [section["key"] for section in summary["sections"]] == ["identity", "finances", "people"]
    assert all(s["section"] in {"identity", "business", "finances", "people", "presence"} and set(s["claim_ids"]) <= ids for s in summary["sentences"])
    assert "official website (the register lists no website and none was verified)" in summary["unknowns_text"]
    assert "job postings (no active NAV job ads with this organisation number in the checked window)" in summary["unknowns_text"]


def test_evidence_carried_from_an_older_snapshot_still_has_the_contract_id():
    older = apply_refresh(None, build_envelope(profile(), run=RUN))
    for item in older["evidence"]:
        item.pop("id")  # snapshots written before the contract name was added
    broken = profile()
    broken["evidence"]["financials"] = evidence("financials", "source_error", "official_annual_accounts", "https://x", note="HTTP 503")
    refreshed = apply_refresh(older, build_envelope(broken, run=RUN2))
    assert any(item.get("from_previous_run") for item in refreshed["evidence"])
    assert all(item["id"] == item["evidence_id"] for item in refreshed["evidence"])


def test_feed_on_a_private_host_is_never_fetched(monkeypatch):
    from norway_company_agent import website

    opened = []
    monkeypatch.setattr(website, "_open", lambda request, **kwargs: opened.append(request.full_url))
    monkeypatch.setattr(website, "_robots_allowed", lambda url, timeout: True)
    assert website._fetch_feed("http://127.0.0.1/feed/", timeout=1.0) == ([], 0)
    assert opened == []


def test_trend_never_compares_group_accounts_with_company_accounts():
    p = accounts_profile([(2025, {"revenue": 5000000.0, "annual_result": 100000.0}), (2024, {"revenue": 1000000.0, "annual_result": 50000.0})])
    p["evidence"]["financials"]["value"]["records"][0]["account_type"] = "KONSERN"  # latest year is the group; the year before is the company alone
    summary, _ = summary_of(p)
    assert "Revenue rose" not in summary["text"] and "The net result rose" not in summary["text"]
    assert "Filed accounts for the period ending 2025-12-31 show revenue of NOK 5.0 m" in summary["text"]


@pytest.mark.parametrize("name, host, title, publishable", [
    ("SAMEIET ST OLAV", "https://st-olav.no/", "Velkommen", True),             # the declared domain spells the name once the entity-type word is set aside
    ("Forsheimer Borettslag", "https://www.usbl.no/", "Usbl - boligbyggelag", False),  # the manager's site, not the housing cooperative's
])
def test_entity_type_words_are_not_part_of_the_distinguishing_name(name, host, title, publishable):
    from norway_company_agent.identity import assess_website_identity

    assessment = assess_website_identity(gate_profile(name, title=title, text="Informasjon til beboere og eiere. " * 5, markers=[], host=host))
    assert assessment["publishable"] is publishable


# ---------- fixes from review

def test_feed_in_a_legacy_encoding_keeps_norwegian_letters():
    from norway_company_agent.website import feed_items

    raw = ('<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel><item><title>Nytt kontor i Bodø åpnet</title>'
           '<link>https://aas.no/nyheter/bodo/</link><pubDate>Tue, 15 Sep 2026 08:30:00 +0000</pubDate></item></channel></rss>').encode("iso-8859-1")
    assert [item["title"] for item in feed_items(raw, "https://aas.no/feed/")] == ["Nytt kontor i Bodø åpnet"]


def test_address_snippet_is_only_recorded_when_the_address_marker_holds():
    from norway_company_agent.website import identity_markers, marker_snippets

    identity = {"organisation_number": "888567232", "phones": [], "postal_code": "0155", "street": "Storgata"}
    html = "<p>Besøk oss i Storgata 12, 5003 Bergen</p>"  # same street name, another town
    assert identity_markers(html, identity) == [] and marker_snippets(html, identity) == {}
    right = "<p>Besøk oss i Storgata 12, 0155 Oslo</p>"
    assert identity_markers(right, identity) == ["address"] and "Storgata 12, 0155 Oslo" in marker_snippets(right, identity)["address"]


def test_same_as_is_read_from_the_organisation_only_not_from_authors():
    from norway_company_agent.website import page_social_links

    html = """<html><head><script type="application/ld+json">{"@context":"https://schema.org","@graph":[
      {"@type":"Organization","name":"Acme AS","sameAs":["https://www.linkedin.com/company/acme-as"]},
      {"@type":"Article","headline":"x","author":{"@type":"Person","name":"Jo","sameAs":["https://x.com/acmejo"]}}]}</script></head><body></body></html>"""
    assert [item["url"] for item in page_social_links(html, "https://acme.no/")] == ["https://linkedin.com/company/acme-as"]


def test_role_quote_names_the_right_person():
    from norway_company_agent.envelope import _role_quote

    raw = '{"rollegrupper":[{"roller":[{"person":{"navn":{"fornavn":"Ola","etternavn":"Berg"}}},{"person":{"navn":{"fornavn":"Kari","etternavn":"Lindberg"}}},{"person":{"navn":{"fornavn":"X","etternavn":""}}}]}]}'
    assert _role_quote(raw, {"name": "Kari Lindberg", "role": "Styremedlem", "role_code": "MEDL"}).startswith('"etternavn":"Lindberg"')
    assert _role_quote(raw, {"name": "Ola Berg", "role": "Styrets leder", "role_code": "LEDE"}).startswith('"etternavn":"Berg"')


def test_span_kind_separates_text_quoted_from_the_source_from_rendered_values():
    p = profile()
    p["evidence"]["registry_live"]["raw_text"] = RAW_ENTITY
    kinds = {(c["family"], c["field"]): c["span_kind"] for c in build_envelope(p, run=RUN)["claims"]}
    assert kinds[("legal_identity", "legal_name")] == "source_text"           # matched in the stored response
    assert kinds[("annual_accounts", "revenue")] == "rendered_value"          # no raw text for the accounts record here
    from norway_company_agent.pipeline import evidence_metrics

    totals = evidence_metrics([build_envelope(p, run=RUN)])
    assert 0 < totals["with_quoted_span"] < totals["claims"] == totals["with_any_span"]


def test_compact_json_does_not_hide_a_nested_key_of_the_same_name():
    from norway_company_agent.envelope import quote_json

    raw = '[{"resultatregnskapResultat":{"driftsresultat":{"driftsinntekter":{"sumDriftsinntekter":25468413.00},"driftsresultat":4150764.00},"aarsresultat":8108431.00}}]'
    assert quote_json(raw, "driftsresultat", 4150764.0) == '"driftsresultat":4150764.00'
    assert quote_json(raw, "driftsresultat", 4150764.0).exact is True and quote_json(None, "driftsresultat", 4150764.0).exact is False


def news_profile():
    p = website_profile("https://aas.no/")
    value = p["evidence"]["website"]["value"]
    value["feed"] = {"url": "https://aas.no/feed/", "content_sha256": "9" * 64, "retrieved_at": "2026-09-27T00:00:20Z"}
    value["news_items"] = [
        {"date": "2026-09-15", "title": "Ny avtale signert", "url": "https://aas.no/nyheter/ny-avtale/", "locator": "rss:item/pubDate", "found_in": {"url": "https://aas.no/feed/", "content_sha256": "9" * 64}},
        {"date": "2026-08-01", "title": "Sommerstengt", "url": "https://aas.no/nyheter/sommer/", "locator": "time[datetime]", "found_in": {"url": "https://aas.no/nyheter/", "content_sha256": "8" * 64}},
    ]
    return p


def test_news_cites_the_feed_or_page_it_was_read_from():
    envelope = build_envelope(news_profile(), run=RUN)
    records = {item["id"]: item for item in envelope["evidence"]}
    cited = {c["value"]["title"]: records[c["evidence_ids"][0]] for c in envelope["claims"] if c["field"] == "website_news"}
    assert (cited["Ny avtale signert"]["source_url"], cited["Ny avtale signert"]["content_sha256"]) == ("https://aas.no/feed/", "9" * 64)
    assert (cited["Sommerstengt"]["source_url"], cited["Sommerstengt"]["content_sha256"]) == ("https://aas.no/nyheter/", "8" * 64)


def test_broken_feed_link_never_costs_the_website(monkeypatch):
    from norway_company_agent import website

    html = b'<html><head><title>Aas Elektronikk AS</title><link rel="alternate" type="application/rss+xml" href="http://[bad/feed"></head><body><p>' + b"Vi leverer elektronikk. " * 20 + b"</p></body></html>"

    class Response:
        headers = {"content-type": "text/html; charset=utf-8"}
        status = 200

        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=None): return html
        def geturl(self): return "https://aas.no/"

    monkeypatch.setattr(website, "assert_public_url", lambda url: None)
    monkeypatch.setattr(website, "_robots_allowed", lambda url, timeout: True)
    monkeypatch.setattr(website, "_open", lambda request, **kwargs: Response())
    record, _ = website.fetch_website("https://aas.no/", max_pages=1)
    assert record["status"] == "available" and record["value"]["news_items"] == []


def test_malformed_limits_in_the_environment_fall_back_to_defaults(monkeypatch):
    from norway_company_agent.pipeline import env_number

    monkeypatch.setenv("SIGNALPOST_SEARCH_MAX_QUERIES", "lots")
    monkeypatch.setenv("SIGNALPOST_SEARCH_COST_PER_QUERY", "0.01")
    assert env_number("SIGNALPOST_SEARCH_MAX_QUERIES", 100) == 100
    assert env_number("SIGNALPOST_SEARCH_COST_PER_QUERY", 0.005) == 0.01


def test_a_query_that_cannot_be_built_does_not_spend_quota(monkeypatch):
    from norway_company_agent import pipeline

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    pipeline.reset_search_quota(5)
    nameless = search_profile()
    nameless["name"] = ""
    result = pipeline.discover_by_search(nameless, search=lambda query, key: SEARCH_RESULTS)
    assert "error" in result and pipeline._search_quota.used == 0 and not nameless.get("search_queries")
