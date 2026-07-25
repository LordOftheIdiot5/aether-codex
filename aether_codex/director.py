"""The Codex Director — orchestrator and project manager of the platform.

The Director is a LangGraph tool-calling agent whose tools are
meta-operations: delegating to specialists (serially or in parallel),
spawning new specialists, managing a persistent project board, refocusing
the platform's mission, and searching long-term memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from .config import DATA_DIR, settings
from .graph import build_tool_agent, message_text
from .llm import get_llm
from .memory import CodexMemory
from .project import ProjectBoard
from .prompts import DEFAULT_MISSION, director_prompt
from .registry import AgentRegistry

MISSION_PATH = DATA_DIR / "mission.txt"


def _shorten(text: str, limit: int = 240) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Director:
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.llm = get_llm(provider=provider, model=model)
        self.memory = CodexMemory()
        self.board = ProjectBoard()
        self.mission = self._load_mission()
        self.registry = AgentRegistry(self.llm, mission=self.mission)
        self.history: list = []  # rolling window of Human/AI messages
        # The prompt is a callable so a mid-conversation set_mission takes
        # effect immediately, without recompiling the graph.
        self.graph = build_tool_agent(
            self.llm, self._build_tools(), lambda: director_prompt(self.mission)
        )

    # ---------------------------------------------------------------- mission
    def _load_mission(self) -> str:
        if MISSION_PATH.exists():
            try:
                text = MISSION_PATH.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except OSError:
                pass
        return DEFAULT_MISSION

    def _save_mission(self) -> None:
        MISSION_PATH.write_text(self.mission, encoding="utf-8")

    def apply_mission(self, new_mission: str) -> None:
        """Refocus this Director: persist the mission and rebuild every
        built-in specialist around it. Used by the set_mission tool and by the
        web UI's focus selector."""
        self.mission = new_mission.strip()
        self._save_mission()
        self.registry.set_mission(self.mission)

    # ------------------------------------------------------------- meta-tools
    def _build_tools(self) -> list:
        director = self  # explicit closure handle for readability

        @tool
        def list_agents() -> str:
            """List every available specialist agent, what it does and which
            tools it has."""
            return director.registry.describe()

        def _run_one(agent_name: str, task: str) -> str:
            agent = director.registry.get(agent_name)
            if agent is None:
                return (f"No agent named '{agent_name}'. "
                        f"Available: {', '.join(director.registry.names())}")
            try:
                result = agent.run(task)
            except Exception as exc:
                # A single failed delegation (API overload, timeout, ...) must
                # not kill a whole project run — report it so the Director can
                # retry the task or continue with the rest.
                return (f"DELEGATION FAILED for {agent.name}: "
                        f"{type(exc).__name__}: {exc}. "
                        f"You may retry this delegation once, or continue and "
                        f"note the gap.")
            director.memory.remember(
                f"[{agent.name}] Task: {task}\nResult: {result}",
                kind="agent_result",
                metadata={"agent": agent.name},
            )
            return result

        @tool
        def delegate(agent_name: str, task: str) -> str:
            """Delegate one focused, self-contained task to a specialist agent.
            The agent cannot see the conversation — include every fact it
            needs (findings, chosen concepts, numbers) in `task`."""
            return _run_one(agent_name, task)

        @tool
        def delegate_many(assignments: str) -> str:
            """Run several specialists IN PARALLEL. `assignments` is a JSON
            array like: [{"agent": "research_agent", "task": "..."},
            {"agent": "crypto_agent", "task": "..."}]. Use only for tasks that
            are independent of each other. Returns all results labelled by
            agent."""
            try:
                jobs = json.loads(assignments)
                assert isinstance(jobs, list) and jobs
                pairs = [(str(j["agent"]), str(j["task"])) for j in jobs]
            except Exception:
                return ('Could not parse assignments. Expected JSON like '
                        '[{"agent": "research_agent", "task": "..."}].')
            if len(pairs) > 5:
                return "Too many parallel assignments (max 5) — split the work."
            with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
                results = list(pool.map(lambda p: _run_one(*p), pairs))
            return "\n\n".join(
                f"=== {agent} ===\n{result}"
                for (agent, _), result in zip(pairs, results)
            )

        @tool
        def spawn_agent(name: str, role_description: str, tools: str = "") -> str:
            """Create a new specialist agent when no existing agent fits the
            job. Write `role_description` like a real job description (scope,
            method, quality bar) — the platform turns it into a full system
            prompt, and the agent is saved for future sessions. `tools` is a
            comma-separated subset of: web_search, run_python,
            write_report_file, read_report_file, crypto_price,
            list_source_files, read_source_file (read code placed in the
            audit_target/ folder), fetch_contract_source (fetch a DEPLOYED
            contract's verified source by on-chain address — for auditing or
            rug-screening any live token), write_poc_file + run_forge_test
            (write a Foundry exploit into poc_workspace/ and actually RUN it —
            for building proof-of-concept exploits that are verified to pass)."""
            tool_names = [t.strip() for t in tools.split(",") if t.strip()]
            return director.registry.spawn(name, role_description, tool_names)

        @tool
        def set_mission(new_mission: str) -> str:
            """Refocus the entire platform on a new topic/domain. All built-in
            specialists are rebuilt around the new mission. Use when the user
            changes subject (e.g. from home energy to crypto markets). Write
            the mission as 2-5 sentences of domain context."""
            new_mission = new_mission.strip()
            if len(new_mission) < 10:
                return "Mission too short — describe the new focus in 2-5 sentences."
            director.apply_mission(new_mission)
            return f"Mission updated. All specialists refocused on:\n{new_mission}"

        @tool
        def create_project(goal: str, tasks: str) -> str:
            """Start a project on the persistent project board. `goal` is one
            sentence; `tasks` is a newline-separated list of concrete tasks in
            execution order. Replaces any previous project."""
            return director.board.create(goal, tasks.split("\n"))

        @tool
        def update_task(task_number: int, status: str, note: str = "") -> str:
            """Update a project task. status: todo | in_progress | done |
            blocked | skipped. Add a one-line result note when marking done."""
            return director.board.update(task_number, status, note)

        @tool
        def show_project() -> str:
            """Show the current project board with task statuses."""
            return director.board.render()

        @tool
        def recall_memory(query: str) -> str:
            """Search long-term memory (past research, agent results and
            conversations) before redoing work."""
            hits = director.memory.recall(query)
            if not hits:
                return "No relevant memories found."
            return "\n---\n".join(hits)

        return [list_agents, delegate, delegate_many, spawn_agent, set_mission,
                create_project, update_task, show_project, recall_memory]

    # ------------------------------------------------------------------- chat
    def stream(self, user_message: str) -> Iterator[tuple[str, str]]:
        """Run one user turn, yielding ("thought"|"tool_call"|"tool_result", text)
        progress events and finally ("final", answer)."""
        self.history.append(HumanMessage(content=user_message))
        window = self.history[-settings.max_history_messages:]

        final_answer = ""
        for update in self.graph.stream(
            {"messages": window},
            config={"recursion_limit": settings.recursion_limit},
            stream_mode="updates",
        ):
            for payload in update.values():
                for msg in payload.get("messages", []):
                    if isinstance(msg, AIMessage):
                        text = message_text(msg)
                        if msg.tool_calls:
                            if text:
                                yield ("thought", _shorten(text))
                            for call in msg.tool_calls:
                                args = call.get("args", {})
                                detail = args.get("agent_name") or args.get("name") \
                                    or args.get("query") or args.get("goal") or ""
                                task = _shorten(str(args.get("task", "")), 160)
                                label = f"{call['name']} → {detail}".rstrip(" →")
                                yield ("tool_call", f"{label}  {task}".rstrip())
                        elif text:
                            final_answer = text
                    elif isinstance(msg, ToolMessage):
                        yield ("tool_result", _shorten(message_text(msg)))

        self.history.append(AIMessage(content=final_answer))
        self.memory.append_exchange(user_message, final_answer)
        yield ("final", final_answer or "(The Director produced no final answer.)")

    def ask(self, user_message: str) -> str:
        """Blocking convenience wrapper: run a turn and return only the answer."""
        answer = ""
        for kind, text in self.stream(user_message):
            if kind == "final":
                answer = text
        return answer
