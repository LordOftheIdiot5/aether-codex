"""Agent registry — the Director's roster of specialists.

Three tiers:
  1. General agents (DEFAULT_AGENT_SPECS) — rebuilt around the current mission
     whenever it changes (research, concept, physics, critic, report, crypto).
  2. The audit team (audit_agents.json, shipped with the package) — fixed-role
     specialists (vuln_hunter, recon_agent, poc_writer, rug_screener) that are
     always available and are NOT re-themed on set_mission.
  3. Agents spawned at runtime — LLM-crafted prompts persisted to
     data/dynamic_agents.json so they survive restarts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .agents.base import SubAgent
from .config import DATA_DIR
from .graph import message_text
from . import prompts
from .tools import TOOLBOX

# Committed, fixed-role audit team shipped inside the package.
AUDIT_AGENTS_PATH = Path(__file__).resolve().parent / "audit_agents.json"

# (name, description shown to the Director, prompt function, tool names)
DEFAULT_AGENT_SPECS = [
    ("research_agent",
     "Searches the web for literature, data, products, prices and market info; returns cited findings.",
     prompts.research_prompt, ["web_search"]),
    ("concept_agent",
     "Brainstorms 3-6 candidate concepts from a problem statement and research findings.",
     prompts.concept_prompt, []),
    ("physics_agent",
     "Runs Python calculations to check feasibility: energy balances, efficiencies, cashflows, break-evens.",
     prompts.physics_prompt, ["run_python"]),
    ("critic_agent",
     "Adversarially reviews concepts for physical, practical, economic and regulatory weaknesses.",
     prompts.critic_prompt, []),
    ("report_agent",
     "Synthesizes all findings into a clean markdown report and saves it to reports/.",
     prompts.report_prompt, ["write_report_file", "read_report_file"]),
    ("crypto_agent",
     "Analyzes crypto markets, tokens and protocols: live prices, tokenomics, risks, and reads deployed contracts on-chain.",
     prompts.crypto_prompt,
     ["crypto_price", "web_search", "run_python", "fetch_contract_source"]),
]


class AgentRegistry:
    def __init__(self, llm, mission: str, dynamic_path: Path | None = None):
        self.llm = llm
        self.mission = mission
        self.dynamic_path = dynamic_path or (DATA_DIR / "dynamic_agents.json")
        self.agents: dict[str, SubAgent] = {}
        self._build_defaults()
        self._load_specialists()  # committed audit team (mission-independent)
        self._load_dynamic()      # user-spawned agents (runtime)

    # ------------------------------------------------------------------ build
    def _build_defaults(self) -> None:
        for name, description, prompt_fn, tool_names in DEFAULT_AGENT_SPECS:
            self.agents[name] = SubAgent(
                name=name,
                description=description,
                system_prompt=prompt_fn(self.mission),
                tools=[TOOLBOX[t] for t in tool_names],
                llm=self.llm,
            )

    def set_mission(self, mission: str) -> None:
        """Rebuild every built-in agent around a new mission focus.
        Dynamic agents keep their crafted prompts (their roles are their own)."""
        self.mission = mission
        self._build_defaults()

    # ------------------------------------------------------------------ query
    def get(self, name: str) -> SubAgent | None:
        return self.agents.get(name.strip().lower())

    def names(self) -> list[str]:
        return list(self.agents)

    def describe(self) -> str:
        lines = []
        for agent in self.agents.values():
            tools = ", ".join(t.name for t in agent.tools) or "none"
            lines.append(f"- {agent.name}: {agent.description} (tools: {tools})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ spawn
    def spawn(self, name: str, role_description: str, tool_names: list[str]) -> str:
        """Create a new agent at runtime. Uses the LLM to craft a proper
        specialist system prompt from the role description; persists the agent
        to disk. Returns a status string (never raises)."""
        slug = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
        if not slug:
            return "Agent name must contain letters or digits."
        if slug in self.agents:
            return f"Agent '{slug}' already exists — delegate to it directly."

        unknown = [t for t in tool_names if t not in TOOLBOX]
        if unknown:
            return (f"Unknown tools: {unknown}. "
                    f"Available tools: {', '.join(TOOLBOX)}")

        system_prompt = self._craft_prompt(slug, role_description, tool_names)
        crafted = system_prompt is not None
        if system_prompt is None:
            system_prompt = prompts.dynamic_agent_prompt(self.mission, role_description)

        self._register_dynamic(slug, role_description, system_prompt, tool_names,
                               persist=True)
        granted = ", ".join(tool_names) or "none"
        how = "LLM-crafted prompt" if crafted else "template prompt"
        return (f"Created agent '{slug}' ({how}, tools: {granted}). "
                f"It is saved and will also be available in future sessions.")

    def _craft_prompt(self, name: str, role_description: str,
                      tool_names: list[str]) -> str | None:
        """Have the LLM write a proper specialist prompt. Returns None on any
        failure so the caller can fall back to the template."""
        try:
            spec = (
                f"Agent name: {name}\n"
                f"Available tools: {', '.join(tool_names) or 'none'}\n"
                f"Platform mission context to include verbatim near the top:\n"
                f"{prompts.mission_context(self.mission)}\n\n"
                f"Role description:\n{role_description}"
            )
            response = self.llm.invoke([
                SystemMessage(content=prompts.PROMPT_ENGINEER_PROMPT),
                HumanMessage(content=spec),
            ])
            text = message_text(response).strip()
            return text if len(text) > 100 else None
        except Exception:
            return None

    def _register_dynamic(self, slug: str, description: str, system_prompt: str,
                          tool_names: list[str], persist: bool) -> None:
        self.agents[slug] = SubAgent(
            name=slug,
            description=description.split("\n")[0][:200],
            system_prompt=system_prompt,
            tools=[TOOLBOX[t] for t in tool_names],
            llm=self.llm,
        )
        if persist:
            saved = self._read_dynamic_file()
            saved = [a for a in saved if a.get("name") != slug]
            saved.append({
                "name": slug,
                "description": description,
                "system_prompt": system_prompt,
                "tools": tool_names,
            })
            self.dynamic_path.write_text(
                json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8"
            )

    # ------------------------------------------------------------ specialists
    def _load_specialists(self) -> None:
        """Load the committed, fixed-role audit team from the package. Unlike
        the general agents these are not re-themed on set_mission — a
        vulnerability hunter stays a vulnerability hunter regardless of the
        platform's current focus."""
        if not AUDIT_AGENTS_PATH.exists():
            return
        try:
            specs = json.loads(AUDIT_AGENTS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for spec in specs:
            try:
                tool_names = [t for t in spec.get("tools", []) if t in TOOLBOX]
                self._register_dynamic(
                    spec["name"], spec.get("description", spec["name"]),
                    spec["system_prompt"], tool_names, persist=False,
                )
            except (KeyError, TypeError):
                continue

    # ------------------------------------------------------------ persistence
    def _read_dynamic_file(self) -> list[dict]:
        if self.dynamic_path.exists():
            try:
                return json.loads(self.dynamic_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _load_dynamic(self) -> None:
        for spec in self._read_dynamic_file():
            try:
                tool_names = [t for t in spec.get("tools", []) if t in TOOLBOX]
                self._register_dynamic(
                    spec["name"], spec.get("description", spec["name"]),
                    spec["system_prompt"], tool_names, persist=False,
                )
            except (KeyError, TypeError):
                continue  # skip malformed entries rather than failing startup
