"""Async scraper for the SOLID.Jobs public REST API."""

from __future__ import annotations

from typing import Any

from job_scout.models import JobOffer, SkillItem, WorkplaceType
from job_scout.scrapers.base import BaseScraper, strip_html

SOLID_JOBS_API_URL = "https://solid.jobs/public-api/offers/IT"
DEFAULT_CAMPAIGN = "job-scout"


def _to_monthly_pln(salary_obj: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """Extract monthly PLN salary bounds from a SOLID.Jobs salary object."""
    if not isinstance(salary_obj, dict):
        return None, None

    currency = str(salary_obj.get("currency") or "PLN").upper()
    if currency != "PLN":
        return None, None

    sal_from = salary_obj.get("salaryFrom") or salary_obj.get("from") or salary_obj.get("min")
    sal_to = salary_obj.get("salaryTo") or salary_obj.get("to") or salary_obj.get("max")

    period = str(salary_obj.get("period") or salary_obj.get("salaryPeriod") or "Month").lower()
    multiplier = 168 if "hour" in period else 1

    min_val = int(float(sal_from) * multiplier) if sal_from is not None else None
    max_val = int(float(sal_to) * multiplier) if sal_to is not None else None
    return min_val, max_val


def _extract_salary(raw: dict[str, Any]) -> tuple[int | None, int | None]:
    """Normalize primary/secondary or flat salary fields into PLN bounds."""
    candidates: list[tuple[int | None, int | None]] = []

    for key in ("salaryPrimary", "salarySecondary", "salary", "salaryRange"):
        val = raw.get(key)
        if isinstance(val, dict):
            lo, hi = _to_monthly_pln(val)
            if lo is not None or hi is not None:
                candidates.append((lo, hi))

    if not candidates:
        minimal = raw.get("minimalSalary")
        maximal = raw.get("maximalSalary")
        if minimal is not None or maximal is not None:
            candidates.append(
                (
                    int(float(minimal)) if minimal is not None else None,
                    int(float(maximal)) if maximal is not None else None,
                )
            )

    if not candidates:
        return None, None

    mins = [lo for lo, _ in candidates if lo is not None]
    maxs = [hi for _, hi in candidates if hi is not None]
    return (min(mins) if mins else None), (max(maxs) if maxs else None)


def _extract_locations_and_workplace(raw: dict[str, Any]) -> tuple[list[str], WorkplaceType]:
    """Extract city list and normalized workplace_type ('remote' | 'hybrid' | 'onsite')."""
    locations: list[str] = []
    raw_locs = raw.get("locations")
    if isinstance(raw_locs, list):
        for loc in raw_locs:
            if isinstance(loc, str) and loc.strip():
                locations.append(loc.strip())
            elif isinstance(loc, dict):
                city = loc.get("city") or loc.get("name") or loc.get("value")
                if city and str(city).strip():
                    locations.append(str(city).strip())
    elif isinstance(raw.get("city"), str) and raw["city"].strip():
        locations.append(raw["city"].strip())

    # Deduplicate preserving order
    deduped_locs: list[str] = []
    seen: set[str] = set()
    for loc in locations:
        low = loc.lower()
        if low not in seen:
            seen.add(low)
            deduped_locs.append(loc)

    raw_wp = str(raw.get("workplaceType") or raw.get("remoteWork") or "").lower()
    is_remote = bool(raw.get("isRemote") or raw.get("remote") or raw.get("remotePossible"))
    is_hybrid = bool(raw.get("isHybrid") or raw.get("hybrid"))
    has_remote_loc = any(
        token in loc.lower() for loc in deduped_locs for token in ("remote", "zdalnie", "zdalna")
    )

    if "hybrid" in raw_wp or "hybryd" in raw_wp or is_hybrid:
        workplace: WorkplaceType = "hybrid"
    elif "remote" in raw_wp or "zdaln" in raw_wp or is_remote or has_remote_loc:
        # If there are also physical cities and remote isn't 100%, check explicit flag
        if raw.get("remote") == "partially" or raw.get("isHybrid"):
            workplace = "hybrid"
        else:
            workplace = "remote"
    else:
        workplace = "onsite"

    clean_locs = [
        loc for loc in deduped_locs if loc.lower() not in {"remote", "zdalnie", "zdalna"}
    ]
    return clean_locs, workplace


def _extract_skills(raw: dict[str, Any]) -> list[SkillItem]:
    """Normalize skills list with proficiency levels/weights."""
    skills: list[SkillItem] = []
    raw_skills = raw.get("requiredSkills") or raw.get("skills") or []
    if not isinstance(raw_skills, list):
        return skills

    for item in raw_skills:
        if isinstance(item, str) and item.strip():
            skills.append(SkillItem(name=item.strip()))
        elif isinstance(item, dict):
            name = item.get("name") or item.get("skill") or item.get("technology")
            if name and str(name).strip():
                level = item.get("level") or item.get("weight") or item.get("experienceLevel")
                skills.append(SkillItem(name=str(name).strip(), level=level))
    return skills


class SolidJobsScraper(BaseScraper):
    """Fetches and normalizes IT job offers from SOLID.Jobs public REST API."""

    source_name = "solid.jobs"

    def normalize_offer(self, raw: dict[str, Any]) -> JobOffer | None:
        """Convert a raw SOLID.Jobs API item into a unified JobOffer."""
        ext_id = str(raw.get("jobOfferKey") or raw.get("id") or raw.get("key") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not ext_id or not title:
            return None

        company_val = raw.get("company")
        if isinstance(company_val, dict):
            company = str(company_val.get("name") or company_val.get("companyName") or "Unknown")
        else:
            company = str(company_val or raw.get("companyName") or "Unknown")
        company = company.strip() or "Unknown"

        url = str(
            raw.get("jobOfferUrl")
            or raw.get("url")
            or raw.get("offerUrl")
            or f"https://solid.jobs/offer/{ext_id}"
        ).strip()
        if url.startswith("/"):
            url = f"https://solid.jobs{url}"

        locations, workplace_type = _extract_locations_and_workplace(raw)
        sal_min, sal_max = _extract_salary(raw)
        skills = _extract_skills(raw)

        # Include category/subCategory as extra context if not already in skills
        for extra_field in ("subCategory", "category"):
            val = raw.get(extra_field)
            if isinstance(val, str) and val.strip():
                if not any(s.name.lower() == val.strip().lower() for s in skills):
                    skills.append(SkillItem(name=val.strip()))

        exp = raw.get("experienceLevel") or raw.get("seniority")
        exp_str = str(exp).strip() if exp is not None else None

        desc_raw = raw.get("description") or raw.get("body") or raw.get("requirements") or ""
        if isinstance(desc_raw, list):
            desc_raw = "\n".join(str(x) for x in desc_raw)
        description = strip_html(str(desc_raw))

        pub_raw = (
            raw.get("publishedAt")
            or raw.get("validFrom")
            or raw.get("createdAt")
            or raw.get("dateCreated")
        )
        published_at = str(pub_raw).strip() if pub_raw else None

        return JobOffer(
            external_id=ext_id,
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

    async def fetch_offers(self, max_pages: int = 5) -> list[JobOffer]:
        """Query SOLID.Jobs IT offers endpoint, supporting both list and paginated objects."""
        offers: list[JobOffer] = []
        seen_ids: set[str] = set()

        for page_idx in range(max_pages):
            params: dict[str, Any] = {"campaign": DEFAULT_CAMPAIGN}
            if page_idx > 0:
                params["pageIndex"] = page_idx

            payload = await self.fetch_json(SOLID_JOBS_API_URL, params=params)

            if isinstance(payload, list):
                raw_items = payload
                total_pages = 1
            elif isinstance(payload, dict):
                raw_items = (
                    payload.get("jobs")
                    or payload.get("offers")
                    or payload.get("data")
                    or payload.get("items")
                    or []
                )
                total_pages = int(payload.get("totalPages") or 1)
            else:
                break

            if not isinstance(raw_items, list) or not raw_items:
                break

            new_on_page = 0
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                offer = self.normalize_offer(item)
                if offer is not None and offer.external_id not in seen_ids:
                    seen_ids.add(offer.external_id)
                    offers.append(offer)
                    new_on_page += 1

            if page_idx + 1 >= total_pages or new_on_page == 0:
                break

        return offers
