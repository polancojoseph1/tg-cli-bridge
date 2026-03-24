"""Agent Registry — persistent storage for named specialist agents and skill packs.

Agents and skills are stored in MEMORY_DIR/agents.db (SQLite).

Each agent has: id, name, type, system prompt, skill list, model, collaborators.
Each skill has: id, description, prompt (injected into agent system prompts), is_builtin.

Usage:
    from agent_registry import (
        create_agent, get_agent, list_agents, update_agent, delete_agent,
        create_skill, get_skill, list_skills_db, update_skill, delete_skill,
        seed_default_agents, seed_default_skills,
    )
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("bridge.agent_registry")

from config import MEMORY_DIR, DEFAULT_AGENT_MODEL  # noqa: E402
AGENTS_DB = str(Path(MEMORY_DIR) / "agents.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    agent_type  TEXT NOT NULL DEFAULT 'custom',
    system_prompt TEXT NOT NULL DEFAULT '',
    skills      TEXT NOT NULL DEFAULT '[]',
    model       TEXT NOT NULL DEFAULT '',
    collaborators TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    proactive   INTEGER NOT NULL DEFAULT 0,
    proactive_schedule TEXT NOT NULL DEFAULT '',
    proactive_task TEXT NOT NULL DEFAULT '',
    ephemeral   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skills (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL DEFAULT '',
    is_builtin  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

# Migrations for existing DBs that predate these columns/tables
_MIGRATIONS = [
    "ALTER TABLE agents ADD COLUMN proactive INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN proactive_schedule TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN proactive_task TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN ephemeral INTEGER NOT NULL DEFAULT 0",
    # skills table added as part of _SCHEMA (CREATE TABLE IF NOT EXISTS — safe to re-run)
]


# ---------------------------------------------------------------------------
# Agent dataclass + DB helpers
# ---------------------------------------------------------------------------

@dataclass
class AgentDefinition:
    id: str
    name: str
    agent_type: str = "custom"
    system_prompt: str = ""
    skills: list[str] = field(default_factory=list)
    model: str = field(default_factory=lambda: DEFAULT_AGENT_MODEL)
    collaborators: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    proactive: bool = False
    proactive_schedule: str = ""   # HH:MM (TIMEZONE env var) — when to fire daily
    proactive_task: str = ""       # the task prompt to run automatically
    ephemeral: bool = False        # if True: instance self-destructs after task completes


@dataclass
class SkillDefinition:
    id: str
    description: str = ""
    prompt: str = ""
    is_builtin: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(AGENTS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    # Run migrations for existing DBs (fail silently if columns already exist)
    for migration in _MIGRATIONS:
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    return conn


def _row_to_agent(row: sqlite3.Row) -> AgentDefinition:
    return AgentDefinition(
        id=row["id"],
        name=row["name"],
        agent_type=row["agent_type"],
        system_prompt=row["system_prompt"],
        skills=json.loads(row["skills"]),
        model=row["model"] or DEFAULT_AGENT_MODEL,
        collaborators=json.loads(row["collaborators"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        proactive=bool(row["proactive"]),
        proactive_schedule=row["proactive_schedule"] or "",
        proactive_task=row["proactive_task"] or "",
        ephemeral=bool(row["ephemeral"]),
    )


def _row_to_skill(row: sqlite3.Row) -> SkillDefinition:
    return SkillDefinition(
        id=row["id"],
        description=row["description"],
        prompt=row["prompt"],
        is_builtin=bool(row["is_builtin"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------

def create_agent(
    agent_id: str,
    name: str,
    agent_type: str = "custom",
    system_prompt: str = "",
    skills: list[str] | None = None,
    model: str = "",
    collaborators: list[str] | None = None,
) -> AgentDefinition:
    """Create and persist a new agent. Raises ValueError if ID already exists."""
    now = time.time()
    agent = AgentDefinition(
        id=agent_id,
        name=name,
        agent_type=agent_type,
        system_prompt=system_prompt,
        skills=skills or [],
        model=model or DEFAULT_AGENT_MODEL,
        collaborators=collaborators or [],
        created_at=now,
        updated_at=now,
    )
    with _get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO agents (id, name, agent_type, system_prompt, skills, model, collaborators, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent.id, agent.name, agent.agent_type, agent.system_prompt,
                    json.dumps(agent.skills), agent.model,
                    json.dumps(agent.collaborators), agent.created_at, agent.updated_at,
                ),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"Agent '{agent_id}' already exists")
    logger.info("Created agent: %s (%s)", agent.name, agent.id)
    return agent


def get_agent(agent_id: str) -> AgentDefinition | None:
    """Get agent by ID. Returns None if not found."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    return _row_to_agent(row) if row else None


def get_agent_by_name(name: str) -> AgentDefinition | None:
    """Case-insensitive partial name match. Returns first match or None."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    name_lower = name.lower()
    for row in rows:
        if name_lower in row["name"].lower() or name_lower == row["id"].lower():
            return _row_to_agent(row)
    return None


def resolve_agent(id_or_name: str) -> AgentDefinition | None:
    """Try exact ID first, then partial name match."""
    return get_agent(id_or_name) or get_agent_by_name(id_or_name)


def list_agents() -> list[AgentDefinition]:
    """Return all agents sorted by creation time."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    return [_row_to_agent(row) for row in rows]


