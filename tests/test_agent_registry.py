import os
import sqlite3
import pytest

import agent_registry
from agent_registry import (
    create_agent,
    get_agent,
    AgentDefinition,
    DEFAULT_AGENT_MODEL,
)


@pytest.fixture(autouse=True)
def mock_agents_db(tmp_path, monkeypatch):
    """Override the database path to use a temporary file for testing."""
    db_path = tmp_path / "test_agents.db"
    monkeypatch.setattr(agent_registry, "AGENTS_DB", str(db_path))

    # Initialize the database schema for the test
    # By calling _get_conn(), it will create the tables automatically
    with agent_registry._get_conn() as conn:
        pass

    return str(db_path)


def test_create_agent_happy_path():
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


def test_create_agent_defaults():
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


def test_create_agent_duplicate_id_raises_value_error():
    """Test creating an agent with an ID that already exists raises a ValueError."""
    agent_id = "test_agent_3"
    name = "Test Agent 3"

    # Create the first time should succeed
    create_agent(agent_id=agent_id, name=name)

    # Creating again with the same ID should fail
    with pytest.raises(ValueError, match=f"Agent '{agent_id}' already exists"):
        create_agent(agent_id=agent_id, name="Another Name")
