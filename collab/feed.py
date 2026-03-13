"""Rolling activity feed for collab events.

Stores the last 50 events in TG_BRIDGE_DATA_DIR/collab_feed.json.
Thread-safe via asyncio.Lock.

Event schema:
    {
        "id": str,
        "timestamp": float,
        "instance_name": str,
        "bot": str,
        "action": str,
        "summary": str,
        "peer_name": str | None,
    }
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger("bridge.collab.feed")

_FEED_MAX = 50
_DATA_DIR = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))
_FEED_FILE = os.path.join(_DATA_DIR, "collab_feed.json")

_lock = asyncio.Lock()


def _ensure_data_dir() -> None:
    Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)


def _load_feed_sync() -> list[dict]:
    """Load feed from disk (sync, call inside lock only)."""
    if not os.path.exists(_FEED_FILE):
        return []
    try:
        with open(_FEED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Failed to load collab feed from %s: %s", _FEED_FILE, e)
        return []


def _save_feed_sync(events: list[dict]) -> None:
    """Save feed to disk (sync, call inside lock only)."""
    _ensure_data_dir()
    try:
        with open(_FEED_FILE, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
    except Exception as e:
        logger.error("Failed to save collab feed to %s: %s", _FEED_FILE, e)


async def append_event(
    bot: str,
    action: str,
    summary: str,
    peer_name: str | None = None,
) -> dict:
    """Append an event to the rolling feed. Trims to last _FEED_MAX entries.

    Returns the new event dict.
    """
    from .config import COLLAB_INSTANCE_NAME

    event: dict = {
        "id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "instance_name": COLLAB_INSTANCE_NAME,
        "bot": bot,
        "action": action,
        "summary": summary,
        "peer_name": peer_name,
    }

    async with _lock:
        events = _load_feed_sync()
        events.append(event)
        # Keep only the last _FEED_MAX events
        if len(events) > _FEED_MAX:
            events = events[-_FEED_MAX:]
        _save_feed_sync(events)

    logger.debug("Feed: %s/%s — %s", action, bot, summary[:80])
    return event


async def get_feed(limit: int = 20) -> list[dict]:
    """Return the most recent events, newest last. Capped at _FEED_MAX."""
    async with _lock:
        events = _load_feed_sync()
    limit = min(limit, _FEED_MAX)
    return events[-limit:] if len(events) > limit else events


async def clear_feed() -> None:
    """Empty the feed."""
    async with _lock:
        _save_feed_sync([])
    logger.info("Collab feed cleared")
