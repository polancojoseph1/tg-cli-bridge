import uuid
import pytest
import agent_registry

@pytest.fixture
def memory_db(monkeypatch):
    """Fixture to provide a clean, unique in-memory database for each test."""
    db_uri = f"file:{uuid.uuid4().hex}?mode=memory&cache=shared"
    monkeypatch.setattr(agent_registry, "AGENTS_DB", db_uri)
    return db_uri

def test_get_agent_success(memory_db):
    """Test retrieving an existing agent."""
    created_agent = agent_registry.create_agent(
        agent_id="test_agent_1",
        name="Test Agent",
        agent_type="custom",
        system_prompt="You are a test agent.",
        skills=["skill1", "skill2"],
        collaborators=["other_agent"]
    )

    retrieved_agent = agent_registry.get_agent("test_agent_1")

    assert retrieved_agent is not None
    assert retrieved_agent.id == created_agent.id
    assert retrieved_agent.name == created_agent.name
    assert retrieved_agent.agent_type == created_agent.agent_type
    assert retrieved_agent.system_prompt == created_agent.system_prompt
    assert retrieved_agent.skills == created_agent.skills
    assert retrieved_agent.model == created_agent.model
    assert retrieved_agent.collaborators == created_agent.collaborators
    assert retrieved_agent.created_at == created_agent.created_at
    assert retrieved_agent.updated_at == created_agent.updated_at

def test_get_agent_not_found(memory_db):
    """Test retrieving a non-existent agent returns None."""
    retrieved_agent = agent_registry.get_agent("non_existent_agent")
    assert retrieved_agent is None
