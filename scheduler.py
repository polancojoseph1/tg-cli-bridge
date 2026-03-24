"""scheduler.py — Simple in-process task scheduler.

Schedules are stored in SQLite and managed entirely via Telegram commands:
    /schedule every day 9am summarize AI news
    /schedule every 2h check for new emails
    /schedule once 2026-03-20 14:00 send weekly report
    /schedules                  ← list active schedules
    /unschedule 3               ← cancel by ID

No file editing required. Survives restarts (SQLite persists state).
Runs inside the FastAPI process using asyncio — no external daemon needed.
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TIMEZONE = os.environ.get("TIMEZONE", "UTC")
_TZ = ZoneInfo(TIMEZONE)
_CHECK_INTERVAL = 30   # seconds between schedule checks
_DEFAULT_TIMEOUT = 300  # seconds per task run


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def _get_db_path() -> str:
    import os
    data_dir = Path(os.path.expanduser(
        os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot")
    ))
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "schedules.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                description TEXT    NOT NULL,
                recurrence  TEXT    NOT NULL,
                next_run    TEXT    NOT NULL,
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Time parsing
# ---------------------------------------------------------------------------

_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


def _parse_time(s: str) -> tuple[int, int] | None:
    """Parse a time string like '9am', '9:30', '14:00' → (hour, minute)."""
    m = _TIME_RE.fullmatch(s.strip())
    if not m:
        return None
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and h != 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mins <= 59):
        return None
    return h, mins


def parse_recurrence(text: str) -> tuple[str, dict] | None:
    """Parse a natural-language recurrence string.

    Returns (recurrence_type, params) or None if unrecognised.

    Types:
        "interval"  — params: {minutes: int}
        "daily"     — params: {hour: int, minute: int}
        "weekly"    — params: {weekday: int, hour: int, minute: int}
        "once"      — params: {dt: str}  ISO datetime string
    """
    s = text.strip().lower()

    # --- interval: "every 30m", "every 2h", "every 1d" ---
    m = re.match(r"every\s+(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b", s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)[0]
        minutes = {"m": val, "h": val * 60, "d": val * 1440}[unit]
        return ("interval", {"minutes": minutes})

    # --- daily: "daily", "every day", "daily 9am", "every day at 9:30" ---
    m = re.match(r"(?:daily|every\s+day)(?:\s+(?:at\s+)?(\S+))?$", s)
    if m:
        time_str = m.group(1) or "09:00"
        parsed = _parse_time(time_str)
        if parsed:
            return ("daily", {"hour": parsed[0], "minute": parsed[1]})

    # --- weekly: "every monday", "weekly monday 9am", "every mon at 9:00" ---
    m = re.match(r"(?:every|weekly)\s+(\w+)(?:\s+(?:at\s+)?(\S+))?$", s)
    if m:
        day_str = m.group(1)
        if day_str in _WEEKDAYS:
            time_str = m.group(2) or "09:00"
            parsed = _parse_time(time_str)
            if parsed:
                return ("weekly", {
                    "weekday": _WEEKDAYS[day_str],
                    "hour": parsed[0],
                    "minute": parsed[1],
                })

    # --- once: "once 2026-03-20", "once 2026-03-20 14:00", "once tomorrow 9am" ---
    m = re.match(r"once\s+(.+)$", s)
    if m:
        date_str = m.group(1).strip()
        now = _now()

        if date_str.startswith("tomorrow"):
            rest = date_str[len("tomorrow"):].strip()
            parsed = _parse_time(rest) if rest else (9, 0)
            if parsed:
                dt = (now + timedelta(days=1)).replace(
                    hour=parsed[0], minute=parsed[1], second=0, microsecond=0
                )
                return ("once", {"dt": dt.isoformat()})

        # ISO date: YYYY-MM-DD [HH:MM]
        dm = re.match(r"(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?$", date_str)
        if dm:
            date_part = dm.group(1)
            time_part = dm.group(2) or "09:00"
            parsed = _parse_time(time_part)
            if parsed:
                dt = datetime.strptime(date_part, "%Y-%m-%d").replace(
                    hour=parsed[0], minute=parsed[1], tzinfo=_TZ
                )
                return ("once", {"dt": dt.isoformat()})

    return None


def recurrence_label(recurrence: str, params: dict) -> str:
    """Human-readable description of a recurrence."""
    if recurrence == "interval":
        mins = params["minutes"]
        if mins < 60:
            return f"every {mins}m"
        if mins % 1440 == 0:
            return f"every {mins // 1440}d"
        if mins % 60 == 0:
            return f"every {mins // 60}h"
        return f"every {mins}m"
    if recurrence == "daily":
        return f"daily at {params['hour']:02d}:{params['minute']:02d}"
    if recurrence == "weekly":
        day = [k for k, v in _WEEKDAYS.items() if v == params["weekday"] and len(k) > 3][0]
        return f"every {day.capitalize()} at {params['hour']:02d}:{params['minute']:02d}"
    if recurrence == "once":
        return f"once at {params['dt'][:16]}"
    return recurrence


# ---------------------------------------------------------------------------
# Next-run calculation
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(_TZ)


def _calc_next_run(recurrence: str, params: dict, from_dt: datetime | None = None) -> datetime:
    now = from_dt or _now()
    if recurrence == "interval":
        return now + timedelta(minutes=params["minutes"])
    if recurrence == "daily":
        next_run = now.replace(
            hour=params["hour"], minute=params["minute"], second=0, microsecond=0
        )
        if next_run <= now:
            next_run += timedelta(days=1)
        return next_run
    if recurrence == "weekly":
        next_run = now.replace(
            hour=params["hour"], minute=params["minute"], second=0, microsecond=0
        )
        days_ahead = params["weekday"] - next_run.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return next_run + timedelta(days=days_ahead)
    if recurrence == "once":
        return datetime.fromisoformat(params["dt"])
    raise ValueError(f"Unknown recurrence: {recurrence}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_schedule(
    chat_id: int,
    description: str,
    recurrence: str,
    params: dict,
) -> int:
    """Add a new schedule. Returns the new schedule ID."""
    import json
    next_run = _calc_next_run(recurrence, params)
    now_iso = _now().isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO schedules (chat_id, description, recurrence, next_run, enabled, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (chat_id, description, json.dumps({"type": recurrence, **params}),
             next_run.isoformat(), now_iso),
        )
        conn.commit()
        return cur.lastrowid


def list_schedules(chat_id: int) -> list[dict]:
    """Return all enabled schedules for a chat."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE chat_id = ? AND enabled = 1 ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_schedule(chat_id: int, schedule_id: int) -> bool:
    """Disable a schedule by ID. Returns True if found and removed."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE schedules SET enabled = 0 WHERE id = ? AND chat_id = ?",
            (schedule_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _get_due_schedules() -> list[dict]:
    """Return all enabled schedules whose next_run is now or past."""
    now_iso = _now().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?",
            (now_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


def _advance_schedule(row: dict) -> None:
    """Update next_run for a recurring schedule, or disable a one-shot."""
    import json
    data = json.loads(row["recurrence"])
    recurrence = data.pop("type")
    params = data

    if recurrence == "once":
        with _connect() as conn:
            conn.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (row["id"],))
            conn.commit()
        return

    next_run = _calc_next_run(recurrence, params)
    with _connect() as conn:
        conn.execute(
            "UPDATE schedules SET next_run = ? WHERE id = ?",
            (next_run.isoformat(), row["id"]),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

def _run_task_sync(row: dict, cli_cmd: str, bot_token: str, chat_id_str: str) -> None:
    """Execute a scheduled task synchronously (called in a thread)."""
    import json
    import os
    import subprocess

    task_id = row["id"]
    description = row["description"]
    data = json.loads(row["recurrence"])
    recurrence_type = data.get("type", "once")  # noqa: F841

    logger.info("Running scheduled task #%s: %s", task_id, description)

    prompt = (
        f"SCHEDULED TASK #{task_id}: {description}\n\n"
        f"Complete this task autonomously. Be concise.\n"
        f"When you are done, output a one-line summary of what you did."
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    try:
        result = subprocess.run(
            [cli_cmd, "-p", prompt],
            capture_output=True, text=True, env=env,
            timeout=_DEFAULT_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("Task #%s exited %s", task_id, result.returncode)
        else:
            summary = result.stdout.strip()[-500:] or "done"
            _notify(bot_token, chat_id_str, f"✅ Task #{task_id} done: {summary}")
    except subprocess.TimeoutExpired:
        logger.error("Task #%s timed out after %ss", task_id, _DEFAULT_TIMEOUT)
        _notify(bot_token, chat_id_str,
                f"⏱ Scheduled task #{task_id} timed out ({_DEFAULT_TIMEOUT}s):\n{description}")
    except FileNotFoundError:
        logger.error("CLI command not found: %s", cli_cmd)
        _notify(bot_token, chat_id_str,
                f"❌ Scheduled task #{task_id} failed: CLI '{cli_cmd}' not found.")


def _notify(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram notify failed: %s", e)


# ---------------------------------------------------------------------------
# Async scheduler loop
# ---------------------------------------------------------------------------

_runner_ref = None   # set by init()
_bot_token: str = ""
_default_chat_id: str = ""


def init(runner, bot_token: str, default_chat_id: str) -> None:
    global _runner_ref, _bot_token, _default_chat_id
    _runner_ref = runner
    _bot_token = bot_token
    _default_chat_id = default_chat_id
    _init_db()


async def scheduler_loop() -> None:
    """Main loop — checks for due tasks every _CHECK_INTERVAL seconds."""
    logger.info("Scheduler started (interval: %ss)", _CHECK_INTERVAL)
    while True:
        try:
            due = await asyncio.to_thread(_get_due_schedules)
            for row in due:
                # Advance/disable before running so a crash doesn't double-fire
                await asyncio.to_thread(_advance_schedule, row)
                chat_id_str = str(row["chat_id"]) or _default_chat_id
                cli_cmd = (
                    getattr(_runner_ref, "cli_command", None)
                    or os.environ.get("CLI_COMMAND", "claude")
                )
                asyncio.create_task(
                    asyncio.to_thread(
                        _run_task_sync, row, cli_cmd, _bot_token, chat_id_str
                    )
                )
        except Exception:
            logger.exception("Scheduler loop error")
        await asyncio.sleep(_CHECK_INTERVAL)
