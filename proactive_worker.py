"""Proactive agent worker — fires scheduled Claude agents automatically.

Schedule formats (stored in proactive_schedule):
    "09:00"       → daily at 09:00 local time (TIMEZONE env var)
    "every 2h"    → every 2 hours from when the worker started
    "every 30m"   → every 30 minutes
    "every 1h30m" → every 1.5 hours

Each proactive agent fires independently based on its own schedule.
The worker does NOT auto-start on server boot — use /agent proactive start.

Public API:
    start(instance_manager, send_fn, chat_id)  — start background loop
    stop()                                      — stop background loop
    is_running()                                — bool
    status()                                    — human-readable summary
    parse_schedule(s)                           — returns (mode, value) or raises ValueError
"""

import asyncio
import logging
import os
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from agent_registry import list_agents

logger = logging.getLogger("bridge.proactive_worker")

LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
CHECK_INTERVAL = 30  # seconds — check every 30s for precision

# ── State ─────────────────────────────────────────────────────────────────────
_running: bool = False
_worker_task: asyncio.Task | None = None
_last_fired: dict[str, datetime] = {}   # agent_id -> last datetime fired
_instance_manager = None
_send_fn = None
_chat_id: int = 0


# ── Schedule parsing ───────────────────────────────────────────────────────────

def parse_schedule(s: str) -> tuple[str, object]:
    """Parse a schedule string. Returns (mode, value).

    Modes:
        ("daily", "HH:MM")               — fire once per day at this time
        ("interval", timedelta)           — fire every N hours/minutes

    Raises ValueError with a helpful message on bad input.
    """
    s = s.strip().lower()

    # HH:MM daily format
    if re.match(r"^\d{1,2}:\d{2}$", s):
        h, m = s.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError(f"Invalid time '{s}' — hours must be 0-23, minutes 0-59")
        return ("daily", f"{int(h):02d}:{int(m):02d}")

    # "every Xh", "every Xm", "every XhYm"
    m_full = re.match(r"^every\s+(?:(\d+)h)?(?:(\d+)m)?$", s)
    if m_full:
        hours = int(m_full.group(1) or 0)
        mins = int(m_full.group(2) or 0)
        total_mins = hours * 60 + mins
        if total_mins < 1:
            raise ValueError("Interval must be at least 1 minute")
        return ("interval", timedelta(minutes=total_mins))

    raise ValueError(
        f"Unknown schedule format '{s}'.\n"
        "Use: '09:00' (daily), 'every 2h', 'every 30m', 'every 1h30m'"
    )


