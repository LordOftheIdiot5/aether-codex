"""LangGraph building blocks.

`build_tool_agent` compiles the canonical LangGraph agent loop:

        ┌────────┐   tool calls    ┌───────┐
    ──▶ │ agent  │ ──────────────▶ │ tools │
        └────────┘ ◀────────────── └───────┘
             │  no tool calls
             ▼
            END

Both the Director and every sub-agent are instances of this same graph — the
Director's "tools" just happen to be other agents.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition


def message_text(message: BaseMessage) -> str:
    """Normalize message content to plain text. Anthropic models may return a
    list of content blocks instead of a string."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def build_tool_agent(llm, tools: list, system_prompt):
    """Compile a tool-calling agent graph around `llm`.

    `system_prompt` may be a string, or a zero-arg callable returning a string
    — the callable form lets the prompt reflect live state (e.g. the current
    mission) without recompiling the graph."""
    model = llm.bind_tools(tools) if tools else llm

    def call_model(state: MessagesState) -> dict:
        prompt = system_prompt() if callable(system_prompt) else system_prompt
        messages = [SystemMessage(content=prompt)] + state["messages"]
        response: AIMessage = model.invoke(messages)
        return {"messages": [response]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", call_model)
    graph.add_edge(START, "agent")

    if tools:
        graph.add_node("tools", ToolNode(tools))
        # Routes to "tools" when the model made tool calls, otherwise to END.
        graph.add_conditional_edges("agent", tools_condition)
        graph.add_edge("tools", "agent")
    else:
        graph.add_edge("agent", END)

    return graph.compile()
