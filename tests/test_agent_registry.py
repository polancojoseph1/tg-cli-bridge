import os
import uuid
import pytest

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")
os.environ.setdefault("ENV_FILE", "/dev/null")

import agent_registry

@pytest.fixture
def mock_db(monkeypatch):
    """Fixture to provide a unique in-memory database per test."""
    db_uri = f"file:{uuid.uuid4().hex}?mode=memory&cache=shared"
    monkeypatch.setattr(agent_registry, "AGENTS_DB", db_uri)
    return db_uri

def test_update_agent_success(mock_db):
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

def test_update_agent_lists(mock_db):
    """Test successful update of list fields (skills, collaborators)."""
    agent = agent_registry.create_agent(
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

def test_update_agent_not_found(mock_db):
    """Test updating an agent that doesn't exist returns None."""
    result = agent_registry.update_agent("nonexistent_agent", name="New Name")
    assert result is None

def test_update_agent_ignores_unallowed_fields(mock_db):
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

def test_update_agent_no_fields(mock_db):
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
