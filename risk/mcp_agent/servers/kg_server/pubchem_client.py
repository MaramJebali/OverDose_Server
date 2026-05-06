"""
pubchem_client.py — PubChem API Client for Biological Agent

Fetches data for chemicals NOT in KG.
Extracts:
- Chemical classes (from "Chemical Classes" subsection, matches KG classes)
- Target organs (from "Toxicity" → "Target Organs")
- GHS pictograms, hazard statements (H‑codes), and signal word (from "Safety and Hazards" → "GHS Classification")
"""

import json
import urllib.request
import urllib.error
import time
import re
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import quote

# ============================================================
# Configuration
# ============================================================

REQUEST_DELAY = 0.5
MAX_RETRIES = 2
RETRY_DELAY = 1

# ============================================================
# KG Chemical Classes (exact strings for matching)
# ============================================================

KG_CHEMICAL_CLASSES = [
    "Cosmetics",
    "CosIng",
    "Drugs",
    "Drug Clinical Phase",
    "Endocrine Disruptors",
    "Food Additives",
    "Food Contact Substances",
    "Pesticides",
    "EU Pesticide Approval",
    "Surfactants",
    "Pesticide Type",
    "Flavouring Agents",
    "Fragrances",
    "Lipids",
    "Polymers"
]


# ============================================================
# HTTP Request with Retry
# ============================================================

def _make_request(url: str, retry_count: int = 0) -> Optional[Dict]:
    """Make HTTP request to PubChem API with retry logic."""
    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "BiologicalAgent/2.0")
        
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode())
            
    except urllib.error.HTTPError as e:
        if e.code in [400, 404]:
            return None
        if e.code == 503 and retry_count < MAX_RETRIES:
            time.sleep(RETRY_DELAY * (retry_count + 1))
            return _make_request(url, retry_count + 1)
        return None
        
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        if retry_count < MAX_RETRIES:
            time.sleep(RETRY_DELAY * (retry_count + 1))
            return _make_request(url, retry_count + 1)
        return None
        
    except json.JSONDecodeError:
        return None
        
    except Exception:
        return None


# ============================================================
# 1. Get CID by Chemical Name
# ============================================================

def search_by_name(chemical_name: str) -> Optional[str]:
    """Search PubChem by chemical name, return CID."""
    encoded = quote(chemical_name)
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{encoded}/cids/JSON"
    
    time.sleep(REQUEST_DELAY)
    data = _make_request(url)
    
    if data and 'IdentifierList' in data and 'CID' in data['IdentifierList']:
        cids = data['IdentifierList']['CID']
        if cids:
            return str(cids[0])
    
    return None


# When PUG `compound/name/{x}` fails, try alternates (INCI, polymers, common spellings)
INCI_NAME_FALLBACKS: Dict[str, List[str]] = {
    "DIMETHICONE": [
        "Dimethicone",
        "Polydimethylsiloxane",
        "Dimeticone",
    ],
    "SODIUM LAURETH SULFATE": [
        "Sodium laureth sulfate",
        "Sodium lauryl ether sulfate",
        "Polyoxyethylene lauryl ether sodium sulfate",
    ],
    "PEG-IOO STEARATE": [
        "PEG-100 stearate",
        "Polyethylene glycol 100 stearate",
    ],
    "PARFUM": [
        "Fragrance",
        "Parfum",
    ],
    "FRAGRANCE": [
        "Fragrance",
        "Parfum",
    ],
}


def search_by_name_with_fallbacks(chemical_name: str) -> Optional[str]:
    """
    Resolve a PubChem CID using the primary string, normalized spacing,
    title case, and optional INCI synonym lists for hard-to-match names.
    """
    if not chemical_name or not chemical_name.strip():
        return None

    seen: set = set()
    candidates: List[str] = []

    def add(s: str) -> None:
        t = s.strip()
        if t and t not in seen:
            seen.add(t)
            candidates.append(t)

    add(chemical_name)
    compact = " ".join(chemical_name.split())
    if compact != chemical_name.strip():
        add(compact)
    titled = compact.title()
    if titled not in seen:
        add(titled)
    for syn in INCI_NAME_FALLBACKS.get(compact.upper(), []):
        add(syn)

    for name in candidates:
        cid = search_by_name(name)
        if cid:
            return cid
    return None


# ============================================================
# 2. Get Basic Properties (Simple JSON)
# ============================================================

