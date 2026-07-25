"""Terminal chat with the Codex Director (no web UI needed).

Usage:
    python -m aether_codex.cli                       # interactive chat
    python -m aether_codex.cli "your question here"  # one-shot
    python -m aether_codex.cli --provider local ...  # switch backend
"""

from __future__ import annotations

import argparse

from .director import Director

_ICONS = {"thought": "💭", "tool_call": "🛠️ ", "tool_result": "↩️ "}


def _run_turn(director: Director, question: str) -> None:
    for kind, text in director.stream(question):
        if kind == "final":
            print(f"\n{'=' * 60}\n{text}\n")
        else:
            print(f"  {_ICONS.get(kind, '•')} {text}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the Codex Director.")
    parser.add_argument("question", nargs="*", help="one-shot question (omit for interactive mode)")
    parser.add_argument("--provider", default=None, help="anthropic | grok | local")
    parser.add_argument("--model", default=None, help="model name override")
    args = parser.parse_args()

    director = Director(provider=args.provider, model=args.model)

    if args.question:
        _run_turn(director, " ".join(args.question))
        return

    print("Aether Codex — type a question, or 'exit' to quit.\n")
    while True:
        try:
            question = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question or question.lower() in {"exit", "quit"}:
            break
        _run_turn(director, question)


if __name__ == "__main__":
    main()
