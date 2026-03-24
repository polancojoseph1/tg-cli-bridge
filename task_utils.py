import logging

logger = logging.getLogger("bridge.task_utils")

async def run_task(task: str, agent_id: str | None, context: str) -> str:
    """Run a task using the configured runner, optionally via a named agent.

    Falls back gracefully if agent_id is unknown or runner is unavailable.
    """
    prompt = task
    if context:
        prompt = f"Context: {context}\n\n{task}"

    if agent_id:
        try:
            from agent_registry import get_agent
            agent = get_agent(agent_id)
            if agent and agent.system_prompt:
                prompt = f"{agent.system_prompt}\n\n{prompt}"
        except Exception as e:
            logger.debug("Could not load agent '%s': %s", agent_id, e)

    try:
        from runners import create_runner
        runner = create_runner()
        result = await runner.run_query(prompt, timeout=120)
        return result or "(no response)"
    except Exception as e:
        logger.error("run_task failed: %s", e)
        return f"Error running task: {e}"
