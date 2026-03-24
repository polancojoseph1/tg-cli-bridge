"""Task list manager for MEMORY_DIR/Goals/TODOS.md.

Simple numbered task list shared across bots. Tasks are added via /task add
and removed via /task done. The file is auto-indexed by ChromaDB on startup
since it lives in MEMORY_DIR.

Status markers:
  [ ] — pending (new task, not started)
  [~] — in progress (task worker is executing)
  [!] — failed (task worker exhausted retries)
  Completed tasks are removed from the list entirely.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("bridge.tasks")

from config import MEMORY_DIR  # noqa: E402
TASK_FILE = Path(MEMORY_DIR) / "Goals" / "TODOS.md"

_HEADER = (
    "# Current Tasks\n\n"
    "> These are tasks the user is tracking. Do NOT work on any of these unless explicitly asked.\n"
    "> Managed via /task command in Telegram.\n\n"
)

# Regex matches both old format and new format with status markers:
#   Old: "1. [2026-02-24 13:35] Task text"
#   New: "1. [ ] [2026-02-24 13:35] Task text"
_TASK_RE = re.compile(r"^(\d+)\.\s+(?:\[([ ~x!])\]\s+)?\[([^\]]+)\]\s+(.+)$")
_CHECKBOX_RE = re.compile(r"^-\s+\[([ xX~!])\]\s+(.+)$")


def _ensure_file() -> None:
    """Create TODOS.md with header if it doesn't exist."""
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not TASK_FILE.exists():
        TASK_FILE.write_text(_HEADER, encoding="utf-8")


def _parse_tasks() -> list[dict]:
    """Parse TODOS.md and return list of {number, status, timestamp, text}.

    Handles both numbered format and markdown checkbox format.
    """
    _ensure_file()
    content = TASK_FILE.read_text(encoding="utf-8")
    tasks = []
    needs_rewrite = False
    for line in content.splitlines():
        match = _TASK_RE.match(line)
        if match:
            tasks.append({
                "number": int(match.group(1)),
                "status": match.group(2) or " ",
                "timestamp": match.group(3),
                "text": match.group(4),
            })
            continue
        cb_match = _CHECKBOX_RE.match(line)
        if cb_match:
            status_char = cb_match.group(1).lower()
            status = " " if status_char in (" ", "") else status_char
            tasks.append({
                "number": len(tasks) + 1,
                "status": status,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "text": cb_match.group(2),
            })
            needs_rewrite = True
    if needs_rewrite and tasks:
        logger.info("Normalizing %d tasks from checkbox to numbered format", len(tasks))
        _write_tasks(tasks)
    return tasks


def _write_tasks(tasks: list[dict]) -> None:
    """Rewrite TODOS.md with renumbered tasks and status markers."""
    lines = [_HEADER]
    for i, task in enumerate(tasks, 1):
        status = task.get("status", " ")
        lines.append(f"{i}. [{status}] [{task['timestamp']}] {task['text']}")
    content = "\n".join(lines) + "\n" if tasks else _HEADER
    TASK_FILE.write_text(content, encoding="utf-8")


def add_task(text: str) -> str:
    """Add a task. Returns confirmation message."""
    _ensure_file()
    tasks = _parse_tasks()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    tasks.append({"number": len(tasks) + 1, "status": " ", "timestamp": timestamp, "text": text})
    _write_tasks(tasks)
    logger.info("Task added: %s", text[:50])
    return f"Task #{len(tasks)} added: {text}"


def done_task(number: int) -> str:
    """Remove a task by number. Returns confirmation or error message."""
    tasks = _parse_tasks()
    if not tasks:
        return "No tasks to complete. List is empty."
    if number < 1 or number > len(tasks):
        return f"Invalid task number. You have {len(tasks)} task{'s' if len(tasks) != 1 else ''} (1-{len(tasks)})."
    removed = tasks.pop(number - 1)
    _write_tasks(tasks)
    logger.info("Task done: %s", removed['text'][:50])
    return f"Completed and removed task #{number}: {removed['text']}"


def list_tasks() -> str:
    """Return formatted task list for display, or a message if empty."""
    tasks = _parse_tasks()
    if not tasks:
        return "No current tasks."
    status_icons = {" ": "\u23f3", "~": "\u26a1", "x": "\u2705", "!": "\u274c"}
    lines = [f"<b>Current Tasks ({len(tasks)}):</b>"]
    for task in tasks:
        icon = status_icons.get(task["status"], "\u2753")
        lines.append(f"{icon} {task['number']}. [{task['status']}] [{task['timestamp']}] {task['text']}")
    return "\n".join(lines)
