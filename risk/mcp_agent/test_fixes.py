import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Test 1: Verify resolution update after PubChem success
print("Test 1: Checking _investigate_chemical PubChem resolution update...")

with open(Path(__file__).parent / "agent" / "agent.py", "r", encoding="utf-8") as f:
    agent_code = f.read()

# Check that resolution is updated with unresolved=False after PubChem success
if '"unresolved": False' in agent_code or "'unresolved': False" in agent_code:
    # Find the context - should be in the PubChem block
    lines = agent_code.split('\n')
    found_pubchem_resolution = False
    for i, line in enumerate(lines):
        if 'finding["resolution"]' in line and i > 400 and i < 530:
            # Check if within 20 lines there's 'unresolved' set to False
            block = '\n'.join(lines[i:i+20])
            if 'unresolved' in block and 'False' in block:
                found_pubchem_resolution = True
                print(f"  PASS: Resolution update with unresolved=False found at line {i+1}")
                break
    
    if not found_pubchem_resolution:
        print("  FAIL: Could not find resolution update with unresolved=False in PubChem block")
else:
    print("  FAIL: No 'unresolved': False found in agent.py")

# Test 2: Verify unverified chemicals excludes PUBCHEM source
print("\nTest 2: Checking unverified_chemicals excludes PUBCHEM source...")

if 'f.get("source") != "PUBCHEM"' in agent_code or "f.get('source') != 'PUBCHEM'" in agent_code:
    print("  PASS: Unverified chemicals logic excludes PUBCHEM source")
else:
    print("  FAIL: Unverified chemicals logic does not exclude PUBCHEM source")

# Test 3: Verify groq.py logger outputs to stderr
print("\nTest 3: Checking groq.py logger configuration...")

with open(Path(__file__).parent / "config" / "groq.py", "r", encoding="utf-8") as f:
    groq_code = f.read()

if 'StreamHandler(sys.stderr)' in groq_code or 'stream=sys.stderr' in groq_code:
    print("  PASS: Logger configured to output to stderr")
else:
    print("  FAIL: Logger not configured to output to stderr")

if 'logger.propagate = False' in groq_code:
    print("  PASS: Logger propagate set to False")
else:
    print("  WARN: Logger propagate not set to False (may still leak to stdout)")

# Test 4: Verify PubChem fallbacks for common INCI names
print("\nTest 4: Checking PubChem INCI fallbacks...")

with open(Path(__file__).parent / "servers" / "kg_server" / "pubchem_client.py", "r", encoding="utf-8") as f:
    pubchem_code = f.read()

fallbacks_to_check = ["DIMETHICONE", "SODIUM LAURETH SULFATE", "PEG-IOO STEARATE", "PARFUM"]
for name in fallbacks_to_check:
    if f'"{name}"' in pubchem_code or f"'{name}'" in pubchem_code:
        print(f"  PASS: {name} has fallback entries")
    else:
        print(f"  WARN: {name} has no fallback entries")

print("\n=== All static checks complete ===")
