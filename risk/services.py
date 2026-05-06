# risk/services.py
import sys
import os
import json
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime
from django.conf import settings   # only if Django is fully loaded, otherwise fallback
# ----------------------------------------------------------------------
# 1. Make sure the MCP agent root is in sys.path
# ----------------------------------------------------------------------
MCP_AGENT_ROOT = Path(__file__).parent / "mcp_agent"
sys.path.insert(0, str(MCP_AGENT_ROOT.parent))          # add .../risk/
sys.path.insert(0, str(MCP_AGENT_ROOT))                 # add .../risk/mcp_agent

# ----------------------------------------------------------------------
# 2. Disable scoring server if chromadb is missing (same as original)
# ----------------------------------------------------------------------
import mcp_agent.agent.agent as agent_module
if "scoring" in agent_module.SERVER_PATHS:
    try:
        import chromadb
    except ImportError:
        del agent_module.SERVER_PATHS["scoring"]
        print("⚠️ Scoring server disabled (chromadb missing)\n")

# ----------------------------------------------------------------------
# 3. Import the real BiologicalAgent and create a DebugAgent subclass
# ----------------------------------------------------------------------
from mcp_agent.agent.agent import BiologicalAgent

class DebugAgent(BiologicalAgent):
    """Subclass that prints intermediate results after each phase."""

    async def _phase_filter(self, products_list):
        print("\n" + "="*70)
        print("🔍 PHASE A: FILTER (classifying ingredients with Groq)")
        print("="*70)
        result = await super()._phase_filter(products_list)
        print(f"\n✅ Filter complete:")
        print(f"   Chemicals to investigate: {[c['name'] for c in result.get('chemicals', [])]}")
        print(f"   Safe (skipped): {[s['name'] for s in result.get('safe_skipped', [])]}")
        if result.get('unclassified'):
            print(f"   Unclassified: {result['unclassified']}")
        return result

    async def _investigate_chemical(self, name, product_usage="cosmetics"):
        print(f"\n  🔬 Investigating: {name} (usage: {product_usage})")
        finding = await super()._investigate_chemical(name, product_usage)
        risk = finding.get('preliminary_risk', 'UNKNOWN')
        source = finding.get('source', '?')
        if finding.get('resolution', {}).get('unresolved'):
            print(f"     ❌ {name} → {risk} (not in KG)")
        else:
            organs = finding.get('target_organs', [])
            print(f"     ✅ {name} → {risk} (source: {source}, organs: {organs if organs else 'none'})")
        return finding

    async def _phase_combination(self, findings, products_list):
        print("\n" + "="*70)
        print("🔗 PHASE C: COMBINATION ANALYSIS (organ overlap, cumulative, hazard intersection)")
        print("="*70)
        result = await super()._phase_combination(findings, products_list)
        organ = result.get('organ_overlap', {})
        print(f"\n📊 Organ overlap: has_overlap={organ.get('has_overlap', False)}")
        if organ.get('has_overlap'):
            print(f"   Overlapping organs: {list(organ.get('global_organ_analysis', {}).keys())}")
            print(f"   Verdict escalation: {organ.get('verdict_escalation')}")
        cumul = result.get('cumulative_flags', [])
        if cumul:
            print(f"⚠️ Cumulative flags: {len(cumul)} chemical(s) appear in multiple products")
        else:
            print("✅ No cumulative concerns")
        return result

    def _build_final_report(self, products_list, filter_result, findings, combination):
        print("\n" + "="*70)
        print("📝 PHASE D: BUILDING FINAL REPORT")
        print("="*70)
        report = super()._build_final_report(products_list, filter_result, findings, combination)
        for p in report.get('products', []):
            summary = p.get('summary', {})
            print(f"\n📦 Product: {p.get('product_name')}")
            print(f"   Critical: {summary.get('critical',0)} | High: {summary.get('high',0)} | Moderate: {summary.get('moderate',0)} | Low: {summary.get('low',0)} | Unknown: {summary.get('unknown',0)}")
            if p.get('drivers'):
                print(f"   Risk drivers: {p['drivers']}")
        return report

    async def _enhance_with_scoring_server(self, report_dict):
        print("\n" + "="*70)
        print("📈 PHASE E: SCORING SERVER (optional)")
        print("="*70)
        result = await super()._enhance_with_scoring_server(report_dict)
        if 'scoring_analysis' in result:
            print("✅ Scoring analysis added.")
        else:
            print("⚠️ Scoring server not available or failed.")
        return result


# ----------------------------------------------------------------------
# 4. The main analysis function (called from Django views)
# ----------------------------------------------------------------------
logger = logging.getLogger(__name__)

def analyze_ingredients_risks(
    ingredients_list: List[str],
    user_type: str = None
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """
    Runs the full MCP agent (with debug prints) and returns risk items + report.
    Saves the full report as a JSON file in the project root.
    """
    if not ingredients_list:
        logger.info("No ingredients provided, returning empty risks")
        return [], {}

    logger.info(f"Analyzing {len(ingredients_list)} ingredients with DebugAgent (step‑by‑step output)")

    agent = DebugAgent(start_servers=True)
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

        # ---------- SAVE FULL REPORT TO PROJECT ROOT ----------
        try:
            # Determine project root (where manage.py is)
            # If Django is fully loaded, use BASE_DIR from settings
            try:
                from django.conf import settings
                project_root = settings.BASE_DIR
            except (ImportError, AttributeError):
                # Fallback: go up until manage.py is found (or use current working directory)
                project_root = Path(__file__).parent.parent  # risk/ -> project root
                while not (project_root / "manage.py").exists():
                    if project_root.parent == project_root:
                        break
                    project_root = project_root.parent

            # Create filename with timestamp and first ingredient as hint
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            first_ing = ingredients_list[0] if ingredients_list else "empty"
            safe_name = "".join(c for c in first_ing if c.isalnum())[:20]
            filename = f"agent_report_{timestamp}_{safe_name}.json"
            filepath = project_root / filename

            # Save the full report (which includes risk_items inside)
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

        return risk_items, report

    finally:
        agent.close()

# ----------------------------------------------------------------------
# 5. Optional: allow running this module standalone for a quick test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Quick test (same as original run_debug_agent.py)
    test_ingredients = ["Lysine", "Formaldehyde", "AQUA"]
    items, full = analyze_ingredients_risks(test_ingredients, user_type="fetal")
    print("\n=== FINAL RISK ITEMS ===")
    for item in items:
        print(f"  {item['ingredient']}: {item['level']}")