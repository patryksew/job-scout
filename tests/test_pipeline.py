"""Unit tests for the job-scout pipeline with mocked HTTP and LLM responses."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

from job_scout.cli import _unique_run_stamp, build_parser, execute_report, execute_run

from job_scout.config import ProfileConfig, Settings, load_profile
from job_scout.database import DatabaseManager
from job_scout.evaluator import LLMEvaluator
from job_scout.filter import Stage1Filter
from job_scout.models import (
    EvaluatedOffer,
    JobEvaluation,
    JobOffer,
    SkillItem,
    SourceRunStats,
    compute_normalized_fingerprint,
    normalize_company,
    normalize_title,
)
from job_scout.reporter import RunMetrics, generate_html_report, generate_markdown_report
from job_scout.scrapers.base import BaseScraper
from job_scout.scrapers.justjoin import JustJoinScraper
from job_scout.scrapers.solid_jobs import SolidJobsScraper


def _sample_profile() -> ProfileConfig:
    return ProfileConfig(
        min_match_score=7,
        must_have_any_tech=["Python", "Go", "AI", "SQL"],
        exclude_keywords=["5+ years", "PHP", "Principal"],
        allowed_workplace_types=["remote", "hybrid"],
        allowed_cities=["Warszawa", "Warsaw"],
        min_salary_pln=9000,
        preferences={"target_levels": ["junior", "mid"]},
        free_text_expectations="Szukam roli Python/AI/Go w Warszawie lub zdalnie.",
    )


def _sample_offer(**overrides: object) -> JobOffer:
    base = {
        "external_id": "offer-1",
        "source": "solid.jobs",
        "url": "https://solid.jobs/offer/offer-1",
        "title": "Python & AI Engineer",
        "company": "Acme AI",
        "locations": ["Warszawa"],
        "workplace_type": "hybrid",
        "experience_level": "Mid",
        "salary_min_pln": 12000,
        "salary_max_pln": 16000,
        "skills": [SkillItem(name="Python", level="Mid"), SkillItem(name="PostgreSQL", level=3)],
        "description": "Budowa usług backendowych w Python oraz integracja modeli LLM.",
    }
    base.update(overrides)
    return JobOffer(**base)  # type: ignore[arg-type]


def test_cross_portal_deduplication_and_30_day_window(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path / "jobs.db", tmp_path / "rejected_log.jsonl")
    try:
        solid_offer = _sample_offer(external_id="s-100", source="solid.jobs")
        db.record_rule_rejection(solid_offer, "Test reason")

        # Same (source, external_id) is duplicate
        is_dup_id, reason_id = db.is_duplicate(solid_offer)
        assert is_dup_id is True
        assert reason_id == "already_seen_source_id"

        # Same company + title (different case/whitespace) on justjoin.it within 30 days
        jj_cross = _sample_offer(
            external_id="jj-acme-python",
            source="justjoin.it",
            title="  python & ai engineer ",
            company="acme ai",
        )
        is_dup_fp, reason_fp = db.is_duplicate(jj_cross)
        assert is_dup_fp is True
        assert reason_fp == "cross_portal_duplicate_30d"

        # Age the stored offer beyond 30 days -> no longer blocked by 30-day fingerprint window
        db._conn.execute(
            "UPDATE offers SET created_at = datetime('now', '-31 days') WHERE external_id = 's-100'"
        )
        db._conn.commit()
        is_dup_old, _ = db.is_duplicate(jj_cross)
        assert is_dup_old is False
    finally:
        db.close()


def test_stage1_filter_and_rejection_log(tmp_path: Path) -> None:
    profile = _sample_profile()
    stage1 = Stage1Filter(profile)
    log_path = tmp_path / "rejected_log.jsonl"
    db = DatabaseManager(tmp_path / "jobs.db", log_path)

    try:
        onsite_krakow = _sample_offer(
            external_id="1", workplace_type="onsite", locations=["Kraków"]
        )
        reason_wp = stage1.filter_offer(onsite_krakow)
        assert reason_wp == "Workplace mismatch: onsite in Kraków"
        db.record_rule_rejection(onsite_krakow, reason_wp)

        hybrid_gdansk = _sample_offer(
            external_id="2", workplace_type="hybrid", locations=["Gdańsk"]
        )
        assert stage1.filter_offer(hybrid_gdansk) == "Workplace mismatch: hybrid in Gdańsk"

        low_salary = _sample_offer(external_id="3", salary_min_pln=5000, salary_max_pln=8000)
        assert stage1.filter_offer(low_salary) == "Salary too low: max 8000 PLN < min 9000 PLN"

        undisclosed_salary = _sample_offer(
            external_id="4", salary_min_pln=None, salary_max_pln=None
        )
        assert stage1.filter_offer(undisclosed_salary) is None

        excluded_exp = _sample_offer(
            external_id="5", description="Looking for Python dev with 5+ years experience."
        )
        assert stage1.filter_offer(excluded_exp) == "Excluded keyword matched: '5+ years'"

        # Word boundary test: "Go" must NOT match "Google" when Python/AI/SQL are absent
        google_only = _sample_offer(
            external_id="6",
            title="Frontend Engineer",
            skills=[SkillItem(name="TypeScript")],
            description="Using Google Cloud Platform and React.",
        )
        reason_tech = stage1.filter_offer(google_only)
        assert reason_tech is not None and reason_tech.startswith("Missing required tech:")

        # Verify rejected_log.jsonl content
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["stage"] == "rule"
        assert rec["rejection_reason"] == "Workplace mismatch: onsite in Kraków"
    finally:
        db.close()


async def test_scrapers_and_justjoin_metadata_optimization() -> None:
    solid_payload = {
        "pageIndex": 0,
        "totalPages": 1,
        "jobs": [
            {
                "jobOfferKey": "sj-42",
                "title": "Mid Python Developer",
                "company": "SolidTech",
                "locations": ["Warszawa"],
                "isHybrid": True,
                "experienceLevel": "Regular",
                "salaryPrimary": {
                    "salaryFrom": 13000,
                    "salaryTo": 18000,
                    "currency": "PLN",
                    "period": "Month",
                },
                "requiredSkills": [{"name": "Python", "level": "Regular"}],
                "description": "<p>FastAPI &amp; PostgreSQL</p>",
            }
        ],
    }

    jj_list_payload = [
        {
            "slug": "krakow-onsite-skip",
            "title": "Python Developer",
            "companyName": "SkipCorp",
            "city": "Kraków",
            "workplaceType": "office",
            "requiredSkills": [{"name": "Python"}],
        },
        {
            "slug": "warsaw-python-survivor",
            "title": "Junior/Mid Python & AI Developer",
            "companyName": "GoodCorp",
            "city": "Warszawa",
            "workplaceType": "hybrid",
            "category": {"key": "python", "parentKey": None},
            "employmentTypes": [
                {
                    "type": "b2b",
                    "currency": "PLN",
                    "from": 13440.0,
                    "fromPerUnit": 80.0,
                    "to": 21840.0,
                    "toPerUnit": 130.0,
                    "unit": "Hour",
                }
            ],
            "requiredSkills": [{"name": "Python"}, {"name": "PostgreSQL"}],
        },
    ]

    detail_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "solid.jobs/public-api/offers/IT" in url_str:
            return httpx.Response(200, json=solid_payload)
        if url_str.endswith("/api/offers"):
            return httpx.Response(200, json=jj_list_payload)
        if "/api/offers/warsaw-python-survivor" in url_str:
            detail_requests.append(url_str)
            return httpx.Response(
                200,
                json={
                    "slug": "warsaw-python-survivor",
                    "body": "<p>Praca nad RAG i function calling w Pythonie.</p>",
                },
            )
        if "/api/offers/krakow-onsite-skip" in url_str:
            detail_requests.append(url_str)
            return httpx.Response(500)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        solid_scraper = SolidJobsScraper(client=client)
        solid_offers = await solid_scraper.fetch_offers()
        assert len(solid_offers) == 1
        assert solid_offers[0].workplace_type == "hybrid"
        assert solid_offers[0].salary_max_pln == 18000
        assert solid_offers[0].description == "FastAPI & PostgreSQL"

        jj_scraper = JustJoinScraper(client=client)
        stage1 = Stage1Filter(_sample_profile())
        jj_offers = await jj_scraper.fetch_offers(stage1_filter=stage1)

        assert len(jj_offers) == 2
        # Only the survivor triggered a detail fetch; the Krakow onsite offer was filtered pre-detail!
        assert detail_requests == ["https://justjoin.it/api/offers/warsaw-python-survivor"]

        rejected_item = next(o for o in jj_offers if o.external_id == "krakow-onsite-skip")
        assert rejected_item.metadata_rejected_reason == "Workplace mismatch: onsite in Kraków"

        survivor_item = next(o for o in jj_offers if o.external_id == "warsaw-python-survivor")
        assert "function calling" in survivor_item.description
        assert survivor_item.salary_min_pln == 13440
        assert survivor_item.salary_max_pln == 21840


async def test_llm_evaluator_structured_output_retry_and_logging(tmp_path: Path) -> None:
    profile = _sample_profile()
    settings = Settings(llm_api_key="test-key", llm_rpm_delay=0.0)

    good_eval = JobEvaluation(
        match_score=8,
        is_recommended=True,
        one_line_pitch="Świetne dopasowanie: backend w Pythonie i wdrożenia RAG/LLM.",
        red_flags=["Brak jawnych widełek na UoP"],
        cv_focus_tips=["Podkreśl projekt Agentic Document Router", "Wyeksponuj PostgreSQL"],
        rejection_reason=None,
    )
    low_eval = JobEvaluation(
        match_score=4,
        is_recommended=False,
        one_line_pitch="Rola nastawiona głównie na utrzymanie starego kodu.",
        red_flags=["Legacy codebase bez testów"],
        cv_focus_tips=["Pokaż znajomość Docker"],
        rejection_reason="Zbyt dużo utrzymania legacy względem prac rozwojowych nad AI/Python.",
    )

    # First call fails with 429 RateLimitError, second succeeds with parsed object, third returns low_eval
    req = httpx.Request("POST", "https://example.com/v1/chat/completions")
    resp_429 = httpx.Response(429, request=req)
    rate_err = openai.RateLimitError("rate limited", response=resp_429, body=None)

    parse_mock = AsyncMock(
        side_effect=[
            rate_err,
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=good_eval, content=None))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=low_eval, content=None))]),
        ]
    )
    mock_client = SimpleNamespace(
        beta=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse_mock))),
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
    )

    evaluator = LLMEvaluator(settings, profile, "# CV", client=mock_client)  # type: ignore[arg-type]

    with patch("job_scout.evaluator.asyncio.sleep", new=AsyncMock()):
        res_good = await evaluator.evaluate_offer(_sample_offer(external_id="good-1"))
        res_low = await evaluator.evaluate_offer(_sample_offer(external_id="low-1"))

    assert res_good.is_recommended is True
    assert res_good.match_score == 8
    assert res_low.is_recommended is False
    assert res_low.rejection_reason is not None
    assert parse_mock.call_args.kwargs.get("reasoning_effort") == "low"

    # Verify DB + rejected_log.jsonl persistence for Stage 2 LLM results
    log_path = tmp_path / "rejected_log.jsonl"
    db = DatabaseManager(tmp_path / "jobs.db", log_path)
    try:
        assert db.record_llm_result(_sample_offer(external_id="good-1"), res_good) is None
        rej = db.record_llm_result(_sample_offer(external_id="low-1"), res_low)
        assert rej is not None
        assert rej.stage == "llm"
        assert rej.match_score == 4

        stats = db.get_db_stats()
        assert stats["by_status"]["shortlisted"] == 1
        assert stats["by_status"]["rejected_llm"] == 1
    finally:
        db.close()


def test_markdown_html_reports_and_github_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    offer_hi = _sample_offer(
        external_id="hi",
        title="AI Backend Engineer",
        workplace_type="remote",
        locations=["Kraków"],
        published_at="2026-11-15T10:00:00Z",
    )
    offer_mid = _sample_offer(
        external_id="mid",
        title="Python Developer",
        salary_max_pln=13000,
        published_at="2026-11-16T08:00:00Z",
    )

    shortlisted = [
        EvaluatedOffer(
            offer=offer_mid,
            evaluation=JobEvaluation(
                match_score=7,
                is_recommended=True,
                one_line_pitch="Solidna rola Python.",
                red_flags=[],
                cv_focus_tips=["Wyeksponuj FastAPI"],
            ),
        ),
        EvaluatedOffer(
            offer=offer_hi,
            evaluation=JobEvaluation(
                match_score=9,
                is_recommended=True,
                one_line_pitch="Idealna rola AI/LLM z function calling.",
                red_flags=["Wymagana praca hybrydowa 2 dni"],
                cv_focus_tips=["Podkreśl constrained decoding"],
            ),
        ),
    ]

    db = DatabaseManager(tmp_path / "jobs.db", tmp_path / "rejected_log.jsonl")
    try:
        rule_rej = [db.record_rule_rejection(_sample_offer(external_id="r1"), "Workplace mismatch: onsite in Kraków")]
        llm_rej = [
            db.record_llm_result(
                _sample_offer(external_id="l1"),
                JobEvaluation(
                    match_score=4,
                    is_recommended=False,
                    one_line_pitch="Słabe dopasowanie.",
                    rejection_reason="Zbyt dużo utrzymania legacy.",
                ),
            )
        ]
    finally:
        db.close()

    metrics = RunMetrics(
        fetched=10,
        duplicates_skipped=2,
        rule_rejected=1,
        llm_rejected=1,
        shortlisted=2,
    )
    md_path = tmp_path / "2026-11-16_report.md"
    html_path = tmp_path / "2026-11-16_report.html"

    md_text = generate_markdown_report(
        date(2026, 11, 16),
        metrics,
        shortlisted,
        [r for r in llm_rej if r is not None],
        rule_rej,
        md_path,
    )
    html_text = generate_html_report(
        date(2026, 11, 16),
        metrics,
        shortlisted,
        [r for r in llm_rej if r is not None],
        rule_rej,
        html_path,
    )

    # Sorted descending by score (9/10 appears before 7/10)
    assert md_text.index("AI Backend Engineer") < md_text.index("Python Developer")
    assert "## Rekomendowane oferty" in md_text
    assert "## Podsumowanie odrzuconych ofert (Kalibracja)" in md_text
    assert summary_file.read_text(encoding="utf-8").strip() == md_text.strip()

    assert "<details class=\"calibration\">" in html_text
    assert "9/10" in html_text
    assert "Podsumowanie odrzuconych ofert" in html_text
    assert 'id="filter-score"' in html_text
    assert 'id="filter-workplace"' in html_text
    assert 'id="filter-city"' in html_text
    assert 'id="sort-by"' in html_text
    assert "fresh_desc" in html_text
    assert 'data-score="9"' in html_text
    assert 'data-workplace="remote"' in html_text
    assert 'data-cities="Kraków"' in html_text
    assert 'data-cities="Warszawa"' in html_text
    assert 'data-published="2026-11-16T08:00:00Z"' in html_text


def test_nested_profile_yaml_loading(tmp_path: Path) -> None:
    example_profile = load_profile(Path("config/profile.example.yaml"))
    assert example_profile.min_match_score == 7

    nested_yaml = tmp_path / "profile.yaml"
    nested_yaml.write_text(
        """
