"""User access management — dynamic allowed-user list with owner approval flow.

Extends the static ALLOWED_USER_IDS env var with a SQLite-backed approved users
table. New users message the bot, the owner gets an inline Approve/Deny button,
and approved users are persisted so they survive restarts.

Public API:
    is_allowed(user_id)              — True if user is permitted
    request_access(user_id, name, username, chat_id)  — record a pending request
    approve_user(user_id)            — approve and persist
    deny_user(user_id)               — deny and remove from pending
    is_pending(user_id)              — True if request already sent
    list_approved()                  — all dynamically approved users
"""

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("bridge.user_access")

from config import MEMORY_DIR, ALLOWED_USER_IDS  # noqa: E402

_DB_PATH = str(Path(MEMORY_DIR) / "user_access.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approved_users (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    approved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_requests (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    chat_id     INTEGER NOT NULL,
    requested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS denied_users (
    user_id     INTEGER PRIMARY KEY,
    denied_at   REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def is_allowed(user_id: int) -> bool:
    """Return True if user_id is in the static env list OR dynamically approved."""
    if user_id in ALLOWED_USER_IDS:
        return True
    with _conn() as c:
        row = c.execute("SELECT 1 FROM approved_users WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def is_denied(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute("SELECT 1 FROM denied_users WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def is_pending(user_id: int) -> bool:
    with _conn() as c:
        row = c.execute("SELECT 1 FROM pending_requests WHERE user_id=?", (user_id,)).fetchone()
    return row is not None


def request_access(user_id: int, first_name: str, username: str, chat_id: int) -> None:
    """Record a pending access request."""
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO pending_requests
               (user_id, first_name, username, chat_id, requested_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, first_name or "", username or "", chat_id, time.time()),
        )
    logger.info("Access request from user_id=%d (%s @%s)", user_id, first_name, username)


def approve_user(user_id: int) -> str | None:
    """Approve a pending user. Returns their display name or None if not found."""
    with _conn() as c:
        row = c.execute(
            "SELECT first_name, username FROM pending_requests WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            # Check if already approved
            already = c.execute("SELECT 1 FROM approved_users WHERE user_id=?", (user_id,)).fetchone()
            return None if not already else "already approved"
        first_name = row["first_name"]
        username = row["username"]
        c.execute(
            "INSERT OR REPLACE INTO approved_users (user_id, first_name, username, approved_at) VALUES (?, ?, ?, ?)",
            (user_id, first_name, username, time.time()),
        )
        c.execute("DELETE FROM pending_requests WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM denied_users WHERE user_id=?", (user_id,))
    logger.info("Approved user_id=%d (%s)", user_id, first_name)
    return first_name or str(user_id)


def deny_user(user_id: int) -> str | None:
    """Deny a pending user. Returns their display name or None if not found."""
    with _conn() as c:
        row = c.execute(
            "SELECT first_name FROM pending_requests WHERE user_id=?", (user_id,)
        ).fetchone()
        first_name = row["first_name"] if row else str(user_id)
        c.execute("DELETE FROM pending_requests WHERE user_id=?", (user_id,))
        c.execute(
            "INSERT OR REPLACE INTO denied_users (user_id, denied_at) VALUES (?, ?)",
            (user_id, time.time()),
        )
    logger.info("Denied user_id=%d", user_id)
    return first_name


def list_approved() -> list[dict]:
    """Return all dynamically approved users."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM approved_users ORDER BY approved_at").fetchall()
    return [dict(r) for r in rows]


def get_pending_chat_id(user_id: int) -> int | None:
    """Return the chat_id for a pending request, or None if not found."""
    with _conn() as c:
        row = c.execute("SELECT chat_id FROM pending_requests WHERE user_id=?", (user_id,)).fetchone()
    return row["chat_id"] if row else None


def revoke_user(user_id: int) -> bool:
    """Remove a dynamically approved user. Cannot revoke env-var users."""
    if user_id in ALLOWED_USER_IDS:
        return False  # can't revoke static users
    with _conn() as c:
        cursor = c.execute("DELETE FROM approved_users WHERE user_id=?", (user_id,))
    return cursor.rowcount > 0
