# risk/services.py
import sys
import os
import json
import asyncio
import concurrent.futures
from typing import List, Dict, Any, Tuple
import logging
from pathlib import Path
from datetime import datetime
from django.conf import settings

# ----------------------------------------------------------------------
# 1. Make sure the MCP agent root is in sys.path
# ----------------------------------------------------------------------
MCP_AGENT_ROOT = Path(__file__).parent / "mcp_agent"
sys.path.insert(0, str(MCP_AGENT_ROOT.parent))
sys.path.insert(0, str(MCP_AGENT_ROOT))

# ----------------------------------------------------------------------
# 2. Disable scoring server if chromadb is missing
# ----------------------------------------------------------------------
import mcp_agent.agent.agent as agent_module
if "scoring" in agent_module.SERVER_PATHS:
    try:
        import chromadb
    except ImportError:
        del agent_module.SERVER_PATHS["scoring"]
        print("⚠️ Scoring server disabled (chromadb missing)\n")

# ----------------------------------------------------------------------
# 3. Import the real BiologicalAgent and create a capturing DebugAgent
# ----------------------------------------------------------------------
from mcp_agent.agent.agent import BiologicalAgent

class DebugAgentWithCapture(BiologicalAgent):
    """
    Subclass that prints intermediate results to console AND collects them in a list.
    """
    def __init__(self, debug_collector: List[str], start_servers=True):
        self.debug_collector = debug_collector
        super().__init__(start_servers=start_servers)

    def _log(self, message: str):
        """Print to console and append to collector."""
        print(message)
        self.debug_collector.append(message)

    async def _phase_filter(self, products_list):
        self._log("\n" + "="*70)
        self._log("🔍 PHASE A: FILTER (classifying ingredients with Groq)")
        self._log("="*70)
        result = await super()._phase_filter(products_list)
        self._log(f"\n✅ Filter complete:")
        self._log(f"   Chemicals to investigate: {[c['name'] for c in result.get('chemicals', [])]}")
        self._log(f"   Safe (skipped): {[s['name'] for s in result.get('safe_skipped', [])]}")
        if result.get('unclassified'):
            self._log(f"   Unclassified: {result['unclassified']}")
        return result

    async def _investigate_chemical(self, name, product_usage="cosmetics"):
        self._log(f"\n  🔬 Investigating: {name} (usage: {product_usage})")
        finding = await super()._investigate_chemical(name, product_usage)
        risk = finding.get('preliminary_risk', 'UNKNOWN')
        source = finding.get('source', '?')
        if finding.get('resolution', {}).get('unresolved'):
            self._log(f"     ❌ {name} → {risk} (not in KG)")
        else:
            organs = finding.get('target_organs', [])
            self._log(f"     ✅ {name} → {risk} (source: {source}, organs: {organs if organs else 'none'})")
        return finding

    async def _phase_combination(self, findings, products_list):
        self._log("\n" + "="*70)
        self._log("🔗 PHASE C: COMBINATION ANALYSIS (organ overlap, cumulative, hazard intersection)")
        self._log("="*70)
        result = await super()._phase_combination(findings, products_list)
        organ = result.get('organ_overlap', {})
        self._log(f"\n📊 Organ overlap: has_overlap={organ.get('has_overlap', False)}")
        if organ.get('has_overlap'):
            self._log(f"   Overlapping organs: {list(organ.get('global_organ_analysis', {}).keys())}")
            self._log(f"   Verdict escalation: {organ.get('verdict_escalation')}")
        cumul = result.get('cumulative_flags', [])
        if cumul:
            self._log(f"⚠️ Cumulative flags: {len(cumul)} chemical(s) appear in multiple products")
        else:
            self._log("✅ No cumulative concerns")
        return result

    def _build_final_report(self, products_list, filter_result, findings, combination):
        self._log("\n" + "="*70)
        self._log("📝 PHASE D: BUILDING FINAL REPORT")
        self._log("="*70)
        report = super()._build_final_report(products_list, filter_result, findings, combination)
        for p in report.get('products', []):
            summary = p.get('summary', {})
            self._log(f"\n📦 Product: {p.get('product_name')}")
            self._log(f"   Critical: {summary.get('critical',0)} | High: {summary.get('high',0)} | Moderate: {summary.get('moderate',0)} | Low: {summary.get('low',0)} | Unknown: {summary.get('unknown',0)}")
            if p.get('drivers'):
                self._log(f"   Risk drivers: {p['drivers']}")
        return report

    async def _enhance_with_scoring_server(self, report_dict):
        self._log("\n" + "="*70)
        self._log("📈 PHASE E: SCORING SERVER (optional)")
        self._log("="*70)
        result = await super()._enhance_with_scoring_server(report_dict)
        if 'scoring_analysis' in result:
            self._log("✅ Scoring analysis added.")
        else:
            self._log("⚠️ Scoring server not available or failed.")
        return result


# ----------------------------------------------------------------------
# 4. Main analysis functions
# ----------------------------------------------------------------------
logger = logging.getLogger(__name__)

