"""SubAgent — the unit of specialization.

Every specialist (built-in or spawned at runtime) is a SubAgent: a name, a
description the Director reads when choosing whom to delegate to, a system
prompt, an optional toolset, and its own compiled LangGraph loop.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from ..config import settings
from ..graph import build_tool_agent, message_text


class SubAgent:
    def __init__(self, name: str, description: str, system_prompt: str,
                 tools: list | None = None, llm=None):
        if llm is None:
            from ..llm import get_llm

            llm = get_llm()
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.graph = build_tool_agent(llm, self.tools, system_prompt)

    def run(self, task: str, context: str = "") -> str:
        """Execute one self-contained task and return the final answer text."""
        content = task if not context else f"{task}\n\nRelevant context:\n{context}"
        state = self.graph.invoke(
            {"messages": [HumanMessage(content=content)]},
            config={"recursion_limit": settings.recursion_limit},
        )
        return message_text(state["messages"][-1])

    def __repr__(self) -> str:  # pragma: no cover
        tool_names = ", ".join(t.name for t in self.tools) or "none"
        return f"<SubAgent {self.name} (tools: {tool_names})>"
