"""Async scraper for Pracuj.pl (dział IT) using Next.js SSR __NEXT_DATA__ pages."""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import TYPE_CHECKING, Any

import httpx

from job_scout.models import JobOffer, SkillItem, WorkplaceType
from job_scout.scrapers.base import BaseScraper

if TYPE_CHECKING:
    from job_scout.config import ProfileConfig
    from job_scout.database import DatabaseManager
    from job_scout.filter import Stage1Filter

PRACUJ_IT_BASE_URL = "https://it.pracuj.pl/praca"
PRACUJ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

DEFAULT_PRACUJ_IT_SPECS = ["backend", "ai-ml", "big-data-science"]

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?")

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

# Pracuj.pl `et` query parameter IDs for experience/position levels
_ET_LEVEL_MAP = {
    "intern": "1",
    "trainee": "1",
    "assistant": "3",
    "junior": "17",
    "mid": "4",
    "regular": "4",
    "medium": "4",
    "senior": "18",
    "expert": "19",
    "lead": "5",
}

_SECTION_HEADINGS = {
    "about-project": "O projekcie",
    "responsibilities": "Zakres obowiązków",
    "requirements-expected": "Wymagania",
    "requirements-optional": "Mile widziane",
    "offered": "Oferujemy",
    "work-organization-work-style": "Organizacja pracy",
}


def extract_next_data(html_text: str) -> dict[str, Any]:
    """Parse Next.js __NEXT_DATA__ JSON payload from a Pracuj.pl SSR HTML page."""
    match = _NEXT_DATA_RE.search(html_text)
    if not match:
        low = html_text.lower()
        if any(tok in low for tok in ("cf-browser-verification", "just a moment", "attention required")):
            raise RuntimeError("Pracuj.pl zwrócił blokadę antybotową / Cloudflare challenge")
        raise ValueError("Pracuj.pl HTML does not contain __NEXT_DATA__ payload")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("Pracuj.pl __NEXT_DATA__ payload is not a JSON object")
    return payload


def _unit_to_monthly(value: float, unit_text: str) -> int:
    low = unit_text.strip().lower()
    if "godz" in low or "hour" in low or "/h" in low:
        return int(round(value * 168))
    if "dzie" in low or "day" in low:
        return int(round(value * 21))
    if "rok" in low or "rocz" in low or "year" in low:
        return int(round(value / 12.0))
    return int(round(value))


def _parse_salary_display_text(text: str | None) -> tuple[int | None, int | None]:
    """Parse Pracuj.pl list salaryDisplayText (e.g. '140–160 zł netto (+ VAT) / godz.')."""
    if not text or not text.strip():
        return None, None
    cleaned = text.replace("\u00a0", " ")
    low = cleaned.lower()
    if not ("zł" in low or "pln" in low):
        return None, None

    matches = _NUMBER_RE.findall(cleaned)
    nums: list[float] = []
    for raw_num in matches:
        normalized = raw_num.replace(" ", "").replace(",", ".")
        try:
            nums.append(float(normalized))
        except ValueError:
            continue
    if not nums:
        return None, None

    lo = nums[0]
    hi = nums[1] if len(nums) > 1 else nums[0]
    if lo > hi:
        lo, hi = hi, lo
    return _unit_to_monthly(lo, low), _unit_to_monthly(hi, low)