candidate:
  target_levels: ["junior", "mid", "regular"]
  min_match_score: 5
hard_filters:
  must_have_any_tech: ["python", "go"]
  exclude_keywords: ["5+ years"]
  allowed_workplace_types: ["remote", "hybrid"]
  allowed_cities: ["Warszawa", "Warsaw"]
  min_salary_pln: 7000
preferences:
  primary_focus: ["AI Engineering"]
free_text_expectations: "Szukam roli Python/AI."
""",
        encoding="utf-8",
    )
    profile = load_profile(nested_yaml)
    assert profile.min_match_score == 5
    assert profile.min_salary_pln == 7000
    assert "python" in profile.must_have_any_tech
    assert "5+ years" in profile.exclude_keywords
    assert "Warszawa" in profile.allowed_cities
    assert profile.preferences.get("target_levels") == ["junior", "mid", "regular"]


def test_timestamped_run_stamps_and_master_report_regeneration(tmp_path: Path) -> None:
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    now = datetime(2026, 11, 17, 7, 45, 12)

    stamp1 = _unique_run_stamp(reports_dir, now)
    assert stamp1 == "2026-11-17_07-45"
    (reports_dir / f"{stamp1}_report.html").write_text("run1", encoding="utf-8")

    stamp2 = _unique_run_stamp(reports_dir, now)
    assert stamp2 == "2026-11-17_07-45-12"
    (reports_dir / f"{stamp2}_report.html").write_text("run2", encoding="utf-8")

    stamp3 = _unique_run_stamp(reports_dir, now)
    assert stamp3 == "2026-11-17_07-45-12_2"

    db_path = tmp_path / "jobs.db"
    log_path = tmp_path / "rejected_log.jsonl"
    db = DatabaseManager(db_path, log_path)
    try:
        db.record_llm_result(
            _sample_offer(external_id="recent-1", title="Recent AI Role"),
            JobEvaluation(
                match_score=8,
                is_recommended=True,
                one_line_pitch="Świeża oferta AI.",
            ),
        )
        db.record_llm_result(
            _sample_offer(external_id="old-1", title="Old Python Role"),
            JobEvaluation(
                match_score=7,
                is_recommended=True,
                one_line_pitch="Starsza oferta Python.",
            ),
        )
        db._conn.execute(
            "UPDATE offers SET created_at = datetime('now', '-10 days') WHERE external_id = 'old-1'"
        )
        db._conn.commit()
    finally:
        db.close()

    settings = Settings(
        db_path=db_path,
        rejected_log_path=log_path,
        reports_dir=reports_dir,
    )
    today = datetime.now(timezone.utc).date()
    md_today, html_today = execute_report(report_date=today, settings=settings)
    assert md_today.name == f"{today.isoformat()}_master.md"
    assert html_today.name == f"{today.isoformat()}_master.html"
    today_html_text = html_today.read_text(encoding="utf-8")
    assert "Recent AI Role" in today_html_text
    assert "Old Python Role" not in today_html_text

    with sqlite3.connect(db_path) as db_path_conn:
        old_date_row = db_path_conn.execute(
            "SELECT date(created_at) FROM offers WHERE external_id = 'old-1'"
        ).fetchone()
    old_date = date.fromisoformat(old_date_row[0])

    _, html_old = execute_report(report_date=old_date, settings=settings)
    assert html_old.name == f"{old_date.isoformat()}_master.html"
    old_html_text = html_old.read_text(encoding="utf-8")
    assert "Old Python Role" in old_html_text
    assert "Recent AI Role" not in old_html_text


def test_normalized_fingerprint_and_legacy_db_migration(tmp_path: Path) -> None:
    assert normalize_company("Acme Sp. z o.o.") == "acme"
    assert normalize_company("ACME S.A.") == "acme"
    assert normalize_company("Acme Spółka z ograniczoną odpowiedzialnością") == "acme"
    assert normalize_title("Python Developer (Remote) - Warszawa") == "python developer"
    assert normalize_title("Python   Developer / 100% Remote [Warsaw]") == "python developer"
    assert compute_normalized_fingerprint("Acme Sp. z o.o.", "Python Developer - Warszawa") == (
        compute_normalized_fingerprint("ACME S.A.", "Python Developer (Remote)")
    )
    # C++ and C# roles at the same company must NOT collide
    assert compute_normalized_fingerprint("Acme", "C++ Developer") != (
        compute_normalized_fingerprint("Acme", "C# Developer")
    )

    # Simulate pre-migration legacy SQLite DB (without normalized_fingerprint column)
    db_path = tmp_path / "legacy.db"
    log_path = tmp_path / "rejected_log.jsonl"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            url TEXT NOT NULL,
            locations_json TEXT NOT NULL DEFAULT '[]',
            workplace_type TEXT NOT NULL DEFAULT 'onsite',
            experience_level TEXT,
            salary_min_pln INTEGER,
            salary_max_pln INTEGER,
            skills_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            match_score INTEGER,
            rejection_reason TEXT,
            evaluation_json TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(source, external_id)
        );
        INSERT INTO offers (source, external_id, fingerprint, title, company, url, status)
        VALUES ('solid.jobs', 'leg-1', 'old-raw-hash', 'Python Developer (Warszawa)', 'TechCorp Sp. z o.o.', 'https://solid.jobs/offer/leg-1', 'shortlisted');
        """
    )
    conn.commit()
    conn.close()

    # Dry-run on unmigrated read-only DB must still catch normalized cross-portal duplicate
    variant_offer = _sample_offer(
        external_id="nfj-999",
        source="nofluffjobs",
        company="TechCorp S.A.",
        title="Python Developer - Remote",
    )
    ro_db = DatabaseManager(db_path, log_path, dry_run=True)
    try:
        is_dup_ro, reason_ro = ro_db.is_duplicate(variant_offer)
        assert is_dup_ro is True
        assert reason_ro == "cross_portal_duplicate_30d"
    finally:
        ro_db.close()

    # Normal run safely migrates schema via ALTER TABLE and backfills normalized_fingerprint
    rw_db = DatabaseManager(db_path, log_path, dry_run=False)
    try:
        cols = {row["name"] for row in rw_db._conn.execute("PRAGMA table_info(offers)").fetchall()}
        assert "normalized_fingerprint" in cols
        row = rw_db._conn.execute(
            "SELECT normalized_fingerprint FROM offers WHERE external_id = 'leg-1'"
        ).fetchone()
        assert row["normalized_fingerprint"] == variant_offer.compute_fingerprint()

        is_dup_rw, reason_rw = rw_db.is_duplicate(variant_offer)
        assert is_dup_rw is True
        assert reason_rw == "cross_portal_duplicate_30d"
    finally:
        rw_db.close()


