"""Aether Codex — Gradio web UI.

Run:  python app.py   (opens http://127.0.0.1:7860 in your browser)

Chat with the Codex Director; its delegations to sub-agents stream live into
an activity log above each answer. A dropdown switches the team's focus, and
the Team panel shows the current mission and roster.
"""

from __future__ import annotations

import json
import os

import gradio as gr

from aether_codex.config import DATA_DIR, settings
from aether_codex.director import Director
from aether_codex.prompts import DEFAULT_MISSION
from aether_codex.registry import DEFAULT_AGENT_SPECS

_EVENT_ICONS = {"thought": "💭", "tool_call": "🛠️", "tool_result": "↩️"}
_MISSION_PATH = DATA_DIR / "mission.txt"

# One Director per (provider, model) so each backend keeps its own history.
_directors: dict[tuple[str, str], Director] = {}


def _get_director(provider: str, model: str) -> Director:
    key = (provider, model.strip())
    if key not in _directors:
        _directors[key] = Director(provider=provider, model=model.strip() or None)
    return _directors[key]


# --------------------------------------------------------------------- focus
# Ready-made team focuses the dropdown offers. "Custom…" reveals a text box.
CUSTOM_LABEL = "✏️  Custom focus…"
MISSIONS: dict[str, str] = {
    "🏠  Home energy (cold climates)": DEFAULT_MISSION,
    "₿  Crypto markets": (
        "Focus on CRYPTO MARKETS — Bitcoin, Ethereum and major altcoins. Track "
        "live prices and recent price action, tokenomics, on-chain and macro "
        "developments, regulatory news, ETF flows and network upgrades. Always "
        "pair upside with explicit risk framing (volatility, smart-contract, "
        "regulatory, liquidity). Provide analysis and scenarios, never "
        "personalized financial advice — decisions are the user's own."
    ),
    "🔒  Smart-contract audits": (
        "Smart-contract security auditing for audit contests (Code4rena, "
        "Sherlock, Cantina). Focus on Solidity/EVM codebases and common "
        "vulnerability classes: reentrancy, access control flaws, oracle/price "
        "manipulation, integer/rounding errors, unchecked external calls, "
        "denial-of-service and signature replay. Deliverables are precise "
        "findings with exact file:line citations, severity "
        "(Critical/High/Medium/Low), impact, and a concrete recommended fix. "
        "Put the code to audit in the audit_target/ folder."
    ),
}


def _current_mission() -> str:
    if _MISSION_PATH.exists():
        try:
            text = _MISSION_PATH.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return DEFAULT_MISSION


def _match_preset(mission: str) -> str:
    """Return the dropdown label matching the current mission, else Custom."""
    for label, text in MISSIONS.items():
        if text.strip() == mission.strip():
            return label
    return CUSTOM_LABEL


def apply_focus(choice: str, custom_text: str):
    """Switch the whole team's focus from the dropdown. Persists to disk and
    rebuilds any already-running Director. Returns (status_md, note)."""
    mission = custom_text.strip() if choice == CUSTOM_LABEL else MISSIONS.get(choice, "")
    if len(mission) < 10:
        return team_status(), "⚠️ Type a focus (at least a sentence), then Apply."
    _MISSION_PATH.write_text(mission, encoding="utf-8")  # for Directors not yet built
    for director in _directors.values():
        try:
            director.apply_mission(mission)
        except Exception:
            pass
    short = choice if choice != CUSTOM_LABEL else "your custom focus"
    return team_status(), f"✅ Team refocused on {short}."


def toggle_custom(choice: str):
    return gr.update(visible=(choice == CUSTOM_LABEL))


