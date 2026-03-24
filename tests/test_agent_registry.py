import os
import pytest
from unittest.mock import patch

# Set necessary environment variables before importing module
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")

import agent_registry

@pytest.fixture
def clean_registry(tmp_path):
    # Patch the db path to a temp db for isolation
    db_path = str(tmp_path / "test_agents.db")
    with patch("agent_registry.AGENTS_DB", db_path):
        # The first time _get_conn is called in each test, it will connect
        # to this new temp db and run the _SCHEMA creation automatically.
        yield db_path

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
