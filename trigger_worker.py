"""Trigger worker — fires agents in response to events.

This module is purely event-driven (no polling loop).
fire() is called by:
  - The /triggers/webhook/<id> HTTP endpoint (external events)
  - The /trigger run <id> Telegram command (manual)

Public API:
    init(instance_manager, send_fn)  — call once at startup
    fire(trigger_id)                 — fire a trigger by ID, returns True on success
"""

import asyncio
import logging

logger = logging.getLogger("bridge.trigger_worker")

_instance_manager = None
_send_fn = None


def init(instance_manager, send_fn) -> None:
    """Initialize the worker. Call once at server startup."""
    global _instance_manager, _send_fn
    _instance_manager = instance_manager
    _send_fn = send_fn
    logger.info("Trigger worker initialized")


async def fire(trigger_id: str) -> bool:
    """Fire a trigger by ID.

    Looks up the trigger and its agent, sends a start notification,
    then runs the agent task in the background.
    Returns True if the trigger was found and dispatched, False otherwise.
    """
    from trigger_registry import get_trigger, record_fired
    from agent_registry import resolve_agent

    if _instance_manager is None or _send_fn is None:
        logger.error("Trigger worker not initialized — call init() at startup")
        return False

    trigger = get_trigger(trigger_id)
    if not trigger:
        logger.warning("fire(): trigger '%s' not found", trigger_id)
        return False

    if not trigger.enabled:
        logger.info("fire(): trigger '%s' is disabled, skipping", trigger_id)
        return False

    agent = resolve_agent(trigger.agent_id)
    if not agent:
        logger.warning("fire(): trigger '%s' references missing agent '%s'", trigger_id, trigger.agent_id)
        return False

    task = trigger.task_override or agent.proactive_task or f"Perform your role as the {agent.name} agent."
    chat_id = trigger.chat_id

    record_fired(trigger_id)
    logger.info("Firing trigger '%s' → agent '%s' | task: %s", trigger_id, agent.id, task[:80])

    asyncio.ensure_future(_run_agent(trigger_id, agent.id, agent.name, task, chat_id))
    return True


async def _run_agent(trigger_id: str, agent_id: str, agent_name: str, task: str, chat_id: int) -> None:
    """Run the agent task.

    Ephemeral agents bypass the Telegram instance system entirely — they run
    directly as a background process and send one plain message when done.
    Non-ephemeral agents go through assign_task() as before.
    """
    from agent_registry import get_agent
    from agent_manager import assign_task

    agent = get_agent(agent_id)
    if agent and agent.ephemeral:
        await _run_ephemeral_direct(trigger_id, agent, task, chat_id)
    else:
        try:
            await assign_task(agent_id, task, chat_id, _instance_manager, _send_fn)
        except Exception as e:
            logger.error("Trigger '%s' agent run failed: %s", trigger_id, e)
            await _send_fn(
                chat_id,
                f"❌ **{agent_name}** [TRIGGER: {trigger_id}] failed: {e}",
                format_markdown=True,
            )


async def _run_ephemeral_direct(trigger_id: str, agent, task: str, chat_id: int) -> None:
    """Run an ephemeral agent directly — no Telegram instance created, no label, one plain message."""
    from runners import create_runner
    from agent_manager import _build_agent_system_prompt
    from instance_manager import Instance

    logger.info("[ephemeral] Starting direct run: trigger=%s agent=%s", trigger_id, agent.id)

    # Temporary instance — never registered in InstanceManager, invisible to /list
    inst = Instance(id=-1, title=agent.name)
    inst.agent_id = agent.id
    inst.agent_system_prompt = _build_agent_system_prompt(agent)
    inst.model = agent.model

    runner = create_runner()

    try:
        from agent_memory import get_agent_context
        memory_context = await asyncio.get_event_loop().run_in_executor(
            None, get_agent_context, agent.id, task
        )
    except Exception:
        memory_context = ""

    try:
        result = await runner.run(task, instance=inst, memory_context=memory_context)
        if result and result.strip():
            await _send_fn(chat_id, result, format_markdown=True)
        else:
            logger.warning("[ephemeral] Agent '%s' returned empty result", agent.id)
    except Exception as e:
        logger.error("[ephemeral] Agent '%s' failed: %s", agent.id, e)
        await _send_fn(chat_id, f"❌ {agent.name} failed: {e}", format_markdown=True)

    logger.info("[ephemeral] Done: trigger=%s agent=%s", trigger_id, agent.id)