def get_properties(cid: str) -> Dict[str, Any]:
    """Get basic chemical properties."""
    props = "MolecularFormula,MolecularWeight,IUPACName,InChIKey,CanonicalSMILES"
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/property/{props}/JSON"
    
    time.sleep(REQUEST_DELAY)
    data = _make_request(url)
    
    if data and 'PropertyTable' in data and 'Properties' in data['PropertyTable']:
        return data['PropertyTable']['Properties'][0]
    
    return {}


# ============================================================
# 3. General PUG View Utilities
# ============================================================

def get_pug_view_raw(cid: str) -> Optional[Dict]:
    """Get full PUG View data for a CID."""
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"
    time.sleep(REQUEST_DELAY)
    return _make_request(url)


def _extract_subsection_text(section: Dict) -> str:
    """Recursively extract plain text from a section and its subsections."""
    texts = []
    heading = section.get('TOCHeading', '')
    if heading:
        texts.append(heading)
    for info in section.get('Information', []):
        strings = info.get('Value', {}).get('StringWithMarkup', [])
        for s in strings:
            text = s.get('String', '')
            if text:
                texts.append(text)
    for subsection in section.get('Section', []):
        sub_text = _extract_subsection_text(subsection)
        if sub_text:
            texts.append(sub_text)
    return '\n'.join(texts)


def _extract_list_from_subsection(section: Dict) -> List[str]:
    """
    Extract a list of plain strings from a subsection (e.g., target organs).
    Splits on newlines, commas, semicolons.
    """
    items = []
    for info in section.get('Information', []):
        strings = info.get('Value', {}).get('StringWithMarkup', [])
        for s in strings:
            text = s.get('String', '')
            if text:
                # Split by common delimiters
                for part in re.split(r'[\n,;]+', text):
                    part = part.strip()
                    if part:
                        items.append(part)
    return items


# ============================================================
# 4. Chemical Classes (exact match against KG list)
# ============================================================

def get_chemical_classes_text(cid: str) -> Tuple[str, List[str]]:
    """
    Extract the exact "Chemical Classes" subsection text (inside "Chemical and Physical Properties")
    and match against KG chemical classes.
    
    Returns:
        (raw_text, matched_classes)
    """
    data = get_pug_view_raw(cid)
    if not data or 'Record' not in data:
        return "", []
    
    for section in data['Record'].get('Section', []):
        if section.get('TOCHeading') == 'Chemical and Physical Properties':
            for subsection in section.get('Section', []):
                if subsection.get('TOCHeading') == 'Chemical Classes':
                    raw_text = _extract_subsection_text(subsection)
                    matched = []
                    text_lower = raw_text.lower()
                    for kg_class in KG_CHEMICAL_CLASSES:
                        if kg_class.lower() in text_lower:
                            matched.append(kg_class)
                    return raw_text, matched
    return "", []


# ============================================================
# 5. Target Organs (from Toxicity → Target Organs)
# ============================================================

def get_target_organs(cid: str) -> List[str]:
    """
    Extract target organs from the Toxicity section's "Target Organs" subsection.
    Returns list of organ names as strings (e.g., ["Hepatic", "Renal", "Respiratory"]).
    """
    data = get_pug_view_raw(cid)
    if not data or 'Record' not in data:
        return []
    
    for section in data['Record'].get('Section', []):
        if section.get('TOCHeading') == 'Toxicity':
            for subsection in section.get('Section', []):
                if subsection.get('TOCHeading') == 'Target Organs':
                    return _extract_list_from_subsection(subsection)
    return []


# ============================================================
# 6. GHS Data (from Safety and Hazards → GHS Classification)
# ============================================================

def get_ghs_data(cid: str) -> Tuple[List[str], List[str], str]:
    """
    Extract GHS pictograms, hazard statements (H‑codes), and signal word from the
    "GHS Classification" subsection under "Safety and Hazards".
    
    Returns: (pictograms_list, h_codes_list, signal_word)
    """
    data = get_pug_view_raw(cid)
    if not data or 'Record' not in data:
        return [], [], ""
    
    for section in data['Record'].get('Section', []):
        if section.get('TOCHeading') == 'Safety and Hazards':
            for subsection in section.get('Section', []):
                if subsection.get('TOCHeading') == 'GHS Classification':
                    pictograms = []
                    h_codes = []
                    signal = ""
                    
                    for info in subsection.get('Information', []):
                        name = info.get('Name', '')
                        strings = info.get('Value', {}).get('StringWithMarkup', [])
                        if not strings:
                            continue
                        text = strings[0].get('String', '')
                        
                        if name == 'Pictogram(s)':
                            # text like "Irritant Health Hazard Environmental Hazard"
                            # Split by spaces, filter out empty
                            for token in text.split():
                                token = token.strip()
                                if token:
                                    pictograms.append(token)
                        elif name == 'Signal':
                            signal = text.strip()
                        elif name == 'GHS Hazard Statements':
                            # Extract H‑codes (e.g., "H317: May cause...")
                            lines = text.split('\n')
                            for line in lines:
                                match = re.search(r'(H\d{3})', line)
                                if match:
                                    h_codes.append(match.group(1))
                    
                    return list(set(pictograms)), list(set(h_codes)), signal
    return [], [], ""