def schedule_label(s: str) -> str:
    """Human-readable label for a schedule string."""
    try:
        mode, val = parse_schedule(s)
        if mode == "daily":
            return f"daily at {val} {os.environ.get('TIMEZONE', 'UTC')}"
        td: timedelta = val
        total_mins = int(td.total_seconds() // 60)
        h, m = divmod(total_mins, 60)
        if h and m:
            return f"every {h}h {m}m"
        elif h:
            return f"every {h}h"
        else:
            return f"every {m}m"
    except ValueError:
        return s or "⚠ not set"


def _should_fire(agent_id: str, schedule: str, now: datetime) -> bool:
    """Return True if this agent should fire right now."""
    try:
        mode, val = parse_schedule(schedule)
    except ValueError:
        return False

    last = _last_fired.get(agent_id)

    if mode == "daily":
        today = now.date()
        current_hhmm = now.strftime("%H:%M")
        if current_hhmm != val:
            return False
        # Only fire once per calendar day
        return last is None or last.astimezone(LOCAL_TZ).date() < today

    elif mode == "interval":
        td: timedelta = val
        if last is None:
            return True  # first run
        return (now - last) >= td

    return False


# ── Public API ─────────────────────────────────────────────────────────────────

async def start(instance_manager, send_fn, chat_id: int) -> None:
    """Start the background proactive worker loop."""
    global _running, _worker_task, _instance_manager, _send_fn, _chat_id
    if _running:
        return
    _instance_manager = instance_manager
    _send_fn = send_fn
    _chat_id = chat_id
    _running = True
    _worker_task = asyncio.ensure_future(_loop())
    logger.info("Proactive worker started")


async def stop() -> None:
    """Stop the background loop."""
    global _running, _worker_task
    _running = False
    if _worker_task:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
        _worker_task = None
    logger.info("Proactive worker stopped")


def is_running() -> bool:
    return _running


def status() -> str:
    """Return a human-readable summary of all proactive agents."""
    agents = [a for a in list_agents() if a.proactive]
    if not agents:
        return (
            "No proactive agents configured.\n\n"
            "To create one:\n"
            "<code>/agent create proactive My Agent</code>\n"
            "Then set its schedule:\n"
            "<code>/agent proactive My Agent set 09:00 summarize AI news</code>\n"
            "<code>/agent proactive My Agent set every 2h check for new jobs</code>"
        )

    lines = [f"<b>Proactive Agents ({len(agents)}):</b>"]
    for a in agents:
        sched_label = schedule_label(a.proactive_schedule)
        task_preview = (a.proactive_task[:80] + "…") if len(a.proactive_task) > 80 else (a.proactive_task or "⚠ no task set")
        last = _last_fired.get(a.id)
        if last:
            last_str = last.astimezone(LOCAL_TZ).strftime("last ran %b %-d at %-I:%M %p")
        else:
            last_str = "never run yet"
        lines.append(
            f"\n🤖 <b>{a.name}</b> [PROACTIVE]\n"
            f"  Schedule: <code>{sched_label}</code>\n"
            f"  Task: <i>{task_preview}</i>\n"
            f"  Status: {last_str}"
        )
    return "\n".join(lines)


# ── Internal loop ──────────────────────────────────────────────────────────────

async def _loop() -> None:
    global _running
    while _running:
        try:
            await _check_and_fire()
        except Exception as e:
            logger.error("Proactive worker loop error: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)


async def _check_and_fire() -> None:
    """Check all proactive agents and fire any that are due."""
    now = datetime.now(tz=LOCAL_TZ)

    for agent in list_agents():
        if not agent.proactive:
            continue
        if not agent.proactive_schedule or not agent.proactive_task:
            continue
        if not _should_fire(agent.id, agent.proactive_schedule, now):
            continue

        _last_fired[agent.id] = now
        sched_label_str = schedule_label(agent.proactive_schedule)
        logger.info("Proactive: firing '%s' (%s) — %s", agent.id, sched_label_str, agent.proactive_task[:80])

        await _send_fn(
            _chat_id,
            f"🤖 <b>{agent.name}</b> [PROACTIVE] — starting now\n"
            f"Schedule: <code>{sched_label_str}</code>\n"
            f"Task: <i>{agent.proactive_task[:150]}</i>",
            parse_mode="HTML",
        )

        asyncio.ensure_future(_fire_agent(agent.id, agent.name, agent.proactive_task))


async def _fire_agent(agent_id: str, agent_name: str, task: str) -> None:
    """Run the agent task and notify the user when done."""
    from agent_manager import assign_task
    import time

    started = time.time()
    try:
        await assign_task(agent_id, task, _chat_id, _instance_manager, _send_fn)
        elapsed = round(time.time() - started)
        await _send_fn(
            _chat_id,
            f"✅ <b>{agent_name}</b> [PROACTIVE] finished in {elapsed}s.",
            parse_mode="HTML",
        )
    except Exception as e:
        elapsed = round(time.time() - started)
        logger.error("Proactive fire failed for agent '%s': %s", agent_id, e)
        await _send_fn(
            _chat_id,
            f"❌ <b>{agent_name}</b> [PROACTIVE] failed after {elapsed}s: {e}",
            parse_mode="HTML",
        )
