"""Async scraper for the:protocol using Next.js SSR __NEXT_DATA__ pages."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

import httpx

from job_scout.models import JobOffer, SkillItem, WorkplaceType
from job_scout.scrapers.base import BaseScraper

if TYPE_CHECKING:
    from job_scout.config import ProfileConfig
    from job_scout.database import DatabaseManager
    from job_scout.filter import Stage1Filter

THEPROTOCOL_BASE_URL = "https://theprotocol.it"
THEPROTOCOL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

DEFAULT_THEPROTOCOL_SPECIALIZATIONS = ["backend", "ai-ml", "data-analytics-and-bi"]

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

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

_LEVEL_MAP = {
    "intern": "trainee",
    "trainee": "trainee",
    "assistant": "assistant",
    "junior": "junior",
    "mid": "mid",
    "regular": "mid",
    "medium": "mid",
    "senior": "senior",
    "expert": "expert",
    "lead": "lead",
}

_CONTRACT_ID_LABELS = {
    0: "UoP",
    1: "UoD",
    2: "UZ",
    3: "B2B",
    7: "Staż",
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
    """Parse Next.js __NEXT_DATA__ JSON payload from a the:protocol SSR HTML page."""
    match = _NEXT_DATA_RE.search(html_text)
    if not match:
        low = html_text.lower()
        if any(tok in low for tok in ("cf-browser-verification", "just a moment", "turnstile")):
            raise RuntimeError("the:protocol zwrócił blokadę antybotową / Cloudflare challenge")
        raise ValueError("the:protocol HTML does not contain __NEXT_DATA__ payload")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise ValueError("the:protocol __NEXT_DATA__ payload is not a JSON object")
    return payload


def _normalize_currency(raw_cur: Any) -> str:
    cur = str(raw_cur or "").strip().upper()
    if cur in {"ZŁ", "ZL", "PLN"}:
        return "PLN"
    return cur or "PLN"


def _unit_to_monthly(value: float, time_unit: Any) -> int:
    unit_text = ""
    if isinstance(time_unit, dict):
        unit_text = f"{time_unit.get('shortForm') or ''} {time_unit.get('longForm') or ''}"
    elif isinstance(time_unit, str):
        unit_text = time_unit
    low = unit_text.strip().lower()
    if "godz" in low or "hour" in low:
        return int(round(value * 168))
    if "dzie" in low or "day" in low:
        return int(round(value * 21))
    if "rok" in low or "rocz" in low or "year" in low:
        return int(round(value / 12.0))
    return int(round(value))


def _extract_salary(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[int | None, int | None, list[str]]:
    """Extract monthly PLN salary bounds and contract notes from the:protocol offer."""
    mins: list[int] = []
    maxs: list[int] = []
    notes: list[str] = []

    contracts: list[dict[str, Any]] = []
    if isinstance(detail_raw, dict):
        emp = (detail_raw.get("attributes") or {}).get("employment")
        if isinstance(emp, dict) and isinstance(emp.get("typesOfContracts"), list):
            contracts = [c for c in emp["typesOfContracts"] if isinstance(c, dict)]
    if not contracts and isinstance(list_raw.get("typesOfContracts"), list):
        contracts = [c for c in list_raw["typesOfContracts"] if isinstance(c, dict)]

    for contract in contracts:
        sal = contract.get("salary")
        if not isinstance(sal, dict):
            continue
        lo = sal.get("from")
        hi = sal.get("to")
        if lo is None and hi is None:
            continue
        lo_f = float(lo) if lo is not None else None
        hi_f = float(hi) if hi is not None else lo_f
        cur = _normalize_currency(sal.get("currencyCode") or sal.get("currencySymbol") or sal.get("currency"))
        tunit = sal.get("timeUnit")
        unit_label = (
            tunit.get("shortForm") if isinstance(tunit, dict) and tunit.get("shortForm") else "mies."
        )
        c_label = contract.get("name") or _CONTRACT_ID_LABELS.get(contract.get("id"), "Umowa")
        kind = sal.get("kindName") or sal.get("kindCode") or ""
        notes.append(
            f"{c_label}: {int(lo_f) if lo_f is not None else '?'}–"
            f"{int(hi_f) if hi_f is not None else '?'} {cur}/{unit_label}"
            + (f" ({kind})" if kind else "")
        )
        if cur == "PLN":
            if lo_f is not None:
                mins.append(_unit_to_monthly(lo_f, tunit))
            if hi_f is not None:
                maxs.append(_unit_to_monthly(hi_f, tunit))

    return (min(mins) if mins else None), (max(maxs) if maxs else None), notes


def _extract_workplace_and_locations(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> tuple[list[str], WorkplaceType]:
    """Normalize locations and work mode ('remote' | 'hybrid' | 'onsite') for the:protocol."""
    locations: list[str] = []
    seen: set[str] = set()

    raw_places: list[Any] = []
    if isinstance(detail_raw, dict):
        attrs = detail_raw.get("attributes")
        if isinstance(attrs, dict) and isinstance(attrs.get("workplaces"), list):
            raw_places = attrs["workplaces"]
    if not raw_places and isinstance(list_raw.get("workplace"), list):
        raw_places = list_raw["workplace"]

    for wp in raw_places:
        if not isinstance(wp, dict):
            continue
        city = wp.get("city") or wp.get("location")
        if isinstance(city, str) and city.strip():
            # Trim district suffix after comma if only location was set
            clean = city.split(",")[0].strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                locations.append(clean)

    modes: list[str] = []
    if isinstance(detail_raw, dict):
        emp = (detail_raw.get("attributes") or {}).get("employment")
        if isinstance(emp, dict) and isinstance(emp.get("detailedWorkModes"), list):
            for wm in emp["detailedWorkModes"]:
                if isinstance(wm, dict):
                    modes.append(f"{wm.get('code') or ''} {wm.get('name') or ''}".lower())
    for wm in list_raw.get("workModes") or []:
        if isinstance(wm, str):
            modes.append(wm.lower())

    joined = " ".join(modes)
    if any(tok in joined for tok in ("remote", "zdaln", "home-office")):
        workplace: WorkplaceType = "remote"
    elif any(tok in joined for tok in ("hybrid", "hybryd")):
        workplace = "hybrid"
    else:
        workplace = "onsite"

    return locations, workplace


def _extract_skills(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None = None,
) -> list[SkillItem]:
    """Extract required ('must') and optional ('nice') technologies."""
    skills: list[SkillItem] = []
    seen: set[str] = set()

    def _add(name: Any, level: str | None = None) -> None:
        if not isinstance(name, str) or not name.strip():
            return
        cleaned = name.strip().rstrip(".")
        low = cleaned.lower()
        if cleaned and low not in seen:
            seen.add(low)
            skills.append(SkillItem(name=cleaned, level=level))

    if isinstance(detail_raw, dict):
        techs = detail_raw.get("technologies")
        if isinstance(techs, dict):
            for item in techs.get("expected") or []:
                _add(item.get("name") if isinstance(item, dict) else item, level="must")
            for item in techs.get("optional") or []:
                _add(item.get("name") if isinstance(item, dict) else item, level="nice")

    for tech in list_raw.get("technologies") or []:
        _add(tech, level="must")

    return skills


def _build_description(
    list_raw: dict[str, Any],
    detail_raw: dict[str, Any] | None,
    salary_notes: list[str],
) -> str:
    """Build plain-text description from detail textSections or list aboutProject."""
    parts: list[str] = []
    if salary_notes:
        parts.append("Widełki / formy zatrudnienia: " + "; ".join(salary_notes))

    added_detail_sections = False
    if isinstance(detail_raw, dict) and isinstance(detail_raw.get("textSections"), list):
        for sec in detail_raw["textSections"]:
            if not isinstance(sec, dict):
                continue
            sec_type = str(sec.get("type") or "")
            if sec_type.startswith("technologies"):
                continue
            heading = _SECTION_HEADINGS.get(sec_type)
            elements = sec.get("elements")
            if isinstance(elements, list) and elements:
                body = "\n".join(f"- {str(e).strip()}" for e in elements if str(e).strip())
            else:
                body = str(sec.get("plainText") or "").strip()
            if body:
                added_detail_sections = True
                parts.append(f"{heading}:\n{body}" if heading else body)

    if not added_detail_sections:
        about = list_raw.get("aboutProject")
        if isinstance(about, list):
            text = "\n".join(str(p).strip() for p in about if str(p).strip())
            if text:
                parts.append(text)

    return "\n\n".join(parts).strip()


class TheProtocolScraper(BaseScraper):
    """Fetches IT job offers from the:protocol SSR __NEXT_DATA__ pages over httpx."""

    source_name = "theprotocol"
    aliases = ("the:protocol", "theprotocol.it", "protocol")
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
    def build_search_urls(profile: ProfileConfig | None) -> list[str]:
        """Build targeted the:protocol listing URLs (city + remote queries)."""
        specs = ",".join(DEFAULT_THEPROTOCOL_SPECIALIZATIONS)
        base_segments = [f"{specs};sp"]

        levels: list[str] = []
        if profile is not None:
            raw_levels = profile.preferences.get("target_levels")
            if isinstance(raw_levels, list):
                seen_lvls: set[str] = set()
                for lvl in raw_levels:
                    mapped = _LEVEL_MAP.get(str(lvl).strip().lower())
                    if mapped and mapped not in seen_lvls:
                        seen_lvls.add(mapped)
                        levels.append(mapped)
        if levels:
            base_segments.append(f"{','.join(levels)};p")

        urls: list[str] = []
        cities: list[str] = []
        if profile is not None:
            seen_cities: set[str] = set()
            for raw_city in profile.allowed_cities:
                slug = _CITY_SLUG_MAP.get(raw_city.strip().lower(), raw_city.strip().lower())
                if slug and slug not in seen_cities:
                    seen_cities.add(slug)
                    cities.append(slug)

        allowed_wp = (
            {w.strip().lower() for w in profile.allowed_workplace_types}
            if profile is not None
            else {"remote", "hybrid"}
        )

        if cities:
            city_segs = [*base_segments, f"{','.join(cities)};wp"]
            urls.append(f"{THEPROTOCOL_BASE_URL}/filtry/{'/'.join(city_segs)}")
        if not allowed_wp or "remote" in allowed_wp:
            remote_segs = [*base_segments, "zdalna;rw"]
            urls.append(f"{THEPROTOCOL_BASE_URL}/filtry/{'/'.join(remote_segs)}")

        if not urls:
            urls.append(f"{THEPROTOCOL_BASE_URL}/filtry/{'/'.join(base_segments)}")
        return urls

    @staticmethod
    def parse_list_html(html_text: str) -> tuple[list[dict[str, Any]], int, int]:
        """Extract (offers, total_pages, total_count) from the:protocol listing SSR HTML."""
        next_data = extract_next_data(html_text)
        page_props = next_data.get("props", {}).get("pageProps", {})
        resp = page_props.get("offersResponse") or {}
        offers = [o for o in (resp.get("offers") or []) if isinstance(o, dict)]
        page_info = resp.get("page") if isinstance(resp.get("page"), dict) else {}
        total_pages = int(page_info.get("count") or 1)
        total_count = int(resp.get("offersCount") or len(offers))
        return offers, max(1, total_pages), total_count

    @staticmethod
    def parse_detail_html(html_text: str) -> dict[str, Any] | None:
        """Extract offer detail dict from the:protocol detail SSR HTML."""
        next_data = extract_next_data(html_text)
        page_props = next_data.get("props", {}).get("pageProps", {})
        offer = page_props.get("offer")
        return offer if isinstance(offer, dict) else None

    def normalize_offer(
        self,
        list_raw: dict[str, Any],
        detail_raw: dict[str, Any] | None = None,
    ) -> JobOffer | None:
        """Convert the:protocol list item (+ optional detail object) into a unified JobOffer."""
        offer_id = str((detail_raw or {}).get("id") or list_raw.get("id") or "").strip()
        attrs = (detail_raw or {}).get("attributes") if isinstance(detail_raw, dict) else None
        det_title = (
            (attrs.get("title") or {}).get("value")
            if isinstance(attrs, dict) and isinstance(attrs.get("title"), dict)
            else None
        )
        title = str(det_title or list_raw.get("title") or "").strip()
        if not offer_id or not title:
            return None

        det_employer = (
            (attrs.get("employer") or {}).get("name")
            if isinstance(attrs, dict) and isinstance(attrs.get("employer"), dict)
            else None
        )
        company = str(det_employer or list_raw.get("employer") or "Unknown").strip() or "Unknown"

        offer_url_name = str(
            (detail_raw or {}).get("offerUrlName") or list_raw.get("offerUrlName") or ""
        ).strip()
        detail_url = str((detail_raw or {}).get("offerUrl") or "").strip()
        if detail_url:
            url = detail_url
        elif offer_url_name.startswith("http"):
            url = offer_url_name
        elif offer_url_name:
            url = f"{THEPROTOCOL_BASE_URL}/szczegoly/praca/{offer_url_name.lstrip('/')}"
        else:
            url = f"{THEPROTOCOL_BASE_URL}/szczegoly/praca/oferta,{offer_id}"

        locations, workplace_type = _extract_workplace_and_locations(list_raw, detail_raw)
        sal_min, sal_max, salary_notes = _extract_salary(list_raw, detail_raw)
        skills = _extract_skills(list_raw, detail_raw)
        description = _build_description(list_raw, detail_raw, salary_notes)

        raw_levels: list[str] = []
        if isinstance(attrs, dict):
            emp = attrs.get("employment")
            if isinstance(emp, dict) and isinstance(emp.get("positionLevelIds"), list):
                raw_levels = [str(v).strip() for v in emp["positionLevelIds"] if str(v).strip()]
        if not raw_levels and isinstance(list_raw.get("positionLevels"), list):
            for entry in list_raw["positionLevels"]:
                if isinstance(entry, dict) and entry.get("value"):
                    raw_levels.append(str(entry["value"]).strip())
                elif isinstance(entry, str) and entry.strip():
                    raw_levels.append(entry.strip())
        experience_level = "/".join(lvl.capitalize() for lvl in raw_levels) if raw_levels else None

        pub_raw = str(list_raw.get("publicationDateUtc") or "").strip()
        if pub_raw and not (pub_raw.endswith("Z") or "+" in pub_raw[10:]):
            pub_raw = f"{pub_raw}Z"
        published_at = pub_raw or None

        return JobOffer(
            external_id=offer_id,
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
            html_text = await self.fetch_text(offer_url, headers=THEPROTOCOL_HEADERS)
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
        """Paginate targeted the:protocol SSR listings, pre-filter metadata, and fetch details."""
        pages_limit = self.max_pages if max_pages is None else max_pages
        search_urls = self.build_search_urls(profile)

        raw_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for query_idx, base_url in enumerate(search_urls):
            for page in range(1, pages_limit + 1):
                if query_idx > 0 or page > 1:
                    await self.polite_sleep(min_factor=0.4, max_factor=0.8)
                params = {"pageNumber": page} if page > 1 else None
                html_text = await self.fetch_text(
                    base_url, params=params, headers=THEPROTOCOL_HEADERS
                )
                page_offers, total_pages, _ = self.parse_list_html(html_text)
                if not page_offers:
                    break

                new_on_page = 0
                for raw in page_offers:
                    oid = str(raw.get("id") or "").strip()
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        raw_items.append(raw)
                        new_on_page += 1

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
