# run_full_debug.py
import sys
import os
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Disable scoring if chromadb missing
import mcp_agent.agent.agent as agent_module
if "scoring" in agent_module.SERVER_PATHS:
    try:
        import chromadb
    except ImportError:
        del agent_module.SERVER_PATHS["scoring"]
        print("Scoring server disabled (chromadb missing)\n")

from mcp_agent.agent.agent import BiologicalAgent

class FullDebugAgent(BiologicalAgent):
    """Prints complete JSON dump after each processing phase."""

    async def _phase_filter(self, products_list):
        print("\n" + "="*80)
        print("PHASE A - FILTER (Groq classification)")
        print("="*80)
        result = await super()._phase_filter(products_list)
        print("-> Full result:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    async def _investigate_chemical(self, name, product_usage="cosmetics"):
        print(f"\nInvestigating: {name}")
        result = await super()._investigate_chemical(name, product_usage)
        print("-> Full finding:")
        clean = {k:v for k,v in result.items() if k not in ('full_profile', 'complete_data')}
        print(json.dumps(clean, indent=2, ensure_ascii=False))
        return result

    async def _phase_combination(self, findings, products_list):
        print("\n" + "="*80)
        print("PHASE C - COMBINATION (organ overlap, cumulative, hazard intersection)")
        print("="*80)
        result = await super()._phase_combination(findings, products_list)
        print("-> Full result:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result

    def _build_final_report(self, products_list, filter_result, findings, combination):
        print("\n" + "="*80)
        print("PHASE D - FINAL REPORT")
        print("="*80)
        report = super()._build_final_report(products_list, filter_result, findings, combination)
        print("-> Full report:")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report

    async def _enhance_with_scoring_server(self, report_dict):
        print("\n" + "="*80)
        print("PHASE E - SCORING SERVER (optional)")
        print("="*80)
        result = await super()._enhance_with_scoring_server(report_dict)
        if 'scoring_analysis' in result:
            print("-> Scoring analysis added (not printed fully to avoid clutter)")
        else:
            print("Scoring server not available.")
        return result

def run_full_debug(ingredients, user_type=None):
    print("\nSTARTING AGENT IN FULL DEBUG MODE\n")
    agent = FullDebugAgent(start_servers=True)
    try:
        product = {
            "product_id": "debug_001",
            "product_name": "Debug Product",
            "product_usage": "cosmetic",
            "exposure_type": "skin",
            "ingredient_list": [{"name": ing} for ing in ingredients]
        }
        result = agent.run_sync([product], user_type=user_type)
        print("\n" + "="*80)
        print("EXECUTION COMPLETE")
        print("="*80)
        return result
    finally:
        agent.close()

if __name__ == "__main__":
    test_ingredients = [ "Lysine", "Formaldehyde", "AQUA"]
    run_full_debug(test_ingredients, user_type="fetal")