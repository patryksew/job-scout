"""Async scraper for the Just Join IT public REST API with pre-detail filtering."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

import httpx

from job_scout.models import JobOffer, SkillItem, WorkplaceType
from job_scout.scrapers.base import BaseScraper, strip_html

if TYPE_CHECKING:
    from job_scout.database import DatabaseManager
    from job_scout.filter import Stage1Filter

JUSTJOIN_OFFERS_URL = "https://justjoin.it/api/offers"
JUSTJOIN_CANDIDATE_OFFERS_URL = "https://justjoin.it/api/candidate-api/offers"
JUSTJOIN_V2_OFFERS_URL = "https://api.justjoin.it/v2/user-panel/offers"
JUSTJOIN_HEADERS = {
    "Referer": "https://justjoin.it/",
    "Origin": "https://justjoin.it",
}
DEFAULT_JJIT_CATEGORIES = ["python", "go", "ai", "data", "devops", "c", "other"]
DEFAULT_JJIT_EXPERIENCE_LEVELS = ["intern", "junior", "mid"]


def _normalize_workplace(raw: dict[str, Any]) -> WorkplaceType:
    wp = str(raw.get("workplaceType") or raw.get("workplace_type") or "").strip().lower()
    if wp == "remote" or raw.get("remote") is True:
        return "remote"
    if wp == "hybrid":
        return "hybrid"
    return "onsite"


def _normalize_locations(raw: dict[str, Any]) -> list[str]:
    locations: list[str] = []
    if isinstance(raw.get("city"), str) and raw["city"].strip():
        locations.append(raw["city"].strip())

    for key in ("locations", "multilocation", "addresses"):
        items = raw.get(key)
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, str) and entry.strip():
                    locations.append(entry.strip())
                elif isinstance(entry, dict):
                    city = entry.get("city") or entry.get("name")
                    if isinstance(city, str) and city.strip():
                        locations.append(city.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for loc in locations:
        low = loc.lower()
        if low not in seen:
            seen.add(low)
            deduped.append(loc)
    return deduped


def _normalize_salary(raw: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract monthly PLN salary bounds from employmentTypes/salaries or flat fields."""
    mins: list[int] = []
    maxs: list[int] = []

    emp_types = raw.get("employmentTypes") or raw.get("employment_types") or raw.get("salaries") or []
    if isinstance(emp_types, list):
        for emp in emp_types:
            if not isinstance(emp, dict):
                continue
            salary_obj = emp.get("salary") if isinstance(emp.get("salary"), dict) else emp
            currency = str(
                salary_obj.get("currency") or emp.get("currency") or "PLN"
            ).upper()
            if currency != "PLN":
                continue

            sal_from = salary_obj.get("from") or salary_obj.get("from_pln") or emp.get("from")
            sal_to = salary_obj.get("to") or salary_obj.get("to_pln") or emp.get("to")
            unit = str(
                salary_obj.get("unit") or emp.get("unit") or emp.get("scale") or "month"
            ).lower()

            # candidate-api returns monthly totals in from/to alongside hourly rates in fromPerUnit/toPerUnit
            has_per_unit = "fromPerUnit" in emp or "toPerUnit" in emp or "fromPerUnit" in salary_obj
            ref_val = float(sal_to if sal_to is not None else (sal_from or 0))
            mult = 168 if ("hour" in unit and not has_per_unit and ref_val < 1000) else 1

            if sal_from is not None:
                mins.append(int(float(sal_from) * mult))
            if sal_to is not None:
                maxs.append(int(float(sal_to) * mult))

    if not mins and not maxs:
        flat_cur = str(raw.get("salaryCurrency") or "PLN").upper()
        if flat_cur == "PLN":
            if raw.get("salaryFrom") is not None:
                mins.append(int(float(raw["salaryFrom"])))
            if raw.get("salaryTo") is not None:
                maxs.append(int(float(raw["salaryTo"])))

    return (min(mins) if mins else None), (max(maxs) if maxs else None)


