"""ProjectBoard — the Director's persistent task board.

One active project at a time (MVP). Stored as JSON so a project survives
restarts and the Director can resume where it left off.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import DATA_DIR

VALID_STATUSES = ("todo", "in_progress", "done", "blocked", "skipped")


class ProjectBoard:
    def __init__(self, path: Path | None = None):
        self.path = path or (DATA_DIR / "project.json")
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False),
                             encoding="utf-8")

    # ------------------------------------------------------------------- API
    def create(self, goal: str, tasks: list[str]) -> str:
        self.data = {
            "goal": goal.strip(),
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "tasks": [
                {"n": i, "task": t.strip(), "status": "todo", "note": ""}
                for i, t in enumerate((t for t in tasks if t.strip()), start=1)
            ],
        }
        self._save()
        return f"Project created with {len(self.data['tasks'])} tasks.\n{self.render()}"

    def update(self, task_number: int, status: str, note: str = "") -> str:
        if not self.data.get("tasks"):
            return "No active project. Use create_project first."
        status = status.strip().lower().replace(" ", "_")
        if status not in VALID_STATUSES:
            return f"Invalid status '{status}'. Use one of: {', '.join(VALID_STATUSES)}"
        for task in self.data["tasks"]:
            if task["n"] == task_number:
                task["status"] = status
                if note:
                    task["note"] = note.strip()[:300]
                self._save()
                return f"Task {task_number} -> {status}.\n{self.render()}"
        return f"No task #{task_number}. {self.render()}"

    def render(self) -> str:
        if not self.data.get("tasks"):
            return "No active project."
        icons = {"todo": "[ ]", "in_progress": "[~]", "done": "[x]",
                 "blocked": "[!]", "skipped": "[-]"}
        lines = [f"PROJECT: {self.data['goal']} (created {self.data['created']})"]
        for t in self.data["tasks"]:
            note = f" — {t['note']}" if t["note"] else ""
            lines.append(f"{icons[t['status']]} {t['n']}. {t['task']}{note}")
        remaining = sum(1 for t in self.data["tasks"]
                        if t["status"] in ("todo", "in_progress", "blocked"))
        lines.append(f"({remaining} task(s) remaining)")
        return "\n".join(lines)
