"""Tests for agent_manager.py"""
import os
import pytest

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")
os.environ.setdefault("ENV_FILE", "/dev/null")

from agent_registry import AgentDefinition
from instance_manager import InstanceManager, Instance
from agent_manager import parse_pipeline_command

import agent_manager


def test_spawn_agent_returns_none_if_agent_not_found(monkeypatch):
    monkeypatch.setattr(agent_manager, "get_agent", lambda agent_id: None)

    instances = InstanceManager()
    result = agent_manager.spawn_agent("unknown_agent", instances)

    assert result is None
    # We expect 1 instance because InstanceManager creates a 'Default' instance on init
    assert len(instances.list_all()) == 1

def test_spawn_agent_creates_instance_successfully(monkeypatch):
    mock_agent = AgentDefinition(
        id="test_agent",
        name="Test Agent",
        agent_type="custom",
        system_prompt="Test Prompt",
        model="claude"
    )

    monkeypatch.setattr(agent_manager, "get_agent", lambda agent_id: mock_agent)
    monkeypatch.setattr(agent_manager, "_build_agent_system_prompt", lambda agent: "Test Prompt")

    instances = InstanceManager()
    owner_id = 42

    result = agent_manager.spawn_agent("test_agent", instances, owner_id)

    assert result is not None
    assert isinstance(result, Instance)

    # Verify title, owner_id, agent_id
    assert result.title == "Test Agent"
    assert result.agent_id == "test_agent"
    assert instances._instance_owner[result.id] == owner_id

    # Verify overridden session_id format
    expected_session_id = f"agent_{mock_agent.id}_{result.id}_{owner_id}"
    assert result.session_id == expected_session_id


@pytest.mark.parametrize(
    "command, expected_agents, expected_task",
    [
        # Happy paths with different separators
        ('Research -> Analytics "summarize the news"', ["research", "analytics"], "summarize the news"),
        ('Research Analytics "summarize the news"', ["research", "analytics"], "summarize the news"),
        ('research -> analytics -> writer "write a story"', ["research", "analytics", "writer"], "write a story"),
        ('research \u2192 analytics "unicode arrow test"', ["research", "analytics"], "unicode arrow test"),
        ('research "do this"', ["research"], "do this"),

        # Edge cases
        ('Research Analytics', ["research", "analytics"], ""),  # No task provided
        ('', [], ""),  # Empty string
        ('   research   ->   analytics   "   spaced out   "   ', ["research", "analytics"], "   spaced out   "),
        ('research "task with -> arrow"', ["research"], "task with -> arrow"), # Ensure arrow inside task is not split
        ('"only task"', [], "only task"), # No agents, only task
        ('research -> analytics', ["research", "analytics"], ""), # No quotes, no task
    ]
)
def test_parse_pipeline_command(command, expected_agents, expected_task):
    agents, task = parse_pipeline_command(command)
    assert agents == expected_agents
    assert task == expected_task
