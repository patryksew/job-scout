"""Async scraper for No Fluff Jobs public search & posting detail JSON API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from job_scout.models import JobOffer, SkillItem, WorkplaceType, compute_normalized_fingerprint
from job_scout.scrapers.base import BaseScraper, strip_html

if TYPE_CHECKING:
    from job_scout.config import ProfileConfig
    from job_scout.database import DatabaseManager
    from job_scout.filter import Stage1Filter

NFJ_SEARCH_URL = "https://nofluffjobs.com/api/search/posting"
NFJ_DETAIL_URL = "https://nofluffjobs.com/api/posting"
NFJ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://nofluffjobs.com",
    "Referer": "https://nofluffjobs.com/pl/",
}

DEFAULT_NFJ_CATEGORIES = ["backend", "artificial-intelligence", "data"]

_CITY_SLUG_MAP = {
    "warsaw": "warszawa",
    "warszawa": "warszawa",
    "krakow": "krakow",
    "kraków": "krakow",
    "cracow": "krakow",
    "wroclaw": "wroclaw",
    "wrocław": "wroclaw",
    "gdansk": "gdansk",
    "gdańsk": "gdansk",
    "poznan": "poznan",
    "poznań": "poznan",
    "lodz": "lodz",
    "łódź": "lodz",
    "katowice": "katowice",
}

_SENIORITY_MAP = {
    "intern": "trainee",
    "trainee": "trainee",
    "junior": "junior",
    "mid": "mid",
    "regular": "mid",
    "medium": "mid",
    "senior": "senior",
    "expert": "expert",
    "lead": "expert",
}


def _period_to_monthly(value: float, period: str) -> int:
    p = period.strip().lower()
    if "hour" in p:
        return int(round(value * 168))
    if "day" in p:
        return int(round(value * 21))
    if "year" in p or "ann" in p:
        return int(round(value / 12.0))
    return int(round(value))


def _epoch_ms_to_iso(raw_ts: Any) -> str | None:
    if isinstance(raw_ts, (int, float)) and raw_ts > 0:
        seconds = raw_ts / 1000.0 if raw_ts > 1e11 else float(raw_ts)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(raw_ts, str) and raw_ts.strip():
        return raw_ts.strip()
    return None


def _extract_locations_and_workplace(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[list[str], WorkplaceType]:
    """Normalize locations (filtering province-only entries) and workplace_type."""
    list_loc = list_raw.get("location") if isinstance(list_raw.get("location"), dict) else {}
    det_loc = (
        detail_raw.get("location")
        if isinstance(detail_raw, dict) and isinstance(detail_raw.get("location"), dict)
        else {}
    )

    places = det_loc.get("places") or list_loc.get("places") or []
    cities: list[str] = []
    seen: set[str] = set()
    has_remote_place = False

    if isinstance(places, list):
        for place in places:
            if not isinstance(place, dict) or place.get("provinceOnly") is True:
                continue
            city = place.get("city")
            if not isinstance(city, str) or not city.strip():
                continue
            city_clean = city.strip()
            if city_clean.lower() in {"remote", "zdalnie", "zdalna"}:
                has_remote_place = True
                continue
            if city_clean.lower() not in seen:
                seen.add(city_clean.lower())
                cities.append(city_clean)

    # List response carries canonical location.type ('remote'|'hybrid'|'onsite') & fullyRemote,
    # whereas detail response often omits location.type and uses numeric remote/hybridDesc.
    loc_type = str(list_loc.get("type") or det_loc.get("type") or "").strip().lower()
    fully_remote = bool(
        list_raw.get("fullyRemote")
        or list_loc.get("fullyRemote")
        or det_loc.get("fullyRemote")
    )
    hybrid_desc = str(list_loc.get("hybridDesc") or det_loc.get("hybridDesc") or "").strip()
    remote_days = det_loc.get("remote")

    if loc_type == "remote" or fully_remote:
        workplace: WorkplaceType = "remote"
    elif loc_type == "hybrid" or hybrid_desc:
        workplace = "hybrid"
    elif loc_type in {"onsite", "office", "stationary"}:
        workplace = "onsite"
    elif isinstance(remote_days, (int, float)) and remote_days >= 5:
        workplace = "remote"
    elif isinstance(remote_days, (int, float)) and 0 < remote_days < 5:
        workplace = "hybrid"
    elif has_remote_place and not cities:
        workplace = "remote"
    elif has_remote_place and cities:
        workplace = "hybrid"
    else:
        workplace = "onsite"

    return cities, workplace


def _extract_salary(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[int | None, int | None, list[str]]:
    """Extract monthly PLN salary bounds and human-readable UoP/B2B salary summary lines."""
    mins: list[int] = []
    maxs: list[int] = []
    contract_notes: list[str] = []

    if isinstance(detail_raw, dict):
        essentials = detail_raw.get("essentials")
        orig = essentials.get("originalSalary") if isinstance(essentials, dict) else None
        if isinstance(orig, dict):
            currency = str(orig.get("currency") or "PLN").upper()
            types_map = orig.get("types")
            if isinstance(types_map, dict):
                for c_type, c_info in types_map.items():
                    if not isinstance(c_info, dict):
                        continue
                    rng = c_info.get("range")
                    if not isinstance(rng, list) or not rng:
                        continue
                    lo_raw = float(rng[0]) if rng[0] is not None else None
                    hi_raw = float(rng[1]) if len(rng) > 1 and rng[1] is not None else lo_raw
                    period = str(c_info.get("period") or "Month")
                    label = {"permanent": "UoP", "b2b": "B2B", "mandate": "UZ"}.get(
                        c_type.lower(), c_type.upper()
                    )
                    if lo_raw is not None or hi_raw is not None:
                        contract_notes.append(
                            f"{label}: {int(lo_raw) if lo_raw is not None else '?'}–"
                            f"{int(hi_raw) if hi_raw is not None else '?'} {currency}/{period}"
                        )
                    if currency == "PLN":
                        if lo_raw is not None:
                            mins.append(_period_to_monthly(lo_raw, period))
                        if hi_raw is not None:
                            maxs.append(_period_to_monthly(hi_raw, period))

    # Fallback to list item salary (pre-converted to PLN/month by NFJ search API)
    if not mins and not maxs:
        sal = list_raw.get("salary")
        if isinstance(sal, dict):
            currency = str(sal.get("currency") or "PLN").upper()
            period = str(sal.get("period") or "Month")
            lo_raw = sal.get("from")
            hi_raw = sal.get("to")
            if currency == "PLN":
                if lo_raw is not None:
                    mins.append(_period_to_monthly(float(lo_raw), period))
                if hi_raw is not None:
                    maxs.append(_period_to_monthly(float(hi_raw), period))
            elif (lo_raw is not None or hi_raw is not None) and not contract_notes:
                c_type = str(sal.get("type") or "").upper() or "Salary"
                contract_notes.append(f"{c_type}: {lo_raw or '?'}–{hi_raw or '?'} {currency}/{period}")

    return (min(mins) if mins else None), (max(maxs) if maxs else None), contract_notes


def _extract_skills(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> list[SkillItem]:
    """Combine NFJ musts, nices, technology, and list tiles into SkillItems."""
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
        reqs = detail_raw.get("requirements")
        if isinstance(reqs, dict):
            for entry in reqs.get("musts") or []:
                if isinstance(entry, dict):
                    _add(entry.get("value"), level="must")
                elif isinstance(entry, str):
                    _add(entry, level="must")
            for entry in reqs.get("nices") or []:
                if isinstance(entry, dict):
                    _add(entry.get("value"), level="nice")
                elif isinstance(entry, str):
                    _add(entry, level="nice")
        basics = detail_raw.get("basics")
        if isinstance(basics, dict):
            _add(basics.get("technology"), level="must")

    _add(list_raw.get("technology"), level="must")

    tiles = list_raw.get("tiles")
    if isinstance(tiles, dict) and isinstance(tiles.get("values"), list):
        for tile in tiles["values"]:
            if not isinstance(tile, dict):
                continue
            val = tile.get("value")
            t_type = str(tile.get("type") or "").lower()
            if t_type == "requirement":
                _add(val, level="must")
            elif t_type == "category":
                _add(val)

    return skills


def _build_description(
    detail_raw: dict[str, Any] | None,
    contract_notes: list[str],
) -> str:
    """Assemble clean plain-text description from NFJ detail sections."""
    parts: list[str] = []
    if contract_notes:
        parts.append("Widełki / formy zatrudnienia: " + "; ".join(contract_notes))

    if not isinstance(detail_raw, dict):
        return "\n\n".join(parts)

    details = detail_raw.get("details")
    if isinstance(details, dict):
        quote = strip_html(details.get("quote"))
        if quote:
            parts.append(quote)
        proj = strip_html(details.get("projectDescription"))
        if proj:
            parts.append(f"Projekt:\n{proj}")
        desc = strip_html(details.get("description"))
        if desc:
            parts.append(desc)

    specs = detail_raw.get("specs")
    if isinstance(specs, dict):
        tasks = specs.get("dailyTasks")
        if isinstance(tasks, list):
            task_lines = [f"- {strip_html(str(t))}" for t in tasks if str(t).strip()]
            if task_lines:
                parts.append("Codzienne zadania:\n" + "\n".join(task_lines))

    reqs = detail_raw.get("requirements")
    if isinstance(reqs, dict):
        req_desc = strip_html(reqs.get("description"))
        if req_desc:
            parts.append(f"Wymagania:\n{req_desc}")

    return "\n\n".join(p for p in parts if p).strip()


class NoFluffJobsScraper(BaseScraper):
    """Fetches and normalizes IT job offers from No Fluff Jobs JSON search & detail APIs."""

    source_name = "nofluffjobs"
    aliases = ("nfj", "nofluff", "nofluffjobs.com", "no-fluff-jobs")
    default_max_pages = 5
    default_request_delay = 1.0
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
    def build_search_criteria(profile: ProfileConfig | None) -> dict[str, list[str]]:
        """Derive NFJ criteriaSearch filters from candidate profile.yaml."""
        criteria: dict[str, list[str]] = {
            "category": list(DEFAULT_NFJ_CATEGORIES),
        }
        if profile is None:
            return criteria

        cities: list[str] = []
        seen_cities: set[str] = set()
        for raw_city in profile.allowed_cities:
            slug = _CITY_SLUG_MAP.get(raw_city.strip().lower(), raw_city.strip().lower())
            if slug and slug not in seen_cities:
                seen_cities.add(slug)
                cities.append(slug)

        allowed_wp = {w.strip().lower() for w in profile.allowed_workplace_types}
        if (not allowed_wp or "remote" in allowed_wp) and "remote" not in seen_cities:
            cities.append("remote")
        if cities:
            criteria["city"] = cities

        raw_levels = profile.preferences.get("target_levels")
        if isinstance(raw_levels, list):
            seniorities: list[str] = []
            seen_sen: set[str] = set()
            for lvl in raw_levels:
                mapped = _SENIORITY_MAP.get(str(lvl).strip().lower())
                if mapped and mapped not in seen_sen:
                    seen_sen.add(mapped)
                    seniorities.append(mapped)
            if "junior" in seen_sen and "trainee" not in seen_sen:
                seniorities.insert(0, "trainee")
            if seniorities:
                criteria["seniority"] = seniorities

        return criteria

    def normalize_offer(
        self,
        list_raw: dict[str, Any],
        detail_raw: dict[str, Any] | None = None,
    ) -> JobOffer | None:
        """Convert NFJ search posting (+ optional detail JSON) into a unified JobOffer."""
        slug_id = str(
            (detail_raw or {}).get("postingUrl")
            or list_raw.get("url")
            or list_raw.get("id")
            or (detail_raw or {}).get("id")
            or ""
        ).strip()
        title = str((detail_raw or {}).get("title") or list_raw.get("title") or "").strip()
        if not slug_id or not title:
            return None

        comp_obj = (detail_raw or {}).get("company")
        if isinstance(comp_obj, dict) and comp_obj.get("name"):
            company = str(comp_obj["name"]).strip()
        else:
            company = str(list_raw.get("name") or "Unknown").strip() or "Unknown"

        url = f"https://nofluffjobs.com/pl/job/{slug_id.lstrip('/')}"
        locations, workplace_type = _extract_locations_and_workplace(list_raw, detail_raw)
        sal_min, sal_max, contract_notes = _extract_salary(list_raw, detail_raw)
        skills = _extract_skills(list_raw, detail_raw)
        description = _build_description(detail_raw, contract_notes)

        raw_sen = (
            ((detail_raw or {}).get("basics") or {}).get("seniority")
            if isinstance((detail_raw or {}).get("basics"), dict)
            else None
        ) or list_raw.get("seniority")
        if isinstance(raw_sen, list) and raw_sen:
            experience_level = "/".join(str(s).strip() for s in raw_sen if str(s).strip()) or None
        elif isinstance(raw_sen, str) and raw_sen.strip():
            experience_level = raw_sen.strip()
        else:
            experience_level = None

        published_at = _epoch_ms_to_iso(
            list_raw.get("renewed")
            or list_raw.get("posted")
            or (detail_raw or {}).get("posted")
        )

        # Prefer stable reference ID when available to collapse per-city listing clones
        ext_id = str(list_raw.get("reference") or slug_id).strip()

        return JobOffer(
            external_id=ext_id,
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

    async def _fetch_search_pages(
        self,
        criteria: dict[str, list[str]],
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Paginate POST /api/search/posting and deduplicate multi-city posting clones."""
        all_items: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        for page in range(1, max_pages + 1):
            if page > 1:
                await self.polite_sleep(min_factor=0.3, max_factor=0.7)
            payload = await self.post_json(
                NFJ_SEARCH_URL,
                params={
                    "salaryCurrency": "PLN",
                    "salaryPeriod": "month",
                    "region": "pl",
                    "page": page,
                },
                json_body={"criteriaSearch": criteria},
                headers=NFJ_HEADERS,
            )
            if not isinstance(payload, dict):
                break
            postings = payload.get("postings")
            if not isinstance(postings, list) or not postings:
                break

            new_on_page = 0
            for item in postings:
                if not isinstance(item, dict):
                    continue
                ref = str(item.get("reference") or "").strip()
                title = str(item.get("title") or "").strip()
                company = str(item.get("name") or "").strip()
                dedup_key = (
                    f"ref:{ref}"
                    if ref
                    else f"fp:{compute_normalized_fingerprint(company, title)}"
                )
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                all_items.append(item)
                new_on_page += 1

            total_pages = int(payload.get("totalPages") or 1)
            if page >= total_pages or new_on_page == 0:
                break

        return all_items

    async def _fetch_detail(self, posting_id: str) -> dict[str, Any] | None:
        detail_url = f"{NFJ_DETAIL_URL}/{posting_id.lstrip('/')}"
        try:
            payload = await self.fetch_json(detail_url, headers=NFJ_HEADERS)
            if isinstance(payload, dict):
                return payload
        except httpx.HTTPStatusError:
            return None
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
        """Search NFJ postings, run lightweight metadata filter, and fetch details politely."""
        pages_limit = self.max_pages if max_pages is None else max_pages
        criteria = self.build_search_criteria(profile)
        raw_items = await self._fetch_search_pages(criteria, pages_limit)

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
                        await self.polite_sleep(min_factor=0.5, max_factor=1.5)
                    detail_id = str(raw_item.get("url") or raw_item.get("id") or "").strip()
                    detail_raw = await self._fetch_detail(detail_id) if detail_id else None
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
