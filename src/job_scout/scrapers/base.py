"""Base async scraper contract, HTTP transport with retry logic, and HTML utilities."""

from __future__ import annotations

import asyncio
import html
import random
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from job_scout.config import ProfileConfig
    from job_scout.database import DatabaseManager
    from job_scout.filter import Stage1Filter
    from job_scout.models import JobOffer

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; job-scout/0.1.0; +https://github.com/job-scout)"
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr)[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def strip_html(raw: str | None) -> str:
    """Convert raw HTML or markdown-ish fragments into clean readable plain text."""
    if not raw:
        return ""
    text = _BLOCK_TAG_RE.sub("\n", raw)
    text = _ANY_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [_MULTI_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return _MULTI_NEWLINE_RE.sub("\n\n", cleaned).strip()


class BaseScraper(ABC):
    """Unified async scraper base class with HTTP retries, politeness delay, and health counters."""

    source_name: str = ""
    aliases: tuple[str, ...] = ()
    default_max_pages: int = 5
    default_request_delay: float = 0.8
    default_timeout: float = 120.0

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        request_delay: float | None = None,
        max_pages: int | None = None,
    ) -> None:
        self._owns_client = client is None
        self.max_retries = max_retries
        self.request_delay = (
            self.default_request_delay if request_delay is None else max(0.0, request_delay)
        )
        self.max_pages = self.default_max_pages if max_pages is None else max_pages
        self.last_total_fetched: int = 0
        self.last_duplicates_skipped: int = 0
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
            },
        )

    async def __aenter__(self) -> BaseScraper:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP client if owned by this scraper instance."""
        if self._owns_client:
            await self.client.aclose()

    async def polite_sleep(self, *, min_factor: float = 0.6, max_factor: float = 1.4) -> None:
        """Sleep politely with jitter proportional to configured request_delay."""
        if self.request_delay <= 0:
            return
        low = max(0.0, self.request_delay * min_factor)
        high = max(low, self.request_delay * max_factor)
        await asyncio.sleep(random.uniform(low, high))

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request with exponential backoff on 429/5xx and network errors."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"Transient HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status is not None and status not in {429, 500, 502, 503, 504}:
                    raise
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(2**attempt)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"Failed to {method} {url}")

    async def fetch_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform GET request and decode JSON response."""
        response = await self._request_with_retry("GET", url, params=params, headers=headers)
        return response.json()

    async def post_json(
        self,
        url: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform POST request with JSON payload and decode JSON response."""
        response = await self._request_with_retry(
            "POST",
            url,
            params=params,
            json_body=json_body,
            headers=headers,
        )
        return response.json()

    async def fetch_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        """Perform GET request and return response text (HTML/XML/etc.)."""
        response = await self._request_with_retry("GET", url, params=params, headers=headers)
        return response.text

    @abstractmethod
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
        """Fetch and return normalized JobOffer objects from this portal."""
        raise NotImplementedError
