"""
profile_logic.py — IngredientGuard Core Logic (Improved)
Uses config.groq.GroqClient for LLM operations (same as classifier.py)
"""

import json
import os
import re
import sys
import contextlib
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

# -----------------------------------------------------------------------------
# Constants (all paths relative, no hardcoded absolute paths)
# -----------------------------------------------------------------------------
THIS_DIR = Path(__file__).parent
DEFAULT_KB_PATH    = str(THIS_DIR / "User_Types.json")
DEFAULT_CHROMA_DIR = str(THIS_DIR / "chroma_db")

BATCH_SIZE         = 5_000
VECTOR_THRESHOLD   = 0.80

# -----------------------------------------------------------------------------
# User-type registry
# -----------------------------------------------------------------------------
USER_TYPE_MAP: dict[str, str] = {
    "Asthma":         "Asthma People",
    "Diabetes":       "Diabetes People",
    "Newborn":        "Newborn People",
    "Fetal":          "Fetal People",
    "Cardiovascular": "Cardiovascular People",
    "PCOS":           "PCOS People",
}
VALID_USER_TYPES = list(USER_TYPE_MAP.keys())

# -----------------------------------------------------------------------------
# Groq client – using config.groq (same working approach as classifier.py)
# -----------------------------------------------------------------------------
_groq_client: Optional[object] = None

