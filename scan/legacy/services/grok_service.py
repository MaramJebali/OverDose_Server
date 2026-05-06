from __future__ import annotations

import json
import logging
from typing import Any

from scan.legacy.config import Settings
from scan.legacy.http import HttpClient


logger = logging.getLogger(__name__)


TITLE_EXTRACTION_PROMPT_TEMPLATE = '''You are an information extraction system for e-commerce product titles.

Task:
Extract structured data from the given product title.

Return ONLY valid JSON with the following fields:
- \"brand\": the product line or product brand (e.g., Danao)
- \"company\": the manufacturer or parent company (e.g., Delice)
- \"product_name\": the flavor/type of the product (no brand, no company)
- \"category\": one of \"food\", \"cosmetic\", or \"unknown\"
- \"confidence\": a number between 0 and 1 indicating confidence

Rules:
- Do NOT include quantities (ml, cl, L, etc.)
- Do NOT include store names, marketplaces, or sellers (e.g., Izy, Zedna, Talabat)
- Do NOT translate unless necessary; keep original language if clear
- If a field is missing or unclear, return null
- Do NOT guess or hallucinate unknown brands or companies
- Prefer known brand words over generic words
- Output must be valid JSON only (no explanation, no text outside JSON)

Examples:

Input:
\"DANAO PECHE ABRICOT 20CL DELICE - Izy by Zedna\"

Output:
{
    \"brand\": \"Danao\",
    \"company\": \"Delice\",
    \"product_name\": \"Peche Abricot\",
    \"category\": \"food\",
    \"confidence\": 0.95
}

Input:
\"اشترِ داناو مشروب عصير خوخ ومشمش بالحليب 180 مل\"

Output:
{
    \"brand\": \"Danao\",
    \"company\": null,
    \"product_name\": \"خوخ ومشمش بالحليب\",
    \"category\": \"food\",
    \"confidence\": 0.85
}

Now process this:

Input:
"{INPUT_TEXT}"'''  # noqa: E501


class GrokService:
    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    def get_readiness(self) -> tuple[bool, str]:
        if not self._settings.grok_api_key:
            return False, "missing_grok_api_key"
        return True, "ready"

    async def extract_product_title_from_url_results(
        self,
        *,
        url_results: list[dict[str, Any]],
        debug: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        ready, reason = self.get_readiness()
        if not ready:
            logger.info("[GROK] Title extraction skipped: %s", reason)
            if debug is not None:
                debug["grok_status"] = f"skipped_{reason}"
            return None

        input_text = self._pick_title_from_url_results(url_results)
        if not input_text:
            logger.info("[GROK] Title extraction skipped: no title in url_results")
            if debug is not None:
                debug["grok_status"] = "skipped_no_title"
            return None

        prompt = TITLE_EXTRACTION_PROMPT_TEMPLATE.replace("{INPUT_TEXT}", input_text)
        url = f"{self._settings.grok_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._settings.grok_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._settings.grok_model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "Return strict JSON only.",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        }

        data = await self._http.post_json(url, json_body=payload, headers=headers)
        if not data:
            if debug is not None:
                debug["grok_status"] = "error"
            return None

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            if debug is not None:
                debug["grok_status"] = "error"
            return None

        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            if debug is not None:
                debug["grok_status"] = "error"
            return None

        parsed = self._extract_json_object(content)
        normalized = self._normalize_title_extraction_payload(parsed)
        if debug is not None:
            debug["grok_status"] = "ok" if normalized is not None else "error"
        return normalized

    @staticmethod
    def _extract_json_object(content: str) -> dict[str, Any] | None:
        text = content.strip()

        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()

        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or start >= end:
                return None
            try:
                parsed = json.loads(text[start : end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                logger.warning("[GROK] Invalid JSON response")
                return None

    @staticmethod
    def _pick_title_from_url_results(url_results: list[dict[str, Any]]) -> str | None:
        keys = ("title", "name", "product_title", "lens_title", "snippet")
        for result in url_results:
            for key in keys:
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _normalize_title_extraction_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        brand = payload.get("brand")
        company = payload.get("company")
        product_name = payload.get("product_name")
        category = payload.get("category")
        confidence = payload.get("confidence")

        if brand is not None and not isinstance(brand, str):
            brand = None
        if company is not None and not isinstance(company, str):
            company = None
        if product_name is not None and not isinstance(product_name, str):
            product_name = None

        if isinstance(category, str):
            category = category.strip().lower()
        else:
            category = "unknown"

        if category not in {"food", "cosmetic", "unknown"}:
            category = "unknown"

        if isinstance(confidence, (int, float)):
            confidence_value: float | None = float(max(0.0, min(1.0, confidence)))
        else:
            confidence_value = None

        return {
            "brand": brand,
            "company": company,
            "product_name": product_name,
            "category": category,
            "confidence": confidence_value,
        }
