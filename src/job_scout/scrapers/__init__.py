"""Async job portal scrapers and central source registry."""

from job_scout.scrapers.base import BaseScraper
from job_scout.scrapers.bulldogjob import BulldogjobScraper
from job_scout.scrapers.justjoin import JustJoinScraper
from job_scout.scrapers.nofluffjobs import NoFluffJobsScraper
from job_scout.scrapers.pracuj import PracujITScraper
from job_scout.scrapers.registry import (
    SCRAPER_CLASSES,
    get_scraper_registry,
    list_sources,
    resolve_source_name,
)
from job_scout.scrapers.solid_jobs import SolidJobsScraper
from job_scout.scrapers.theprotocol import TheProtocolScraper

__all__ = [
    "BaseScraper",
    "BulldogjobScraper",
    "JustJoinScraper",
    "NoFluffJobsScraper",
    "PracujITScraper",
    "SCRAPER_CLASSES",
    "SolidJobsScraper",
    "TheProtocolScraper",
    "get_scraper_registry",
    "list_sources",
    "resolve_source_name",
]