def analyze_ingredients_risks(
    ingredients_list: List[str],
    user_type: str = None
) -> Tuple[List[Dict[str, str]], Dict[str, Any], List[str], str]:
    """
    Runs the full MCP agent (with debug prints and capture) and returns:
        risk_items, report, debug_log, saved_file_path
    """
    if not ingredients_list:
        logger.info("No ingredients provided, returning empty risks")
        return [], {}, [], ""

    logger.info(f"Analyzing {len(ingredients_list)} ingredients with DebugAgentWithCapture")
    debug_log = []
    agent = DebugAgentWithCapture(debug_log, start_servers=True)
    saved_file_path = ""

    try:
        product = {
            "product_id": "django_scan",
            "product_name": "Product from Scan",
            "product_usage": "cosmetic",
            "exposure_type": "skin",
            "ingredient_list": [{"name": ing} for ing in ingredients_list]
        }

        result = agent.run_sync([product], user_type=user_type)
        report = result.get("report", {})

        # Extract risk items as before
        risk_items = []
        for product_out in report.get("products", []):
            for chem in product_out.get("ingredients", {}).get("chemicals_evaluated", []):
                name = chem.get("name")
                danger = chem.get("verdict", {}).get("danger_level", "UNKNOWN")
                if danger in ("CRITICAL", "HIGH"):
                    level = "high"
                elif danger == "MODERATE":
                    level = "medium"
                else:
                    level = "low"
                risk_items.append({"ingredient": name, "level": level})

        # Save full report to disk
        try:
            try:
                from django.conf import settings
                project_root = settings.BASE_DIR
            except (ImportError, AttributeError):
                project_root = Path(__file__).parent.parent
                while not (project_root / "manage.py").exists():
                    if project_root.parent == project_root:
                        break
                    project_root = project_root.parent

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            first_ing = ingredients_list[0] if ingredients_list else "empty"
            safe_name = "".join(c for c in first_ing if c.isalnum())[:20]
            filename = f"agent_report_{timestamp}_{safe_name}.json"
            filepath = project_root / filename
            saved_file_path = str(filepath)

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": timestamp,
                    "ingredients": ingredients_list,
                    "user_type": user_type,
                    "risk_items": risk_items,
                    "full_report": report
                }, f, indent=2, ensure_ascii=False)

            logger.info(f"Agent report saved to {filepath}")
        except Exception as e:
            logger.warning(f"Could not save agent report to disk: {e}")

        return risk_items, report, debug_log, saved_file_path

    finally:
        agent.close()


def analyze_cumulative_risks(
    products_with_reports: List[Dict[str, Any]],
    user_type: str = None,
    timeout_seconds: int = 120
) -> Dict[str, Any]:
    """
    Run only Phases C, D, E using cached investigation reports.
    No debug capture (silent).
    """
    if not products_with_reports or len(products_with_reports) < 2:
        return {"error": "Cumulative analysis requires at least 2 products"}

    from mcp_agent.agent.agent import BiologicalAgent as SilentAgent

    agent_products = []
    for p in products_with_reports:
        agent_products.append({
            "product_id": p["product_id"],
            "product_name": p["product_name"],
            "product_usage": p.get("product_usage", "cosmetic"),
            "exposure_type": p.get("exposure_type", "skin"),
            "ingredient_list": p.get("ingredient_list", [])
        })

    findings = []
    for prod in products_with_reports:
        report = prod.get("investigation_report")
        if not report or not isinstance(report, dict):
            continue
        chemicals = report.get("ingredients", {}).get("chemicals_evaluated", [])
        prod_id = prod["product_id"]
        for chem in chemicals:
            findings.append({
                "name": chem.get("name"),
                "uid": chem.get("uid"),
                "target_organs": chem.get("body_effects", {}).get("target_organs", []),
                "h_codes": chem.get("hazard", {}).get("h_codes", []),
                "preliminary_risk": chem.get("verdict", {}).get("danger_level", "UNKNOWN"),
                "risk_score": chem.get("verdict", {}).get("risk_calculation_breakdown", {}).get("total_score", 0),
                "source": chem.get("resolution", {}).get("method", "cached"),
                "confidence": chem.get("resolution", {}).get("confidence", 0.5),
                "kg_confidence": chem.get("resolution", {}).get("confidence", 0.5),
                "resolution": chem.get("resolution", {}),
                "identity": chem.get("identity", {}),
                "hazard": chem.get("hazard", {}),
                "body_effects": chem.get("body_effects", {}),
                "dose_evaluation": chem.get("dose_evaluation", {}),
                "verdict": chem.get("verdict", {}),
                "personalisation": chem.get("personalisation"),
                "product_id": prod_id,
            })

    if not findings:
        return {"error": "No chemical findings could be extracted"}

    agent = None
    try:
        agent = SilentAgent(start_servers=True)
        loop = agent._loop

        async def _run_cumulative():
            combination = await agent._phase_combination(findings, agent_products)
            report_dict = agent._build_final_report(
                agent_products,
                filter_result={"chemicals": [], "safe_skipped": []},
                findings=findings,
                combination=combination
            )
            report_dict = await agent._enhance_with_scoring_server(report_dict)
            return report_dict

        future = asyncio.run_coroutine_threadsafe(_run_cumulative(), loop)
        cumulative_report = future.result(timeout=timeout_seconds)
        return cumulative_report

    except concurrent.futures.TimeoutError:
        return {"error": f"Cumulative analysis timed out after {timeout_seconds} seconds"}
    except Exception as e:
        logger.exception("Cumulative analysis failed")
        return {"error": str(e)}
    finally:
        if agent:
            agent.close()


if __name__ == "__main__":
    test_ingredients = ["Lysine", "Formaldehyde", "AQUA"]
    risk_items, report, debug_log, filepath = analyze_ingredients_risks(test_ingredients, user_type="fetal")
    print("\n=== FINAL RISK ITEMS ===")
    for item in risk_items:
        print(f"  {item['ingredient']}: {item['level']}")
    print(f"\nDebug log length: {len(debug_log)} lines")
    print(f"Report saved to: {filepath}")
