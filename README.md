# ⚡ Aether Codex

A multi-agent AI platform for discovering and simulating new energy and
propulsion concepts. The MVP mission focus: **practical home energy solutions
for cold climates** (especially Norway).

Built on **LangGraph**: the orchestrator ("Codex Director") and every
specialist agent are tool-calling LangGraph state machines. The Director's
tools are *meta-operations* — delegating to specialists, spawning brand-new
specialists at runtime, and searching long-term memory.

## Architecture

```
                         ┌──────────────────────┐
        Gradio UI ──────▶│    CODEX DIRECTOR    │◀────── CLI
      (app.py)           │  (LangGraph agent)   │   (python -m aether_codex.cli)
                         └──────────┬───────────┘
      tools: delegate · delegate_many (parallel) · spawn_agent · set_mission
             create_project / update_task / show_project · recall_memory
                                    │
     ┌──────────┬──────────┬────────┴─┬──────────┬──────────┬────────────┐
     ▼          ▼          ▼          ▼          ▼          ▼            ▼
  research_  concept_   physics_   critic_    report_    crypto_    (spawned on
    agent     agent      agent      agent      agent      agent     the fly...)
 [web_search]  [—]    [run_python]   [—]    [write/read [crypto_price,  [any]
                                             report]  search, python]
                                    │
                         ┌──────────┴───────────┐
                         │     CodexMemory      │
                         │ ChromaDB vector store│
                         │ + conversation log   │
                         └──────────────────────┘
```

Every agent runs the same compiled LangGraph loop (`aether_codex/graph.py`):
`agent → tools → agent → … → END`. Sub-agents execute *inside* the Director's
`delegate` tool calls, giving a hierarchical multi-agent system.

## Folder structure

```
aether-codex/
├── app.py                  # Gradio web UI (entry point)
├── requirements.txt
├── .env.example            # copy to .env and fill in
├── reports/                # reports written by the Report Agent
├── data/                   # vector store + conversation log (auto-created)
└── aether_codex/
    ├── config.py           # env-driven settings, paths
    ├── llm.py              # provider factory: Claude / Grok / Ollama
    ├── graph.py            # the reusable LangGraph agent loop
    ├── memory.py           # ChromaDB vector memory + conversation history
    ├── tools.py            # web_search, run_python, write/read report files
    ├── prompts.py          # system prompts for every agent (edit freely)
    ├── registry.py         # roster of agents + runtime spawning
    ├── director.py         # the orchestrator
    ├── cli.py              # terminal chat
    └── agents/
        └── base.py         # SubAgent: name + prompt + tools + graph
```

## Setup

Requires **Python 3.11+**.

```powershell
cd aether-codex
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # then edit .env: set your API key
```

Minimal `.env` for Claude:

```
AETHER_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

**Web UI** (recommended):

```powershell
python app.py
# open http://127.0.0.1:7860
```

**Terminal:**

```powershell
python -m aether_codex.cli "How can I cut heating costs in an older Norwegian house?"
python -m aether_codex.cli --provider local          # interactive, via Ollama
```

## Switching LLMs

| Provider    | `.env` setting            | Needs                         | Default model     |
|-------------|---------------------------|-------------------------------|-------------------|
| Claude      | `AETHER_PROVIDER=anthropic` | `ANTHROPIC_API_KEY`         | `claude-sonnet-5` |
| Grok (xAI)  | `AETHER_PROVIDER=grok`      | `XAI_API_KEY`               | `grok-4`          |
| Local       | `AETHER_PROVIDER=local`     | [Ollama](https://ollama.com) running | `llama3.1` |

Override the model with `AETHER_MODEL=...`, the dropdown in the web UI, or
`--model` on the CLI. Adding a provider = one new branch in
`aether_codex/llm.py`.

## Example prompts

Quick question (Director answers with at most one delegation):

> What are the most cost-effective ways to cut heating costs in a 1970s
> Norwegian detached house?

Full pipeline — research → concepts → physics → critic → saved report:

> Do a full concept study: storing cheap night-time electricity as heat for
> daytime use in a Nordic home. Save a report.

Dynamic agent creation:

> Spawn an economics agent and have it compare 10-year total cost of
> ownership: air-source vs. ground-source heat pump for a house in Oslo.

Physics check:

> Is a small home wind turbine ever worth it on the Norwegian coast? Check
> the physics and economics.

## Autonomy features

- **Project mode**: for multi-step work the Director creates a persistent
  project board (`data/project.json`), executes tasks in order, marks them
  done, and finishes with a saved report — minimal human input needed. Trigger
  it with "run this as a project: …".
- **Parallel team**: `delegate_many` runs up to 5 specialists concurrently for
  independent tasks.
- **Smart spawning**: when the Director spawns a new agent, the LLM writes a
  full specialist system prompt from the role description, and the agent is
  persisted (`data/dynamic_agents.json`) so it exists in future sessions.
- **Switchable mission**: the platform focus is runtime state
  (`data/mission.txt`). Say "switch the mission to crypto markets" (or any
  topic) and every built-in specialist is rebuilt around it. The default
  mission is home energy in cold climates.
- **Crypto agent**: built-in specialist with live CoinGecko prices
  (`crypto_price`), web search and Python. It provides analysis with explicit
  risk framing — never personalized financial advice.

## Extending

- **New built-in agent**: add a system prompt in `prompts.py` and one line in
  `DEFAULT_AGENT_SPECS` in `registry.py`.
- **New tool**: add an `@tool` function in `tools.py` and register it in
  `TOOLBOX` — it becomes grantable to spawned agents immediately.
- **New mission focus**: edit `MISSION_CONTEXT` in `prompts.py`; the entire
  crew refocuses.

## Notes & limitations (MVP)

- `run_python` executes code **in-process, unsandboxed** — fine for a local
  prototype driven by your own prompts; do not expose the app publicly as-is.
- Web search uses DuckDuckGo (no API key). If ChromaDB's embedding model
  can't download (offline), memory falls back to a keyword store automatically.
- Conversation history is kept per Director instance (per provider/model pair
  in the web UI) and persisted to `data/conversation.json`.
