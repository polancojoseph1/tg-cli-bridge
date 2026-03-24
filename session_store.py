"""Persistent session store for crash recovery.

SQLite-backed store that tracks conversation state across bot restarts.
When a bot crashes, it can query unresolved sessions and restore them.

Key concepts:
  - Each (chat_id, bot_name, instance_number) is one tracked session.
  - status='unresolved' means the bot was mid-task when it stopped.
  - status='resolved'   means the task finished cleanly (or /stop was called).
  - On startup, check the shutdown_clean flag file. If missing → crash →
    call get_all_sessions() and restore instances.
"""

import os
import sqlite3
import time
import logging
from pathlib import Path

logger = logging.getLogger("bridge.session_store")

def _default_db_path() -> str:
    data_dir = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))
    return os.path.join(data_dir, "session_store.db")

DEFAULT_DB_PATH = _default_db_path()


class SessionStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id          TEXT NOT NULL,
                    bot_name         TEXT NOT NULL,
                    instance_number  INTEGER NOT NULL,
                    title            TEXT DEFAULT 'Instance',
                    session_id       TEXT,
                    status           TEXT DEFAULT 'resolved',
                    original_prompt  TEXT,
                    summary          TEXT,
                    summary_updated_at INTEGER,
                    created_at       INTEGER NOT NULL,
                    updated_at       INTEGER NOT NULL,
                    PRIMARY KEY (chat_id, bot_name, instance_number)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id         TEXT NOT NULL,
                    bot_name        TEXT NOT NULL,
                    instance_number INTEGER NOT NULL,
                    role            TEXT NOT NULL,
                    content         TEXT NOT NULL,
                    created_at      INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_lookup
                ON messages(chat_id, bot_name, instance_number, created_at);

            """)
            # Subprocess survival tracking (added in v2)
            for col_sql in [
                "ALTER TABLE sessions ADD COLUMN subprocess_pid INTEGER",
                "ALTER TABLE sessions ADD COLUMN subprocess_log_file TEXT",
                "ALTER TABLE sessions ADD COLUMN subprocess_log_offset INTEGER DEFAULT 0",
                "ALTER TABLE sessions ADD COLUMN subprocess_start_time TEXT",
            ]:
                try:
                    conn.execute(col_sql)
                except sqlite3.OperationalError:
                    pass  # column already exists

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------

    def upsert_session(
        self,
        chat_id: int,
        bot_name: str,
        instance_number: int,
        *,
        title: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        original_prompt: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE chat_id=? AND bot_name=? AND instance_number=?",
                (str(chat_id), bot_name, instance_number),
            ).fetchone()

            if existing:
                cols, params = ["updated_at=?"], [now]
                if title is not None:
                    cols.append("title=?")
                    params.append(title)
                if session_id is not None:
                    cols.append("session_id=?")
                    params.append(session_id)
                if status is not None:
                    cols.append("status=?")
                    params.append(status)
                if original_prompt is not None:
                    cols.append("original_prompt=?")
                    params.append(original_prompt[:1000])
                _ALLOWED_SESSION_COLS = {
                    "updated_at=?", "title=?", "session_id=?", "status=?", "original_prompt=?",
                }
                assert all(c in _ALLOWED_SESSION_COLS for c in cols), \
                    f"Unexpected SQL column clause: {[c for c in cols if c not in _ALLOWED_SESSION_COLS]}"
                params += [str(chat_id), bot_name, instance_number]
                conn.execute(
                    f"UPDATE sessions SET {', '.join(cols)} "
                    f"WHERE chat_id=? AND bot_name=? AND instance_number=?",
                    params,
                )
            else:
                conn.execute(
                    """INSERT INTO sessions
                       (chat_id, bot_name, instance_number, title, session_id,
                        status, original_prompt, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(chat_id), bot_name, instance_number,
                        title or "Instance",
                        session_id,
                        status or "resolved",
                        original_prompt[:1000] if original_prompt else None,
                        now, now,
                    ),
                )

    def mark_unresolved(
        self,
        chat_id: int,
        bot_name: str,
        instance_number: int,
        original_prompt: str,
        session_id: str | None = None,
        title: str | None = None,
    ) -> None:
        """Mark this instance as actively processing a task."""
        self.upsert_session(
            chat_id, bot_name, instance_number,
            title=title,
            session_id=session_id,
            status="unresolved",
            original_prompt=original_prompt,
        )

    def mark_resolved(self, chat_id: int, bot_name: str, instance_number: int) -> None:
        """Mark this instance as done (task finished or /stop called)."""
        self.upsert_session(chat_id, bot_name, instance_number, status="resolved")

    def has_unresolved(self, bot_name: str) -> bool:
        """Return True if any session for this bot is still unresolved."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sessions WHERE bot_name=? AND status='unresolved' LIMIT 1",
                (bot_name,),
            ).fetchone()
        return row is not None

    def update_session_id(
        self, chat_id: int, bot_name: str, instance_number: int, session_id: str
    ) -> None:
        self.upsert_session(chat_id, bot_name, instance_number, session_id=session_id)

    def update_summary(
        self, chat_id: int, bot_name: str, instance_number: int, summary: str
    ) -> None:
        """Store a compressed progress summary for this instance."""
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """UPDATE sessions SET summary=?, summary_updated_at=?, updated_at=?
                   WHERE chat_id=? AND bot_name=? AND instance_number=?""",
                (summary, now, now, str(chat_id), bot_name, instance_number),
            )

    # -------------------------------------------------------------------------
    # Message logging
    # -------------------------------------------------------------------------

    def log_message(
        self,
        chat_id: int,
        bot_name: str,
        instance_number: int,
        role: str,
        content: str,
    ) -> None:
        """Append a conversation turn to the message log."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO messages
                   (chat_id, bot_name, instance_number, role, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(chat_id), bot_name, instance_number, role, content[:4000], int(time.time())),
            )

    def get_recent_messages(
        self,
        chat_id: int,
        bot_name: str,
        instance_number: int,
        limit: int = 20,
    ) -> list[dict]:
        """Return the most recent messages in chronological order."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT role, content, created_at FROM messages
                   WHERE chat_id=? AND bot_name=? AND instance_number=?
                   ORDER BY created_at DESC LIMIT ?""",
                (str(chat_id), bot_name, instance_number, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_message_count(
        self, chat_id: int, bot_name: str, instance_number: int
    ) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id=? AND bot_name=? AND instance_number=?",
                (str(chat_id), bot_name, instance_number),
            ).fetchone()[0]

    # -------------------------------------------------------------------------
    # Query
    # -------------------------------------------------------------------------

    def get_all_sessions(self, bot_name: str) -> list[dict]:
        """All sessions for this bot, ordered by chat_id then instance_number."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM sessions WHERE bot_name=?
                   ORDER BY chat_id, instance_number ASC""",
                (bot_name,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # Recovery context builder
    # -------------------------------------------------------------------------

    def build_recovery_context(
        self, chat_id: int, bot_name: str, instance_number: int
    ) -> str:
        """Build a context string to inject when resuming after a crash."""
        with self._conn() as conn:
            session = conn.execute(
                "SELECT * FROM sessions WHERE chat_id=? AND bot_name=? AND instance_number=?",
                (str(chat_id), bot_name, instance_number),
            ).fetchone()

        if not session:
            return ""

        session = dict(session)
        parts = []

        if session.get("summary"):
            parts.append(f"=== Progress Summary ===\n{session['summary']}")

        messages = self.get_recent_messages(chat_id, bot_name, instance_number, limit=20)
        if messages:
            history_lines = []
            for m in messages:
                role_label = "User" if m["role"] == "user" else "You"
                history_lines.append(f"{role_label}: {m['content'][:500]}")
            parts.append("=== Recent Conversation ===\n" + "\n".join(history_lines))

        if session.get("original_prompt"):
            parts.append(
                f"=== Resume Task ===\n"
                f"You were working on: {session['original_prompt']}\n"
                f"Please continue from where you left off."
            )

        return "\n\n".join(parts)

    # -------------------------------------------------------------------------
    # Subprocess survival tracking
    # -------------------------------------------------------------------------

    def set_subprocess(
        self, chat_id: int, bot_name: str, instance_number: int,
        pid: int, log_file: str, start_time: str,
    ) -> None:
        """Record the detached subprocess PID and log file path."""
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """UPDATE sessions SET subprocess_pid=?, subprocess_log_file=?,
                   subprocess_start_time=?, subprocess_log_offset=0, updated_at=?
                   WHERE chat_id=? AND bot_name=? AND instance_number=?""",
                (pid, log_file, start_time, now, str(chat_id), bot_name, instance_number),
            )

    def update_log_offset(
        self, chat_id: int, bot_name: str, instance_number: int, offset: int
    ) -> None:
        """Update the byte offset of how much of the log file has been processed."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE sessions SET subprocess_log_offset=?
                   WHERE chat_id=? AND bot_name=? AND instance_number=?""",
                (offset, str(chat_id), bot_name, instance_number),
            )

    def get_subprocess_info(
        self, chat_id: int, bot_name: str, instance_number: int
    ) -> dict | None:
        """Return subprocess tracking info, or None if not set."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT subprocess_pid, subprocess_log_file, subprocess_log_offset,
                          subprocess_start_time
                   FROM sessions WHERE chat_id=? AND bot_name=? AND instance_number=?""",
                (str(chat_id), bot_name, instance_number),
            ).fetchone()
        if not row or not row["subprocess_pid"]:
            return None
        return dict(row)

    def clear_subprocess(
        self, chat_id: int, bot_name: str, instance_number: int
    ) -> None:
        """Clear subprocess tracking after clean completion."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE sessions SET subprocess_pid=NULL, subprocess_log_file=NULL,
                   subprocess_log_offset=0, subprocess_start_time=NULL
                   WHERE chat_id=? AND bot_name=? AND instance_number=?""",
                (str(chat_id), bot_name, instance_number),
            )

    # -------------------------------------------------------------------------
    # Maintenance
    # -------------------------------------------------------------------------

    def delete_session(self, chat_id: int, bot_name: str, instance_number: int) -> None:
        """Permanently delete a session and its messages from the store."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM messages WHERE chat_id=? AND bot_name=? AND instance_number=?",
                (str(chat_id), bot_name, instance_number),
            )
            conn.execute(
                "DELETE FROM sessions WHERE chat_id=? AND bot_name=? AND instance_number=?",
                (str(chat_id), bot_name, instance_number),
            )

    def prune_old_messages(self, grace_seconds: int = 86400) -> int:
        """Delete messages for sessions resolved more than grace_seconds ago."""
        cutoff = int(time.time()) - grace_seconds
        with self._conn() as conn:
            deleted = conn.execute(
                """DELETE FROM messages
                   WHERE rowid IN (
                       SELECT m.rowid FROM messages m
                       JOIN sessions s
                         ON  m.chat_id         = s.chat_id
                         AND m.bot_name        = s.bot_name
                         AND m.instance_number = s.instance_number
                       WHERE s.status = 'resolved'
                         AND s.updated_at < ?
                   )""",
                (cutoff,),
            ).rowcount
        if deleted:
            logger.info("Pruned %d old messages from session store", deleted)
        return deleted
