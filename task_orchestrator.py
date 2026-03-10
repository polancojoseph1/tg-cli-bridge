"""Task Orchestrator — run a complex task across multiple parallel sub-agents.

Usage:
    # In server.py lifespan startup:
    if task_orchestrator:
        task_orchestrator.init(runner)

    # Then:
    result = await task_orchestrator.orchestrate(task, chat_id, instance_manager, send_fn)

Flow:
    1. Plan  — runner generates a 2-4 sub-task JSON breakdown
    2. Run   — Execute all sub-tasks concurrently via asyncio.gather()
    3. Sync  — Report progress as each agent finishes
    4. Synth — runner synthesizes all results into one final response
"""

import asyncio
import json
import logging

logger = logging.getLogger("bridge.orchestrator")

_runner = None


def init(runner) -> None:
    """Set the AI runner used for planning and synthesis. Call from server.py lifespan."""
    global _runner
    _runner = runner


_PLAN_PROMPT = """\
You are a task planner. Break the following task into 2-4 independent sub-tasks that can run in parallel.

Output ONLY valid JSON — no markdown, no explanation — in this exact structure:
{{
  "subtasks": [
    {{"id": 1, "title": "Short title", "prompt": "Full self-contained prompt for an AI agent. Include all context needed to complete this sub-task independently."}},
    {{"id": 2, "title": "Short title", "prompt": "Full self-contained prompt for an AI agent. Include all context needed to complete this sub-task independently."}}
  ]
}}

Rules:
- 2-4 sub-tasks max
- Sub-tasks must be INDEPENDENT — they run in parallel and cannot reference each other
- Each prompt must be fully self-contained — include all context, don't reference "the other agent"
- Titles must be 5 words or fewer
- Together, the sub-tasks must fully cover the original task

Task: {task}"""

_SYNTHESIS_PROMPT = """\
You received results from {n} specialized agents that worked in parallel on this task:

ORIGINAL TASK:
{task}

AGENT RESULTS:
{results}

Synthesize these into a single comprehensive, well-organized response. Address the original task directly. \
Remove redundancy. Preserve the best insights from each agent. Be concise."""


async def orchestrate(
    task: str,
    chat_id: int,
    instance_manager,
    send_fn,
) -> str:
    """Orchestrate a complex task across parallel AI sub-agents.

    Args:
        task: The user's full task description.
        chat_id: Telegram chat ID for progress updates.
        instance_manager: The shared InstanceManager from server.py.
        send_fn: Async function send_fn(chat_id, text) to send Telegram messages.

    Returns:
        Final synthesized response as a string.
    """
    if _runner is None:
        return "❌ Orchestrator not initialized — runner not set."

    await send_fn(chat_id, "🎯 Orchestrating — planning sub-tasks...")

    # 1. Plan
    plan_raw = await _runner.run_query(_PLAN_PROMPT.format(task=task), timeout_secs=30)
    subtasks = _parse_plan(plan_raw)

    if not subtasks:
        logger.error("Orchestrator plan parsing failed. Raw output:\n%s", plan_raw[:800])
        return (
            "❌ Failed to generate a task plan.\n\n"
            "Try rephrasing with a more specific task, or break it into steps yourself."
        )

    plan_lines = "\n".join(f"  {i + 1}. {st['title']}" for i, st in enumerate(subtasks))
    await send_fn(chat_id, f"📋 {len(subtasks)} agents running in parallel:\n{plan_lines}")

    # 2. Run all sub-tasks concurrently (stateless run_query per sub-task)
    results: list[str] = ["(no result)"] * len(subtasks)

    async def _run_subtask(idx: int, st: dict) -> None:
        await send_fn(chat_id, f"⚡ [{st['title']}] starting...")
        try:
            result = await _runner.run_query(st["prompt"], timeout_secs=300)
            results[idx] = result
            await send_fn(chat_id, f"✅ [{st['title']}] done")
        except Exception as exc:
            results[idx] = f"[Agent error: {exc}]"
            await send_fn(chat_id, f"❌ [{st['title']}] failed: {exc}")
            logger.exception("Sub-task %d failed", idx + 1)

    await asyncio.gather(*[_run_subtask(i, st) for i, st in enumerate(subtasks)])

    # 3. Synthesize
    await send_fn(chat_id, "🔄 Synthesizing results from all agents...")

    results_text = "\n\n".join(
        f"=== Agent {i + 1}: {st['title']} ===\n{result}"
        for i, (st, result) in enumerate(zip(subtasks, results))
    )

    synth_prompt = _SYNTHESIS_PROMPT.format(
        n=len(subtasks),
        task=task,
        results=results_text,
    )

    final = await _runner.run_query(synth_prompt, timeout_secs=120)
    return final


def _parse_plan(raw: str) -> list[dict] | None:
    """Parse JSON plan from model output, handling markdown code fences and preamble text."""
    text = raw.strip()

    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    start = text.find("{")
    end = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]

    try:
        data = json.loads(text)
        subtasks = data.get("subtasks", [])
        if not subtasks or not isinstance(subtasks, list):
            logger.error("Plan JSON missing 'subtasks' list")
            return None
        for st in subtasks:
            if not all(k in st for k in ("id", "title", "prompt")):
                logger.error("Sub-task missing required fields: %s", st)
                return None
        return subtasks[:4]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.error("Plan JSON parse error: %s", exc)
        return None
