"""Fixture-based unit tests for Wave 2 scrapers: the:protocol and Pracuj.pl (dział IT)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from job_scout.config import ProfileConfig
from job_scout.filter import Stage1Filter
from job_scout.models import normalize_company, normalize_title
from job_scout.scrapers.pracuj import PracujITScraper, extract_next_data as pracuj_extract_next_data
from job_scout.scrapers.registry import list_sources, resolve_source_name
from job_scout.scrapers.theprotocol import (
    TheProtocolScraper,
    extract_next_data as protocol_extract_next_data,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _sample_profile() -> ProfileConfig:
    return ProfileConfig(
        min_match_score=7,
        must_have_any_tech=["Python", "Go", "FastAPI", "RAG", "LLM"],
        exclude_keywords=["PHP", "5+ years"],
        allowed_workplace_types=["remote", "hybrid"],
        allowed_cities=["Warszawa", "Warsaw"],
        min_salary_pln=9000,
        preferences={"target_levels": ["junior", "mid", "regular"]},
    )


async def test_theprotocol_parser_and_fetch_from_fixtures() -> None:
    list_html = (FIXTURES_DIR / "theprotocol_list.html").read_text(encoding="utf-8")
    detail_html = (FIXTURES_DIR / "theprotocol_detail.html").read_text(encoding="utf-8")

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        requested_urls.append(url_str)
        if "/filtry/" in url_str:
            return httpx.Response(200, text=list_html)
        if "11111111-1111-1111-1111-111111111111" in url_str:
            return httpx.Response(200, text=detail_html)
        if "22222222-2222-2222-2222-222222222222" in url_str:
            return httpx.Response(404)
        return httpx.Response(404)

    profile = _sample_profile()
    stage1 = Stage1Filter(profile)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scraper = TheProtocolScraper(client=client, request_delay=0.0)
        offers = await scraper.fetch_offers(profile=profile, stage1_filter=stage1)

    # Queries both Warszawa city filter and Polish-wide remote ('zdalna;rw') filter
    assert any("warszawa;wp" in u and "junior,mid;p" in u for u in requested_urls)
    assert any("zdalna;rw" in u for u in requested_urls)

    # Deduplicated across both queries -> 3 unique offers
    assert scraper.last_total_fetched == 3
    assert len(offers) == 3

    # Krakow onsite PHP offer rejected before detail fetch
    assert not any("33333333" in u for u in requested_urls)
    php_offer = next(o for o in offers if o.external_id.startswith("33333333"))
    assert php_offer.metadata_rejected_reason is not None

    # Enriched Python/AI offer combines hourly B2B (100-130 * 168 = 16800-21840) + monthly UoP (14000-18000)
    py_offer = next(o for o in offers if o.external_id.startswith("11111111"))
    assert py_offer.source == "theprotocol"
    assert py_offer.company == "SampleTech Sp. z o.o."
    assert py_offer.workplace_type == "hybrid"
    assert py_offer.locations == ["Warszawa"]
    assert py_offer.experience_level == "Mid"
    assert py_offer.salary_min_pln == 14000
    assert py_offer.salary_max_pln == 21840
    skill_levels = {s.name: s.level for s in py_offer.skills}
    assert skill_levels["Python"] == "must"
    assert skill_levels["LangGraph"] == "nice"
    assert "function calling" in py_offer.description
    assert py_offer.published_at == "2026-11-24T12:28:54.03Z"
    assert normalize_title(py_offer.title) == "python ai backend developer"

    # Remote Go offer falls back gracefully to list payload on 404 detail
    go_offer = next(o for o in offers if o.external_id.startswith("22222222"))
    assert go_offer.workplace_type == "remote"
    assert go_offer.locations == ["Kraków"]
    assert go_offer.experience_level == "Junior/Mid"
    assert go_offer.salary_min_pln == 13000
    assert go_offer.salary_max_pln == 17000


async def test_pracuj_it_parser_and_fetch_from_fixtures() -> None:
    list_html = (FIXTURES_DIR / "pracuj_list.html").read_text(encoding="utf-8")
    detail_html = (FIXTURES_DIR / "pracuj_detail.html").read_text(encoding="utf-8")

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        requested_urls.append(url_str)
        if "it.pracuj.pl/praca" in url_str:
            return httpx.Response(200, text=list_html)
        if "oferta,1005000001" in url_str:
            return httpx.Response(200, text=detail_html)
        if "oferta,1005000002" in url_str:
            return httpx.Response(404)
        return httpx.Response(404)

    profile = _sample_profile()
    stage1 = Stage1Filter(profile)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scraper = PracujITScraper(client=client, request_delay=0.0)
        offers = await scraper.fetch_offers(profile=profile, stage1_filter=stage1)

    # Tight URL scoping: IT specialization (its=backend,ai-ml,big-data-science), Junior/Mid (et=17,4), Warszawa + remote
    assert any("warszawa;wp" in u and "et=17%2C4" in u and "its=backend" in u for u in requested_urls)
    assert any("wm=home-office" in u and "et=17%2C4" in u for u in requested_urls)

    assert scraper.last_total_fetched == 3
    assert len(offers) == 3

    # Gdansk onsite PHP offer rejected before detail fetch
    assert not any("1005000003" in u for u in requested_urls)
    php_offer = next(o for o in offers if o.external_id == "1005000003")
    assert php_offer.metadata_rejected_reason is not None

    # Enriched AI Developer hybrid Warsaw offer
    ai_offer = next(o for o in offers if o.external_id == "1005000001")
    assert ai_offer.source == "pracuj.pl"
    assert ai_offer.workplace_type == "hybrid"
    assert ai_offer.locations == ["Warszawa"]
    assert ai_offer.experience_level == "Mid"
    # 110-140 PLN/h * 168 = 18480-23520
    assert ai_offer.salary_min_pln == 18480
    assert ai_offer.salary_max_pln == 23520
    skill_levels = {s.name: s.level for s in ai_offer.skills}
    assert skill_levels["Python"] == "must"
    assert skill_levels["LangChain"] == "nice"
    assert "ewaluacja pipeline'ów RAG" in ai_offer.description
    assert normalize_company(ai_offer.company) == "sample polska"
    assert normalize_title(ai_offer.title) == "ai developer ai developerka"

    # Remote Junior Data Engineer falls back to parsed list salaryDisplayText ('10 500–14 000 zł brutto / mies.')
    data_offer = next(o for o in offers if o.external_id == "1005000002")
    assert data_offer.workplace_type == "remote"
    assert data_offer.locations == ["Kraków"]
    assert data_offer.experience_level == "Junior"
    assert data_offer.salary_min_pln == 10500
    assert data_offer.salary_max_pln == 14000


def test_registry_has_all_six_sources_and_waf_detection() -> None:
    assert list_sources() == [
        "solid.jobs",
        "justjoin.it",
        "nofluffjobs",
        "bulldogjob",
        "theprotocol",
        "pracuj.pl",
    ]
    assert resolve_source_name("nfj") == "nofluffjobs"
    assert resolve_source_name("the:protocol") == "theprotocol"
    assert resolve_source_name("pracuj") == "pracuj.pl"
    assert resolve_source_name("bulldog") == "bulldogjob"

    with pytest.raises(RuntimeError, match="Cloudflare"):
        pracuj_extract_next_data("<html><title>Just a moment...</title></html>")
    with pytest.raises(RuntimeError, match="Cloudflare"):
        protocol_extract_next_data("<html><div id='cf-browser-verification'></div></html>")
