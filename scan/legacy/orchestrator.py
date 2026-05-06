from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Literal

from scan.legacy.providers.product_facts_provider import ProductFactsProvider
from scan.legacy.schemas import ProductAnalysis
from scan.legacy.services.barcode_service import BarcodeService
from scan.legacy.services.grok_service import GrokService
from scan.legacy.services.lens_service import LensService
from scan.legacy.services.ocr_service import OCRService


logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    def __init__(
        self,
        barcode_service: BarcodeService,
        lens_service: LensService,
        ocr_service: OCRService,
        facts_provider: ProductFactsProvider,
        product_cache_service: Any | None = None,
        grok_service: GrokService | None = None,
    ) -> None:
        self._barcode = barcode_service
        self._lens = lens_service
        self._ocr = ocr_service
        self._facts = facts_provider
        self._product_cache = product_cache_service
        self._grok = grok_service

    async def analyze_product(
        self,
        *,
        product_id: str,
        image_path: str,
        original_image_path: str | None = None,
        crop_url: str,
        detected_label: str,
    ) -> ProductAnalysis:
        debug: dict[str, object] = {}
        debug.setdefault("serpapi_status", "not_called")
        debug.setdefault("off_status", "not_called")
        debug.setdefault("obf_status", "not_called")
        debug.setdefault("grok_status", "not_called")

        barcode_start_ms = time.perf_counter()
        logger.info("[BARCODE] extract start product_id=%s source=crop path=%s", product_id, image_path)
        barcode = await asyncio.to_thread(self._barcode.extract, image_path)
        logger.info(
            "[BARCODE] extract end product_id=%s source=crop elapsed_ms=%.1f found=%s",
            product_id,
            (time.perf_counter() - barcode_start_ms) * 1000,
            barcode is not None,
        )
        barcode_from = "crop"

        if barcode is None and original_image_path and original_image_path != image_path:
            fallback_start_ms = time.perf_counter()
            logger.info("[BARCODE] extract start product_id=%s source=original path=%s", product_id, original_image_path)
            barcode = await asyncio.to_thread(self._barcode.extract, original_image_path)
            logger.info(
                "[BARCODE] extract end product_id=%s source=original elapsed_ms=%.1f found=%s",
                product_id,
                (time.perf_counter() - fallback_start_ms) * 1000,
                barcode is not None,
            )
            if barcode:
                barcode_from = "original"

        cached_result = self._lookup_cached_result(
            product_id=product_id,
            barcode_code=barcode.code if barcode else None,
            detected_label=detected_label,
            debug=debug,
        )
        if cached_result:
            return cached_result

        logger.info("[BARCODE] stage start product_id=%s", product_id)
        debug["barcode_status"] = "detected" if barcode else "not_detected"
        debug["barcode_source"] = barcode_from if barcode else "none"
        if barcode:
            debug["barcode_format"] = barcode.format_type
            preferred_category = self._infer_preferred_category(detected_label)
            if preferred_category is not None:
                debug["barcode_inferred_category"] = preferred_category
            facts = await self._facts.fetch_by_barcode(
                barcode.code,
                preferred_category=preferred_category,
                debug=debug,
            )
            if facts and facts.ingredients:
                logger.info("[BARCODE] success product_id=%s", product_id)
                result = ProductAnalysis(
                    product_id=product_id,
                    source="barcode",
                    confidence=self._score_confidence("barcode", len(facts.ingredients), True),
                    category=facts.category,
                    name=facts.name,
                    brand=facts.brand,
                    ingredients=facts.ingredients,
                    additives=facts.additives,
                    barcode=barcode.code,
                    debug=debug,
                )
                return self._persist_result(
                    result,
                    ean=barcode.code,
                    extraction_method=f"pipeline:barcode:{barcode_from}",
                )
            debug["barcode_lookup"] = "no_ingredients"

        logger.info("[LENS] stage start product_id=%s", product_id)
        if hasattr(self._lens, "get_readiness"):
            ready, reason = self._lens.get_readiness()
            debug["lens_status"] = reason
        else:
            ready = True

        lens_start_ms = time.perf_counter()
        lens = await self._lens.resolve_name(crop_url, detected_label, debug=debug)
        logger.info(
            "[LENS] resolve end product_id=%s elapsed_ms=%.1f found=%s",
            product_id,
            (time.perf_counter() - lens_start_ms) * 1000,
            lens is not None,
        )
        if lens:
            debug["lens_candidates"] = lens.candidates
            if lens.upload_route:
                debug["lens_upload_route"] = lens.upload_route
            if lens.public_image_url:
                debug["lens_public_image_url"] = lens.public_image_url
            groq_lookup = await self._lookup_via_groq_brand_company(
                lens.title,
                debug,
            )
            if groq_lookup is not None:
                facts, groq_query = groq_lookup
                logger.info("[LENS] success via Groq+OFF product_id=%s query=%s", product_id, groq_query)
                result = ProductAnalysis(
                    product_id=product_id,
                    source="lens",
                    confidence=self._score_confidence("lens", len(facts.ingredients), True),
                    category=facts.category,
                    name=facts.name,
                    brand=facts.brand,
                    ingredients=facts.ingredients,
                    additives=facts.additives,
                    lens_title=lens.title,
                    debug=debug,
                )
                return self._persist_result(
                    result,
                    ean=barcode.code if barcode else None,
                    extraction_method="pipeline:lens:grok_brand_company_lookup",
                )

            if self._should_skip_raw_lens_fallback_after_groq(debug):
                debug["lens_lookup"] = "skipped_raw_title_after_groq_structured_miss"
            else:
                preferred_category = self._get_groq_category(debug)
                if self._is_query_attempted(debug, lens.title):
                    debug["lens_lookup"] = "skipped_duplicate_query"
                else:
                    self._record_query_attempt(debug, lens.title)
                    facts = await self._facts.fetch_by_name(
                        lens.title,
                        preferred_category=preferred_category,
                        debug=debug,
                    )
                    if facts and facts.ingredients:
                        debug["facts_query_success"] = lens.title
                        logger.info("[LENS] success product_id=%s", product_id)
                        result = ProductAnalysis(
                            product_id=product_id,
                            source="lens",
                            confidence=self._score_confidence("lens", len(facts.ingredients), True),
                            category=facts.category,
                            name=facts.name,
                            brand=facts.brand,
                            ingredients=facts.ingredients,
                            additives=facts.additives,
                            lens_title=lens.title,
                            debug=debug,
                        )
                        return self._persist_result(
                            result,
                            ean=barcode.code if barcode else None,
                            extraction_method="pipeline:lens:name_lookup",
                        )

            debug.setdefault("lens_lookup", "no_ingredients")
        elif ready:
            debug["lens_status"] = "no_match"

        logger.info("[OCR] stage start product_id=%s", product_id)
        ocr_start_ms = time.perf_counter()
        ocr = await asyncio.to_thread(self._ocr.extract, image_path)
        logger.info(
            "[OCR] extract end product_id=%s elapsed_ms=%.1f found=%s",
            product_id,
            (time.perf_counter() - ocr_start_ms) * 1000,
            ocr is not None,
        )
        if ocr:
            debug["ocr_text_chars"] = len(ocr.raw_text)

            if ocr.name:
                preferred_category = self._get_groq_category(debug)
                facts = await self._facts.fetch_by_name(
                    ocr.name,
                    preferred_category=preferred_category,
                    debug=debug,
                )
                if facts and facts.ingredients:
                    logger.info("[OCR] success via name lookup product_id=%s", product_id)
                    result = ProductAnalysis(
                        product_id=product_id,
                        source="ocr",
                        confidence=self._score_confidence("ocr", len(facts.ingredients), True),
                        category=facts.category,
                        name=facts.name,
                        brand=facts.brand,
                        ingredients=facts.ingredients,
                        additives=facts.additives,
                        debug=debug,
                    )
                    return self._persist_result(
                        result,
                        ean=barcode.code if barcode else None,
                        extraction_method="pipeline:ocr:name_lookup",
                    )

            if ocr.ingredients:
                logger.info("[OCR] success via local extraction product_id=%s", product_id)
                result = ProductAnalysis(
                    product_id=product_id,
                    source="ocr",
                    confidence=self._score_confidence("ocr", len(ocr.ingredients), False),
                    category=ocr.category,
                    name=ocr.name or detected_label,
                    brand=None,
                    ingredients=ocr.ingredients,
                    additives=[],
                    debug=debug,
                )
                return self._persist_result(
                    result,
                    ean=barcode.code if barcode else None,
                    extraction_method="pipeline:ocr:local_extraction",
                )
        else:
            debug["ocr_status"] = "unavailable_or_no_text"

        logger.warning("Pipeline exhausted fallbacks product_id=%s", product_id)
        result = ProductAnalysis(
            product_id=product_id,
            source="failed",
            confidence=0.0,
            category="unknown",
            name=detected_label,
            brand=None,
            ingredients=[],
            additives=[],
            debug=debug,
        )
        return self._persist_result(
            result,
            ean=barcode.code if barcode else None,
            extraction_method="pipeline:fallback_failed",
        )

    def _lookup_cached_result(
        self,
        *,
        product_id: str,
        barcode_code: str | None,
        detected_label: str,
        debug: dict[str, object],
    ) -> ProductAnalysis | None:
        if self._product_cache is None:
            return None

        if barcode_code:
            cached = self._product_cache.get_cached_analysis_by_ean(barcode_code)
            if cached:
                logger.info("[CACHE] hit via EAN=%s product_id=%s", barcode_code, product_id)
                return self._finalize_cached_result(cached, product_id, debug, "ean")

        normalized_label = " ".join((detected_label or "").strip().lower().split())
        if normalized_label and normalized_label not in {"product", "unknown"}:
            fingerprint = self._product_cache.build_fingerprint(detected_label, None)
            debug["lookup_fingerprint"] = fingerprint
            cached = self._product_cache.get_cached_analysis_by_fingerprint(fingerprint)
            if cached:
                logger.info("[CACHE] hit via fingerprint product_id=%s", product_id)
                return self._finalize_cached_result(cached, product_id, debug, "fingerprint")

        return None

    def _finalize_cached_result(
        self,
        cached: ProductAnalysis,
        product_id: str,
        debug: dict[str, object],
        lookup: str,
    ) -> ProductAnalysis:
        cached_debug: dict[str, object] = dict(cached.debug)
        cached_debug.update(debug)
        cached_debug["cache_hit"] = True
        cached_debug["cache_lookup"] = lookup
        return cached.model_copy(update={"product_id": product_id, "debug": cached_debug})

    def _persist_result(
        self,
        result: ProductAnalysis,
        *,
        ean: str | None,
        extraction_method: str,
    ) -> ProductAnalysis:
        if self._product_cache is None:
            return result

        try:
            self._product_cache.save_analysis(
                result,
                ean=ean,
                extraction_method=extraction_method,
                metadata_json={
                    "pipeline_source": result.source,
                    "cached": False,
                },
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("[CACHE] Persist failed product_id=%s error=%s", result.product_id, exc)

        return result

    def _score_confidence(self, source: str, ingredients_count: int, api_backed: bool) -> float:
        base = {
            "barcode": 0.86,
            "lens": 0.73,
            "ocr": 0.58,
        }.get(source, 0.4)

        ingredient_bonus = min(0.12, ingredients_count * 0.01)
        api_bonus = 0.04 if api_backed else 0.0
        return round(min(0.99, base + ingredient_bonus + api_bonus), 3)

    async def _lookup_via_groq_brand_company(
        self,
        best_lens_title: str,
        debug: dict[str, object],
        *,
        preferred_category: Literal["food", "cosmetic"] | None = None,
    ) -> tuple[Any, str] | None:
        if self._grok is None:
            debug.setdefault("grok_status", "skipped_missing_service")
            return None

        extracted = await self._grok.extract_product_title_from_url_results(
            url_results=[{"title": best_lens_title}],
            debug=debug,
        )
        if not isinstance(extracted, dict):
            debug["grok_title_extraction"] = "none"
            return None
        debug["grok_title_extraction"] = "parsed"

        brand_raw = extracted.get("brand")
        company_raw = extracted.get("company")
        product_name_raw = extracted.get("product_name")
        category_raw = extracted.get("category")
        brand = brand_raw.strip() if isinstance(brand_raw, str) and brand_raw.strip() else None
        company = company_raw.strip() if isinstance(company_raw, str) and company_raw.strip() else None
        product_name = product_name_raw.strip() if isinstance(product_name_raw, str) and product_name_raw.strip() else None
        product_name = self._clean_query_text(product_name)
        if isinstance(category_raw, str):
            category_value = category_raw.strip().lower()
        else:
            category_value = "unknown"

        if category_value not in {"food", "cosmetic", "unknown"}:
            category_value = "unknown"
        category = category_value if category_value in {"food", "cosmetic"} else None

        debug["grok_brand"] = brand
        debug["grok_company"] = company
        debug["grok_product_name"] = product_name
        debug["grok_category"] = category_value
        debug["grok_confidence"] = extracted.get("confidence")

        if preferred_category is None:
            preferred_category = category
            if preferred_category is not None:
                debug["grok_inferred_category"] = preferred_category

        queries: list[str] = []
        if product_name and brand:
            queries.append(f"{product_name} {brand}")
        if product_name and company:
            queries.append(f"{product_name} {company}")
        if brand and company:
            queries.append(f"{brand} {company}")
        if product_name and brand and company:
            queries.append(f"{product_name} {brand} {company}")
        if product_name:
            queries.append(product_name)
        if brand:
            queries.append(brand)
        if company:
            queries.append(company)

        if not brand and not company:
            fallback_word = self._first_clean_word(best_lens_title)
            if fallback_word:
                queries.append(fallback_word)
                debug["grok_lookup_fallback"] = "first_word"

        seen: set[str] = set()
        for query in queries:
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)

            if self._is_query_attempted(debug, query):
                continue
            self._record_query_attempt(debug, query)

            facts = await self._facts.fetch_by_name(
                query,
                preferred_category=preferred_category,
                debug=debug,
            )
            if facts and facts.ingredients:
                debug["facts_query_success"] = query
                if not self._is_relevant_facts_match(
                    facts_name=facts.name,
                    facts_brand=facts.brand,
                    product_name=product_name,
                    expected_brand=brand,
                ):
                    rejected = debug.get("grok_relevance_rejected_queries")
                    if isinstance(rejected, list):
                        rejected.append(query)
                    else:
                        debug["grok_relevance_rejected_queries"] = [query]
                    continue
                debug["grok_lookup_query"] = query
                return facts, query

        return None

    @staticmethod
    def _should_skip_raw_lens_fallback_after_groq(debug: dict[str, object]) -> bool:
        extraction_state = debug.get("grok_title_extraction")
        confidence_raw = debug.get("grok_confidence")

        if extraction_state != "parsed":
            return False

        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0

        return confidence >= 0.6

    @staticmethod
    def _first_clean_word(text: str) -> str | None:
        normalized = " ".join(text.strip().split())
        if not normalized:
            return None

        token = normalized.split(" ", 1)[0]
        cleaned = re.sub(r"^\W+|\W+$", "", token, flags=re.UNICODE).strip()
        return cleaned or None

    @staticmethod
    def _clean_query_text(text: str | None) -> str | None:
        if not text:
            return None
        cleaned = re.sub(r"\ball\s*[-–—]?\s*weather\b", " ", text, flags=re.IGNORECASE)
        cleaned = " ".join(cleaned.split())
        return cleaned or None

    @staticmethod
    def _infer_preferred_category(*values: str | None) -> Literal["food", "cosmetic"] | None:
        merged = " ".join(v.strip().lower() for v in values if isinstance(v, str) and v.strip())
        if not merged:
            return None

        cosmetic_keywords = ("cosmetic", "beauty", "shampoo", "soap", "lotion", "cream", "makeup", "skincare")
        food_keywords = (
            "food",
            "beverage",
            "drink",
            "juice",
            "milk",
            "snack",
            "yogurt",
            "biscuit",
            "biscuits",
            "cookie",
            "cookies",
            "chocolate",
            "tomate",
            "tomato",
            "thon",
            "tuna",
        )

        if any(word in merged for word in cosmetic_keywords):
            return "cosmetic"
        if any(word in merged for word in food_keywords):
            return "food"
        return None

    @staticmethod
    def _get_groq_category(debug: dict[str, object]) -> Literal["food", "cosmetic"] | None:
        value = debug.get("grok_inferred_category")
        if value == "food":
            return "food"
        if value == "cosmetic":
            return "cosmetic"
        return None

    @staticmethod
    def _is_query_attempted(debug: dict[str, object], query: str) -> bool:
        if not query:
            return False
        attempts = debug.get("facts_query_attempts")
        if not isinstance(attempts, list):
            return False
        key = query.casefold()
        return any(isinstance(item, str) and item.casefold() == key for item in attempts)

    @staticmethod
    def _record_query_attempt(debug: dict[str, object], query: str) -> None:
        if not query:
            return
        attempts = debug.get("facts_query_attempts")
        if not isinstance(attempts, list):
            attempts = []
            debug["facts_query_attempts"] = attempts
        attempts.append(query)

    @classmethod
    def _is_relevant_facts_match(
        cls,
        *,
        facts_name: str,
        facts_brand: str | None,
        product_name: str | None,
        expected_brand: str | None,
    ) -> bool:
        if not product_name:
            return True

        expected_tokens = cls._tokenize_for_match(product_name)
        if not expected_tokens:
            return True

        if cls._is_placeholder_name(facts_name):
            return cls._brands_match(expected_brand, facts_brand)

        facts_tokens = cls._tokenize_for_match(facts_name)
        if not facts_tokens:
            return cls._brands_match(expected_brand, facts_brand)

        for expected in expected_tokens:
            if cls._has_approx_token_match(expected, facts_tokens):
                return True
        return False

    @staticmethod
    def _is_placeholder_name(name: str) -> bool:
        normalized = " ".join(name.strip().lower().split())
        return normalized in {"unknown", "n/a", "na", "none", "null", "-"}

    @classmethod
    def _brands_match(cls, expected_brand: str | None, facts_brand: str | None) -> bool:
        if not expected_brand or not facts_brand:
            return False

        expected_tokens = cls._tokenize_for_match(expected_brand)
        facts_tokens = cls._tokenize_for_match(facts_brand)
        if not expected_tokens or not facts_tokens:
            return False

        for expected in expected_tokens:
            if cls._has_approx_token_match(expected, facts_tokens):
                return True
        return False

    @staticmethod
    def _has_approx_token_match(expected: str, candidates: set[str]) -> bool:
        for token in candidates:
            if token == expected:
                return True
            if token.startswith(expected) or expected.startswith(token):
                return True
            if len(token) >= 4 and len(expected) >= 4:
                similarity = SequenceMatcher(None, token, expected).ratio()
                if similarity >= 0.78:
                    return True
        return False

    @staticmethod
    def _tokenize_for_match(text: str) -> set[str]:
        normalized = unicodedata.normalize("NFKD", text)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        lowered = ascii_text.lower()
        cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
        return {token for token in cleaned.split() if len(token) >= 3}
