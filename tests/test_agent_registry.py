import sqlite3
import pytest
from unittest.mock import patch

from agent_registry import create_skill, get_skill, SkillDefinition

# Use an in-memory DB for tests
@pytest.fixture(autouse=True)
def memory_db():
    # We patch AGENTS_DB so anything directly reading it sees memory
    with patch("agent_registry.AGENTS_DB", "file::memory:?cache=shared"):
        # We patch _get_conn so every call gets a connection configured correctly
        with patch("agent_registry._get_conn") as mock_get_conn:
            # We initialize the DB once
            conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
            conn.row_factory = sqlite3.Row
            from agent_registry import _SCHEMA, _MIGRATIONS
            conn.executescript(_SCHEMA)
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

            # The mocked function will return new connections to the shared memory DB
            def get_conn():
                c = sqlite3.connect("file::memory:?cache=shared", uri=True)
                c.row_factory = sqlite3.Row
                return c

            mock_get_conn.side_effect = get_conn

            yield

            # Clean up tables between tests to ensure isolation
            conn = get_conn()
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM skills")
            conn.commit()

def test_create_skill_success():
    """Test creating a new skill successfully."""
    skill = create_skill(
        skill_id="test_skill",
        description="A test skill",
        prompt="Test prompt",
        is_builtin=False
    )

    assert isinstance(skill, SkillDefinition)
    assert skill.id == "test_skill"
    assert skill.description == "A test skill"
    assert skill.prompt == "Test prompt"
    assert not skill.is_builtin
    assert isinstance(skill.created_at, float)
    assert isinstance(skill.updated_at, float)

    # Verify it can be retrieved from DB
    fetched_skill = get_skill("test_skill")
    assert fetched_skill is not None
    assert fetched_skill.id == "test_skill"
    assert fetched_skill.description == "A test skill"
    assert fetched_skill.prompt == "Test prompt"
    assert fetched_skill.is_builtin is False

def test_create_skill_duplicate_raises_value_error():
    """Test that creating a duplicate skill raises a ValueError."""
    create_skill(
        skill_id="dup_skill",
        description="First skill"
    )

    with pytest.raises(ValueError, match="Skill 'dup_skill' already exists"):
        create_skill(
            skill_id="dup_skill",
            description="Second skill"
        )

def test_create_skill_builtin():
    """Test creating a builtin skill."""
    skill = create_skill(
        skill_id="builtin_skill",
        is_builtin=True
    )

    assert skill.is_builtin is True

    fetched = get_skill("builtin_skill")
    assert fetched is not None
    assert fetched.is_builtin is True

def test_create_skill_defaults():
    """Test creating a skill with default values."""
    skill = create_skill("minimal_skill")

    assert skill.id == "minimal_skill"
    assert skill.description == ""
    assert skill.prompt == ""
    assert skill.is_builtin is False

    fetched = get_skill("minimal_skill")
    assert fetched is not None
    assert fetched.description == ""
    assert fetched.prompt == ""
