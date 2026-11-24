"""Registry of available job portal scrapers and CLI source name resolution."""

from __future__ import annotations

from collections import OrderedDict

from job_scout.scrapers.base import BaseScraper
from job_scout.scrapers.bulldogjob import BulldogjobScraper
from job_scout.scrapers.justjoin import JustJoinScraper
from job_scout.scrapers.nofluffjobs import NoFluffJobsScraper
from job_scout.scrapers.pracuj import PracujITScraper
from job_scout.scrapers.solid_jobs import SolidJobsScraper
from job_scout.scrapers.theprotocol import TheProtocolScraper

SCRAPER_CLASSES: list[type[BaseScraper]] = [
    SolidJobsScraper,
    JustJoinScraper,
    NoFluffJobsScraper,
    BulldogjobScraper,
    TheProtocolScraper,
    PracujITScraper,
]


def get_scraper_registry() -> OrderedDict[str, type[BaseScraper]]:
    """Return an ordered mapping of canonical source_name -> scraper class."""
    return OrderedDict((cls.source_name, cls) for cls in SCRAPER_CLASSES)


def list_sources() -> list[str]:
    """Return canonical names of all registered scrapers."""
    return [cls.source_name for cls in SCRAPER_CLASSES]


def resolve_source_name(name: str, registry: dict[str, type[BaseScraper]] | None = None) -> str:
    """Resolve a user-supplied source name or alias to its canonical source_name."""
    cleaned = name.strip().lower()
    if not cleaned:
        raise ValueError("Nazwa źródła nie może być pusta.")

    reg = registry if registry is not None else get_scraper_registry()
    for canonical, cls in reg.items():
        if cleaned == canonical.lower():
            return canonical
        if any(cleaned == alias.lower() for alias in getattr(cls, "aliases", ())):
            return canonical

    available = ", ".join(reg.keys())
    raise ValueError(f"Nieznane źródło '{name}'. Dostępne portale: {available}")
