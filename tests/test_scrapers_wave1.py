"""Fixture-based unit tests for Wave 1 scrapers: No Fluff Jobs and Bulldogjob."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from job_scout.config import ProfileConfig
from job_scout.filter import Stage1Filter
from job_scout.scrapers.bulldogjob import BulldogjobScraper
from job_scout.scrapers.nofluffjobs import NoFluffJobsScraper

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


async def test_nofluffjobs_parser_and_fetch_from_fixtures() -> None:
    search_payload = json.loads(
        (FIXTURES_DIR / "nofluffjobs_search.json").read_text(encoding="utf-8")
    )
    detail_payload = json.loads(
        (FIXTURES_DIR / "nofluffjobs_detail.json").read_text(encoding="utf-8")
    )

    detail_calls: list[str] = []
    search_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/search/posting" in url_str:
            search_bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json=search_payload)
        if "/api/posting/python-ai-backend-developer-example-corp-warszawa" in url_str:
            detail_calls.append(url_str)
            return httpx.Response(200, json=detail_payload)
        if "/api/posting/remote-ml-engineer-sample-ai-remote" in url_str:
            detail_calls.append(url_str)
            return httpx.Response(
                200,
                json={
                    "id": "remote-ml-engineer-sample-ai-remote",
                    "postingUrl": "remote-ml-engineer-sample-ai-remote",
                    "title": "Junior/Mid AI Engineer",
                    "company": {"name": "Sample AI Labs"},
                    "basics": {"seniority": ["Junior", "Mid"], "technology": "Python"},
                    "requirements": {
                        "musts": [{"value": "Python"}, {"value": "LLM"}],
                        "nices": [{"value": "Docker"}],
                    },
                    "details": {"description": "<p>Building production AI agents.</p>"},
                    "location": {"places": [{"city": "Remote"}], "fullyRemote": True},
                },
            )
        return httpx.Response(404)

    profile = _sample_profile()
    stage1 = Stage1Filter(profile)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scraper = NoFluffJobsScraper(client=client, request_delay=0.0)
        offers = await scraper.fetch_offers(profile=profile, stage1_filter=stage1)

    # Search payload properly filtered by profile (Warszawa/remote, categories, trainee/junior/mid)
    assert len(search_bodies) == 1
    criteria = search_bodies[0]["criteriaSearch"]
    assert "warszawa" in criteria["city"] and "remote" in criteria["city"]
    assert "backend" in criteria["category"] and "artificial-intelligence" in criteria["category"]
    assert "junior" in criteria["seniority"] and "mid" in criteria["seniority"]

    # Duplicate reference REF-PY-001 (Warszawa + Remote clones) collapsed to 1 -> 3 unique offers
    assert scraper.last_total_fetched == 3
    assert len(offers) == 3

    # PHP Krakow onsite offer rejected at lightweight pre-filter (never fetched detail)
    assert all("php" not in u for u in detail_calls)
    php_offer = next(o for o in offers if o.external_id == "REF-PHP-003")
    assert php_offer.metadata_rejected_reason is not None

    # Enriched Python/AI hybrid offer preserves hybrid mode, filters provinceOnly, maps B2B hourly + UoP monthly
    py_offer = next(o for o in offers if o.external_id == "REF-PY-001")
    assert py_offer.source == "nofluffjobs"
    assert py_offer.company == "Example Corp Sp. z o.o."
    assert py_offer.workplace_type == "hybrid"
    assert py_offer.locations == ["Warszawa"]
    assert py_offer.experience_level == "Mid"
    # B2B: 70*168=11760 .. 90*168=15120, UoP: 12500..16000 -> min 11760, max 16000
    assert py_offer.salary_min_pln == 11760
    assert py_offer.salary_max_pln == 16000
    skill_levels = {s.name: s.level for s in py_offer.skills}
    assert skill_levels["Python"] == "must"
    assert skill_levels["FastAPI"] == "must"
    assert skill_levels["LangGraph"] == "nice"
    assert "pipeline'ów RAG" in py_offer.description
    assert "B2B:" in py_offer.description and "UoP:" in py_offer.description
    assert py_offer.published_at is not None and py_offer.published_at.endswith("Z")

    # Remote AI offer
    ai_offer = next(o for o in offers if o.external_id == "REF-AI-002")
    assert ai_offer.workplace_type == "remote"
    assert ai_offer.locations == []
    assert ai_offer.salary_min_pln == 14000
    assert ai_offer.salary_max_pln == 20000


async def test_bulldogjob_parser_and_fetch_from_fixtures() -> None:
    list_html = (FIXTURES_DIR / "bulldogjob_list.html").read_text(encoding="utf-8")
    detail_html = (FIXTURES_DIR / "bulldogjob_detail.html").read_text(encoding="utf-8")

    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        requested_urls.append(url_str)
        if "/companies/jobs/s/" in url_str:
            return httpx.Response(200, text=list_html)
        if "/companies/jobs/100001-python-ai-engineer-warszawa-acme-tech" in url_str:
            return httpx.Response(200, text=detail_html)
        if "/companies/jobs/100002-remote-go-backend-developer-cloudware" in url_str:
            return httpx.Response(404)
        return httpx.Response(404)

    profile = _sample_profile()
    stage1 = Stage1Filter(profile)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scraper = BulldogjobScraper(client=client, request_delay=0.0)
        offers = await scraper.fetch_offers(profile=profile, stage1_filter=stage1)

    assert any("city,Warszawa,Remote" in u and "experienceLevel,junior,medium" in u for u in requested_urls)
    assert scraper.last_total_fetched == 3
    assert len(offers) == 3

    # Krakow onsite PHP offer was filtered before detail fetch
    assert not any("100003-php-developer" in u for u in requested_urls)
    php_offer = next(o for o in offers if o.external_id.startswith("100003"))
    assert php_offer.metadata_rejected_reason is not None

    # Enriched hybrid Python & AI offer
    py_offer = next(o for o in offers if o.external_id.startswith("100001"))
    assert py_offer.source == "bulldogjob"
    assert py_offer.company == "Acme Tech"
    assert py_offer.workplace_type == "hybrid"
    assert py_offer.locations == ["Warszawa"]
    assert py_offer.experience_level == "Mid"
    # UoP 12000-16000, B2B 85-110/h -> 14280-18480 => min 12000, max 18480
    assert py_offer.salary_min_pln == 12000
    assert py_offer.salary_max_pln == 18480
    assert "Python" in py_offer.skill_names()
    assert "LangChain" in py_offer.skill_names()
    assert "agentów LLM" in py_offer.description
    assert py_offer.published_at == "2026-11-24T08:20:30+01:00"

    # Remote EUR offer falls back cleanly when detail returns 404 (undisclosed PLN salary kept as None)
    go_offer = next(o for o in offers if o.external_id.startswith("100002"))
    assert go_offer.workplace_type == "remote"
    assert go_offer.locations == ["Warsaw"]
    assert go_offer.experience_level == "Junior"
    assert go_offer.salary_min_pln is None
    assert go_offer.salary_max_pln is None
    assert "EUR" in go_offer.description
