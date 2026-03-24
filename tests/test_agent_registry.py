import os
import pytest
from unittest.mock import patch

# Set necessary environment variables before importing module
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")

import agent_registry
from agent_registry import (
    create_agent,
    get_agent,
    create_skill,
    get_skill,
    SkillDefinition,
    AgentDefinition,
    DEFAULT_AGENT_MODEL,
)


@pytest.fixture
def clean_registry(tmp_path):
    # Patch the db path to a temp db for isolation
    db_path = str(tmp_path / "test_agents.db")
    with patch("agent_registry.AGENTS_DB", db_path):
        # The first time _get_conn is called in each test, it will connect
        # to this new temp db and run the _SCHEMA creation automatically.
        yield db_path


# --- resolve_agent tests ---

def test_resolve_agent_exact_id(clean_registry):
    agent_registry.create_agent(agent_id="test_agent_1", name="Alpha Bot")
    agent_registry.create_agent(agent_id="test_agent_2", name="Beta Bot")

    resolved = agent_registry.resolve_agent("test_agent_1")
    assert resolved is not None
    assert resolved.id == "test_agent_1"
    assert resolved.name == "Alpha Bot"

def test_resolve_agent_exact_name(clean_registry):
    agent_registry.create_agent(agent_id="test_agent_1", name="Alpha Bot")
    agent_registry.create_agent(agent_id="test_agent_2", name="Beta Bot")

    resolved = agent_registry.resolve_agent("Alpha Bot")
    assert resolved is not None
    assert resolved.id == "test_agent_1"
    assert resolved.name == "Alpha Bot"

def test_resolve_agent_partial_name(clean_registry):
    agent_registry.create_agent(agent_id="test_agent_1", name="Alpha Bot")
    agent_registry.create_agent(agent_id="test_agent_2", name="Beta Bot")

    resolved = agent_registry.resolve_agent("lph")
    assert resolved is not None
    assert resolved.id == "test_agent_1"

def test_resolve_agent_case_insensitive_name(clean_registry):
    agent_registry.create_agent(agent_id="test_agent_1", name="Alpha Bot")

    resolved = agent_registry.resolve_agent("ALPHA BOT")
    assert resolved is not None
    assert resolved.id == "test_agent_1"

def test_resolve_agent_not_found(clean_registry):
    agent_registry.create_agent(agent_id="test_agent_1", name="Alpha Bot")

    resolved = agent_registry.resolve_agent("unknown")
    assert resolved is None

def test_resolve_agent_id_preferred_over_name(clean_registry):
    # Create one agent with id "alpha"
    agent_registry.create_agent(agent_id="alpha", name="Beta Bot")
    # Create another agent with name "alpha"
    agent_registry.create_agent(agent_id="beta", name="Alpha Bot")

    # Resolving "alpha" should return the one with id="alpha" (exact ID matches first)
    resolved = agent_registry.resolve_agent("alpha")
    assert resolved is not None
    assert resolved.id == "alpha"
    assert resolved.name == "Beta Bot"

def test_resolve_agent_case_insensitive_id(clean_registry):
    # Try creating an agent with an uppercase ID to see if `get_agent_by_name` catches it
    # note: `get_agent` does exact matching, but `get_agent_by_name` also searches over row["id"].lower()
    agent_registry.create_agent(agent_id="TEST_ID", name="Gamma Bot")

    # Exact ID won't match "test_id", but partial name match should catch the lowercased ID
    resolved = agent_registry.resolve_agent("test_id")
    assert resolved is not None
    assert resolved.id == "TEST_ID"


# --- create_agent tests ---

def test_create_agent_happy_path(clean_registry):
    """Test creating an agent with all fields provided."""
    agent_id = "test_agent_1"
    name = "Test Agent 1"
    agent_type = "research"
    system_prompt = "You are a test researcher."
    skills = ["skill_1", "skill_2"]
    model = "test-model-v1"
    collaborators = ["collab_1"]

    agent = create_agent(
        agent_id=agent_id,
        name=name,
        agent_type=agent_type,
        system_prompt=system_prompt,
        skills=skills,
        model=model,
        collaborators=collaborators,
    )

    assert isinstance(agent, AgentDefinition)
    assert agent.id == agent_id
    assert agent.name == name
    assert agent.agent_type == agent_type
    assert agent.system_prompt == system_prompt
    assert agent.skills == skills
    assert agent.model == model
    assert agent.collaborators == collaborators
    assert agent.created_at > 0
    assert agent.updated_at == agent.created_at

    # Verify it was persisted to the database
    db_agent = get_agent(agent_id)
    assert db_agent is not None
    assert db_agent.id == agent_id
    assert db_agent.name == name
    assert db_agent.agent_type == agent_type
    assert db_agent.system_prompt == system_prompt
    assert db_agent.skills == skills
    assert db_agent.model == model
    assert db_agent.collaborators == collaborators


def test_create_agent_defaults(clean_registry):
    """Test creating an agent with minimal fields to check defaults."""
    agent_id = "test_agent_2"
    name = "Test Agent 2"

    agent = create_agent(
        agent_id=agent_id,
        name=name,
    )

    assert agent.id == agent_id
    assert agent.name == name
    assert agent.agent_type == "custom"
    assert agent.system_prompt == ""
    assert agent.skills == []
    assert agent.model == DEFAULT_AGENT_MODEL
    assert agent.collaborators == []

    # Verify it was persisted to the database
    db_agent = get_agent(agent_id)
    assert db_agent is not None
    assert db_agent.id == agent_id
    assert db_agent.name == name
    assert db_agent.agent_type == "custom"
    assert db_agent.system_prompt == ""
    assert db_agent.skills == []
    assert db_agent.model == DEFAULT_AGENT_MODEL
    assert db_agent.collaborators == []