def _normalize_skills(raw: dict[str, Any]) -> list[SkillItem]:
    """Combine requiredSkills, niceToHaveSkills, skills, and category into SkillItems."""
    skills: list[SkillItem] = []
    seen: set[str] = set()

    for key in ("requiredSkills", "skills", "niceToHaveSkills", "optionalSkills"):
        raw_list = raw.get(key)
        if not isinstance(raw_list, list):
            continue
        for entry in raw_list:
            if isinstance(entry, str) and entry.strip():
                name = entry.strip()
                level = None
            elif isinstance(entry, dict):
                name_val = entry.get("name") or entry.get("skill")
                if not name_val or not str(name_val).strip():
                    continue
                name = str(name_val).strip()
                level = entry.get("level")
            else:
                continue

            if name.lower() not in seen:
                seen.add(name.lower())
                skills.append(SkillItem(name=name, level=level))

    for cat_key in ("category", "markerIcon", "marker_icon"):
        cat = raw.get(cat_key)
        cat_name = cat.get("key") if isinstance(cat, dict) else cat
        if isinstance(cat_name, str) and cat_name.strip() and cat_name.strip().lower() not in seen:
            seen.add(cat_name.strip().lower())
            skills.append(SkillItem(name=cat_name.strip()))

    return skills


class JustJoinScraper(BaseScraper):
    """Fetches offers from Just Join IT with lightweight Stage 1 metadata pre-filtering."""

    source_name = "justjoin.it"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__(client=client, timeout=timeout, max_retries=max_retries)
        self.last_total_fetched: int = 0
        self.last_duplicates_skipped: int = 0

    def normalize_offer(self, raw: dict[str, Any]) -> JobOffer | None:
        """Convert a Just Join IT list or detail JSON object into a unified JobOffer."""
        slug = str(raw.get("slug") or raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not slug or not title:
            return None

        company = str(
            raw.get("companyName") or raw.get("company_name") or raw.get("company") or "Unknown"
        ).strip() or "Unknown"

        url = str(raw.get("offerUrl") or f"https://justjoin.it/job-offer/{slug}").strip()
        locations = _normalize_locations(raw)
        workplace_type = _normalize_workplace(raw)
        sal_min, sal_max = _normalize_salary(raw)
        skills = _normalize_skills(raw)

        exp = raw.get("experienceLevel") or raw.get("experience_level")
        exp_str = str(exp).strip() if exp is not None else None

        desc_raw = raw.get("body") or raw.get("description") or ""
        description = strip_html(str(desc_raw))

        pub_raw = raw.get("publishedAt") or raw.get("lastPublishedAt") or raw.get("published_at")
        published_at = str(pub_raw).strip() if pub_raw else None

        return JobOffer(
            external_id=slug,
            source=self.source_name,
            url=url,
            title=title,
            company=company,
            locations=locations,
            workplace_type=workplace_type,
            experience_level=exp_str,
            salary_min_pln=sal_min,
            salary_max_pln=sal_max,
            skills=skills,
            description=description,
            published_at=published_at,
        )

    @staticmethod
    def _extract_list_items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for key in ("data", "offers", "items", "results"):
                val = payload.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    async def _fetch_list_payload(
        self,
        max_pages: int = 20,
        items_count: int = 500,
        categories: list[str] | None = None,
        experience_levels: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """Fetch list endpoint, falling back to targeted /api/candidate-api/offers or v2 API."""
        try:
            payload = await self.fetch_json(JUSTJOIN_OFFERS_URL, headers=JUSTJOIN_HEADERS)
            items = self._extract_list_items(payload)
            if items or isinstance(payload, (list, dict)):
                return items, JUSTJOIN_OFFERS_URL
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {403, 404, 410}:
                raise

        # Current live Just Join IT candidate API with category/seniority filters and cursor pagination
        cats = DEFAULT_JJIT_CATEGORIES if categories is None else categories
        levels = DEFAULT_JJIT_EXPERIENCE_LEVELS if experience_levels is None else experience_levels
        filter_params: list[tuple[str, Any]] = [
            *[("categories", cat) for cat in cats],
            *[("experienceLevels", lvl) for lvl in levels],
        ]

        try:
            all_items: list[dict[str, Any]] = []
            seen_slugs: set[str] = set()
            cursor: int | None = 0

            for _ in range(max_pages):
                if cursor is None:
                    break
                page_params = [*filter_params, ("from", cursor), ("itemsCount", items_count)]
                payload = await self.fetch_json(
                    JUSTJOIN_CANDIDATE_OFFERS_URL,
                    params=page_params,
                    headers=JUSTJOIN_HEADERS,
                )
                items = self._extract_list_items(payload)
                if not items:
                    break
                new_count = 0
                for item in items:
                    slug = str(item.get("slug") or item.get("id") or "")
                    if slug and slug not in seen_slugs:
                        seen_slugs.add(slug)
                        all_items.append(item)
                        new_count += 1
                if len(items) < items_count or new_count == 0:
                    break
                meta = payload.get("meta") if isinstance(payload, dict) else None
                next_meta = meta.get("next") if isinstance(meta, dict) else None
                if isinstance(next_meta, dict) and next_meta.get("cursor") is not None:
                    cursor = int(next_meta["cursor"])
                else:
                    break

            if all_items:
                return all_items, JUSTJOIN_CANDIDATE_OFFERS_URL
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {403, 404, 410, 503}:
                raise

        v2_headers = {**JUSTJOIN_HEADERS, "Version": "2"}
        all_items = []
        page = 1
        while page <= max_pages:
            payload = await self.fetch_json(
                JUSTJOIN_V2_OFFERS_URL,
                params={"page": page, "sortBy": "published", "orderBy": "DESC", "perPage": 100},
                headers=v2_headers,
            )
            items = self._extract_list_items(payload)
            if not items:
                break
            all_items.extend(items)
            meta = payload.get("meta") if isinstance(payload, dict) else None
            total_pages = int(meta.get("totalPages") or 1) if isinstance(meta, dict) else 1
            if page >= total_pages:
                break
            page += 1
        return all_items, JUSTJOIN_V2_OFFERS_URL

    async def _fetch_detail(self, slug: str, base_list_url: str) -> dict[str, Any] | None:
        detail_url = f"{base_list_url.rstrip('/')}/{slug}"
        headers = {**JUSTJOIN_HEADERS, "Version": "2"} if "api.justjoin.it/v2" in detail_url else JUSTJOIN_HEADERS
        try:
            payload = await self.fetch_json(detail_url, headers=headers)
            if isinstance(payload, dict):
                if isinstance(payload.get("data"), dict):
                    return payload["data"]
                return payload
        except httpx.HTTPStatusError:
            return None
        return None

    async def fetch_offers(
        self,
        stage1_filter: Stage1Filter | None = None,
        db: DatabaseManager | None = None,
        seen_fingerprints: set[str] | None = None,
        max_detail_fetches: int | None = None,
    ) -> list[JobOffer]:
        """Fetch list items, pre-filter metadata, and politely fetch details for survivors."""
        raw_items, base_list_url = await self._fetch_list_payload()
        self.last_total_fetched = 0
        self.last_duplicates_skipped = 0
        results: list[JobOffer] = []
        survivors_to_fetch: list[tuple[dict[str, Any], JobOffer]] = []

        for raw in raw_items:
            offer = self.normalize_offer(raw)
            if offer is None:
                continue

            self.last_total_fetched += 1

            if db is not None:
                is_dup, _ = db.is_duplicate(offer, seen_fingerprints=seen_fingerprints)
                if is_dup:
                    self.last_duplicates_skipped += 1
                    continue

            if stage1_filter is not None:
                lightweight_reason = stage1_filter.filter_lightweight(offer)
                if lightweight_reason is not None:
                    offer.metadata_rejected_reason = lightweight_reason
                    results.append(offer)
                    continue

            if max_detail_fetches is not None and len(survivors_to_fetch) >= max_detail_fetches:
                continue

            survivors_to_fetch.append((raw, offer))

        if survivors_to_fetch:
            sem = asyncio.Semaphore(5)
            use_jitter = len(survivors_to_fetch) > 1

            async def _enrich(raw_item: dict[str, Any], base_offer: JobOffer) -> JobOffer:
                async with sem:
                    if use_jitter:
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                    detail_raw = await self._fetch_detail(base_offer.external_id, base_list_url)
                    if detail_raw is not None:
                        enriched = self.normalize_offer({**raw_item, **detail_raw})
                        if enriched is not None:
                            return enriched
                    return base_offer

            enriched_offers = await asyncio.gather(
                *(_enrich(raw_item, base_offer) for raw_item, base_offer in survivors_to_fetch)
            )
            results.extend(enriched_offers)

        return results