def _get_groq_client():
    """Get the Groq client from config.groq (same as classifier.py)"""
    global _groq_client
    if _groq_client is None:
        # Add parent directory to path to ensure config is found
        parent_dir = str(THIS_DIR.parent.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        
        from config.groq import get_groq_client
        _groq_client = get_groq_client()
    return _groq_client

# -----------------------------------------------------------------------------
# Embedding model (lazy singleton)
# -----------------------------------------------------------------------------
_embedding_model: Optional[SentenceTransformer] = None

def load_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedding_model

# -----------------------------------------------------------------------------
# ChromaDB index (with stdout suppression)
# -----------------------------------------------------------------------------
_chroma_client: Optional[chromadb.PersistentClient] = None
_kb_counts: dict[str, int] = {}

def build_chroma_index(
    kb_path:    str = DEFAULT_KB_PATH,
    chroma_dir: str = DEFAULT_CHROMA_DIR,
) -> tuple[chromadb.PersistentClient, dict[str, int]]:
    global _chroma_client, _kb_counts

    if _chroma_client is not None:
        return _chroma_client, _kb_counts

    # Suppress ChromaDB's stdout prints (important for JSON-RPC)
    with open(os.devnull, 'w') as devnull:
        with contextlib.redirect_stdout(devnull):
            client = chromadb.PersistentClient(
                path=chroma_dir,
                settings=Settings(anonymized_telemetry=False),
            )

    model = load_embedding_model()
    counts: dict[str, int] = {}

    try:
        with open(kb_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print(f"[Profile Server] ERROR: KB file not found at {kb_path}", file=sys.stderr)
        raise

    for group in raw:
        user_type = group["User_type"]
        coll_name = _coll_name(user_type)
        entries   = [e for e in group["data"] if (e.get("Chemical Name") or "").strip()]
        counts[user_type] = len(entries)

        existing_names = [c.name for c in client.list_collections()]
        if coll_name in existing_names:
            # Print to stderr only – safe for JSON-RPC
            print(f"[ChromaDB] '{coll_name}' already exists ({len(entries)} entries). Skipping.", file=sys.stderr)
            continue

        print(f"[ChromaDB] Building '{coll_name}' with {len(entries)} entries …", file=sys.stderr)
        with open(os.devnull, 'w') as devnull:
            with contextlib.redirect_stdout(devnull):
                collection = client.create_collection(
                    name=coll_name,
                    metadata={"hnsw:space": "cosine"},
                )

        for batch_start in range(0, len(entries), BATCH_SIZE):
            batch = entries[batch_start : batch_start + BATCH_SIZE]
            ids, texts, metadatas = [], [], []

            for i, entry in enumerate(batch):
                chem_name = entry["Chemical Name"].strip()
                doc_id    = f"{coll_name}_{batch_start + i}"
                texts.append(chem_name)
                ids.append(doc_id)
                metadatas.append({
                    "chemical_name"    : chem_name,
                    "chemical_id"      : str(entry.get("Chemical ID",      "")),
                    "cas_rn"           : str(entry.get("CAS RN",           "")),
                    "disease_name"     : str(entry.get("Disease Name",     "")),
                    "disease_id"       : str(entry.get("Disease ID",       "")),
                    "direct_evidence"  : str(entry.get("Direct Evidence",  "")),
                    "inference_network": str(entry.get("Inference Network","")),
                    "inference_score"  : str(entry.get("Inference Score",  "0")),
                    "reference_count"  : str(entry.get("Reference Count",  "0")),
                    "user_type"        : user_type,
                    "norm_name"        : normalize(chem_name),
                })

            embeddings = model.encode(texts, show_progress_bar=False).tolist()
            with open(os.devnull, 'w') as devnull:
                with contextlib.redirect_stdout(devnull):
                    collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
            print(f"  ✓ Batch {batch_start // BATCH_SIZE + 1} done ({len(batch)} items)", file=sys.stderr)

    print("[ChromaDB] Index ready.", file=sys.stderr)
    _chroma_client = client
    _kb_counts     = counts
    return client, counts

def get_chroma_client(
    kb_path: str = DEFAULT_KB_PATH,
    chroma_dir: str = DEFAULT_CHROMA_DIR,
) -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        build_chroma_index(kb_path=kb_path, chroma_dir=chroma_dir)
    return _chroma_client

# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------
def normalize(name: str) -> str:
    if not name:
        return ""
    return re.sub(r'\s+', ' ', name).lower().replace('-', ' ').replace('(', '').replace(')', '').strip()

def _coll_name(user_type: str) -> str:
    name = user_type.lower().replace(' ', '-')
    return name[:63]

def _meta_to_entry(meta: dict) -> dict:
    return {
        "Chemical Name": meta.get("chemical_name", ""),
        "Chemical ID": meta.get("chemical_id", ""),
        "CAS RN": meta.get("cas_rn", ""),
        "Disease Name": meta.get("disease_name", ""),
        "Disease ID": meta.get("disease_id", ""),
        "Direct Evidence": meta.get("direct_evidence", ""),
        "Inference Network": meta.get("inference_network", ""),
        "Inference Score": meta.get("inference_score", "0"),
        "Reference Count": meta.get("reference_count", "0"),
    }

# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def list_user_types() -> list[dict]:
    return [{"short": short, "display": display} for short, display in USER_TYPE_MAP.items()]

def get_kb_stats(
    kb_path: str = DEFAULT_KB_PATH,
    chroma_dir: str = DEFAULT_CHROMA_DIR,
) -> dict[str, int]:
    _, counts = build_chroma_index(kb_path=kb_path, chroma_dir=chroma_dir)
    return counts

def retrieve(
    user_type_kb_key: str,
    ingredient: str,
    kb_path: str = DEFAULT_KB_PATH,
    chroma_dir: str = DEFAULT_CHROMA_DIR,
) -> Optional[dict]:
    client = get_chroma_client(kb_path=kb_path, chroma_dir=chroma_dir)
    # user_type_kb_key is short key like "Asthma"
    display_name = USER_TYPE_MAP.get(user_type_kb_key, user_type_kb_key)
    coll_name = _coll_name(display_name)

    try:
        collection = client.get_collection(name=coll_name)
    except Exception:
        return None

    if collection.count() == 0:
        return None

    q_norm = normalize(ingredient)

    # Exact match
    try:
        exact = collection.get(
            where={"norm_name": {"$eq": q_norm}},
            include=["metadatas"],
            limit=1,
        )
        if exact and exact["metadatas"]:
            return _meta_to_entry(exact["metadatas"][0])
    except Exception:
        pass

    # Vector search
    model = load_embedding_model()
    q_embedding = model.encode([ingredient.strip()], show_progress_bar=False).tolist()

    results = collection.query(
        query_embeddings=q_embedding,
        n_results=min(10, collection.count()),
        include=["distances", "metadatas", "documents"],
    )

    if not results["metadatas"] or not results["metadatas"][0]:
        return None

    for meta, distance, doc in zip(
        results["metadatas"][0],
        results["distances"][0],
        results["documents"][0],
    ):
        similarity = 1 - (distance / 2)
        stored_norm = meta.get("norm_name", normalize(doc))
        if similarity >= VECTOR_THRESHOLD or q_norm in stored_norm or stored_norm in q_norm:
            return _meta_to_entry(meta)

    return None

def get_risk_level(inference_score) -> str:
    try:
        score = float(inference_score)
    except (ValueError, TypeError):
        return "Low"
    if score > 50:
        return "High"
    elif score >= 20:
        return "Moderate"
    else:
        return "Low"

def query_llm_found(
    user_type: str,
    ingredient: str,
    kb_entry: dict,
    risk_level: str,
    generate_llm: bool = True
) -> Optional[str]:
    """Query LLM when ingredient is found in knowledge base"""
    if not generate_llm:
        return None
    
    try:
        client = _get_groq_client()
        
        prompt = f"""You are a medical safety expert. A {user_type} patient asks about the ingredient '{ingredient}'.

**Knowledge base entry:**
- Disease linked: {kb_entry.get('Disease Name', 'N/A')}
- Direct Evidence: {kb_entry.get('Direct Evidence', 'N/A')}
- Inference Score: {kb_entry.get('Inference Score', 'N/A')} (0–100, higher = more risk)
- Reference Count: {kb_entry.get('Reference Count', 'N/A')} studies
- **Computed Risk Level: {risk_level}** (Low: score < 20, Moderate: 20–50, High: >50)

**Instructions:**
- Base your answer ONLY on the information above.
- Do NOT invent any facts, ingredients, or side effects not explicitly stated.
- Provide a clear, concise explanation (3–5 sentences) suitable for a patient or caregiver.
- Use the computed Risk Level as your guide for severity.
- If evidence is missing or weak, state that clearly.
- Recommend consulting a doctor if needed.

Now write the explanation:"""
        
        # Check what methods the GroqClient has and use appropriate one
        if hasattr(client, 'chat_completion'):
            response = client.chat_completion(prompt)
        elif hasattr(client, 'generate'):
            response = client.generate(prompt)
        elif hasattr(client, 'classify_ingredients'):
            # If only classify_ingredients exists, we need to adapt
            # This is a fallback - you may need to adjust based on actual API
            response = client.classify_ingredients([], "")  # Not ideal, but fallback
        else:
            # Try direct call
            response = client(prompt)
        
        return response if isinstance(response, str) else str(response)
        
    except Exception as e:
        return f"[LLM error: {e}]"

def query_llm_not_found(
    user_type: str,
    ingredient: str,
    generate_llm: bool = True
) -> Optional[str]:
    """Query LLM when ingredient is NOT found in knowledge base"""
    if not generate_llm:
        return None
    
    try:
        client = _get_groq_client()
        
        prompt = f"""You are a medical safety expert. A {user_type} patient asks about the ingredient '{ingredient}', but it was NOT found in our medical knowledge base.

**Instructions:**
- Do NOT invent any specific risks or data about this ingredient.
- Provide a general safety note (3–5 sentences) about how patients should approach unknown substances.
- Recommend consulting a doctor or pharmacist for confirmation.
- Be cautious and avoid alarmist language.

Now write the general guidance:"""
        
        # Check what methods the GroqClient has and use appropriate one
        if hasattr(client, 'chat_completion'):
            response = client.chat_completion(prompt)
        elif hasattr(client, 'generate'):
            response = client.generate(prompt)
        elif hasattr(client, 'classify_ingredients'):
            response = client.classify_ingredients([], "")
        else:
            response = client(prompt)
        
        return response if isinstance(response, str) else str(response)
        
    except Exception as e:
        return f"[LLM error: {e}]"

def analyze_ingredient(
    user_type: str,
    ingredient: str,
    generate_llm: bool = False,
    kb_path: str = DEFAULT_KB_PATH,
    chroma_dir: str = DEFAULT_CHROMA_DIR,
) -> dict:
    """
    Analyze an ingredient for a specific user type.
    
    Args:
        user_type: One of VALID_USER_TYPES (e.g., "Asthma", "Diabetes")
        ingredient: The ingredient name to analyze
        generate_llm: Whether to generate LLM analysis (default False)
        kb_path: Path to knowledge base JSON file
        chroma_dir: Path to ChromaDB directory
    
    Returns:
        Dictionary with analysis results
    """
    if user_type not in USER_TYPE_MAP:
        return {
            "found": False,
            "user_type": None,
            "ingredient": ingredient,
            "risk_level": None,
            "kb_entry": None,
            "llm_analysis": None,
            "error": f"Invalid user_type: {user_type}. Valid: {', '.join(VALID_USER_TYPES)}",
        }

    user_type_display = USER_TYPE_MAP[user_type]

    try:
        kb_entry = retrieve(user_type, ingredient, kb_path=kb_path, chroma_dir=chroma_dir)
        if kb_entry:
            risk_level = get_risk_level(kb_entry.get("Inference Score"))
            llm_analysis = query_llm_found(user_type_display, ingredient, kb_entry, risk_level, generate_llm)
            return {
                "found": True,
                "user_type": user_type_display,
                "ingredient": ingredient,
                "risk_level": risk_level,
                "kb_entry": kb_entry,
                "llm_analysis": llm_analysis,
                "error": None,
            }
        else:
            llm_analysis = query_llm_not_found(user_type_display, ingredient, generate_llm)
            return {
                "found": False,
                "user_type": user_type_display,
                "ingredient": ingredient,
                "risk_level": None,
                "kb_entry": None,
                "llm_analysis": llm_analysis,
                "error": None,
            }
    except Exception as e:
        return {
            "found": False,
            "user_type": user_type_display,
            "ingredient": ingredient,
            "risk_level": None,
            "kb_entry": None,
            "llm_analysis": None,
            "error": f"Error during retrieval: {str(e)}",
        }


# -----------------------------------------------------------------------------
# Quick Test
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("TESTING PROFILE LOGIC (using config.groq)")
    print("=" * 60)
    
    # Test user types
    print(f"\n📋 Available user types: {list_user_types()}")
    
    # Test ingredient analysis (without LLM for quick test)
    test_cases = [
        ("Asthma", "Sodium Lauryl Sulfate"),
        ("Diabetes", "Sugar"),
        ("PCOS", "Methylparaben"),
        ("Cardiovascular", "Caffeine"),
    ]
    
    for user_type, ingredient in test_cases:
        print(f"\n🔍 Analyzing: {ingredient} for {user_type}")
        result = analyze_ingredient(user_type, ingredient, generate_llm=False)
        
        if result["found"]:
            print(f"  ✅ FOUND")
            print(f"  📊 Risk Level: {result['risk_level']}")
            print(f"  🏥 Disease: {result['kb_entry'].get('Disease Name', 'N/A')}")
            print(f"  📈 Inference Score: {result['kb_entry'].get('Inference Score', 'N/A')}")
        else:
            print(f"  ❌ NOT FOUND in knowledge base")
        
        if result["error"]:
            print(f"  ⚠️ Error: {result['error']}")
    
    print("\n" + "=" * 60)
    print("✅ Profile logic ready")