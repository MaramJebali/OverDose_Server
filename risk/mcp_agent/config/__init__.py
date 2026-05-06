import logging
import os
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# The .env is always at the Django project root (same dir as manage.py)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_dotenv_path = _PROJECT_ROOT / ".env"

if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path, override=True)
else:
    logger.warning("No .env file found at %s", _dotenv_path)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

def validate():
    missing = []
    if not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    if not NEO4J_URI:
        missing.append("NEO4J_URI")
    if not NEO4J_USER:
        missing.append("NEO4J_USER")
    if not NEO4J_PASSWORD:
        missing.append("NEO4J_PASSWORD")
    if missing:
        raise EnvironmentError(f"Missing environment variables: {', '.join(missing)}")
    logger.info("✅ Configuration validated")