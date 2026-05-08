"""
Ingredient classifier - uses config.groq.GroqClient
All caching, timeout, retry, and fallback are handled in config.groq
"""

from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config.groq import get_groq_client


def classify_with_groq(ingredients: list, usage: str = "cosmetic") -> dict:
    """
    Classify ingredients using the global Groq client.
    
    Returns:
        {
            "chemicals": [{"name": str, "reason": str, "unverified": bool}, ...],
            "safe_skipped": [{"name": str, "reason": str}, ...],
            "garbage": [{"name": str, "reason": str}, ...]
        }
    """
    if not ingredients:
        return {"chemicals": [], "safe_skipped": [], "garbage": []}
    
    client = get_groq_client()
    return client.classify_ingredients(ingredients, usage)


# Quick test
if __name__ == "__main__":
    print("=" * 60)
    print("TESTING FILTER SERVER (using config.groq)")
    print("=" * 60)
    
    test_ingredients = [
        {"name": "AQUA"},
        {"name": "WATER"},
        {"name": "CITRIC ACID"},
        {"name": "SODIUM LAURETH SULFATE"},
        {"name": "COCO-BETAINE"},
        {"name": "POLYSORBATE 20"},
        {"name": "PEG-200 HYDROGENATED GLYCERYL PALMATE"},
        {"name": "PARFUM"},
        {"name": "2"},
        {"name": "Z288697"},
        {"name": "F.I.L"},
        {"name": "SHEA BUTTER"},
        {"name": "METHYLPARABEN"},
        {"name": "LIMONENE"},
    ]
    
    print(f"\n📋 Testing {len(test_ingredients)} ingredients...")
    
    result = classify_with_groq(test_ingredients, "cosmetic")
    
    print(f"\n✅ RESULTS:")
    print(f"\n  🔬 CHEMICALS ({len(result.get('chemicals', []))}):")
    for c in result.get("chemicals", []):
        unverified = "⚠️ UNVERIFIED" if c.get("unverified") else ""
        print(f"    - {c['name']}: {c['reason']} {unverified}")
    
    print(f"\n  ✅ SAFE SKIPPED ({len(result.get('safe_skipped', []))}):")
    for s in result.get("safe_skipped", []):
        print(f"    - {s['name']}: {s['reason']}")
    
    print(f"\n  🗑️ GARBAGE ({len(result.get('garbage', []))}):")
    for g in result.get("garbage", []):
        print(f"    - {g['name']}: {g['reason']}")
    
    print("\n" + "=" * 60)
    print("✅ Filter server ready")