def _extract_salary(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[int | None, int | None, list[str]]:
    """Extract monthly PLN salary bounds and readable contract lines from Pracuj.pl."""
    mins: list[int] = []
    maxs: list[int] = []
    notes: list[str] = []

    if isinstance(detail_raw, dict):
        emp = (detail_raw.get("attributes") or {}).get("employment")
        contracts = emp.get("typesOfContracts") if isinstance(emp, dict) else None
        if isinstance(contracts, list):
            for contract in contracts:
                if not isinstance(contract, dict):
                    continue
                sal = contract.get("salary")
                if not isinstance(sal, dict):
                    continue
                lo = sal.get("from")
                hi = sal.get("to")
                if lo is None and hi is None:
                    continue
                lo_f = float(lo) if lo is not None else None
                hi_f = float(hi) if hi is not None else lo_f
                cur_obj = sal.get("currency")
                cur_raw = (
                    (cur_obj.get("code") or cur_obj.get("symbol"))
                    if isinstance(cur_obj, dict)
                    else cur_obj
                )
                cur = str(cur_raw or "PLN").strip().upper()
                if cur in {"ZŁ", "ZL"}:
                    cur = "PLN"
                tu = sal.get("timeUnit")
                unit_label = "mies."
                if isinstance(tu, dict):
                    sf = tu.get("shortForm")
                    lf = tu.get("longForm")
                    unit_label = (
                        (sf.get("name") if isinstance(sf, dict) else sf)
                        or (lf.get("name") if isinstance(lf, dict) else lf)
                        or "mies."
                    )
                kind_obj = sal.get("salaryKind")
                kind = (
                    kind_obj.get("name") if isinstance(kind_obj, dict) else str(kind_obj or "")
                ) or ""
                c_name = str(contract.get("name") or "Umowa").strip()
                notes.append(
                    f"{c_name}: {int(lo_f) if lo_f is not None else '?'}–"
                    f"{int(hi_f) if hi_f is not None else '?'} {cur}/{unit_label}"
                    + (f" ({kind})" if kind else "")
                )
                if cur == "PLN":
                    if lo_f is not None:
                        mins.append(_unit_to_monthly(lo_f, str(unit_label)))
                    if hi_f is not None:
                        maxs.append(_unit_to_monthly(hi_f, str(unit_label)))

    if not mins and not maxs:
        disp = str(list_raw.get("salaryDisplayText") or "").replace("\u00a0", " ").strip()
        if disp:
            if not notes:
                notes.append(disp)
            lo_pln, hi_pln = _parse_salary_display_text(disp)
            if lo_pln is not None:
                mins.append(lo_pln)
            if hi_pln is not None:
                maxs.append(hi_pln)

    return (min(mins) if mins else None), (max(maxs) if maxs else None), notes


def _normalize_position_level(raw_levels: list[str]) -> str | None:
    """Normalize verbose Polish Pracuj.pl position level labels into concise tags."""
    mapped: list[str] = []
    seen: set[str] = set()
    for raw in raw_levels:
        low = raw.lower()
        if "junior" in low or "młodszy" in low or "mlodszy" in low:
            tag = "Junior"
        elif "mid" in low or "regular" in low or ("specjalista" in low and "starszy" not in low):
            tag = "Mid"
        elif "senior" in low or "starszy" in low:
            tag = "Senior"
        elif "ekspert" in low or "expert" in low:
            tag = "Expert"
        elif "praktykant" in low or "staż" in low or "trainee" in low or "intern" in low:
            tag = "Intern"
        elif "lead" in low or "kierownik" in low or "manager" in low:
            tag = "Lead"
        else:
            tag = raw.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            mapped.append(tag)
    return "/".join(mapped) if mapped else None


def _extract_workplace_and_locations(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[list[str], WorkplaceType]:
    """Extract cities and normalized workplace_type from Pracuj.pl list + detail data."""
    locations: list[str] = []
    seen: set[str] = set()

    for sub in list_raw.get("offers") or []:
        if not isinstance(sub, dict):
            continue
        disp = sub.get("displayWorkplace")
        if isinstance(disp, str) and disp.strip():
            # e.g. "Warszawa, Praga-Południe" -> "Warszawa"
            city = disp.split(",")[0].strip()
            if city and city.lower() not in seen and city.lower() not in {"praca zdalna", "zdalna", "remote"}:
                seen.add(city.lower())
                locations.append(city)

    mode_tokens: list[str] = []
    if isinstance(detail_raw, dict):
        emp = (detail_raw.get("attributes") or {}).get("employment")
        if isinstance(emp, dict):
            if emp.get("entirelyRemoteWork") is True:
                mode_tokens.append("remote")
            for wm in emp.get("workModes") or []:
                if isinstance(wm, dict):
                    mode_tokens.append(f"{wm.get('code') or ''} {wm.get('name') or ''}".lower())

    for wm in list_raw.get("workModes") or []:
        if isinstance(wm, str):
            mode_tokens.append(wm.lower())
    if list_raw.get("isRemoteWorkAllowed") is True:
        mode_tokens.append("remote")

    joined = " ".join(mode_tokens)
    if any(tok in joined for tok in ("zdaln", "home-office", "remote")):
        workplace: WorkplaceType = "remote"
    elif any(tok in joined for tok in ("hybryd", "hybrid")):
        workplace = "hybrid"
    else:
        workplace = "onsite"

    return locations, workplace


def _extract_skills(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> list[SkillItem]:
    """Combine Pracuj.pl detail expected/optional technologies and list technologies."""
    skills: list[SkillItem] = []
    seen: set[str] = set()

    def _add(name: Any, level: str | None = None) -> None:
        if not isinstance(name, str):
            return
        cleaned = name.strip().rstrip(".,;")
        low = cleaned.lower()
        if cleaned and low not in seen:
            seen.add(low)
            skills.append(SkillItem(name=cleaned, level=level))

    if isinstance(detail_raw, dict) and isinstance(detail_raw.get("textSections"), list):
        for sec in detail_raw["textSections"]:
            if not isinstance(sec, dict):
                continue
            stype = str(sec.get("sectionType") or "")
            if stype == "technologies-expected":
                for elem in sec.get("textElements") or []:
                    _add(elem, level="must")
            elif stype == "technologies-optional":
                for elem in sec.get("textElements") or []:
                    _add(elem, level="nice")

    for tech in list_raw.get("technologies") or []:
        _add(tech, level="must")

    return skills


def _build_description(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None,
    salary_notes: list[str],
) -> str:
    """Assemble plain-text description from Pracuj.pl detail textSections or list snippets."""
    parts: list[str] = []
    if salary_notes:
        parts.append("Widełki / formy zatrudnienia: " + "; ".join(salary_notes))

    added_detail = False
    if isinstance(detail_raw, dict) and isinstance(detail_raw.get("textSections"), list):
        for sec in detail_raw["textSections"]:
            if not isinstance(sec, dict):
                continue
            stype = str(sec.get("sectionType") or "")
            if stype.startswith("technologies"):
                continue
            heading = _SECTION_HEADINGS.get(stype)
            elems = sec.get("textElements")
            if isinstance(elems, list) and elems:
                body = "\n".join(f"- {str(e).strip()}" for e in elems if str(e).strip())
            else:
                body = str(sec.get("plainText") or "").strip()
            if body:
                added_detail = True
                parts.append(f"{heading}:\n{body}" if heading else body)

    if not added_detail:
        for key in ("aboutProjectShortDescription", "jobDescription"):
            snippet = str(list_raw.get(key) or "").strip()
            if snippet:
                parts.append(snippet)

    return "\n\n".join(parts).strip()


class PracujITScraper(BaseScraper):
    """Fetches targeted IT job offers from Pracuj.pl (it.pracuj.pl) SSR __NEXT_DATA__ pages."""

    source_name = "pracuj.pl"
    aliases = ("pracuj", "pracuj_pl", "it.pracuj.pl")
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
    def build_search_queries(profile: ProfileConfig | None) -> list[tuple[str, dict[str, Any]]]:
        """Build tightly scoped it.pracuj.pl queries (IT specs, Junior/Mid, Warszawa & remote)."""
        et_ids: list[str] = []
        if profile is not None:
            raw_levels = profile.preferences.get("target_levels")
            if isinstance(raw_levels, list):
                seen_et: set[str] = set()
                for lvl in raw_levels:
                    mapped = _ET_LEVEL_MAP.get(str(lvl).strip().lower())
                    if mapped and mapped not in seen_et:
                        seen_et.add(mapped)
                        et_ids.append(mapped)
        if not et_ids:
            # Default strictly to Junior (17) + Mid/Regular (4) to keep Pracuj.pl focused
            et_ids = ["17", "4"]

        common_params: dict[str, Any] = {
            "et": ",".join(et_ids),
            "its": ",".join(DEFAULT_PRACUJ_IT_SPECS),
        }

        cities: list[str] = []
        allowed_wp = (
            {w.strip().lower() for w in profile.allowed_workplace_types}
            if profile is not None
            else {"remote", "hybrid"}
        )
        if profile is not None:
            seen_cities: set[str] = set()
            for raw_city in profile.allowed_cities:
                slug = _CITY_SLUG_MAP.get(raw_city.strip().lower(), raw_city.strip().lower())
                if slug and slug not in seen_cities:
                    seen_cities.add(slug)
                    cities.append(slug)

        queries: list[tuple[str, dict[str, Any]]] = []
        if cities:
            city_path = f"{PRACUJ_IT_BASE_URL}/{','.join(cities)};wp"
            city_params = dict(common_params)
            wm_list: list[str] = []
            if "remote" in allowed_wp:
                wm_list.append("home-office")
            if "hybrid" in allowed_wp:
                wm_list.append("hybrid")
            if "onsite" in allowed_wp:
                wm_list.append("full-office")
            if wm_list and len(wm_list) < 3:
                city_params["wm"] = ",".join(wm_list)
            queries.append((city_path, city_params))

        if not allowed_wp or "remote" in allowed_wp:
            remote_params = {**common_params, "wm": "home-office"}
            queries.append((PRACUJ_IT_BASE_URL, remote_params))

        if not queries:
            queries.append((PRACUJ_IT_BASE_URL, common_params))

        return queries

    @staticmethod
    def parse_list_html(html_text: str) -> tuple[list[dict[str, Any]], int, int]:
        """Extract (grouped_offers, total_count, per_page) from Pracuj.pl listing SSR HTML."""
        next_data = extract_next_data(html_text)
        page_props = next_data.get("props", {}).get("pageProps", {})
        dehydrated = page_props.get("dehydratedState") or {}
        queries = dehydrated.get("queries") if isinstance(dehydrated, dict) else []
        search_criteria = page_props.get("searchCriteria") or {}
        per_page = int(search_criteria.get("rop") or 50)

        for query in queries:
            if not isinstance(query, dict):
                continue
            data = (query.get("state") or {}).get("data")
            if isinstance(data, dict) and isinstance(data.get("groupedOffers"), list):
                offers = [o for o in data["groupedOffers"] if isinstance(o, dict)]
                total = int(data.get("groupedOffersTotalCount") or len(offers))
                return offers, total, max(1, per_page)

        return [], 0, max(1, per_page)

    @staticmethod
    def parse_detail_html(html_text: str) -> dict[str, Any] | None:
        """Extract offer detail payload from Pracuj.pl detail page SSR HTML."""
        next_data = extract_next_data(html_text)
        page_props = next_data.get("props", {}).get("pageProps", {})
        dehydrated = page_props.get("dehydratedState") or {}
        queries = dehydrated.get("queries") if isinstance(dehydrated, dict) else []

        for query in queries:
            if not isinstance(query, dict):
                continue
            data = (query.get("state") or {}).get("data")
            if isinstance(data, dict) and ("textSections" in data or "sections" in data or "attributes" in data):
                return data
        return None

    def normalize_offer(
        self,
        list_raw: dict[str, Any],
        detail_raw: dict[str, Any] | None = None,
    ) -> JobOffer | None:
        """Convert Pracuj.pl groupedOffer (+ optional detail object) into a unified JobOffer."""
        sub_offers = list_raw.get("offers") if isinstance(list_raw.get("offers"), list) else []
        first_sub = sub_offers[0] if sub_offers and isinstance(sub_offers[0], dict) else {}

        ext_id = str(
            (detail_raw or {}).get("jobOfferWebId")
            or first_sub.get("partitionId")
            or list_raw.get("commonOfferId")
            or list_raw.get("groupId")
            or ""
        ).strip()
        attrs = (detail_raw or {}).get("attributes") if isinstance(detail_raw, dict) else None
        title = str(
            (attrs.get("jobTitle") if isinstance(attrs, dict) else None)
            or list_raw.get("jobTitle")
            or ""
        ).strip()
        if not ext_id or not title:
            return None

        company = str(
            (attrs.get("displayEmployerName") if isinstance(attrs, dict) else None)
            or list_raw.get("companyName")
            or "Unknown"
        ).strip() or "Unknown"

        url = str(
            (attrs.get("offerAbsoluteUrl") if isinstance(attrs, dict) else None)
            or first_sub.get("offerAbsoluteUri")
            or f"https://www.pracuj.pl/praca/oferta,{ext_id}"
        ).strip()

        locations, workplace_type = _extract_workplace_and_locations(list_raw, detail_raw)
        sal_min, sal_max, salary_notes = _extract_salary(list_raw, detail_raw)
        skills = _extract_skills(list_raw, detail_raw)
        description = _build_description(list_raw, detail_raw, salary_notes)

        raw_levels: list[str] = []
        if isinstance(attrs, dict):
            emp = attrs.get("employment")
            if isinstance(emp, dict) and isinstance(emp.get("positionLevels"), list):
                for lvl in emp["positionLevels"]:
                    if isinstance(lvl, dict) and lvl.get("name"):
                        raw_levels.append(str(lvl["name"]))
        if not raw_levels and isinstance(list_raw.get("positionLevels"), list):
            raw_levels = [str(lvl) for lvl in list_raw["positionLevels"] if lvl]
        experience_level = _normalize_position_level(raw_levels)

        pub_det = (detail_raw or {}).get("publicationDetails") if isinstance(detail_raw, dict) else None
        pub_raw = (
            (pub_det.get("lastPublishedUtc") if isinstance(pub_det, dict) else None)
            or list_raw.get("lastPublicated")
            or list_raw.get("initialPublicated")
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
            experience_level=experience_level,
            salary_min_pln=sal_min,
            salary_max_pln=sal_max,
            skills=skills,
            description=description,
            published_at=published_at,
        )

    async def _fetch_detail(self, offer_url: str) -> dict[str, Any] | None:
        try:
            html_text = await self.fetch_text(offer_url, headers=PRACUJ_HEADERS)
            return self.parse_detail_html(html_text)
        except (httpx.HTTPStatusError, ValueError, RuntimeError, json.JSONDecodeError):
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
        """Paginate tightly scoped it.pracuj.pl SSR pages, pre-filter metadata, and enrich survivors."""
        pages_limit = self.max_pages if max_pages is None else max_pages
        queries = self.build_search_queries(profile)

        raw_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for query_idx, (base_url, base_params) in enumerate(queries):
            for page in range(1, pages_limit + 1):
                if query_idx > 0 or page > 1:
                    await self.polite_sleep(min_factor=0.4, max_factor=0.8)
                params = dict(base_params)
                if page > 1:
                    params["pn"] = page
                html_text = await self.fetch_text(base_url, params=params, headers=PRACUJ_HEADERS)
                page_offers, total_count, per_page = self.parse_list_html(html_text)
                if not page_offers:
                    break

                new_on_page = 0
                for raw in page_offers:
                    sub = raw.get("offers") if isinstance(raw.get("offers"), list) else []
                    first_id = sub[0].get("partitionId") if sub and isinstance(sub[0], dict) else None
                    oid = str(first_id or raw.get("groupId") or "").strip()
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        raw_items.append(raw)
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
                        await self.polite_sleep(min_factor=0.5, max_factor=1.4)
                    detail_raw = await self._fetch_detail(base_offer.url)
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
