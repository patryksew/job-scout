"""Async scraper for Bulldogjob using Next.js SSR __NEXT_DATA__ pages."""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import TYPE_CHECKING, Any

import httpx

from job_scout.models import JobOffer, SkillItem, WorkplaceType
from job_scout.scrapers.base import BaseScraper, strip_html

if TYPE_CHECKING:
    from job_scout.config import ProfileConfig
    from job_scout.database import DatabaseManager
    from job_scout.filter import Stage1Filter

BULLDOG_BASE_URL = "https://bulldogjob.pl/companies/jobs"
BULLDOG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?")

_CITY_CANONICAL_MAP = {
    "warsaw": "Warszawa",
    "warszawa": "Warszawa",
    "krakow": "Kraków",
    "kraków": "Kraków",
    "cracow": "Kraków",
    "wroclaw": "Wrocław",
    "wrocław": "Wrocław",
    "gdansk": "Gdańsk",
    "gdańsk": "Gdańsk",
    "poznan": "Poznań",
    "poznań": "Poznań",
    "lodz": "Łódź",
    "łódź": "Łódź",
    "katowice": "Katowice",
}

_EXP_FILTER_MAP = {
    "intern": "junior",
    "junior": "junior",
    "mid": "medium",
    "regular": "medium",
    "medium": "medium",
    "senior": "senior",
    "lead": "lead",
}


def extract_next_data(html_text: str) -> dict[str, Any]:
    """Parse Next.js __NEXT_DATA__ JSON payload from a Bulldogjob SSR HTML page."""
    match = _NEXT_DATA_RE.search(html_text)
    if not match:
        raise ValueError("Bulldogjob HTML does not contain __NEXT_DATA__ payload")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("Bulldogjob __NEXT_DATA__ payload is not a JSON object")
    return payload


def _parse_money_range(money_str: str | None) -> tuple[float | None, float | None]:
    if not money_str:
        return None, None
    matches = _NUMBER_RE.findall(money_str)
    nums: list[float] = []
    for raw_num in matches:
        cleaned = raw_num.replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            nums.append(float(cleaned))
        except ValueError:
            continue
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums[0], nums[1]), max(nums[0], nums[1])


def _timeframe_to_monthly(value: float, timeframe: str | None) -> int:
    tf = (timeframe or "month").strip().lower()
    if "hour" in tf:
        return int(round(value * 168))
    if "day" in tf:
        return int(round(value * 21))
    if "year" in tf or "ann" in tf:
        return int(round(value / 12.0))
    return int(round(value))


