"""Async job portal scrapers for SOLID.Jobs and Just Join IT."""

from job_scout.scrapers.justjoin import JustJoinScraper
from job_scout.scrapers.solid_jobs import SolidJobsScraper

__all__ = ["JustJoinScraper", "SolidJobsScraper"]