def test_create_agent_duplicate_id_raises_value_error(clean_registry):
    """Test creating an agent with an ID that already exists raises a ValueError."""
    agent_id = "test_agent_3"
    name = "Test Agent 3"

    # Create the first time should succeed
    create_agent(agent_id=agent_id, name=name)

    # Creating again with the same ID should fail
    with pytest.raises(ValueError, match=f"Agent '{agent_id}' already exists"):
        create_agent(agent_id=agent_id, name="Another Name")


# --- delete_agent tests ---

def test_delete_agent(clean_registry):
    """Test deleting an agent successfully and failing when agent does not exist."""
    agent_id = "test_agent_del"
    agent_registry.create_agent(
        agent_id=agent_id,
        name="Test Agent",
        agent_type="custom",
        system_prompt="You are a test agent."
    )
    assert agent_registry.get_agent(agent_id) is not None

    # Delete the agent
    result = agent_registry.delete_agent(agent_id)
    assert result is True
    assert agent_registry.get_agent(agent_id) is None

    # Delete the same agent again should return False
    result_second = agent_registry.delete_agent(agent_id)
    assert result_second is False

    # Delete a completely non-existent agent
    assert agent_registry.delete_agent("non_existent_agent") is False


# --- update_agent tests ---

def test_update_agent_success(clean_registry):
    """Test successful update of allowed fields."""
    agent = agent_registry.create_agent(
        agent_id="agent1",
        name="Test Agent",
        agent_type="custom",
        system_prompt="Initial prompt",
        skills=[],
        model="gpt-3.5-turbo",
        collaborators=[]
    )

    initial_updated_at = agent.updated_at

    updated_agent = agent_registry.update_agent(
        "agent1",
        name="Updated Agent",
        agent_type="research",
        system_prompt="New prompt",
        model="gpt-4",
        proactive=True,
        proactive_schedule="12:00",
        proactive_task="Daily summary",
        ephemeral=True
    )

    assert updated_agent is not None
    assert updated_agent.id == "agent1"
    assert updated_agent.name == "Updated Agent"
    assert updated_agent.agent_type == "research"
    assert updated_agent.system_prompt == "New prompt"
    assert updated_agent.model == "gpt-4"
    assert updated_agent.proactive is True
    assert updated_agent.proactive_schedule == "12:00"
    assert updated_agent.proactive_task == "Daily summary"
    assert updated_agent.ephemeral is True
    assert updated_agent.updated_at > initial_updated_at

def test_update_agent_lists(clean_registry):
    """Test successful update of list fields (skills, collaborators)."""
    agent_registry.create_agent(
        agent_id="agent2",
        name="List Agent",
        skills=["skill1"],
        collaborators=["collab1"]
    )

    updated_agent = agent_registry.update_agent(
        "agent2",
        skills=["skill1", "skill2"],
        collaborators=["collab1", "collab2", "collab3"]
    )

    assert updated_agent is not None
    assert updated_agent.skills == ["skill1", "skill2"]
    assert updated_agent.collaborators == ["collab1", "collab2", "collab3"]

def test_update_agent_not_found(clean_registry):
    """Test updating an agent that doesn't exist returns None."""
    result = agent_registry.update_agent("nonexistent_agent", name="New Name")
    assert result is None

def test_update_agent_ignores_unallowed_fields(clean_registry):
    """Test updating an agent with fields not in allowed list returns agent unchanged."""
    agent = agent_registry.create_agent(
        agent_id="agent3",
        name="Unallowed Agent",
    )

    initial_updated_at = agent.updated_at

    updated_agent = agent_registry.update_agent(
        "agent3",
        id="new_id",
        created_at=12345.0,
        unallowed_field="some_value"
    )

    assert updated_agent is not None
    assert updated_agent.id == "agent3"
    assert updated_agent.updated_at == initial_updated_at

def test_update_agent_no_fields(clean_registry):
    """Test updating an agent with no fields returns agent unchanged."""
    agent = agent_registry.create_agent(
        agent_id="agent4",
        name="No Fields Agent",
    )

    initial_updated_at = agent.updated_at

    updated_agent = agent_registry.update_agent("agent4")

    assert updated_agent is not None
    assert updated_agent.name == "No Fields Agent"
    assert updated_agent.updated_at == initial_updated_at


# --- create_skill tests ---

def test_create_skill_success(clean_registry):
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

    fetched_skill = get_skill("test_skill")
    assert fetched_skill is not None
    assert fetched_skill.id == "test_skill"

def test_create_skill_duplicate_raises_value_error(clean_registry):
    """Test that creating a duplicate skill raises a ValueError."""
    create_skill(skill_id="dup_skill", description="First skill")

    with pytest.raises(ValueError, match="Skill 'dup_skill' already exists"):
        create_skill(skill_id="dup_skill", description="Second skill")

def test_create_skill_builtin(clean_registry):
    """Test creating a builtin skill."""
    skill = create_skill(skill_id="builtin_skill", is_builtin=True)
    assert skill.is_builtin is True

    fetched = get_skill("builtin_skill")
    assert fetched is not None
    assert fetched.is_builtin is True

def test_create_skill_defaults(clean_registry):
    """Test creating a skill with default values."""
    skill = create_skill("minimal_skill")

    assert skill.id == "minimal_skill"
    assert skill.description == ""
    assert skill.prompt == ""
    assert skill.is_builtin is False