async def test_fail_soft_error_isolation_source_flag_and_health_table(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    cv_path = tmp_path / "base_cv.md"
    db_path = tmp_path / "jobs.db"
    log_path = tmp_path / "rejected_log.jsonl"
    reports_dir = tmp_path / "reports"

    profile_path.write_text(
        """
min_match_score: 7
must_have_any_tech: ["Python"]
exclude_keywords: ["PHP"]
allowed_workplace_types: ["remote", "hybrid"]
allowed_cities: ["Warszawa"]
sources:
  disabled_portal:
    enabled: false
""",
        encoding="utf-8",
    )
    cv_path.write_text("# Candidate CV\nPython Engineer", encoding="utf-8")

    class FailingScraper(BaseScraper):
        source_name = "broken.portal"

        async def fetch_offers(self, **kwargs: object) -> list[JobOffer]:
            raise httpx.ConnectTimeout("Connection timed out to broken.portal")

    class WorkingScraperA(BaseScraper):
        source_name = "portal.a"

        async def fetch_offers(self, **kwargs: object) -> list[JobOffer]:
            offers = [
                _sample_offer(
                    external_id="a-1",
                    source="portal.a",
                    company="Acme Sp. z o.o.",
                    title="Python Engineer (Remote)",
                )
            ]
            self.last_total_fetched = len(offers)
            return offers

    class WorkingScraperB(BaseScraper):
        source_name = "portal.b"

        async def fetch_offers(self, **kwargs: object) -> list[JobOffer]:
            offers = [
                # Cross-portal duplicate of portal.a with different legal suffix & location tag
                _sample_offer(
                    external_id="b-dup",
                    source="portal.b",
                    company="ACME S.A.",
                    title="Python Engineer - Warszawa",
                ),
                # Unique offer
                _sample_offer(
                    external_id="b-uniq",
                    source="portal.b",
                    company="BetaLabs",
                    title="Junior Python Developer",
                ),
            ]
            self.last_total_fetched = len(offers)
            return offers

    class DisabledScraper(BaseScraper):
        source_name = "disabled_portal"

        async def fetch_offers(self, **kwargs: object) -> list[JobOffer]:
            raise AssertionError("Disabled portal should not be invoked")

    fake_registry = {
        "broken.portal": FailingScraper,
        "portal.a": WorkingScraperA,
        "portal.b": WorkingScraperB,
        "disabled_portal": DisabledScraper,
    }

    settings = Settings(
        llm_api_key="",
        profile_path=profile_path,
        cv_path=cv_path,
        db_path=db_path,
        rejected_log_path=log_path,
        reports_dir=reports_dir,
    )

    # 1. Dry-run across all sources: broken.portal fails soft, portal.a & portal.b succeed, dedup works, DB untouched
    metrics = await execute_run(
        dry_run=True,
        settings=settings,
        scraper_registry=fake_registry,
    )
    assert not db_path.exists()
    assert metrics.fetched == 3
    assert metrics.duplicates_skipped == 1
    assert metrics.rule_rejected == 0

    stats_by_src = {s.source: s for s in metrics.source_stats}
    assert stats_by_src["broken.portal"].error is not None
    assert "timed out" in stats_by_src["broken.portal"].error
    assert stats_by_src["portal.a"].fetched == 1
    assert stats_by_src["portal.a"].passed_prefilter == 1
    assert stats_by_src["portal.b"].fetched == 2
    assert stats_by_src["portal.b"].duplicates_skipped == 1
    assert stats_by_src["portal.b"].passed_prefilter == 1
    assert stats_by_src["disabled_portal"].status == "Wyłączony"

    # 2. --source flag runs only the selected portal
    single_metrics = await execute_run(
        dry_run=True,
        sources=["portal.b"],
        settings=settings,
        scraper_registry=fake_registry,
    )
    assert [s.source for s in single_metrics.source_stats] == ["portal.b"]
    assert single_metrics.fetched == 2

    # 3. CLI parser accepts --source and --dry-run
    parser = build_parser()
    parsed = parser.parse_args(["run", "--source", "solid.jobs", "--dry-run"])
    assert parsed.dry_run is True
    assert parsed.sources == ["solid.jobs"]

    # 4. Reports render the Source Health table at the top
    md_path = tmp_path / "health_report.md"
    html_path = tmp_path / "health_report.html"
    md_out = generate_markdown_report(
        date(2026, 11, 24), metrics, [], [], [], md_path, write_step_summary=False
    )
    html_out = generate_html_report(date(2026, 11, 24), metrics, [], [], [], html_path)
    assert "| Portal | Pobrane | Po pre-filtrze | Duplikaty | Ocenione LLM | Status/Błąd |" in md_out
    assert "broken.portal" in md_out and "BŁĄD:" in md_out
    assert md_out.index("## Kondycja źródeł") < md_out.index("## Rekomendowane oferty")
    assert 'id="source-health-table"' in html_out
    assert "<th>Portal</th>" in html_out
    assert "<th>Status/Błąd</th>" in html_out
    assert "broken.portal" in html_out


async def test_cross_portal_dedup_prevents_duplicate_llm_calls(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    cv_path = tmp_path / "base_cv.md"
    db_path = tmp_path / "jobs.db"
    log_path = tmp_path / "rejected_log.jsonl"
    reports_dir = tmp_path / "reports"

    profile_path.write_text(
        """
min_match_score: 7
must_have_any_tech: ["Python"]
allowed_workplace_types: ["remote", "hybrid"]
allowed_cities: ["Warszawa"]
""",
        encoding="utf-8",
    )
    cv_path.write_text("# CV\nPython Engineer", encoding="utf-8")

    class NFJMock(BaseScraper):
        source_name = "nofluffjobs"

        async def fetch_offers(self, **kwargs: object) -> list[JobOffer]:
            offers = [
                _sample_offer(
                    external_id="nfj-1",
                    source="nofluffjobs",
                    company="NovaTech Sp. z o.o.",
                    title="Mid Python Engineer (Remote)",
                )
            ]
            self.last_total_fetched = 1
            return offers

    class PracujMock(BaseScraper):
        source_name = "pracuj.pl"

        async def fetch_offers(self, **kwargs: object) -> list[JobOffer]:
            offers = [
                _sample_offer(
                    external_id="pr-1",
                    source="pracuj.pl",
                    company="NOVATECH S.A.",
                    title="Mid Python Engineer - Warszawa (k/m/x)",
                )
            ]
            self.last_total_fetched = 1
            return offers

    eval_calls: list[str] = []

    async def fake_eval(self: LLMEvaluator, offer: JobOffer, max_attempts: int = 3) -> JobEvaluation:
        eval_calls.append(f"{offer.source}:{offer.external_id}")
        return JobEvaluation(
            match_score=8,
            is_recommended=True,
            one_line_pitch="Świetna oferta Python.",
        )

    settings = Settings(
        llm_api_key="dummy-key",
        profile_path=profile_path,
        cv_path=cv_path,
        db_path=db_path,
        rejected_log_path=log_path,
        reports_dir=reports_dir,
    )

    with patch.object(LLMEvaluator, "evaluate_offer", new=fake_eval):
        metrics = await execute_run(
            dry_run=False,
            settings=settings,
            scraper_registry={"nofluffjobs": NFJMock, "pracuj.pl": PracujMock},
        )

    # Duplicate from pracuj.pl must be skipped BEFORE LLM invocation
    assert eval_calls == ["nofluffjobs:nfj-1"]
    assert metrics.shortlisted == 1
    assert metrics.duplicates_skipped == 1
    by_src = {s.source: s for s in metrics.source_stats}
    assert by_src["nofluffjobs"].llm_evaluated == 1
    assert by_src["pracuj.pl"].llm_evaluated == 0
    assert by_src["pracuj.pl"].duplicates_skipped == 1