# --------------------------------------------------------------------- status
def team_status() -> str:
    """Read the current mission + agent roster straight from disk, so the
    panel works even before an API key is configured (no Director needed)."""
    mission = _current_mission()
    lines = [f"**🎯 Current focus**\n\n{mission}\n", "**🤖 Team roster**\n"]
    for name, description, _fn, tool_names in DEFAULT_AGENT_SPECS:
        tools = ", ".join(tool_names) or "—"
        lines.append(f"- **{name}** · _{tools}_  \n  {description}")

    dynamic_file = DATA_DIR / "dynamic_agents.json"
    if dynamic_file.exists():
        try:
            spawned = json.loads(dynamic_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            spawned = []
        if spawned:
            lines.append("\n**✨ Spawned specialists** (persisted)\n")
            for a in spawned:
                tools = ", ".join(a.get("tools", [])) or "—"
                lines.append(f"- **{a['name']}** · _{tools}_  \n  {a.get('description', '')}")
    return "\n".join(lines)


def _render(log: list[str], answer: str) -> str:
    parts = []
    if log:
        body = "\n".join(f"- {line}" for line in log)
        parts.append(
            "<details open><summary><b>⚙️ Codex activity</b></summary>\n\n"
            f"{body}\n\n</details>\n\n---"
        )
    parts.append(answer)
    return "\n\n".join(parts)


def chat(message: str, history, provider: str, model: str):
    try:
        director = _get_director(provider, model)
    except Exception as exc:  # e.g. missing API key — show it in the chat
        yield f"⚠️ Could not initialize the **{provider}** backend: {exc}"
        return

    log: list[str] = []
    try:
        for kind, text in director.stream(message):
            if kind == "final":
                yield _render(log, text)
            else:
                log.append(f"{_EVENT_ICONS.get(kind, '•')} {text}")
                yield _render(log, "_working…_")
    except Exception as exc:
        yield _render(log, f"⚠️ Run failed: {exc}")


EXAMPLES = [
    "Give me a quick overview of this topic and the 3 questions worth researching first.",
    "Run this as a project: research the options, check the numbers, stress-test them, and save a report.",
    "Spawn an economics agent, then have it compare two options over 10 years.",
]

THEME = gr.themes.Soft(
    primary_hue="amber",
    secondary_hue="orange",
    neutral_hue="slate",
).set(body_background_fill="*neutral_50")

CSS = """
.gradio-container {max-width: 1180px !important;}
#hero {text-align:center; padding: 10px 0 0;}
#hero h1 {font-size: 2.2rem; margin-bottom: 2px;}
#hero p {color: var(--body-text-color-subdued); margin-top: 0; font-size: 1.02rem;}
#focus-card {background: var(--block-background-fill);
  border: 1px solid var(--border-color-primary); border-radius: 12px;
  padding: 12px 14px;}
#status-md {font-size: 0.9rem; line-height: 1.45;}
footer {visibility: hidden;}
"""

with gr.Blocks(title="Aether Codex") as demo:
    gr.HTML(
        "<div id='hero'><h1>⚡ Aether Codex</h1>"
        "<p>An AI research team. Pick a focus, then ask in plain language — "
        "the <b>Director</b> plans the work and assigns it to specialist agents.</p></div>"
    )

    with gr.Row():
        # --- left column: focus + conversation ---------------------------
        with gr.Column(scale=3):
            with gr.Group(elem_id="focus-card"):
                gr.Markdown("#### Step 1 — Choose what the team works on")
                with gr.Row():
                    focus_dd = gr.Dropdown(
                        choices=list(MISSIONS) + [CUSTOM_LABEL],
                        value=_match_preset(_current_mission()),
                        label=None,
                        container=False,
                        scale=4,
                    )
                    apply_btn = gr.Button("Apply focus", variant="primary", scale=1)
                custom_tb = gr.Textbox(
                    label="Describe your custom focus (2–5 sentences)",
                    placeholder="e.g. Evaluate small-scale solar options for a farm in southern Spain…",
                    visible=(_match_preset(_current_mission()) == CUSTOM_LABEL),
                    lines=2,
                )
                focus_note = gr.Markdown("")

            gr.Markdown("#### Step 2 — Ask the Director anything")
            with gr.Accordion("⚙️ Advanced: LLM provider & model", open=False):
                with gr.Row():
                    provider = gr.Dropdown(
                        ["anthropic", "grok", "local"],
                        value=settings.provider,
                        label="LLM provider",
                    )
                    model = gr.Textbox(
                        value=settings.model or "",
                        label="Model override (optional)",
                        placeholder="claude-sonnet-5 / grok-4 / llama3.1",
                    )
            gr.ChatInterface(
                fn=chat,
                additional_inputs=[provider, model],
                examples=[[e] for e in EXAMPLES],
            )

        # --- right column: live team status ------------------------------
        with gr.Column(scale=1, min_width=290):
            gr.Markdown("### 👥 Your team")
            status = gr.Markdown(team_status, elem_id="status-md")
            refresh = gr.Button("🔄 Refresh", size="sm")

    # wiring
    focus_dd.change(fn=toggle_custom, inputs=focus_dd, outputs=custom_tb)
    apply_btn.click(fn=apply_focus, inputs=[focus_dd, custom_tb],
                    outputs=[status, focus_note])
    refresh.click(fn=team_status, outputs=status)
    demo.load(fn=team_status, outputs=status)

if __name__ == "__main__":
    # The .bat launcher sets AETHER_OPEN_BROWSER=1 so a double-click pops open
    # the browser; other launchers (e.g. dev preview harnesses that manage the
    # browser themselves) leave it unset. In Gradio 6, theme and css belong on
    # launch(), not the Blocks constructor.
    open_browser = os.getenv("AETHER_OPEN_BROWSER") == "1"
    demo.launch(inbrowser=open_browser, theme=THEME, css=CSS)
