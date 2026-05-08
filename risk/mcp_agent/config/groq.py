"""Groq LLM configuration - PRODUCTION VERSION v5.0
Changes from v4.0:
- Domain-specialized prompts: cosmetic / food / detergent
- Garbage patterns consolidated into ONE place (GARBAGE_PATTERNS)
- lru_cache replaced with module-level dicts (no memory leak)
- retry_with_backoff raises explicitly after exhaustion
- Inter-batch throttle (0.3s) to respect RPM limits
- Mixtral removed (deprecated on Groq)
- Models loaded from env with fallback defaults
- Prompt version hash included in cache keys
- Two-pass classification: fast model → escalate low-confidence to balanced
- chain-of-thought reasoning inside LLM before JSON output
- MCP logic untouched — same return shapes, same method signatures
"""

import os
import sys
import json
import logging
import re
import time
import hashlib
from functools import wraps
from typing import Dict, List, Optional, Any, Union, Tuple
from groq import Groq, APIError, RateLimitError, APIConnectionError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(_handler)


# ============================================================
# SINGLE SOURCE OF TRUTH — GARBAGE PATTERNS
# ============================================================
GARBAGE_PATTERNS: List[Tuple[str, str]] = [
    (r'^\d+$',              "NUMERIC_ONLY"),
    (r'^[A-Z]\d{5,}$',     "BATCH_CODE"),
    (r'^[A-Z]{2,}\d{4,}$', "BATCH_CODE"),
    (r'^[A-Z]\.[A-Z]\.[A-Z](\.[A-Z])?$', "ACRONYM_CODE"),
    (r'^F\.I\.L\.?$',      "FIL_CODE"),
    (r'^FIL$',             "FIL_CODE"),
    (r'^[A-Z]{1,2}$',      "SINGLE_OR_DOUBLE_CHAR"),   # e.g. "Z", "AB"
]


# ============================================================
# MODULE-LEVEL CACHES  (avoids lru_cache self-reference leak)
# ============================================================
_classification_cache: Dict[str, dict] = {}
_risk_cache: Dict[str, dict]           = {}
_organs_cache: Dict[str, dict]         = {}


# ============================================================
# RETRY DECORATOR
# ============================================================
def retry_with_backoff(max_retries: int = 3, initial_delay: float = 1.0, backoff_factor: float = 2.0):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (RateLimitError, APIConnectionError) as e:
                    last_exc = e
                    logger.warning(f"[{func.__name__}] Rate/connection error (attempt {attempt+1}/{max_retries}), retrying in {delay}s: {e}")
                    time.sleep(delay)
                    delay *= backoff_factor
                except APIError as e:
                    last_exc = e
                    logger.warning(f"[{func.__name__}] API error (attempt {attempt+1}/{max_retries}), retrying in {delay}s: {e}")
                    time.sleep(delay)
                    delay *= backoff_factor
            # Explicit raise after exhaustion — never return None silently
            raise RuntimeError(
                f"[{func.__name__}] All {max_retries} retries exhausted. Last error: {last_exc}"
            )
        return wrapper
    return decorator


# ============================================================
# DOMAIN-SPECIALIZED PROMPTS
# ============================================================

_BASE_RULES = """
RULES:
1. When uncertain → classify as chemical.
2. Return ONLY valid JSON. Keys: chemicals, safe, garbage.
3. Values are arrays of plain strings (ingredient names, no extra quotes inside).
4. DO NOT write anything outside the JSON block.
5. Think step-by-step BEFORE writing the JSON (write your reasoning in a <!-- --> comment, then the JSON).
"""

_GARBAGE_EXAMPLES = '"2", "Z288697", "F.I.L", "ABC1234", "X", single letters, pure numbers'

