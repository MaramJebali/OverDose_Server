from __future__ import annotations

import logging

from scan.legacy.cache import SimpleTTLCache
from scan.legacy.config import Settings
from scan.legacy.http import HttpClient


logger = logging.getLogger(__name__)


class LensProvider:
    def __init__(self, http: HttpClient, settings: Settings, cache: SimpleTTLCache[dict]) -> None:
        self._http = http
        self._settings = settings
        self._cache = cache

    async def search_titles(
        self,
        image_url: str,
        max_results: int = 5,
        *,
        debug: dict[str, object] | None = None,
    ) -> list[str]:
        cache_key = f"lens::{image_url}::{max_results}"
        cached = self._cache.get(cache_key)
        if cached:
            titles = list(cached.get("titles", []))
            if debug is not None:
                debug["serpapi_status"] = "ok" if titles else "no_match"
                debug["serpapi_match_count"] = len(titles)
            return titles

        params = {
            "engine": "google_lens",
            "url": image_url,
            "api_key": self._settings.serpapi_key,
            "type": "products",
            "safe": self._settings.lens_safe,
        }
        if self._settings.lens_country:
            params["country"] = self._settings.lens_country
        data = await self._http.get_json("https://serpapi.com/search.json", params=params)
        if not data:
            if debug is not None:
                debug["serpapi_status"] = "error"
            return []

        visual_matches = data.get("visual_matches", [])
        titles = []
        for item in visual_matches[:max_results]:
            title = str(item.get("title") or "").strip()
            if title:
                titles.append(title)

        titles = self._prioritize_titles(titles)

        self._cache.set(cache_key, {"titles": titles})
        if debug is not None:
            debug["serpapi_status"] = "ok" if titles else "no_match"
            debug["serpapi_match_count"] = len(titles)
        logger.info("[LENS] Provider returned matches=%s", len(titles))
        return titles

    @staticmethod
    def _prioritize_titles(titles: list[str]) -> list[str]:
        if not titles:
            return titles

        preferred = ("otrity", "izy by zedna", "amazon.ae")
        deprioritized = ("amazon.com",)
        ranked: list[tuple[int, int, str]] = []
        for index, title in enumerate(titles):
            lowered = title.casefold()
            is_preferred = any(keyword in lowered for keyword in preferred)
            is_deprioritized = any(keyword in lowered for keyword in deprioritized)
            has_arabic = LensProvider._has_arabic_char(title)
            if has_arabic:
                rank = 2
            elif is_preferred:
                rank = 0
            elif is_deprioritized:
                rank = 2
            else:
                rank = 1
            ranked.append((rank, index, title))

        ranked.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in ranked]

    @staticmethod
    def _has_arabic_char(text: str) -> bool:
        for ch in text:
            code = ord(ch)
            if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
                return True
        return False
