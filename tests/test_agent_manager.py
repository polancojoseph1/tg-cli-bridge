import pytest
from agent_manager import parse_pipeline_command

@pytest.mark.parametrize(
    "command, expected_agents, expected_task",
    [
        # Happy paths with different separators
        ('Research -> Analytics "summarize the news"', ["research", "analytics"], "summarize the news"),
        ('Research Analytics "summarize the news"', ["research", "analytics"], "summarize the news"),
        ('research -> analytics -> writer "write a story"', ["research", "analytics", "writer"], "write a story"),
        ('research → analytics "unicode arrow test"', ["research", "analytics"], "unicode arrow test"),
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