PROMPTS: Dict[str, str] = {

    # ── COSMETIC ───────────────────────────────────────────────────────────────
    "cosmetic": f"""You classify cosmetic/personal-care ingredients.

GROUP 1 - CHEMICAL (needs investigation):
1. ACTIVE DRUGS: salicylic acid, caffeine, retinol, niacinamide, hyaluronic acid
2. SURFACTANTS: anything ending in sulfate, betaine, glucoside, polysorbate, laureth, lauryl
3. PRESERVATIVES: benzoate, phenoxyethanol, sorbate, any paraben, imidazolidinyl urea
4. ENDOCRINE DISRUPTORS: parabens, phthalates, benzophenones, oxybenzone, homosalate, triclosan, PFAS
5. EMULSIFIERS / PEG compounds: PEG-*, polysorbate, hydrogenated castor oil derivatives
6. SILICONES & POLYMERS: dimethicone, cyclopentasiloxane, polyquaternium, acrylates
7. SYNTHETIC FRAGRANCES: parfum, fragrance, perfume, limonene, linalool, geraniol
8. PROCESSED LIPIDS: hydrogenated oils, fatty acids, fatty alcohols, cetyl alcohol, stearyl alcohol, lecithin
9. pH ADJUSTERS / CHELATORS: citric acid, lactic acid, sodium hydroxide, EDTA, triethanolamine
10. EXTRACTS / ENZYMES / COLORS: any plant extract, enzyme, CI number, dye

GROUP 2 - SAFE (skip — ONLY these):
- Water: aqua, water, eau
- Salt: sodium chloride
- Simple humectants: glycerin, glycerol, sorbitol
- Unprocessed natural oils: jojoba seed oil, shea butter, cocoa butter, coconut oil, olive oil
- Simple thickeners: xanthan gum, guar gum
- Pure vitamin E: tocopherol (NOT tocopherol acetate)

GROUP 3 - GARBAGE (not real ingredients):
{_GARBAGE_EXAMPLES}
{_BASE_RULES}

Example:
Input: aqua, citric acid, sodium laureth sulfate, parfum, shea butter, 2, Z288697
<!--reasoning: aqua=safe, citric acid=pH adjuster=chemical, SLS=surfactant=chemical, parfum=fragrance=chemical, shea butter=safe, 2=garbage, Z288697=batch code=garbage-->
Output: {{"chemicals": ["citric acid", "sodium laureth sulfate", "parfum"], "safe": ["aqua", "shea butter"], "garbage": ["2", "Z288697"]}}""",

    # ── FOOD ───────────────────────────────────────────────────────────────────
    "food": f"""You classify food product ingredients.

GROUP 1 - CHEMICAL (needs investigation):
1. PRESERVATIVES: sodium benzoate, potassium sorbate, BHA, BHT, TBHQ, nitrates, nitrites, sulfites
2. ARTIFICIAL COLORS: any CI number, Red 40, Yellow 5, Yellow 6, Blue 1, caramel color
3. ARTIFICIAL SWEETENERS: aspartame, sucralose, acesulfame-K, saccharin, neotame
4. EMULSIFIERS: lecithin (soy/sunflower), mono- and diglycerides, polysorbate 80, carrageenan, DATEM
5. FLAVOR ENHANCERS: MSG (monosodium glutamate), disodium inosinate, disodium guanylate
6. ARTIFICIAL / NATURAL FLAVORS: "natural flavor", "artificial flavor", vanillin, ethyl maltol
7. THICKENERS / STABILIZERS (synthetic): xanthan in highly processed context, carboxymethyl cellulose, methylcellulose
8. ACIDULANTS: citric acid, phosphoric acid, lactic acid, acetic acid (in processed form)
9. BLEACHING / MATURING AGENTS: azodicarbonamide, benzoyl peroxide, chlorine
10. FOOD CONTACT POLYMERS: polyethylene, polypropylene, BPA-related

GROUP 2 - SAFE (skip):
- Water, aqua
- Plain salt: sodium chloride
- Plain sugar: sucrose, glucose, fructose (when listed simply)
- Whole spices and herbs: pepper, oregano, turmeric, cinnamon (unextracted)
- Plain vinegar: acetic acid from fermentation labeled as "vinegar"
- Simple starches: corn starch, potato starch (unmodified)
- Simple oils: olive oil, sunflower oil, canola oil (not hydrogenated)

GROUP 3 - GARBAGE:
{_GARBAGE_EXAMPLES}
{_BASE_RULES}

Example:
Input: water, sodium benzoate, sucrose, red 40, olive oil, MSG, Z123456
<!--reasoning: water=safe, sodium benzoate=preservative=chemical, sucrose=safe, red 40=artificial color=chemical, olive oil=safe, MSG=flavor enhancer=chemical, Z123456=batch code=garbage-->
Output: {{"chemicals": ["sodium benzoate", "red 40", "MSG"], "safe": ["water", "sucrose", "olive oil"], "garbage": ["Z123456"]}}""",

    # ── DETERGENT ──────────────────────────────────────────────────────────────
    "detergent": f"""You classify household detergent / cleaning product ingredients.

GROUP 1 - CHEMICAL (needs investigation):
1. SURFACTANTS: linear alkylbenzene sulfonate (LAS), sodium lauryl sulfate, alcohol ethoxylates, APE/APEO (nonylphenol ethoxylate), betaines, amine oxides
2. BUILDERS / CHELATORS: EDTA, DTPA, sodium tripolyphosphate (STPP), zeolites (if synthetic), sodium citrate, NTA
3. BLEACHING AGENTS: sodium hypochlorite, hydrogen peroxide, sodium percarbonate, peracetic acid, TAED
4. ENZYMES: protease, lipase, amylase, cellulase (bioactive — needs investigation)
5. OPTICAL BRIGHTENERS / FLUORESCERS: stilbene derivatives, FWA compounds
6. SOLVENTS: ethanol (industrial), isopropanol, glycol ethers, propylene glycol (industrial grade)
7. PRESERVATIVES: MIT (methylisothiazolinone), CMIT, benzisothiazolinone, BIT
8. FRAGRANCES: parfum, fragrance, limonene, linalool, any terpene
9. pH ADJUSTERS: sodium hydroxide, citric acid, sulfuric acid, sodium carbonate (soda ash)
10. POLYMERS / ANTI-REDEPOSITION: carboxymethyl cellulose, polyvinyl alcohol, PEG compounds

GROUP 2 - SAFE (skip):
- Water, aqua
- Plain sodium chloride (as filler/salt)
- Simple glycerin (as humectant carrier, not solvent)

GROUP 3 - GARBAGE:
{_GARBAGE_EXAMPLES}
{_BASE_RULES}

Example:
Input: water, sodium lauryl sulfate, EDTA, parfum, sodium chloride, MIT, 99, F.I.L
<!--reasoning: water=safe, SLS=surfactant=chemical, EDTA=chelator=chemical, parfum=fragrance=chemical, sodium chloride=safe, MIT=preservative=chemical, 99=numeric=garbage, F.I.L=FIL code=garbage-->
Output: {{"chemicals": ["sodium lauryl sulfate", "EDTA", "parfum", "MIT"], "safe": ["water", "sodium chloride"], "garbage": ["99", "F.I.L"]}}""",
}

