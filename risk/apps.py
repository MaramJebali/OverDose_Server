# risk/apps.py
from django.apps import AppConfig
import logging

logger = logging.getLogger(__name__)


class RiskConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "risk"

    def ready(self):
        # Pre‑initialise the agent when Django starts (optional, avoids first‑request delay)
        try:
            from .mcp_agent.agent_runner import get_agent
            get_agent()
            logger.info("BiologicalAgent pre‑initialised successfully")
        except Exception as e:
            logger.warning(f"Could not pre‑initialise agent: {e}")