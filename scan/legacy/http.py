from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from scan.legacy.config import Settings


logger = logging.getLogger(__name__)


class HttpClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def close(self) -> None:
        # Kept for compatibility with older call sites.
        return

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        retries = self._settings.max_retries
        backoff = self._settings.retry_backoff_seconds

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
                    response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                if "application/json" not in response.headers.get("Content-Type", "").lower():
                    return None
                return response.json()
            except httpx.HTTPStatusError as exc:
                logger.error("HTTP GET failed url=%s error=%s", url, exc)
                # 404 = product not found, 429 = rate limited — no point retrying
                if exc.response.status_code in (404, 429):
                    return None
                if attempt >= retries:
                    return None
                sleep_seconds = backoff * (2 ** attempt)
                await asyncio.sleep(sleep_seconds)
            except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
                if attempt >= retries:
                    logger.error("HTTP GET failed url=%s error=%s", url, exc)
                    return None
                sleep_seconds = backoff * (2 ** attempt)
                await asyncio.sleep(sleep_seconds)

        return None

    async def post_json(
        self,
        url: str,
        *,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        retries = self._settings.max_retries
        backoff = self._settings.retry_backoff_seconds

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._settings.request_timeout_seconds) as client:
                    response = await client.post(url, json=json_body, headers=headers)
                response.raise_for_status()
                if "application/json" not in response.headers.get("Content-Type", "").lower():
                    return None
                return response.json()
            except httpx.HTTPStatusError as exc:
                logger.error("HTTP POST failed url=%s error=%s", url, exc)
                if exc.response.status_code in (404, 429):
                    return None
                if attempt >= retries:
                    return None
                sleep_seconds = backoff * (2 ** attempt)
                await asyncio.sleep(sleep_seconds)
            except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
                if attempt >= retries:
                    logger.error("HTTP POST failed url=%s error=%s", url, exc)
                    return None
                sleep_seconds = backoff * (2 ** attempt)
                await asyncio.sleep(sleep_seconds)

        return None
