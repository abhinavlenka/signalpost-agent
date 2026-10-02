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
