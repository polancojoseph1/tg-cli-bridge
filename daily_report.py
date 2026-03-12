"""Append completed task summaries to today's daily report in MEMORY_DIR/Tasks Done/
and to the structured daily log in MEMORY_DIR/Daily/.

Enable with DAILY_REPORT_ENABLED=true in your .env (disabled by default).
"""

import datetime
import os
from config import MEMORY_DIR

DAILY_REPORT_ENABLED: bool = os.environ.get("DAILY_REPORT_ENABLED", "false").lower() == "true"

TASKS_DONE_DIR = os.path.join(MEMORY_DIR, "Tasks Done")
DAILY_DIR = os.path.join(MEMORY_DIR, "Daily")

_SKIP_INPUTS = {
    "hi", "hello", "hey", "ok", "okay", "thanks", "thank you", "yes", "no",
    "bye", "goodbye", "sure", "yep", "nope", "nice", "cool", "great",
}


def _is_trivial(user_input: str) -> bool:
    stripped = user_input.strip().lower().rstrip("!.,?")
    return stripped in _SKIP_INPUTS or len(stripped) < 8


def log_task(bot_name: str, user_input: str, response: str) -> None:
    """Log a one-line entry to today's daily report if this looks like a real task."""
    if not DAILY_REPORT_ENABLED:
        return
    if _is_trivial(user_input):
        return
    if len(response.strip()) < 80:
        return

    today = datetime.date.today().strftime("%Y-%m-%d")
    timestamp = datetime.datetime.now().strftime("%H:%M")
    report_path = os.path.join(TASKS_DONE_DIR, f"Daily Report - {today}.md")

    summary = user_input.strip().replace("\n", " ")[:100]
    if len(user_input.strip()) > 100:
        summary += "..."

    line = f"[{timestamp}] [{bot_name}]: {summary}\n"

    os.makedirs(TASKS_DONE_DIR, exist_ok=True)

    if not os.path.exists(report_path):
        with open(report_path, "w") as f:
            f.write(f"# Daily Report - {today}\n\n")

    with open(report_path, "a") as f:
        f.write(line)

    # Also write to Daily/YYYY-MM-DD.md
    os.makedirs(DAILY_DIR, exist_ok=True)
    daily_path = os.path.join(DAILY_DIR, f"{today}.md")

    if not os.path.exists(daily_path):
        with open(daily_path, "w") as f:
            f.write(
                f"# {today} — Daily Notes\n\n"
                "## Wins\n\n-\n\n"
                "## What I Worked On\n\n-\n\n"
                "## Thoughts & Ideas\n\n-\n\n"
                "## Notes\n\n-\n\n"
                "## Activity Log\n\n"
            )

    with open(daily_path, "r") as f:
        content = f.read()
    if "## Activity Log" not in content:
        with open(daily_path, "a") as f:
            f.write("\n## Activity Log\n\n")

    with open(daily_path, "a") as f:
        f.write(line)