def _extract_salary(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[int | None, int | None, list[str]]:
    """Extract monthly PLN salary bounds and readable contract lines from Bulldogjob data."""
    mins: list[int] = []
    maxs: list[int] = []
    notes: list[str] = []

    def _consume_salary_obj(sal_obj: Any, label: str) -> None:
        if not isinstance(sal_obj, dict) or sal_obj.get("hidden") is True:
            return
        currency = str(sal_obj.get("currency") or "").upper()
        timeframe = str(sal_obj.get("timeframe") or "month")
        lo = sal_obj.get("minValue")
        hi = sal_obj.get("maxValue")
        lo_f = float(lo) if isinstance(lo, (int, float)) else None
        hi_f = float(hi) if isinstance(hi, (int, float)) else None
        if lo_f is None and hi_f is None:
            lo_f, hi_f = _parse_money_range(sal_obj.get("money"))
        if lo_f is None and hi_f is None:
            return
        if not currency:
            currency = "PLN"
        notes.append(
            f"{label}: {int(lo_f) if lo_f is not None else '?'}–"
            f"{int(hi_f) if hi_f is not None else '?'} {currency}/{timeframe}"
        )
        if currency == "PLN":
            if lo_f is not None:
                mins.append(_timeframe_to_monthly(lo_f, timeframe))
            if hi_f is not None:
                maxs.append(_timeframe_to_monthly(hi_f, timeframe))

    if isinstance(detail_raw, dict):
        _consume_salary_obj(detail_raw.get("employmentSalary"), "UoP")
        _consume_salary_obj(detail_raw.get("b2bSalary"), "B2B")
        _consume_salary_obj(detail_raw.get("otherSalary"), "Inne")

    if not mins and not maxs:
        _consume_salary_obj(list_raw.get("denominatedSalaryLong"), "Widełki")

    return (min(mins) if mins else None), (max(maxs) if maxs else None), notes


def _extract_locations_and_workplace(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[list[str], WorkplaceType]:
    """Extract normalized cities and workplace_type from Bulldogjob list + detail payloads."""
    locations: list[str] = []
    seen: set[str] = set()
    has_remote_city = False

    def _add_city(raw_city: Any) -> None:
        nonlocal has_remote_city
        if not isinstance(raw_city, str) or not raw_city.strip():
            return
        for part in raw_city.split(","):
            cleaned = part.strip()
            if not cleaned:
                continue
            low = cleaned.lower()
            if low in {"remote", "zdalnie", "zdalna"}:
                has_remote_city = True
                continue
            if low not in seen:
                seen.add(low)
                locations.append(cleaned)

    if isinstance(detail_raw, dict) and isinstance(detail_raw.get("locations"), list):
        for loc_entry in detail_raw["locations"]:
            if not isinstance(loc_entry, dict):
                continue
            loc_obj = loc_entry.get("location")
            if isinstance(loc_obj, dict):
                _add_city(loc_obj.get("cityPl") or loc_obj.get("cityEn"))

    if not locations:
        _add_city(list_raw.get("city"))

    work_modes: list[str] = []
    if isinstance(detail_raw, dict) and isinstance(detail_raw.get("workModes"), list):
        work_modes = [str(m).strip().lower() for m in detail_raw["workModes"] if m]

    env = (
        (detail_raw or {}).get("environment")
        if isinstance((detail_raw or {}).get("environment"), dict)
        else list_raw.get("environment")
    )
    remote_pct = env.get("remotePossible") if isinstance(env, dict) else None
    is_remote_flag = bool((detail_raw or {}).get("remote") or list_raw.get("remote"))

    if any("remote" in m for m in work_modes) or is_remote_flag or remote_pct == 100:
        workplace: WorkplaceType = "remote"
    elif any("hybrid" in m for m in work_modes) or (
        isinstance(remote_pct, (int, float)) and 0 < remote_pct < 100
    ):
        workplace = "hybrid"
    elif any("on-site" in m or "onsite" in m for m in work_modes):
        workplace = "onsite"
    elif has_remote_city:
        workplace = "remote"
    else:
        workplace = "onsite"

    return locations, workplace


def _extract_skills(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> list[SkillItem]:
    """Combine mainTechnology, technologyTags, and roles into SkillItems."""
    skills: list[SkillItem] = []
    seen: set[str] = set()

    def _add(name: Any, level: str | None = None) -> None:
        if not isinstance(name, str) or not name.strip():
            return
        cleaned = name.strip()
        low = cleaned.lower()
        if low not in seen:
            seen.add(low)
            skills.append(SkillItem(name=cleaned, level=level))

    if isinstance(detail_raw, dict):
        _add(detail_raw.get("mainTechnology"), level="main")
        for tag in detail_raw.get("technologyTags") or []:
            _add(tag)

    for tag in list_raw.get("technologyTags") or []:
        _add(tag)

    if isinstance(detail_raw, dict):
        for role in detail_raw.get("roles") or []:
            _add(role)

    return skills


def _build_description(
    detail_raw: dict[str, Any] | None,
    salary_notes: list[str],
) -> str:
    """Assemble plain-text offer description from Bulldogjob detail HTML sections."""
    parts: list[str] = []
    if salary_notes:
        parts.append("Widełki / formy zatrudnienia: " + "; ".join(salary_notes))
    if not isinstance(detail_raw, dict):
        return "\n\n".join(parts)

    for field, heading in (
        ("details", "Opis roli"),
        ("requirements", "Wymagania"),
        ("offer", "Oferujemy"),
    ):
        cleaned = strip_html(detail_raw.get(field))
        if cleaned:
            parts.append(f"{heading}:\n{cleaned}")

    return "\n\n".join(parts).strip()


class BulldogjobScraper(BaseScraper):
    """Fetches IT job offers from Bulldogjob SSR __NEXT_DATA__ list and detail pages."""

    source_name = "bulldogjob"
    aliases = ("bulldog", "bulldogjob.pl")
    default_max_pages = 5
    default_request_delay = 0.8
    default_timeout = 180.0

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        request_delay: float | None = None,
        max_pages: int | None = None,
    ) -> None:
        super().__init__(
            client=client,
            timeout=timeout,
            max_retries=max_retries,
            request_delay=request_delay,
            max_pages=max_pages,
        )

    @staticmethod
    def build_list_url(profile: ProfileConfig | None, page: int = 1) -> str:
        """Build targeted Bulldogjob SSR listing URL with city, experienceLevel, and page."""
        segments: list[str] = []

        if profile is not None:
            cities: list[str] = []
            seen_cities: set[str] = set()
            for raw_city in profile.allowed_cities:
                canon = _CITY_CANONICAL_MAP.get(raw_city.strip().lower(), raw_city.strip())
                if canon and canon.lower() not in seen_cities:
                    seen_cities.add(canon.lower())
                    cities.append(canon)
            allowed_wp = {w.strip().lower() for w in profile.allowed_workplace_types}
            if (not allowed_wp or "remote" in allowed_wp) and "remote" not in seen_cities:
                cities.append("Remote")
            if cities:
                segments.append(f"city,{','.join(cities)}")

            raw_levels = profile.preferences.get("target_levels")
            if isinstance(raw_levels, list):
                levels: list[str] = []
                seen_lvls: set[str] = set()
                for lvl in raw_levels:
                    mapped = _EXP_FILTER_MAP.get(str(lvl).strip().lower())
                    if mapped and mapped not in seen_lvls:
                        seen_lvls.add(mapped)
                        levels.append(mapped)
                if levels:
                    segments.append(f"experienceLevel,{','.join(levels)}")

        if page > 1:
            segments.append(f"page,{page}")

        if not segments:
            return BULLDOG_BASE_URL
        return f"{BULLDOG_BASE_URL}/s/{'/'.join(segments)}"

    @staticmethod
    def parse_list_html(html_text: str) -> tuple[list[dict[str, Any]], int, int]:
        """Extract (jobs_list, total_count, per_page) from Bulldogjob listing SSR HTML."""
        next_data = extract_next_data(html_text)
        page_props = next_data.get("props", {}).get("pageProps", {})
        jobs = page_props.get("jobs") or []
        total_count = int(page_props.get("totalCount") or len(jobs))
        slug_state = page_props.get("slugState") or {}
        per_page = int(slug_state.get("perPage") or 50)
        return [j for j in jobs if isinstance(j, dict)], total_count, max(1, per_page)

    @staticmethod
    def parse_detail_html(html_text: str) -> dict[str, Any] | None:
        """Extract job detail object from Bulldogjob detail page SSR HTML."""
        next_data = extract_next_data(html_text)
        page_props = next_data.get("props", {}).get("pageProps", {})
        data_obj = page_props.get("data")
        if isinstance(data_obj, dict) and isinstance(data_obj.get("job"), dict):
            return data_obj["job"]
        apollo = page_props.get("__APOLLO_STATE__")
        if isinstance(apollo, dict):
            for key, val in apollo.items():
                if key.startswith("Job:") and isinstance(val, dict):
                    return val
        return None

    def normalize_offer(
        self,
        list_raw: dict[str, Any],
        detail_raw: dict[str, Any] | None = None,
    ) -> JobOffer | None:
        """Convert Bulldogjob list item (+ optional detail object) into a unified JobOffer."""
        job_id = str((detail_raw or {}).get("id") or list_raw.get("id") or "").strip()
        title = str((detail_raw or {}).get("position") or list_raw.get("position") or "").strip()
        if not job_id or not title:
            return None

        comp_obj = (detail_raw or {}).get("company") or list_raw.get("company")
        if isinstance(comp_obj, dict):
            company = str(comp_obj.get("name") or "Unknown").strip() or "Unknown"
        else:
            company = str(comp_obj or "Unknown").strip() or "Unknown"

        url = f"{BULLDOG_BASE_URL}/{job_id.lstrip('/')}"
        locations, workplace_type = _extract_locations_and_workplace(list_raw, detail_raw)
        sal_min, sal_max, salary_notes = _extract_salary(list_raw, detail_raw)
        skills = _extract_skills(list_raw, detail_raw)
        description = _build_description(detail_raw, salary_notes)

        raw_exp = (detail_raw or {}).get("experienceLevel") or list_raw.get("experienceLevel")
        exp_map = {"medium": "Mid", "junior": "Junior", "senior": "Senior", "lead": "Lead"}
        experience_level = (
            exp_map.get(str(raw_exp).strip().lower(), str(raw_exp).strip())
            if raw_exp
            else None
        )

        pub_raw = (detail_raw or {}).get("publishedAt") or list_raw.get("publishedAt")
        published_at = str(pub_raw).strip() if pub_raw else None

        return JobOffer(
            external_id=job_id,
            source=self.source_name,
            url=url,
            title=title,
            company=company,
            locations=locations,
            workplace_type=workplace_type,
            experience_level=experience_level,
            salary_min_pln=sal_min,
            salary_max_pln=sal_max,
            skills=skills,
            description=description,
            published_at=published_at,
        )

    async def _fetch_detail(self, job_id: str) -> dict[str, Any] | None:
        url = f"{BULLDOG_BASE_URL}/{job_id.lstrip('/')}"
        try:
            html_text = await self.fetch_text(url, headers=BULLDOG_HEADERS)
            return self.parse_detail_html(html_text)
        except (httpx.HTTPStatusError, ValueError, json.JSONDecodeError):
            return None

    async def fetch_offers(
        self,
        *,
        profile: ProfileConfig | None = None,
        stage1_filter: Stage1Filter | None = None,
        db: DatabaseManager | None = None,
        seen_fingerprints: set[str] | None = None,
        max_pages: int | None = None,
        max_detail_fetches: int | None = None,
    ) -> list[JobOffer]:
        """Paginate Bulldogjob SSR listing pages, pre-filter metadata, and enrich survivors."""
        pages_limit = self.max_pages if max_pages is None else max_pages
        raw_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for page in range(1, pages_limit + 1):
            if page > 1:
                await self.polite_sleep(min_factor=0.4, max_factor=0.8)
            url = self.build_list_url(profile, page=page)
            html_text = await self.fetch_text(url, headers=BULLDOG_HEADERS)
            page_jobs, total_count, per_page = self.parse_list_html(html_text)
            if not page_jobs:
                break

            new_on_page = 0
            for job in page_jobs:
                jid = str(job.get("id") or "").strip()
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    raw_items.append(job)
                    new_on_page += 1

            total_pages = max(1, math.ceil(total_count / per_page))
            if page >= total_pages or new_on_page == 0:
                break

        self.last_total_fetched = 0
        self.last_duplicates_skipped = 0
        results: list[JobOffer] = []
        survivors_to_fetch: list[tuple[dict[str, Any], JobOffer]] = []

        for raw in raw_items:
            base_offer = self.normalize_offer(raw)
            if base_offer is None:
                continue

            self.last_total_fetched += 1

            if db is not None:
                is_dup, _ = db.is_duplicate(base_offer, seen_fingerprints=seen_fingerprints)
                if is_dup:
                    self.last_duplicates_skipped += 1
                    continue

            if stage1_filter is not None:
                lightweight_reason = stage1_filter.filter_lightweight(base_offer)
                if lightweight_reason is not None:
                    base_offer.metadata_rejected_reason = lightweight_reason
                    results.append(base_offer)
                    continue

            if max_detail_fetches is not None and len(survivors_to_fetch) >= max_detail_fetches:
                continue

            survivors_to_fetch.append((raw, base_offer))

        if survivors_to_fetch:
            sem = asyncio.Semaphore(3)
            use_delay = self.request_delay > 0

            async def _enrich(raw_item: dict[str, Any], base_offer: JobOffer) -> JobOffer:
                async with sem:
                    if use_delay:
                        await self.polite_sleep(min_factor=0.5, max_factor=1.3)
                    detail_raw = await self._fetch_detail(base_offer.external_id)
                    if detail_raw is not None:
                        enriched = self.normalize_offer(raw_item, detail_raw)
                        if enriched is not None:
                            return enriched
                    return base_offer

            enriched_offers = await asyncio.gather(
                *(_enrich(raw_item, base_offer) for raw_item, base_offer in survivors_to_fetch)
            )
            results.extend(enriched_offers)

        return results
