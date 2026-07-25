"""Central configuration, loaded from environment variables / a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "reports"   # where the Report Agent writes files
DATA_DIR = PROJECT_ROOT / "data"         # vector store + conversation persistence
AUDIT_DIR = PROJECT_ROOT / "audit_target"  # drop code-to-audit here; agents read it
POC_DIR = PROJECT_ROOT / "poc_workspace"   # isolated Foundry project for exploit PoCs

REPORTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
AUDIT_DIR.mkdir(exist_ok=True)
(POC_DIR / "src").mkdir(parents=True, exist_ok=True)
(POC_DIR / "test").mkdir(parents=True, exist_ok=True)


@dataclass
class Settings:
    """Runtime settings. Values come from the environment; every field can also
    be overridden per-Director via constructor arguments."""

    provider: str = os.getenv("AETHER_PROVIDER", "anthropic")
    model: str | None = os.getenv("AETHER_MODEL") or None
    temperature: float = float(os.getenv("AETHER_TEMPERATURE", "0.4"))

    # How many LangGraph steps (LLM calls + tool calls) one request may take.
    # Project mode runs many delegations in one turn, so this is generous.
    recursion_limit: int = int(os.getenv("AETHER_RECURSION_LIMIT", "120"))

    # How many past messages the Director keeps in its rolling context window.
    max_history_messages: int = int(os.getenv("AETHER_MAX_HISTORY", "20"))

    # Block-explorer API key (Etherscan V2 multichain — one key covers all
    # supported chains). Free tier at https://etherscan.io/apis. Optional:
    # without it, fetch_contract_source explains how to add one.
    etherscan_api_key: str | None = os.getenv("ETHERSCAN_API_KEY") or None


settings = Settings()
