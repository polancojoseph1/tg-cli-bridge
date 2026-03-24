"""Trigger Registry — persistent storage for event-driven agent triggers.

Triggers are stored in MEMORY_DIR/agents.db (triggers table).
Agents persist independently — deleting a trigger never touches the agent.

Trigger types:
    "manual"      — fired only via /trigger run <id> or the API
    "webhook"     — fired by HTTP POST to /triggers/webhook/<id>
                    config: {"secret": "...", "event": "push", "branch": "main"}
    "file_change" — fired when a file/directory changes (requires watchdog)
                    config: {"path": "/path/to/watch", "pattern": "*.py"}

Usage:
    from trigger_registry import (
        init_db, create_trigger, get_trigger, list_triggers,
        delete_trigger, set_enabled, record_fired,
    )
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bridge.trigger_registry")

from config import MEMORY_DIR  # noqa: E402
AGENTS_DB = str(Path(MEMORY_DIR) / "agents.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    trigger_type  TEXT NOT NULL,
    config        TEXT NOT NULL DEFAULT '{}',
    task_override TEXT NOT NULL DEFAULT '',
    chat_id       INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fired    REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class TriggerDefinition:
    id: str
    agent_id: str
    trigger_type: str
    config: dict = field(default_factory=dict)
    task_override: str = ""
    chat_id: int = 0
    enabled: bool = True
    last_fired: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(AGENTS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _row_to_trigger(row: sqlite3.Row) -> TriggerDefinition:
    return TriggerDefinition(
        id=row["id"],
        agent_id=row["agent_id"],
        trigger_type=row["trigger_type"],
        config=json.loads(row["config"]),
        task_override=row["task_override"] or "",
        chat_id=row["chat_id"],
        enabled=bool(row["enabled"]),
        last_fired=row["last_fired"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def init_db() -> None:
    """Ensure triggers table exists. Call once at startup."""
    with _get_conn():
        pass
    logger.info("Trigger registry initialized")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_trigger(
    trigger_id: str,
    agent_id: str,
    trigger_type: str,
    config: dict | None = None,
    task_override: str = "",
    chat_id: int = 0,
    enabled: bool = True,
) -> TriggerDefinition:
    """Create and persist a new trigger. Raises ValueError if ID already exists."""
    now = time.time()
    t = TriggerDefinition(
        id=trigger_id,
        agent_id=agent_id,
        trigger_type=trigger_type,
        config=config or {},
        task_override=task_override,
        chat_id=chat_id,
        enabled=enabled,
        last_fired=None,
        created_at=now,
        updated_at=now,
    )
    with _get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO triggers
                   (id, agent_id, trigger_type, config, task_override, chat_id, enabled, last_fired, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    t.id, t.agent_id, t.trigger_type,
                    json.dumps(t.config), t.task_override,
                    t.chat_id, int(t.enabled), t.last_fired,
                    t.created_at, t.updated_at,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"Trigger '{trigger_id}' already exists")
    logger.info("Created trigger: %s (%s) → agent %s", trigger_id, trigger_type, agent_id)
    return t


def get_trigger(trigger_id: str) -> TriggerDefinition | None:
    """Get a trigger by ID. Returns None if not found."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
    return _row_to_trigger(row) if row else None


def list_triggers(agent_id: str | None = None) -> list[TriggerDefinition]:
    """Return all triggers, optionally filtered by agent_id."""
    with _get_conn() as conn:
        if agent_id:
            rows = conn.execute(
                "SELECT * FROM triggers WHERE agent_id = ? ORDER BY created_at", (agent_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM triggers ORDER BY created_at").fetchall()
    return [_row_to_trigger(row) for row in rows]


def delete_trigger(trigger_id: str) -> bool:
    """Delete a trigger. Agent is NOT touched. Returns True if deleted."""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    deleted = cursor.rowcount > 0
    if deleted:
        logger.info("Deleted trigger: %s", trigger_id)
    return deleted


def set_enabled(trigger_id: str, enabled: bool) -> bool:
    """Enable or disable a trigger. Returns True if found and updated."""
    now = time.time()
    with _get_conn() as conn:
        cursor = conn.execute(
            "UPDATE triggers SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now, trigger_id),
        )
    return cursor.rowcount > 0


def record_fired(trigger_id: str) -> None:
    """Update last_fired timestamp for a trigger."""
    now = time.time()
    with _get_conn() as conn:
        conn.execute(
            "UPDATE triggers SET last_fired = ?, updated_at = ? WHERE id = ?",
            (now, now, trigger_id),
        )
