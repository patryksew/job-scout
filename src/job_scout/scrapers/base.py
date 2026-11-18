"""Base async HTTP client with retry logic and HTML stripping utilities."""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import httpx

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


class BaseScraper:
    """Shared async HTTP transport with retries and exponential backoff."""

    source_name: str = ""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        self._owns_client = client is None
        self.max_retries = max_retries
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

    async def fetch_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform GET request with exponential backoff on 429/5xx and network errors."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = await self.client.get(url, params=params, headers=headers)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"Transient HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response.json()
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
        raise RuntimeError(f"Failed to fetch JSON from {url}")