# Default fallback prompt (cosmetic)
PROMPTS["default"] = PROMPTS["cosmetic"]

# Prompt version hash — used in cache keys so old cached results
# are invalidated automatically when prompts change
def _prompt_hash(domain: str) -> str:
    content = PROMPTS.get(domain, PROMPTS["default"])
    return hashlib.md5(content.encode()).hexdigest()[:8]


# ============================================================
# ALWAYS-CHEMICAL / ALWAYS-SAFE RULES  (post-processing)
# ============================================================
ALWAYS_CHEMICAL_PATTERNS = [
    r'CITRIC ACID', r'LACTIC ACID', r'PHOSPHORIC ACID', r'\bACID\b',
    r'PEG-', r'SULFATE', r'SULFONATE', r'BETAINE', r'POLYSORBATE',
    r'PARFUM', r'FRAGRANCE', r'LIMONENE', r'LINALOOL', r'GERANIOL',
    r'BENZOATE', r'PHENOXY', r'SORBATE',
    r'PARABEN', r'OXYBENZONE', r'HOMOSALATE', r'TRICLOSAN',
    r'DIMETHICONE', r'POLYQUATERNIUM', r'CYCLOPENTASILOXANE',
    r'SODIUM HYDROXIDE', r'EDTA', r'DTPA',
    r'LECITHIN', r'CETYL ALCOHOL', r'STEARYL ALCOHOL',
    r'MIT\b', r'CMIT\b', r'METHYLISOTHIAZOLINONE',
    r'SODIUM HYPOCHLORITE', r'HYDROGEN PEROXIDE',
    r'MONOSODIUM GLUTAMATE', r'MSG\b',
    r'ASPARTAME', r'SUCRALOSE', r'ACESULFAME',
    r'EXTRACT$',  # ends with EXTRACT
]

