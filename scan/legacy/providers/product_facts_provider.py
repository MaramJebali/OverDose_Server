from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from scan.legacy.cache import SimpleTTLCache
from scan.legacy.config import Settings
from scan.legacy.http import HttpClient
from scan.legacy.schemas import ProductFacts


OFF_BARCODE_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
OBF_BARCODE_URL = "https://world.openbeautyfacts.org/api/v0/product/{barcode}.json"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OBF_SEARCH_URL = "https://world.openbeautyfacts.org/cgi/search.pl"
OFF_MAX_ATTEMPTS = 15


class ProductFactsProvider:
    def __init__(self, http: HttpClient, settings: Settings, cache: SimpleTTLCache[dict]) -> None:
        self._http = http
        self._settings = settings
        self._cache = cache

    async def fetch_by_barcode(
        self,
        barcode: str,
        *,
        preferred_category: Literal["food", "cosmetic"] | None = None,
        debug: dict[str, object] | None = None,
    ) -> ProductFacts | None:
        cache_key = f"barcode::{barcode}"
        cached = self._cache.get(cache_key)
        if cached:
            self._set_status(debug, "off_status", "cache_hit")
            self._set_status(debug, "obf_status", "cache_hit")
            return ProductFacts(**cached)

        headers = {"User-Agent": self._settings.off_user_agent}
        source_order = self._source_order(preferred_category)

        if preferred_category == "food":
            self._set_status(debug, "obf_status", "skipped_category")
        elif preferred_category == "cosmetic":
            self._set_status(debug, "off_status", "skipped_category")

        for source in source_order:
            if source == "off":
                off_data = await self._fetch_off_json(OFF_BARCODE_URL.format(barcode=barcode), headers=headers)
                if off_data and off_data.get("status") == 1:
                    parsed = self._parse_off(off_data.get("product", {}))
                    if parsed and parsed.ingredients:
                        self._set_status(debug, "off_status", "ok")
                        self._cache.set(cache_key, parsed.model_dump())
                        return parsed
                    self._set_status(debug, "off_status", "no_match")
                else:
                    self._set_status(debug, "off_status", "error" if off_data is None else "no_match")
            else:
                obf_data = await self._fetch_off_json(OBF_BARCODE_URL.format(barcode=barcode), headers=headers)
                if obf_data and obf_data.get("status") == 1:
                    parsed = self._parse_obf(obf_data.get("product", {}))
                    if parsed and parsed.ingredients:
                        self._set_status(debug, "obf_status", "ok")
                        self._cache.set(cache_key, parsed.model_dump())
                        return parsed
                    self._set_status(debug, "obf_status", "no_match")
                else:
                    self._set_status(debug, "obf_status", "error" if obf_data is None else "no_match")

        return None

    async def fetch_by_name(
        self,
        name: str,
        *,
        preferred_category: Literal["food", "cosmetic"] | None = None,
        debug: dict[str, object] | None = None,
    ) -> ProductFacts | None:
        candidates = self._prepare_name_candidates(name)
        if not candidates:
            self._set_status(debug, "off_status", "skipped_no_name")
            self._set_status(debug, "obf_status", "skipped_no_name")
            return None

        for candidate in candidates:
            cache_key = f"name::{candidate.lower()}"
            cached = self._cache.get(cache_key)
            if cached:
                self._set_status(debug, "off_status", "cache_hit")
                self._set_status(debug, "obf_status", "cache_hit")
                return ProductFacts(**cached)

        headers = {"User-Agent": self._settings.off_user_agent}
        source_order = self._source_order(preferred_category)

        if preferred_category == "food":
            self._set_status(debug, "obf_status", "skipped_category")
        elif preferred_category == "cosmetic":
            self._set_status(debug, "off_status", "skipped_category")

        for candidate in candidates:
            for source in source_order:
                params = {
                    "search_terms": candidate,
                    "search_simple": 1,
                    "action": "process",
                    "json": 1,
                    "page_size": 3,
                }

                if source == "off":
                    data = await self._fetch_off_json(OFF_SEARCH_URL, params=params, headers=headers)
                    if data and data.get("products"):
                        best = self._pick_best_product(data.get("products", []), query=candidate)
                        parsed = self._parse_off(best)
                        if parsed and parsed.ingredients:
                            self._set_status(debug, "off_status", "ok")
                            self._cache_name_result(candidates, parsed)
                            return parsed
                        self._set_status(debug, "off_status", "no_match")
                    else:
                        self._set_status(debug, "off_status", "error" if data is None else "no_match")
                else:
                    data = await self._fetch_off_json(OBF_SEARCH_URL, params=params, headers=headers)
                    if data and data.get("products"):
                        best = self._pick_best_product(data.get("products", []), query=candidate)
                        parsed = self._parse_obf(best)
                        if parsed and parsed.ingredients:
                            self._set_status(debug, "obf_status", "ok")
                            self._cache_name_result(candidates, parsed)
                            return parsed
                        self._set_status(debug, "obf_status", "no_match")
                    else:
                        self._set_status(debug, "obf_status", "error" if data is None else "no_match")

        return None

    @staticmethod
    def _set_status(debug: dict[str, object] | None, key: str, status: str) -> None:
        if debug is None:
            return
        if debug.get(key) == "error":
            return
        debug[key] = status

    @staticmethod
    def _source_order(preferred_category: Literal["food", "cosmetic"] | None) -> list[str]:
        if preferred_category == "food":
            return ["off"]
        if preferred_category == "cosmetic":
            return ["obf"]
        return ["off", "obf"]

    async def _fetch_off_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        for _ in range(OFF_MAX_ATTEMPTS):
            data = await self._http.get_json(url, params=params, headers=headers)
            if data is not None:
                return data
        return None

    def _cache_name_result(self, candidates: list[str], parsed: ProductFacts) -> None:
        payload = parsed.model_dump()
        for candidate in candidates:
            self._cache.set(f"name::{candidate.lower()}", payload)

    @staticmethod
    def _prepare_name_candidates(name: str) -> list[str]:
        base = " ".join(name.strip().split())
        if not base:
            return []

        base_without_suffix = re.sub(r"\s[-–—]\s.*$", "", base).strip()
        cleaned = ProductFactsProvider._strip_marketplace_noise(base_without_suffix or base)

        cleanup_patterns = [
            r"\ball\s*[-–—]?\s*weather\b",
            r"\bmaska\s+na\s+vlasy\b",
            r"\bmaska\s+no\s+vlasy\b",
            r"[\(\[\{].*?[\)\]\}]",
            r"\b\d+\s?[xX]\s?\d+(?:[\.,]\d+)?\s?(?:ml|l|cl|dl|lt|ltr|liter|liters|litre|litres|g|gr|grs|gram|grams|gramme|grammes|kg|kgs|mg|mcg|oz|lb|lbs|fl\.?oz)\b",
            r"\b\d+(?:[\.,]\d+)?\s?(?:ml|l|cl|dl|lt|ltr|liter|liters|litre|litres|g|gr|grs|gram|grams|gramme|grammes|kg|kgs|mg|mcg|oz|lb|lbs|fl\.?oz)\b",
            r"\b(?:pack|lot|set)\s+of\s+\d+\b",
            r"\b\d+\s?(?:pcs?|pieces?)\b",
        ]
        for pattern in cleanup_patterns:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

        cleaned = re.sub(r"[\|/_-]+", " ", cleaned)
        cleaned = " ".join(cleaned.split())
        primary = cleaned or base

        variants = [primary]

        tokens = primary.split()
        if len(tokens) > 6:
            variants.append(" ".join(tokens[:6]))

        deduped: list[str] = []
        seen: set[str] = set()
        for variant in variants:
            key = variant.casefold()
            if not variant or key in seen:
                continue
            seen.add(key)
            deduped.append(variant)

        return deduped

    @staticmethod
    def _strip_marketplace_noise(text: str) -> str:
        cleaned = text
        cleaned = re.sub(r"[\u2010-\u2015]+", " ", cleaned)
        marketplace_patterns = [
            r"\b[\w-]*buy[\w-]*\b",
            r"\bby\b",
            r"\b(?:shop|store|seller|marketplace|deal)\b",
            r"\b(?:izy|zedna|talabat|ayshek|aychek|nestle|wheat|honey|mint|gel|paste|miel|green|clean|fresh|vanille|Kwik)\b",
        ]

        for pattern in marketplace_patterns:
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

        return " ".join(cleaned.split())

    def _pick_best_product(self, products: list[dict[str, Any]], *, query: str) -> dict[str, Any]:
        if not products:
            return {}

        query_tokens = self._tokenize_for_match(query)
        normalized_query_tokens = [token for token in self._normalize_for_match(query).split() if len(token) >= 3]
        head_size = max(1, len(normalized_query_tokens) // 2)
        head_tokens = set(normalized_query_tokens[:head_size])

        def _score(product: dict[str, Any]) -> float:
            name_text = str(
                product.get("product_name")
                or product.get("product_name_en")
                or product.get("generic_name")
                or ""
            )
            brand_text = str(product.get("brands") or "")
            name_tokens = self._tokenize_for_match(name_text)
            brand_tokens = self._tokenize_for_match(brand_text)

            overlap_count = len(query_tokens & name_tokens)
            head_overlap_count = len(head_tokens & name_tokens)
            overlap_ratio = overlap_count / max(1, len(query_tokens))
            brand_overlap_count = len(query_tokens & brand_tokens)
            ingredient_count = len(product.get("ingredients", [])) if isinstance(product.get("ingredients"), list) else 0

            query_norm = self._normalize_for_match(query)
            name_norm = self._normalize_for_match(name_text)
            phrase_bonus = 2.0 if query_norm and query_norm in name_norm else 0.0

            ingredient_bonus = min(0.6, ingredient_count * 0.03)
            return (
                overlap_count * 3.0
                + head_overlap_count * 3.5
                + overlap_ratio * 4.0
                + brand_overlap_count * 1.2
                + phrase_bonus
                + ingredient_bonus
            )

        return max(products, key=_score)

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        lowered = ascii_text.lower()
        return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())

    @classmethod
    def _tokenize_for_match(cls, text: str) -> set[str]:
        normalized = cls._normalize_for_match(text)
        return {token for token in normalized.split() if len(token) >= 3}

    def _parse_off(self, product: dict[str, Any]) -> ProductFacts | None:
        ingredients: list[str] = []
        additives: list[str] = []

        for item in product.get("ingredients", []):
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            ingredient_id = str(item.get("id") or "")
            if text and not ingredient_id.startswith("fr:"):
                ingredients.append(text)
            if ingredient_id.startswith("en:e"):
                additives.append(ingredient_id.replace("en:", "").upper())

        if not ingredients:
            fallback_text = str(product.get("ingredients_text") or "").strip()
            if fallback_text:
                ingredients = [part.strip() for part in fallback_text.split(",") if part.strip()]

        if not ingredients:
            return None

        parsed_name = self._best_product_name(product)
        return ProductFacts(
            name=parsed_name,
            brand=str(product.get("brands") or "") or None,
            category="food",
            ingredients=list(dict.fromkeys(ingredients)),
            additives=list(dict.fromkeys(additives)),
            source_detail="openfoodfacts",
        )

    def _parse_obf(self, product: dict[str, Any]) -> ProductFacts | None:
        ingredients: list[str] = []
        for item in product.get("ingredients", []):
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                if text:
                    ingredients.append(text)

        if not ingredients:
            fallback_text = str(product.get("ingredients_text") or "").strip()
            if fallback_text:
                ingredients = [part.strip() for part in fallback_text.split(",") if part.strip()]

        if not ingredients:
            return None

        parsed_name = self._best_product_name(product)
        return ProductFacts(
            name=parsed_name,
            brand=str(product.get("brands") or "") or None,
            category="cosmetic",
            ingredients=list(dict.fromkeys(ingredients)),
            additives=[],
            source_detail="openbeautyfacts",
        )

    @staticmethod
    def _best_product_name(product: dict[str, Any]) -> str:
        fields = (
            "product_name",
            "product_name_en",
            "product_name_fr",
            "generic_name",
            "generic_name_en",
            "generic_name_fr",
        )
        for field in fields:
            value = product.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "Unknown"
