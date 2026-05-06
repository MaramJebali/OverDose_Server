"""agent/agent.py — persistent event loop fix for Django compatibility"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows requires ProactorEventLoop for subprocess support
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Add the mcp_agent parent to path
MCP_AGENT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(MCP_AGENT_ROOT))

from groq import Groq
import config as agent_config
from agent.state import AgentState
from models.output_schema import (
    FinalReport, ProductOutput, IngredientsSection, ChemicalEvaluation,
    ResolutionInfo, IdentityInfo, HazardInfo, BodyEffectsInfo, DoseEvaluationInfo,
    ChemicalVerdict, SafeSkipped, UnverifiedChemical, CombinationRisks,
    OrganOverlap, CumulativePresence, ProductSummary, GlobalSummary,
    ExposureEffects, OrganGlobalAnalysis,
    create_resolution_info, create_identity_info, create_hazard_info,
    create_body_effects, create_dose_evaluation, create_verdict
)
from servers.kg_server.risk_scoring import calculate_risk_for_chemical

logger = logging.getLogger(__name__)
logger.propagate = False

SERVER_PATHS = {
    "kg":          str(MCP_AGENT_ROOT / "servers/kg_server/server.py"),
    "filter":      str(MCP_AGENT_ROOT / "servers/filter_server/server.py"),
    "combination": str(MCP_AGENT_ROOT / "servers/combination_server/server.py"),
    "evaluation":  str(MCP_AGENT_ROOT / "servers/evaluation_server/server.py"),
    "scoring":     str(MCP_AGENT_ROOT / "servers/scoring_server/server.py"),
    "profile":     str(MCP_AGENT_ROOT / "servers/profile_server/server.py"),
}

class MCPClient:
    def __init__(self, server_name: str, server_path: str):
        self.name = server_name
        self.path = server_path
        self.process = None
        self._id = 0

    async def start(self):
        logger.info(f"Starting server '{self.name}' from {self.path}")
        server_dir = str(Path(self.path).parent)
        env = os.environ.copy()
        env.update({
            "CHROMA_LOG_LEVEL": "WARNING",
            "GRPC_VERBOSITY": "ERROR",
            "TRANSFORMERS_VERBOSITY": "error",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "1",
        })
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, self.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=server_dir,
            env=env
        )
        await asyncio.sleep(2.0)  # Allow server to initialise
        if self.process.returncode is not None:
            stderr = await self.process.stderr.read()
            raise RuntimeError(f"Server {self.name} died. stderr: {stderr.decode()[:500]}")
        await self._send({"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize", "params": {}})
        await self._recv()
        logger.info(f"Server '{self.name}' started and initialized")

    async def stop(self):
        if self.process:
            logger.info(f"Stopping server '{self.name}'")
            try:
                self.process.stdin.close()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except Exception:
                self.process.kill()
            logger.info(f"Server '{self.name}' stopped")

    def _next_id(self):
        self._id += 1
        return self._id

    async def _send(self, payload):
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        await self.process.stdin.drain()

    async def _recv(self):
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise ConnectionError(f"Server {self.name} closed stdout")
            line = line.decode().strip()
            if not line:
                continue
            if not (line.startswith("{") or line.startswith("[")):
                logger.warning(f"Server {self.name} sent non-JSON: {line[:100]}")
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"Server {self.name} sent invalid JSON: {line[:100]}")
                continue

    async def call(self, tool_name, arguments):
        logger.debug(f"MCP call to {self.name}.{tool_name}")
        await self._send({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments}
        })
        resp = await self._recv()
        if "error" in resp:
            raise RuntimeError(f"MCP error [{self.name}/{tool_name}]: {resp['error']}")
        content = resp.get("result", {}).get("content", [])
        text = content[0].get("text", "{}") if content else "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

class GroqCaller:
    def __init__(self):
        self._client = Groq(api_key=agent_config.GROQ_API_KEY)

    def call(self, system, user, max_tokens=2000):
        resp = self._client.chat.completions.create(
            model=agent_config.GROQ_MODEL, temperature=0, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content.strip()

    def call_json(self, system, user, max_tokens=2000):
        raw = self.call(system, user + "\nReturn only valid JSON, no markdown.", max_tokens)
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\}|\[.*\])", clean, re.DOTALL)
            if m:
                return json.loads(m.group(1))
            return {"raw": raw}

class BiologicalAgent:
    def __init__(self, start_servers=True):
        self.groq = GroqCaller()
        self.clients = {}
        self.state = AgentState()
        self.user_type = None
        self._servers_started = False
        # One persistent loop for the entire agent lifetime.
        # Subprocess pipes (stdin/stdout) are bound to the loop that created them
        # and CANNOT be used from a different loop — so we must never close this loop.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        if start_servers:
            self._start_sync()

    def _start_sync(self):
        self._loop.run_until_complete(self._start_servers())
        self._servers_started = True

    async def _start_servers(self):
        logger.info("Starting all MCP servers...")
        for name, path in SERVER_PATHS.items():
            c = MCPClient(name, path)
            await c.start()
            self.clients[name] = c
            await asyncio.sleep(2.0)
        logger.info("All servers started")

    async def _stop_servers(self):
        for name, c in self.clients.items():
            await c.stop()
        self._servers_started = False

    def close(self):
        if not self._servers_started:
            return
        try:
            self._loop.run_until_complete(self._stop_servers())
        finally:
            self._loop.close()

    async def _kg(self, tool, **kwargs):
        return await self.clients["kg"].call(tool, kwargs)

    async def _filter_call(self, ingredients: list, usage: str) -> dict:
        return await self.clients["filter"].call("classify_ingredients", {
            "ingredients": ingredients, "usage": usage,
        })

    async def _combo(self, tool, arguments=None, **kwargs):
        if arguments is None:
            arguments = {}
        if kwargs:
            arguments.update(kwargs)
        return await self.clients["combination"].call(tool, arguments)

    async def _profile(self, tool: str, arguments: dict) -> dict:
        return await self.clients["profile"].call(tool, arguments)

    async def _scoring(self, tool, arguments):
        if "scoring" not in self.clients:
            return {"error": "Scoring server not available"}
        return await self.clients["scoring"].call(tool, arguments)

    # ── Phase 1: Product context ──────────────────────────────────────────────
    def _analyze_product_context(self, products_list: list) -> dict:
        product_count = len(products_list)
        exposure_types = [p.get("exposure_type") for p in products_list if p.get("exposure_type")]
        return {
            "product_count": product_count,
            "needs_cumulative": product_count >= 2,
            "has_mixed_usage": len(set(exposure_types)) > 1 if exposure_types else False,
            "strategy": "multiple" if product_count >= 2 else "single",
        }

    # ── Escalation enforcement ────────────────────────────────────────────────
    def _enforce_escalation(self, products_list: list, combination: dict) -> None:
        escalation = combination.get("organ_overlap", {}).get("verdict_escalation")
        if escalation == "HIGH":
            logger.info("Escalation enforced: flagging products as HIGH due to organ overlap")
            for product in products_list:
                product["_enforced_risk"] = "HIGH"

    # ── Personalisation ───────────────────────────────────────────────────────
    async def _apply_personalisation(
        self, finding: dict, chemical_name: str, current_risk: str
    ) -> None:
        if not self.user_type:
            return

        user_type_map = {
            "asthma": "Asthma",
            "diabetes": "Diabetes",
            "newborn": "Newborn",
            "fetal": "Fetal",
            "pcos": "PCOS",
        }
        profile_key = user_type_map.get(self.user_type.lower())
        if not profile_key:
            logger.debug(f"No profile mapping for user_type '{self.user_type}'")
            return

        try:
            result = await self._profile("analyze_ingredient", {
                "user_type": profile_key,
                "ingredient": chemical_name,
                "generate_llm": True,
            })

            if not result.get("found"):
                logger.debug(f"No personalisation data for '{chemical_name}' / {profile_key}")
                finding["personalisation"] = {
                    "user_type": self.user_type,
                    "found": False,
                    "message": "No entry in profile knowledge base",
                }
                return

            kb_entry = result.get("kb_entry", {})
            profile_risk_level = result.get("risk_level")
            llm_analysis = result.get("llm_analysis")
            inference_score = kb_entry.get("Inference Score", "0")

            finding["personalisation"] = {
                "user_type": self.user_type,
                "profile_key": profile_key,
                "inference_score": inference_score,
                "risk_level": profile_risk_level,
                "kb_entry": kb_entry,
                "disease_name": kb_entry.get("Disease Name"),
                "direct_evidence": kb_entry.get("Direct Evidence"),
                "llm_analysis": llm_analysis,
                "found": True,
            }

            profile_to_verdict = {"High": "HIGH", "Moderate": "MODERATE", "Low": "LOW"}
            mapped_profile_risk = profile_to_verdict.get(profile_risk_level)
            if not mapped_profile_risk:
                return

            risk_priority = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "LOW": 1, "UNKNOWN": 0}
            if risk_priority.get(mapped_profile_risk, 0) > risk_priority.get(current_risk, 0):
                finding["preliminary_risk"] = mapped_profile_risk
                finding["personalisation_adjusted"] = True
                finding["personalisation_reason"] = (
                    f"Profile server returned {profile_risk_level} risk for "
                    f"{self.user_type} patient (inference score {inference_score}), "
                    f"upgrading from {current_risk}."
                )
                logger.info(
                    f"Personalisation upgraded '{chemical_name}': "
                    f"{current_risk} -> {mapped_profile_risk}"
                )
            else:
                finding["personalisation_adjusted"] = False

        except Exception as e:
            logger.warning(f"Profile server call failed for '{chemical_name}': {e}")
            finding["personalisation_error"] = str(e)

    # ── Phase A: Filter ───────────────────────────────────────────────────────
    async def _phase_filter(self, products_list: list) -> dict:
        seen_chemicals = set()
        seen_safe = set()
        all_chemicals = []
        all_safe_skipped = []

        for product in products_list:
            usage = product.get("product_usage", "cosmetic")
            product_name = product.get("product_name", "unknown")

            new_ingredients = []
            for ing in product.get("ingredient_list", []):
                name = ing.get("name", "").strip()
                if not name:
                    continue
                key = name.upper()
                if key not in seen_chemicals and key not in seen_safe:
                    new_ingredients.append({"name": name})

            if not new_ingredients:
                continue

            logger.info(f"Filter: classifying {len(new_ingredients)} new ingredients from '{product_name}' as usage='{usage}'")
            result = await self._filter_call(ingredients=new_ingredients, usage=usage)

            for chem in result.get("chemicals", []):
                key = chem.get("name", "").upper()
                if key and key not in seen_chemicals:
                    seen_chemicals.add(key)
                    all_chemicals.append(chem)

            for safe in result.get("safe_skipped", []):
                key = safe.get("name", "").upper()
                if key and key not in seen_safe and key not in seen_chemicals:
                    seen_safe.add(key)
                    all_safe_skipped.append(safe)

        logger.info(f"Filter complete: {len(all_chemicals)} chemicals to investigate, {len(all_safe_skipped)} safe skipped")
        return {
            "chemicals": all_chemicals,
            "safe_skipped": all_safe_skipped,
        }

    # ── Phase B: Investigate each chemical ───────────────────────────────────
    async def _investigate_chemical(self, name: str, product_usage: str = "cosmetics") -> dict:
        logger.debug(f"=== Investigating chemical: {name} (usage: {product_usage}) ===")

        if self.state.is_investigated(name):
            logger.debug(f"Chemical {name} already investigated, returning cached finding")
            for f in self.state.findings:
                if f.get("name") == name:
                    return f
            return {"name": name, "skipped": True}

        finding = {"name": name}

        resolution = await self._kg("resolve_ingredient", ingredient_name=name)
        finding["resolution"] = resolution

        if not resolution.get("unresolved"):
            uid = resolution["uid"]
            logger.info(f"Chemical '{name}' found in KG. UID: {uid}, strategy: {resolution.get('match_strategy')}, confidence: {resolution.get('confidence')}")
            finding["uid"] = uid
            self.state.mark_resolved(name, uid)

            complete_data = await self._kg("get_complete_chemical_data", chemical_uid=uid)
            finding["complete_data"] = complete_data

            ghs_pictograms = complete_data.get("ghs_pictograms", [])
            h_codes = complete_data.get("h_codes", [])
            target_organs = complete_data.get("target_organs", [])
            chemical_classes = complete_data.get("chemical_classes", [])

            organ_names = [o.get("name") for o in target_organs if o.get("name")]

            logger.info(f"Complete data for '{name}': h_codes={h_codes}, organs={organ_names}, classes={chemical_classes}")

            finding["h_codes"] = h_codes
            finding["target_organs"] = organ_names
            finding["chemical_classes"] = chemical_classes

            risk_result = calculate_risk_for_chemical(
                ghs_pictograms=ghs_pictograms,
                h_codes=h_codes,
                target_organs=target_organs,
                chemical_classes=chemical_classes,
                product_usage=product_usage,
            )

            logger.info(f"Risk for '{name}': hazard={risk_result['hazard_score']}, organ={risk_result['organ_score']}, class={risk_result['class_score']}, usage={risk_result['usage_score']} → total={risk_result['total_score']} → verdict={risk_result['verdict']}")

            finding.update({
                "preliminary_risk": risk_result["verdict"],
                "risk_score": risk_result["total_score"],
                "risk_breakdown": risk_result["breakdown"],
                "confidence": complete_data.get("data_confidence", 0.7),
                "kg_confidence": complete_data.get("data_confidence", 0.7),
                "source": "KG",
                "full_profile": complete_data,
                "hazard": {
                    "h_codes": h_codes,
                    "highest_signal": complete_data.get("highest_signal", "None"),
                    "has_critical_hazard": complete_data.get("has_critical_hazard", False),
                },
                "exposure_limits": {
                    "exposure_limits": complete_data.get("exposure_limits", [])
                },
            })

            await self._apply_personalisation(finding, name, risk_result["verdict"])
            self.state.add_finding(finding)
            return finding

        # NOT in KG — try PubChem fallback
        logger.debug(f"Chemical '{name}' not found in KG — trying PubChem fallback...")
        try:
            from servers.kg_server.pubchem_client import enrich_chemical_from_pubchem
            pubchem_data = enrich_chemical_from_pubchem(name)
        except Exception as e:
            logger.warning(f"PubChem fallback failed for '{name}': {e}")
            pubchem_data = {"found": False}

        if pubchem_data.get("found"):
            logger.info(
                f"PubChem found '{name}' (CID={pubchem_data.get('cid')}): "
                f"h_codes={pubchem_data.get('ghs_h_codes', [])}, "
                f"organs={pubchem_data.get('target_organs', [])}, "
                f"classes={pubchem_data.get('kg_classes', [])}"
            )

            # Build organ dicts compatible with risk_scoring
            pc_organs = [{"name": o, "uid": None} for o in pubchem_data.get("target_organs", [])]
            pc_h_codes = pubchem_data.get("ghs_h_codes", [])
            pc_pictograms = [{"name": p} for p in pubchem_data.get("ghs_pictograms", [])]
            pc_classes = pubchem_data.get("kg_classes", [])

            risk_result = calculate_risk_for_chemical(
                ghs_pictograms=pc_pictograms,
                h_codes=pc_h_codes,
                target_organs=pc_organs,
                chemical_classes=pc_classes,
                product_usage=product_usage,
            )

            logger.info(
                f"PubChem risk for '{name}': total={risk_result['total_score']} "
                f"→ verdict={risk_result['verdict']}"
            )

            pc_confidence = 0.4
            if pc_h_codes:
                pc_confidence += 0.2
            if pc_organs:
                pc_confidence += 0.15
            if pc_classes:
                pc_confidence += 0.1
            pc_confidence = min(round(pc_confidence, 2), 0.85)

            props = pubchem_data.get("properties") or {}
            preferred = (
                props.get("IUPACName")
                or props.get("Title")
                or name
            )
            finding["resolution"] = {
                "original_name": name,
                "uid": None,
                "name": preferred,
                "preferred_name": preferred,
                "cas": None,
                "molecular_formula": props.get("MolecularFormula"),
                "molecular_weight": (
                    str(props["MolecularWeight"]) if props.get("MolecularWeight") is not None else None
                ),
                "description": pubchem_data.get("description"),
                "synonyms": [],
                "match_strategy": "pubchem_match",
                "confidence": pc_confidence,
                "unresolved": False,
                "error": None,
                "pubchem_cid": pubchem_data.get("cid"),
            }

            finding.update({
                "preliminary_risk": risk_result["verdict"],
                "risk_score": risk_result["total_score"],
                "risk_breakdown": risk_result["breakdown"],
                "confidence": pc_confidence,
                "kg_confidence": 0.0,
                "h_codes": pc_h_codes,
                "target_organs": [o.get("name") for o in pc_organs if o.get("name")],
                "chemical_classes": pc_classes,
                "source": "PUBCHEM",
                "pubchem_cid": pubchem_data.get("cid"),
                "hazard": {
                    "h_codes": pc_h_codes,
                    "highest_signal": pubchem_data.get("ghs_signal", "None"),
                    "has_critical_hazard": any(
                        c in {"H340", "H341", "H350", "H351", "H360", "H361", "H370", "H372"}
                        for c in pc_h_codes
                    ),
                },
                "exposure_limits": {"exposure_limits": []},
                "full_profile": {
                    "description": pubchem_data.get("description"),
                    "data_confidence": pc_confidence,
                    "preferred_name": preferred,
                    "molecular_formula": props.get("MolecularFormula"),
                    "chemical_classes": pc_classes,
                },
            })

            await self._apply_personalisation(finding, name, risk_result["verdict"])
            self.state.add_finding(finding)
            return finding

        # Neither KG nor PubChem found this chemical (expected often for OCR fragments)
        logger.debug(f"Chemical '{name}' not found in KG or PubChem — marking UNKNOWN.")
        finding.update({
            "preliminary_risk": "UNKNOWN",
            "confidence": 0.0,
            "kg_confidence": 0.0,
            "recommended_depth": "basic",
            "reasoning": f"{name} not found in Knowledge Graph or PubChem",
            "h_codes": [],
            "target_organs": [],
            "chemical_classes": [],
            "source": "UNKNOWN",
        })

        await self._apply_personalisation(finding, name, "UNKNOWN")
        self.state.mark_unresolved(name)
        self.state.add_finding(finding)
        return finding

    # ── Phase C: Combination analysis ────────────────────────────────────────
    async def _phase_combination(self, findings: list, products_list: list) -> dict:
        logger.info("Starting combination analysis...")

        product_ingredients = {
            p.get("product_id", "unknown"): {
                ing.get("name", "").upper()
                for ing in p.get("ingredient_list", [])
            }
            for p in products_list
        }

        seen = set()
        unique_chemicals = []
        for finding in findings:
            if finding.get("skipped"):
                continue
            chem_name = finding.get("name", "")
            for product in products_list:
                pid = product.get("product_id", "unknown")
                if chem_name.upper() in product_ingredients.get(pid, set()):
                    key = (chem_name, pid)
                    if key not in seen:
                        seen.add(key)
                        unique_chemicals.append({
                            "name": chem_name,
                            "uid": finding.get("uid"),
                            "target_organs": finding.get("target_organs", []),
                            "h_codes": finding.get("h_codes", []),
                            "product_id": pid,
                        })

        logger.info(f"Running organ overlap with {len(unique_chemicals)} unique chemical-product pairs")
        organ_result = await self._combo("check_organ_overlap", {
            "chemicals": unique_chemicals,
            "global_mode": True,
        })

        global_analysis = organ_result.get("global_organ_analysis", {})
        if global_analysis:
            logger.info(f"Organ overlap: {len(global_analysis)} organs affected")
            for organ, data in global_analysis.items():
                logger.info(f"  {organ}: {data.get('total_unique_count', 0)} chemicals")

        # Cumulative presence
        freq = {}
        for p in products_list:
            pid = p.get("product_id", "?")
            pname = p.get("product_name", "?")
            exp_type = p.get("exposure_type", "unknown")
            for ing in p.get("ingredient_list", []):
                n = ing.get("name", "").strip()
                if n:
                    freq.setdefault(n.upper(), []).append({
                        "product_id": pid,
                        "product_name": pname,
                        "original_name": n,
                        "exposure_type": exp_type,
                    })

        cumulative_flags = []
        for key, prod_list in freq.items():
            if len(prod_list) >= 2:
                logger.info(f"Chemical '{key}' in {len(prod_list)} products — checking cumulative")
                cf = await self._combo("check_cumulative_presence",
                    chemical_name=prod_list[0]["original_name"],
                    products=prod_list)
                if cf.get("is_cumulative"):
                    cumulative_flags.append(cf)

        hazard_profiles = [
            {"name": f.get("name", ""), "h_codes": f.get("h_codes", [])}
            for f in findings if not f.get("skipped")
        ]
        hazard_intersection = await self._combo(
            "check_hazard_intersection", chemicals=hazard_profiles
        )

        logger.info("Combination analysis completed")
        return {
            "organ_overlap": organ_result,
            "cumulative_flags": cumulative_flags,
            "hazard_intersection": hazard_intersection,
        }

    # ── Phase D: Build final report ───────────────────────────────────────────
    def _build_final_report(
        self, products_list: list, filter_result: dict,
        findings: list, combination: dict
    ) -> dict:
        logger.info("Building final report...")
        report_id = f"rpt_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        organ_overlap_result = combination.get("organ_overlap", {})
        global_organ_analysis_raw = organ_overlap_result.get("global_organ_analysis", {})

        global_organ_analysis = {
            organ: OrganGlobalAnalysis(
                unique_chemicals=data.get("unique_chemicals", []),
                total_unique_count=data.get("total_unique_count", 0),
                chemical_frequency=data.get("chemical_frequency", {}),
                products_per_chemical=data.get("products_per_chemical", {}),
            )
            for organ, data in global_organ_analysis_raw.items()
        }

        products_output = []

        for product in products_list:
            product_id = product.get("product_id", "unknown")
            product_name = product.get("product_name", "Unknown Product")
            usage = product.get("product_usage", "unknown")
            exposure_type = [product.get("exposure_type")] if product.get("exposure_type") else []

            product_ingredients = {
                ing.get("name", "").upper()
                for ing in product.get("ingredient_list", [])
            }

            drivers = [
                f.get("name", "")
                for f in findings
                if not f.get("skipped")
                and f.get("name", "").upper() in product_ingredients
                and f.get("preliminary_risk") in {"CRITICAL", "HIGH"}
            ]

            chemicals_evaluated = []
            safe_skipped_list = []
            unverified_list = []

            for f in findings:
                if f.get("skipped"):
                    continue

                chem_name = f.get("name", "")
                if chem_name.upper() not in product_ingredients:
                    continue

                uid = f.get("uid")
                cas = f.get("resolution", {}).get("cas") if f.get("resolution") else None

                res_conf = (
                    f.get("confidence", 0.5)
                    if f.get("source") == "PUBCHEM"
                    else f.get("kg_confidence", 0.5)
                )
                resolution_info = create_resolution_info(
                    f.get("resolution", {}),
                    res_conf,
                )

                full_profile = f.get("full_profile", {})
                identity_info = create_identity_info(full_profile, confidence=f.get("confidence", 0.5))
                hazard_info = create_hazard_info(f.get("hazard", {}), confidence=f.get("confidence", 0.5))
                body_effects = create_body_effects(full_profile, confidence=f.get("confidence", 0.5))
                dose_eval = create_dose_evaluation(f.get("exposure_limits", {}))

                justifications = []
                if f.get("reasoning"):
                    justifications.append(f.get("reasoning"))
                if f.get("fusion_reasoning"):
                    justifications.append(f.get("fusion_reasoning"))
                if f.get("risk_breakdown"):
                    justifications.append(
                        f"Risk score: {f.get('risk_score', 0)} - "
                        f"{f.get('preliminary_risk', 'UNKNOWN')}"
                    )
                if f.get("personalisation_adjusted"):
                    justifications.append(
                        f.get("personalisation_reason",
                              "Risk upgraded based on user-type personalisation")
                    )
                if not justifications:
                    justifications.append(f"Risk level: {f.get('preliminary_risk', 'UNKNOWN')}")

                final_risk = f.get("preliminary_risk", "UNKNOWN")
                verdict = create_verdict(final_risk, justifications, f.get("risk_breakdown"))

                chemicals_evaluated.append(ChemicalEvaluation(
                    name=chem_name,
                    uid=uid,
                    cas=cas,
                    resolution=resolution_info,
                    identity=identity_info,
                    hazard=hazard_info,
                    body_effects=body_effects,
                    dose_evaluation=dose_eval,
                    verdict=verdict,
                    personalisation=f.get("personalisation"),
                ))

            for safe in filter_result.get("safe_skipped", []):
                safe_name = safe.get("name", "")
                if safe_name.upper() in product_ingredients:
                    safe_skipped_list.append(SafeSkipped(
                        name=safe_name,
                        reason=safe.get("reason", "Classified as non-chemical"),
                    ))

            for f in findings:
                if (
                    f.get("resolution", {}).get("unresolved")
                    and f.get("source") != "PUBCHEM"
                    and f.get("name", "").upper() in product_ingredients
                ):
                    unverified_list.append(UnverifiedChemical(
                        name=f.get("name", ""),
                        reason=f.get("reasoning", "Not found in Knowledge Graph"),
                        flag="unverified_chemical",
                    ))

            product_specific_overlaps = []
            for organ, data in global_organ_analysis_raw.items():
                chem_list = [
                    chem
                    for chem, products_for_chem in data.get("products_per_chemical", {}).items()
                    if product_id in products_for_chem
                ]
                if chem_list:
                    product_specific_overlaps.append({
                        "organ": organ,
                        "chemicals": chem_list,
                        "count": len(chem_list),
                    })

            organ_overlap_obj = OrganOverlap(
                fetch_status="done",
                has_overlap=len(product_specific_overlaps) > 0,
                verdict_escalation=organ_overlap_result.get("verdict_escalation"),
                overlapping_organs=product_specific_overlaps or None,
                note=organ_overlap_result.get("summary"),
                error_message=None,
            )

            cumulative_flags_for_product = [
                cf for cf in combination.get("cumulative_flags", [])
                if cf.get("chemical_name", "").upper() in product_ingredients
            ]
            cumulative_obj = CumulativePresence(
                fetch_status="done" if cumulative_flags_for_product else "skipped",
                checked=bool(cumulative_flags_for_product),
                note=(
                    f"{len(cumulative_flags_for_product)} chemical(s) appear in multiple products"
                    if cumulative_flags_for_product
                    else "No cumulative concerns detected"
                ),
            )

            risk_counts = {
                "CRITICAL": 0, "HIGH": 0, "MODERATE": 0,
                "LOW": 0, "SAFE": 0, "UNKNOWN": 0
            }
            for c in chemicals_evaluated:
                risk = c.verdict.danger_level
                if risk in risk_counts:
                    risk_counts[risk] += 1

            summary = ProductSummary(
                total_ingredients=len(product.get("ingredient_list", [])),
                chemicals_evaluated=len(chemicals_evaluated),
                safe_skipped=len(safe_skipped_list),
                unverified=len(unverified_list),
                critical=risk_counts["CRITICAL"],
                high=risk_counts["HIGH"],
                moderate=risk_counts["MODERATE"],
                low=risk_counts["LOW"],
                safe=risk_counts["SAFE"],
                unknown=risk_counts["UNKNOWN"],
                organ_overlap_flags=1 if product_specific_overlaps else 0,
            )

            products_output.append(ProductOutput(
                product_id=product_id,
                product_name=product_name,
                usage=usage,
                exposure_type=exposure_type,
                drivers=list(set(drivers))[:5],
                ingredients=IngredientsSection(
                    chemicals_evaluated=chemicals_evaluated,
                    safe_skipped=safe_skipped_list,
                    unverified_chemicals=unverified_list,
                ),
                combination_risks=CombinationRisks(
                    organ_overlap=organ_overlap_obj,
                    cumulative_presence=cumulative_obj,
                ),
                summary=summary,
            ))

        all_critical, all_high, all_organs = [], [], set()
        for f in findings:
            if f.get("skipped"):
                continue
            risk = f.get("preliminary_risk", "UNKNOWN")
            if risk == "CRITICAL":
                all_critical.append(f.get("name", ""))
            elif risk == "HIGH":
                all_high.append(f.get("name", ""))
            for organ in f.get("target_organs", []):
                if organ:
                    all_organs.add(organ)

        global_summary = GlobalSummary(
            total_products=len(products_list),
            products_to_avoid=sum(
                1 for p in products_output
                if p.summary.critical > 0 or p.summary.high > 0
            ),
            products_to_reduce=sum(
                1 for p in products_output if p.summary.moderate > 0
            ),
            products_safe=sum(
                1 for p in products_output
                if p.summary.critical == 0
                and p.summary.high == 0
                and p.summary.moderate == 0
            ),
            products_unknown=sum(
                1 for p in products_output if p.summary.unknown > 0
            ),
            unique_chemicals_found=len({
                f.get("name") for f in findings if not f.get("skipped")
            }),
            critical_chemicals=all_critical[:10],
            high_chemicals=all_high[:10],
            organs_under_pressure=list(all_organs)[:10] if all_organs else None,
            depth_used="full",
            organ_global_analysis=global_organ_analysis,
        )

        report = FinalReport(
            report_id=report_id,
            analyzed_at=datetime.now().isoformat(),
            agent_version="3.1.0",
            no_dose_data=True,
            depth="full",
            products=products_output,
            global_summary=global_summary,
        )

        def to_dict(obj):
            if hasattr(obj, "__dataclass_fields__"):
                d = {k: to_dict(v) for k, v in obj.__dict__.items()}
                if hasattr(obj, "personalisation"):
                    d["personalisation"] = obj.personalisation
                return d
            elif isinstance(obj, list):
                return [to_dict(i) for i in obj]
            elif isinstance(obj, dict):
                return {k: to_dict(v) for k, v in obj.items()}
            return obj

        report_dict = to_dict(report)

        backward_compatible = {
            "product_verdicts": [
                {
                    "product_id": p["product_id"],
                    "product_name": p["product_name"],
                    "risk_level": (
                        "HIGH" if (
                            products_list[i].get("_enforced_risk") == "HIGH"
                            or p["summary"]["critical"] > 0
                            or p["summary"]["high"] > 0
                        )
                        else "MODERATE" if p["summary"]["moderate"] > 0
                        else "LOW"
                    ),
                    "recommendation": (
                        "avoid" if (
                            products_list[i].get("_enforced_risk") == "HIGH"
                            or p["summary"]["critical"] > 0
                            or p["summary"]["high"] > 0
                        )
                        else "reduce_use" if p["summary"]["moderate"] > 0
                        else "keep"
                    ),
                    "recommendation_reason": (
                        f"Based on {p['summary']['critical']} critical, "
                        f"{p['summary']['high']} high risk chemicals"
                        + (" + organ overlap escalation" if products_list[i].get("_enforced_risk") == "HIGH" else "")
                    ),
                    "risk_drivers": p.get("drivers", []),
                }
                for i, p in enumerate(report_dict["products"])
            ],
            "chemicals_summary": [
                {
                    "name": c["name"],
                    "risk_level": c["verdict"]["danger_level"],
                    "confidence": c["resolution"].get("confidence", 0.5) or 0.5,
                    "key_hazards": c["hazard"]["h_codes"][:5],
                    "target_organs": c["body_effects"]["target_organs"],
                    "is_unresolved": c["resolution"]["fetch_status"] == "error",
                    "source": (
                        "PUBCHEM" if c["resolution"].get("method") == "pubchem"
                        else "KG" if (c["resolution"].get("confidence") or 0) >= 0.7
                        else "UNKNOWN" if (c["resolution"].get("confidence") or 0) < 0.4
                        else "FUSED"
                    ),
                    "personalisation": c.get("personalisation"),
                }
                for p in report_dict["products"]
                for c in p["ingredients"]["chemicals_evaluated"]
            ],
            "combination_risks": {
                "organ_overlap_summary": next(
                    (
                        p["combination_risks"]["organ_overlap"].get("note", "No overlap")
                        for p in report_dict["products"]
                        if p["combination_risks"]["organ_overlap"].get("has_overlap")
                    ),
                    "No organ overlap detected",
                ),
                "cumulative_chemicals": [],
                "verdict_escalation": next(
                    (
                        p["combination_risks"]["organ_overlap"].get("verdict_escalation")
                        for p in report_dict["products"]
                        if p["combination_risks"]["organ_overlap"].get("verdict_escalation")
                    ),
                    None,
                ),
            },
            "overall_assessment": (
                f"Analysis of {len(products_list)} product(s) completed. "
                f"{len(all_critical)} critical, {len(all_high)} high risk chemicals."
            ),
            "safe_ingredients": [
                s["name"]
                for p in report_dict["products"]
                for s in p["ingredients"]["safe_skipped"]
            ],
            "unverified_chemicals": [
                u["name"]
                for p in report_dict["products"]
                for u in p["ingredients"]["unverified_chemicals"]
            ],
        }

        report_dict.update(backward_compatible)
        logger.info(
            f"Final report built: {len(products_output)} products, "
            f"{len({f.get('name') for f in findings if not f.get('skipped')})} unique chemicals"
        )
        return report_dict

    # ── Phase E: Scoring server ───────────────────────────────────────────────
    async def _enhance_with_scoring_server(self, report_dict: dict) -> dict:
        if "scoring" not in self.clients:
            logger.warning("Scoring server not available, skipping")
            return report_dict

        logger.info(f"Calling scoring server. user_type={self.user_type}")
        try:
            result = await self._scoring("run_full_pipeline", {
                "input_data": report_dict,
                "user_type": self.user_type,
                "generate_llm_report": True,
            })
            report_dict["scoring_analysis"] = result
            logger.info("Scoring server analysis added.")
        except Exception as e:
            logger.warning(f"Scoring server call failed: {e}")
            report_dict["scoring_analysis"] = {"error": str(e)}

        return report_dict

    # ── Public synchronous entry point ──────────────────────────────────────
    def run_sync(self, products_list: list, user_type: str = None) -> dict:
        if not self._servers_started:
            raise RuntimeError("Agent not started. Call start() first.")
        # Reuse the same persistent loop that was used to start the subprocesses.
        # Creating a NEW loop here would leave subprocess stdin transports with a
        # None proactor (the old loop's proactor is gone) → AttributeError crash.
        return self._loop.run_until_complete(self._run_async(products_list, user_type))

    async def _run_async(self, products_list: list, user_type: str = None) -> dict:
        start = datetime.now(timezone.utc)
        agent_config.validate()

        self.user_type = user_type
        if user_type:
            logger.info(f"User type set to: {user_type}")

        context = self._analyze_product_context(products_list)
        logger.info(f"Product context: {context}")

        # Phase A
        filter_result = await self._phase_filter(products_list)
        chemicals = filter_result.get("chemicals", [])

        # Phase B
        findings = []
        for chem in chemicals:
            name = chem.get("name", "").strip()
            if not name:
                continue
            product_usage = "cosmetics"
            for product in products_list:
                for ing in product.get("ingredient_list", []):
                    if ing.get("name", "").strip().upper() == name.upper():
                        product_usage = product.get("product_usage", "cosmetics")
                        break
                else:
                    continue
                break
            findings.append(await self._investigate_chemical(name, product_usage))

        # Phase C
        combination = await self._phase_combination(findings, products_list)

        # Escalation
        self._enforce_escalation(products_list, combination)

        # Phase D
        report = self._build_final_report(products_list, filter_result, findings, combination)

        # Phase E
        report = await self._enhance_with_scoring_server(report)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info(f"Agent run completed in {elapsed:.1f} seconds")
        return {
            "analyzed_at": start.isoformat(),
            "elapsed_s": round(elapsed, 1),
            "agent_stats": self.state.summary(),
            "report": report,
        }