ALWAYS_SAFE_NAMES = {
    'AQUA', 'WATER', 'EAU',
    'SODIUM CHLORIDE',
    'GLYCERIN', 'GLYCEROL',
    'XANTHAN GUM', 'GUAR GUM',
    'TOCOPHEROL',
    'SIMMONDSIA CHINENSIS SEED OIL', 'JOJOBA SEED OIL',
    'BUTYROSPERMUM PARKII BUTTER', 'SHEA BUTTER',
    'COCOS NUCIFERA OIL', 'COCONUT OIL',
    'OLEA EUROPAEA FRUIT OIL', 'OLIVE OIL',
    'SUCROSE', 'GLUCOSE', 'FRUCTOSE',
    'CORN STARCH', 'POTATO STARCH',
}

# Safe fallback exact matches per domain (extends ALWAYS_SAFE_NAMES)
DOMAIN_SAFE_EXTRAS: Dict[str, set] = {
    "food":      {"VINEGAR", "PEPPER", "OREGANO", "TURMERIC", "CINNAMON", "SUNFLOWER OIL", "CANOLA OIL"},
    "detergent": set(),
    "cosmetic":  {"SORBITOL", "COCOA BUTTER"},
}


# ============================================================
# GROQ CLIENT
# ============================================================
class GroqClient:
    """Groq LLM client — single shared instance for all MCP servers.
    Method signatures are identical to v4.0 so MCP agents need no changes.
    """

    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise ValueError("GROQ_API_KEY not found in environment")

        # Models loaded from env so you can swap without code changes
        self.models = {
            "fast":     os.getenv("GROQ_MODEL_FAST",     "llama-3.1-8b-instant"),
            "balanced": os.getenv("GROQ_MODEL_BALANCED", "llama-3.3-70b-versatile"),
        }
        self.timeout = float(os.getenv("GROQ_TIMEOUT", "30.0"))
        self._init_client()

    def _init_client(self):
        self.client = Groq(
            api_key=self.api_key,
            max_retries=2,
            timeout=self.timeout
        )

    # ── LOW-LEVEL REQUEST ──────────────────────────────────────────────────────
    @retry_with_backoff(max_retries=3, initial_delay=1.0, backoff_factor=2.0)
    def _make_request(self, model: str, messages: list,
                      temperature: float, max_tokens: int):
        return self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens
        )

    # ── STRING HELPERS ─────────────────────────────────────────────────────────
    def _clean_string(self, text: str) -> str:
        if not isinstance(text, str):
            return str(text)
        text = text.strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]
        text = text.replace('\\"', '"').replace("\\'", "'")
        return text.strip()

    def _normalize_result_item(self, item: Union[str, dict]) -> dict:
        if isinstance(item, str):
            return {"name": self._clean_string(item), "reason": "LLM classification"}
        elif isinstance(item, dict):
            return {
                "name": self._clean_string(item.get("name", "")),
                "reason": item.get("reason", "LLM classification")
            }
        return {"name": self._clean_string(str(item)), "reason": "LLM classification"}

    # ── GARBAGE DETECTION  (single source — uses GARBAGE_PATTERNS) ────────────
    def _is_garbage_ingredient(self, name: str) -> Tuple[bool, str]:
        name_clean = name.strip().upper()
        if not name_clean:
            return True, "EMPTY"
        for pattern, reason in GARBAGE_PATTERNS:
            if re.match(pattern, name_clean):
                return True, reason
        return False, ""

    # ── JSON PARSING ───────────────────────────────────────────────────────────
    def _parse_llm_json(self, raw: str) -> Optional[dict]:
        """Strip CoT comment + markdown fences, then parse JSON."""
        # Remove <!-- reasoning --> block
        clean = re.sub(r'<!--.*?-->', '', raw, flags=re.DOTALL)
        # Remove markdown fences
        clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', clean, flags=re.MULTILINE).strip()

        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

        # Regex fallback
        result = {}
        for key in ("chemicals", "safe", "garbage"):
            m = re.search(rf'"{key}"\s*:\s*\[(.*?)\]', clean, re.DOTALL)
            if m:
                result[key] = [c.strip(' "\'') for c in m.group(1).split(',') if c.strip()]
        return result if result else None

    # ── SINGLE BATCH — fast model ──────────────────────────────────────────────
    def _classify_batch_fast(self, ingredients: List[str], domain: str) -> dict:
        prompt_text = PROMPTS.get(domain, PROMPTS["default"])
        user_msg = (
            f"Product type: {domain}\n\n"
            f"Ingredients: {', '.join(ingredients)}\n\n"
            "Classify each ingredient. Think step-by-step in a <!-- --> comment, then return the JSON."
        )
        try:
            resp = self._make_request(
                model=self.models["fast"],
                messages=[
                    {"role": "system", "content": prompt_text},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=2000
            )
            raw = resp.choices[0].message.content.strip()
            parsed = self._parse_llm_json(raw)
            if parsed is not None:
                return parsed
        except Exception as e:
            logger.warning(f"Fast model batch error: {e}")
        return {}

    # ── SINGLE BATCH — balanced model (escalation) ────────────────────────────
    def _classify_batch_balanced(self, ingredients: List[str], domain: str) -> dict:
        prompt_text = PROMPTS.get(domain, PROMPTS["default"])
        user_msg = (
            f"Product type: {domain}\n\n"
            f"Ingredients (uncertain from first pass): {', '.join(ingredients)}\n\n"
            "Classify carefully. Think step-by-step in a <!-- --> comment, then return the JSON."
        )
        try:
            resp = self._make_request(
                model=self.models["balanced"],
                messages=[
                    {"role": "system", "content": prompt_text},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=2000
            )
            raw = resp.choices[0].message.content.strip()
            parsed = self._parse_llm_json(raw)
            if parsed is not None:
                return parsed
        except Exception as e:
            logger.warning(f"Balanced model batch error: {e}")
        return {}

    # ── CACHED BATCH ───────────────────────────────────────────────────────────
    def _classify_batch_cached(self, ingredients_tuple: tuple, domain: str) -> dict:
        """Module-level cache keyed by (ingredients, domain, prompt_version)."""
        cache_key = f"{domain}:{_prompt_hash(domain)}:{','.join(ingredients_tuple)}"
        if cache_key in _classification_cache:
            return _classification_cache[cache_key]

        # First pass — fast model
        result = self._classify_batch_fast(list(ingredients_tuple), domain)

        # Identify unclassified ingredients for escalation
        classified = set()
        for key in ("chemicals", "safe", "garbage"):
            for item in result.get(key, []):
                classified.add((item if isinstance(item, str) else item).upper()
                               if isinstance(item, str)
                               else item.get("name", "").upper())

        unclassified = [i for i in ingredients_tuple if i.upper() not in classified]

        # Second pass — escalate unclassified to balanced model
        if unclassified:
            logger.info(f"Escalating {len(unclassified)} uncertain ingredients to balanced model")
            escalated = self._classify_batch_balanced(unclassified, domain)
            for key in ("chemicals", "safe", "garbage"):
                result.setdefault(key, [])
                result[key].extend(escalated.get(key, []))

        # Normalize to safe_skipped key (MCP compatibility)
        normalized = {
            "chemicals":   result.get("chemicals", []),
            "safe_skipped": result.get("safe",     []),
            "garbage":     result.get("garbage",   [])
        }
        _classification_cache[cache_key] = normalized
        return normalized

    # ── RULE ENFORCEMENT ───────────────────────────────────────────────────────
    def _enforce_classification_rules(self, result: dict, domain: str) -> dict:
        domain_safe = ALWAYS_SAFE_NAMES | DOMAIN_SAFE_EXTRAS.get(domain, set())

        chemicals  = [self._normalize_result_item(i) for i in result.get("chemicals",    [])]
        safe_items = [self._normalize_result_item(i) for i in result.get("safe_skipped", [])]

        final_chemicals, final_safe = [], []

        for chem in chemicals:
            name_upper = chem["name"].upper()
            if name_upper in domain_safe:
                final_safe.append(chem)
            else:
                final_chemicals.append(chem)

        for safe_item in safe_items:
            name_upper = safe_item["name"].upper()
            forced = False
            for pattern in ALWAYS_CHEMICAL_PATTERNS:
                if re.search(pattern, name_upper, re.IGNORECASE):
                    final_chemicals.append({
                        "name":       safe_item["name"],
                        "reason":     "FORCED_CHEMICAL_BY_RULE",
                        "unverified": False
                    })
                    forced = True
                    break
            if not forced:
                final_safe.append(safe_item)

        return {"chemicals": final_chemicals, "safe_skipped": final_safe}

    # ── RULE-BASED FALLBACK ────────────────────────────────────────────────────
    def _fallback_classification(self, ingredients: List[str], domain: str) -> dict:
        """Pure rule-based fallback — uses GARBAGE_PATTERNS (single source)."""
        domain_safe = ALWAYS_SAFE_NAMES | DOMAIN_SAFE_EXTRAS.get(domain, set())

        chemicals, safe_skipped, garbage = [], [], []

        for ing in ingredients:
            name_upper = ing.upper()

            # Garbage check — uses the ONE shared list
            is_garbage = False
            for pattern, reason in GARBAGE_PATTERNS:
                if re.match(pattern, name_upper):
                    garbage.append(ing)
                    is_garbage = True
                    break
            if is_garbage:
                continue

            if name_upper in domain_safe:
                safe_skipped.append(ing)
                continue

            # Chemical pattern check
            is_chemical = False
            for pattern in ALWAYS_CHEMICAL_PATTERNS:
                if re.search(pattern, name_upper, re.IGNORECASE):
                    chemicals.append(ing)
                    is_chemical = True
                    break

            if not is_chemical:
                chemicals.append(ing)   # default: treat as chemical

        return {"chemicals": chemicals, "safe_skipped": safe_skipped, "garbage": garbage}

    # ── SMART FALLBACK (per ingredient) ───────────────────────────────────────
    def _smart_fallback(self, ingredient_name: str, domain: str) -> dict:
        name_upper = ingredient_name.upper()
        domain_safe = ALWAYS_SAFE_NAMES | DOMAIN_SAFE_EXTRAS.get(domain, set())

        if name_upper in domain_safe:
            return {"category": "safe_skip", "reason": "Matches safe set (fallback)"}

        for pattern in ALWAYS_CHEMICAL_PATTERNS:
            if re.search(pattern, name_upper, re.IGNORECASE):
                return {"category": "chemical", "reason": f"Matches {pattern} (fallback)", "unverified": True}

        return {"category": "chemical", "reason": "Unknown — treating as chemical (fallback)", "unverified": True}

    # ============================================================
    # MAIN PUBLIC METHOD — signature identical to v4.0
    # ============================================================
    def classify_ingredients(self, ingredients: list, usage: str = "cosmetic") -> dict:
        """Multi-stage classification. Return shape unchanged for MCP compatibility."""
        # Normalize domain
        domain = usage.lower()
        if domain not in PROMPTS:
            logger.warning(f"Unknown domain '{domain}', falling back to 'cosmetic'")
            domain = "cosmetic"

        if not ingredients:
            return {"chemicals": [], "safe_skipped": [], "garbage": []}

        # Extract names
        ingredient_names: List[str] = []
        for ing in ingredients:
            name = (ing.get("name", "") if isinstance(ing, dict) else str(ing)).strip()
            if name:
                ingredient_names.append(name)

        # Step 1 — Pre-filter garbage
        valid_ingredients: List[str] = []
        garbage: List[dict] = []

        for name in ingredient_names:
            is_g, reason = self._is_garbage_ingredient(name)
            if is_g:
                garbage.append({"name": name, "reason": reason})
            else:
                valid_ingredients.append(name)

        if not valid_ingredients:
            return {"chemicals": [], "safe_skipped": [], "garbage": garbage}

        # Step 2 — Batch LLM classification (cached, two-pass)
        all_chemicals_raw: List = []
        all_safe_raw: List      = []
        batch_size = 15

        for idx, i in enumerate(range(0, len(valid_ingredients), batch_size)):
            batch = valid_ingredients[i:i + batch_size]
            try:
                result = self._classify_batch_cached(tuple(batch), domain)
            except Exception as e:
                logger.warning(f"Batch LLM failed, using rule fallback: {e}")
                result = self._fallback_classification(batch, domain)

            all_chemicals_raw.extend(result.get("chemicals",    []))
            all_safe_raw.extend(     result.get("safe_skipped", []))

            for g in result.get("garbage", []):
                garbage.append({
                    "name":   self._clean_string(g if isinstance(g, str) else g.get("name", "")),
                    "reason": "LLM classified as garbage"
                })

            # Throttle between batches to respect RPM limits
            if i + batch_size < len(valid_ingredients):
                time.sleep(0.3)

        # Step 3 — Apply domain rules
        processed = self._enforce_classification_rules(
            {"chemicals": all_chemicals_raw, "safe_skipped": all_safe_raw},
            domain
        )

        # Step 4 — Track existing (by name, uppercase)
        existing_chemical = {c["name"].upper() for c in processed.get("chemicals",    [])}
        existing_safe     = {s["name"].upper() for s in processed.get("safe_skipped", [])}

        # Step 5 — Smart fallback for missed ingredients
        for ing_name in valid_ingredients:
            name_upper = ing_name.upper()
            if name_upper not in existing_chemical and name_upper not in existing_safe:
                fb = self._smart_fallback(ing_name, domain)
                if fb["category"] == "chemical":
                    processed.setdefault("chemicals", []).append({
                        "name":       ing_name,
                        "reason":     fb["reason"],
                        "unverified": fb.get("unverified", True)
                    })
                else:
                    processed.setdefault("safe_skipped", []).append({
                        "name":   ing_name,
                        "reason": fb["reason"]
                    })

        # Step 6 — Deduplicate by name (keep first occurrence)
        unique_chemicals: Dict[str, dict] = {}
        for chem in processed.get("chemicals", []):
            key = chem.get("name", "").upper()
            if key and key not in unique_chemicals:
                unique_chemicals[key] = chem

        unique_safe: Dict[str, dict] = {}
        for s in processed.get("safe_skipped", []):
            key = s.get("name", "").upper()
            if key and key not in unique_safe:
                unique_safe[key] = s

        # Step 7 — Ensure required fields
        final_chemicals = list(unique_chemicals.values())
        final_safe      = list(unique_safe.values())

        for c in final_chemicals:
            c.setdefault("unverified", False)
            c.setdefault("reason",     "LLM classification")

        for s in final_safe:
            s.setdefault("reason", "LLM classification")

        return {
            "chemicals":    final_chemicals,
            "safe_skipped": final_safe,
            "garbage":      garbage
        }

    # ============================================================
    # RISK ESTIMATION — signature identical to v4.0
    # ============================================================
    def estimate_chemical_risk(self, chemical_name: str) -> dict:
        if chemical_name in _risk_cache:
            return _risk_cache[chemical_name]
        result = self._estimate_chemical_risk_uncached(chemical_name)
        _risk_cache[chemical_name] = result
        return result

    def _estimate_chemical_risk_uncached(self, chemical_name: str) -> dict:
        prompt = (
            f"Chemical: {chemical_name}\n\n"
            "Estimate the safety risk level for human exposure.\n\n"
            'Return JSON: {"risk": "CRITICAL|HIGH|MODERATE|LOW|SAFE|UNKNOWN", '
            '"confidence": 0.0-1.0, "reasoning": "..."}'
        )
        try:
            resp = self._make_request(
                model=self.models["fast"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300
            )
            raw   = resp.choices[0].message.content.strip()
            clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
            return json.loads(clean)
        except Exception as e:
            return {"risk": "UNKNOWN", "confidence": 0.2, "reasoning": f"Error: {e}"}

    # ── ORGANS ────────────────────────────────────────────────────────────────
    def estimate_organs(self, chemical_name: str, hazard_codes: list) -> dict:
        cache_key = f"{chemical_name}:{','.join(hazard_codes)}"
        if cache_key in _organs_cache:
            return _organs_cache[cache_key]
        result = self._estimate_organs_uncached(chemical_name, hazard_codes)
        _organs_cache[cache_key] = result
        return result

    def _estimate_organs_uncached(self, chemical_name: str, hazard_codes: list) -> dict:
        prompt = (
            f"Chemical: {chemical_name}\n"
            f"Hazard codes: {hazard_codes if hazard_codes else 'None'}\n\n"
            "Estimate target organs affected.\n\n"
            'Return JSON: {"organs": ["skin", "liver", ...], "confidence": 0.0-1.0, "reasoning": "..."}'
        )
        try:
            resp = self._make_request(
                model=self.models["fast"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200
            )
            raw   = resp.choices[0].message.content.strip()
            clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()
            return json.loads(clean)
        except Exception as e:
            return {"organs": [], "confidence": 0.2, "reasoning": f"Error: {e}"}


# ============================================================
# SINGLETON
# ============================================================
_groq_client: Optional[GroqClient] = None


def get_groq_client() -> GroqClient:
    global _groq_client
    if _groq_client is None:
        _groq_client = GroqClient()
    return _groq_client


# ============================================================
# QUICK SELF-TEST
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    client = get_groq_client()
    print("✅ Groq client initialized", file=sys.stderr)

    tests = {
        "cosmetic": [
            "AQUA", "CITRIC ACID", "SODIUM LAURETH SULFATE", "COCO-BETAINE",
            "POLYSORBATE 20", "PEG-200 HYDROGENATED GLYCERYL PALMATE",
            "PARFUM", "2", "Z288697", "F.I.L", "SHEA BUTTER", "METHYLPARABEN",
        ],
        "food": [
            "WATER", "SODIUM BENZOATE", "SUCROSE", "RED 40",
            "OLIVE OIL", "MSG", "ASPARTAME", "Z123456",
        ],
        "detergent": [
            "AQUA", "SODIUM LAURYL SULFATE", "EDTA", "PARFUM",
            "SODIUM CHLORIDE", "MIT", "99", "F.I.L", "PROTEASE",
        ],
    }

    for domain, ings in tests.items():
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"DOMAIN: {domain.upper()} — {len(ings)} ingredients", file=sys.stderr)
        result = client.classify_ingredients(ings, domain)
        print(f"  🔬 CHEMICALS ({len(result['chemicals'])}):", file=sys.stderr)
        for c in result["chemicals"]:
            flag = " ⚠️ UNVERIFIED" if c.get("unverified") else ""
            print(f"    - {c['name']}: {c['reason']}{flag}", file=sys.stderr)
        print(f"  ✅ SAFE ({len(result['safe_skipped'])}):", file=sys.stderr)
        for s in result["safe_skipped"]:
            print(f"    - {s['name']}: {s['reason']}", file=sys.stderr)
        print(f"  🗑️  GARBAGE ({len(result['garbage'])}):", file=sys.stderr)
        for g in result["garbage"]:
            print(f"    - {g['name']}: {g['reason']}", file=sys.stderr)

    print(f"\n{'='*60}", file=sys.stderr)
    print("✅ All domains tested.", file=sys.stderr)