# ============================================================
# 7. Description
# ============================================================

def get_description(cid: str) -> Optional[str]:
    """Get chemical description from Identification section."""
    data = get_pug_view_raw(cid)
    if not data or 'Record' not in data:
        return None
    
    for section in data['Record'].get('Section', []):
        if section.get('TOCHeading') == 'Identification':
            for subsection in section.get('Section', []):
                if subsection.get('TOCHeading') == 'Description':
                    text = _extract_subsection_text(subsection)
                    if text and len(text) < 2000:
                        return text
    return None


# ============================================================
# 8. Main Enrichment Function
# ============================================================

def enrich_chemical_from_pubchem(chemical_name: str) -> Dict[str, Any]:
    """
    Fetch ALL data for a chemical NOT in KG.
    
    Returns:
    {
        "found": bool,
        "cid": str,
        "properties": {...},
        "chemical_classes_text": str,
        "kg_classes": List[str],
        "target_organs": List[str],
        "ghs_pictograms": List[str],
        "ghs_h_codes": List[str],
        "ghs_signal": str,
        "description": str,
        "error": str
    }
    """
    cid = search_by_name_with_fallbacks(chemical_name)
    if not cid:
        return {
            "found": False,
            "cid": None,
            "properties": {},
            "chemical_classes_text": "",
            "kg_classes": [],
            "target_organs": [],
            "ghs_pictograms": [],
            "ghs_h_codes": [],
            "ghs_signal": "",
            "description": None,
            "error": f"Chemical '{chemical_name}' not found in PubChem"
        }
    
    properties = get_properties(cid)
    classes_text, kg_classes = get_chemical_classes_text(cid)
    target_organs = get_target_organs(cid)
    ghs_pictograms, ghs_h_codes, ghs_signal = get_ghs_data(cid)
    description = get_description(cid)
    
    return {
        "found": True,
        "cid": cid,
        "properties": properties,
        "chemical_classes_text": classes_text,
        "kg_classes": kg_classes,
        "target_organs": target_organs,
        "ghs_pictograms": ghs_pictograms,
        "ghs_h_codes": ghs_h_codes,
        "ghs_signal": ghs_signal,
        "description": description,
        "error": None
    }


# ============================================================
# LLM Judge Fallback Prompts (optional, for when matching fails)
# ============================================================

EXTRACT_CLASSES_FALLBACK_PROMPT = """You are a chemical data extractor. From the PubChem text below, identify which of these chemical classes apply to this compound.

Allowed classes: {classes}

Return ONLY JSON:
{
    "kg_classes": ["class1", "class2"],
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation"
}

If no classes found: {{"kg_classes": [], "confidence": 0.2, "reasoning": "No matching classes found"}}

PubChem Text:
{text}
"""


# ============================================================
# Test Function
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Testing Enhanced PubChem Client")
    print("=" * 60)
    
    test_name = "sodium lauryl sulfate"
    print(f"\n[1] Testing with '{test_name}'...")
    result = enrich_chemical_from_pubchem(test_name)
    
    if result["found"]:
        print(f"  ✅ Found CID: {result['cid']}")
        print(f"  Properties: {list(result['properties'].keys())}")
        print(f"\n  Raw Chemical Classes Text ({len(result['chemical_classes_text'])} chars):")
        if result['chemical_classes_text']:
            print(f"    {result['chemical_classes_text'][:300]}...")
        else:
            print("    (empty)")
        print(f"\n  Matched KG Classes: {result['kg_classes']}")
        print(f"\n  Target Organs: {result['target_organs']}")
        print(f"\n  GHS Pictograms: {result['ghs_pictograms']}")
        print(f"  GHS H‑Codes: {result['ghs_h_codes']}")
        print(f"  GHS Signal Word: {result['ghs_signal']}")
    else:
        print(f"  ❌ {result['error']}")
    
    print("\n✅ PubChem Client Ready")