def update_agent(agent_id: str, **fields) -> AgentDefinition | None:
    """Update agent fields. Supported: name, agent_type, system_prompt, skills, model, collaborators.
    Returns updated agent or None if not found."""
    agent = get_agent(agent_id)
    if not agent:
        return None

    allowed = {"name", "agent_type", "system_prompt", "skills", "model", "collaborators",
               "proactive", "proactive_schedule", "proactive_task", "ephemeral"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return agent

    # Apply updates to the retrieved object
    for k, v in updates.items():
        setattr(agent, k, v)

    agent.updated_at = time.time()

    with _get_conn() as conn:
        conn.execute(
            """UPDATE agents SET
               name = ?, agent_type = ?, system_prompt = ?, skills = ?, model = ?,
               collaborators = ?, updated_at = ?, proactive = ?,
               proactive_schedule = ?, proactive_task = ?, ephemeral = ?
               WHERE id = ?""",
            (
                agent.name, agent.agent_type, agent.system_prompt,
                json.dumps(agent.skills), agent.model,
                json.dumps(agent.collaborators), agent.updated_at,
                int(agent.proactive), agent.proactive_schedule,
                agent.proactive_task, int(agent.ephemeral),
                agent_id
            )
        )

    logger.info("Updated agent %s: %s", agent_id, list(updates.keys()))
    return get_agent(agent_id)


def delete_agent(agent_id: str) -> bool:
    """Delete an agent. Returns True if deleted, False if not found."""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    deleted = cursor.rowcount > 0
    if deleted:
        logger.info("Deleted agent: %s", agent_id)
    return deleted


# ---------------------------------------------------------------------------
# Skill CRUD
# ---------------------------------------------------------------------------

def create_skill(
    skill_id: str,
    description: str = "",
    prompt: str = "",
    is_builtin: bool = False,
) -> SkillDefinition:
    """Create and persist a new skill. Raises ValueError if ID already exists."""
    now = time.time()
    skill = SkillDefinition(
        id=skill_id,
        description=description,
        prompt=prompt,
        is_builtin=is_builtin,
        created_at=now,
        updated_at=now,
    )
    with _get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO skills (id, description, prompt, is_builtin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (skill.id, skill.description, skill.prompt, int(skill.is_builtin), skill.created_at, skill.updated_at),
            )
        except sqlite3.IntegrityError:
            raise ValueError(f"Skill '{skill_id}' already exists")
    logger.info("Created skill: %s", skill_id)
    return skill


def get_skill(skill_id: str) -> SkillDefinition | None:
    """Get skill by ID. Returns None if not found."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
    return _row_to_skill(row) if row else None


def list_skills_db() -> list[SkillDefinition]:
    """Return all skills sorted by creation time."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM skills ORDER BY is_builtin DESC, created_at").fetchall()
    return [_row_to_skill(row) for row in rows]


def update_skill(skill_id: str, **fields) -> SkillDefinition | None:
    """Update skill fields. Supported: description, prompt.
    Returns updated skill or None if not found."""
    skill = get_skill(skill_id)
    if not skill:
        return None

    allowed = {"description", "prompt"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return skill

    # Apply updates to the retrieved object
    for k, v in updates.items():
        setattr(skill, k, v)

    skill.updated_at = time.time()

    with _get_conn() as conn:
        conn.execute(
            "UPDATE skills SET description = ?, prompt = ?, updated_at = ? WHERE id = ?",
            (skill.description, skill.prompt, skill.updated_at, skill_id)
        )

    logger.info("Updated skill %s: %s", skill_id, list(updates.keys()))
    return get_skill(skill_id)


def delete_skill(skill_id: str) -> tuple[bool, str]:
    """Delete a skill. Returns (True, '') on success or (False, reason) on failure.
    Built-in skills cannot be deleted."""
    skill = get_skill(skill_id)
    if not skill:
        return False, f"Skill '{skill_id}' not found."
    if skill.is_builtin:
        return False, f"'{skill_id}' is a built-in skill and cannot be deleted. Use /skill edit to modify it."
    with _get_conn() as conn:
        conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
    logger.info("Deleted skill: %s", skill_id)
    return True, ""


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_default_skills() -> int:
    """Seed built-in skill packs if they don't already exist. Returns count seeded."""
    from agent_skills import SKILL_PACKS
    seeded = 0
    for skill_id, pack in SKILL_PACKS.items():
        if get_skill(skill_id) is None:
            try:
                create_skill(
                    skill_id=skill_id,
                    description=pack["description"],
                    prompt=pack["system_prompt_section"],
                    is_builtin=True,
                )
                seeded += 1
            except ValueError:
                pass  # Already exists (race condition)
    if seeded:
        logger.info("Seeded %d default skills", seeded)
    return seeded


def seed_default_agents() -> int:
    """Seed the built-in agents if they don't already exist. Returns count seeded."""
    from agent_skills import DEFAULT_AGENT_PROMPTS

    defaults = [
        {
            "agent_id": "research",
            "name": "Research Expert",
            "agent_type": "research",
            "skills": ["research"],
            "collaborators": ["analytics"],
        },
        {
            "agent_id": "analytics",
            "name": "Analytics Expert",
            "agent_type": "analytics",
            "skills": ["analytics"],
            "collaborators": ["research"],
        },
        {
            "agent_id": "coding",
            "name": "Coding Expert",
            "agent_type": "coding",
            "skills": ["coding"],
            "collaborators": [],
        },
        {
            "agent_id": "manager",
            "name": "Manager Agent",
            "agent_type": "manager",
            "skills": ["manager"],
            "collaborators": ["research", "analytics", "coding"],
        },
    ]

    seeded = 0
    for d in defaults:
        if get_agent(d["agent_id"]) is None:
            try:
                create_agent(
                    agent_id=d["agent_id"],
                    name=d["name"],
                    agent_type=d["agent_type"],
                    system_prompt=DEFAULT_AGENT_PROMPTS.get(d["agent_id"], ""),
                    skills=d["skills"],
                    collaborators=d["collaborators"],
                )
                seeded += 1
            except ValueError:
                pass  # Already exists (race condition)

    if seeded:
        logger.info("Seeded %d default agents", seeded)
    return seeded
