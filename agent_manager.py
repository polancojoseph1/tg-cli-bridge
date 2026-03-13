"""Agent Manager — runtime lifecycle for named specialist agents.

Bridges agent_registry.py (definitions) with instance_manager.py (runtime sessions).
Agents run as named Instance objects with their expert system prompt injected.

Public API:
    spawn_agent(agent_id, instances) -> Instance
    get_or_spawn(agent_id, instances) -> Instance
    talk_to_agent(agent_id, instances, owner_id) -> Instance | None
    assign_task(agent_id, task, chat_id, instances, send_fn) -> bool
    run_pipeline(agent_ids, task, chat_id, instances, send_fn) -> str
    schedule_agent_task(agent_id, time_str, task_desc) -> str
    format_agent_list() -> str
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path

try:
    import pytz
    LOCAL_TZ = pytz.timezone(os.environ.get("TIMEZONE", "UTC"))
except ImportError:
    from zoneinfo import ZoneInfo
    class _PytzCompat:
        def __init__(self, key): self._zi = ZoneInfo(key)
        def localize(self, dt): return dt.replace(tzinfo=self._zi)
        def __call__(self): return self._zi
    LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))  # type: ignore

from agent_registry import AgentDefinition, resolve_agent, list_agents, seed_default_agents, seed_default_skills, get_agent
from agent_skills import build_skills_prompt
from instance_manager import InstanceManager, Instance

logger = logging.getLogger("bridge.agent_manager")
from config import MEMORY_DIR
SCHEDULE_FILE = str(Path(MEMORY_DIR) / "SCHEDULE.md")

# Maps agent_id -> instance_id for currently-running agent instances
_agent_instance_map: dict[str, int] = {}


def _build_agent_system_prompt(agent: AgentDefinition) -> str:
    """Combine agent's system prompt with its skill pack sections."""
    parts = []
    if agent.system_prompt:
        parts.append(agent.system_prompt)
    skill_section = build_skills_prompt(agent.skills)
    if skill_section:
        parts.append(skill_section)
    return "\n\n".join(parts)


def spawn_agent(agent_id: str, instances: InstanceManager, owner_id: int = 0) -> Instance | None:
    """Create a new Instance for the agent. Links it in the tracking map.
    Does NOT switch the active instance — caller decides when to switch."""
    agent = get_agent(agent_id)
    if agent is None:
        logger.error("spawn_agent: agent '%s' not found", agent_id)
        return None

    inst = instances.create(agent.name, owner_id=owner_id)
    inst.agent_id = agent_id
    inst.agent_system_prompt = _build_agent_system_prompt(agent)
    inst.model = agent.model

    _agent_instance_map[agent_id] = inst.id
    logger.info("Spawned agent '%s' as instance #%d", agent_id, inst.id)
    return inst


def get_running_instance(agent_id: str, instances: InstanceManager) -> Instance | None:
    """Return the currently running instance for this agent, or None."""
    inst_id = _agent_instance_map.get(agent_id)
    if inst_id is not None:
        inst = instances.get(inst_id)
        if inst is not None:
            return inst
        # Instance was removed — clean up mapping
        _agent_instance_map.pop(agent_id, None)

    # Fallback: scan all instances for a matching agent_id.
    # Handles cases where _agent_instance_map was cleared (e.g. server restart)
    # so a second trigger doesn't spawn a duplicate instance.
    for inst in instances.list_all():
        if inst.agent_id == agent_id:
            _agent_instance_map[agent_id] = inst.id  # re-register for fast lookups
            return inst

    return None


def get_or_spawn(agent_id: str, instances: InstanceManager, owner_id: int = 0) -> Instance | None:
    """Return existing instance for this agent, spawning a new one if needed."""
    existing = get_running_instance(agent_id, instances)
    if existing:
        return existing
    return spawn_agent(agent_id, instances, owner_id)


def talk_to_agent(agent_id: str, instances: InstanceManager, owner_id: int = 0) -> Instance | None:
    """Switch the active instance to the agent's instance, spawning if needed.
    Returns the agent's Instance or None on error."""
    inst = get_or_spawn(agent_id, instances, owner_id)
    if inst is None:
        return None
    instances.set_active_for(owner_id, inst.id)
    logger.info("Switched owner=%d to agent '%s' (instance #%d)", owner_id, agent_id, inst.id)
    return inst


async def assign_task(
    agent_id: str,
    task: str,
    chat_id: int,
    instances: InstanceManager,
    send_fn,
    owner_id: int = 0,
) -> bool:
    """Queue a task to the agent's instance. Returns True if queued successfully."""
    from server import QueuedMessage, MessageType, _ensure_worker

    inst = get_or_spawn(agent_id, instances, owner_id)
    if inst is None:
        return False

    if inst.queue.full():
        logger.warning("Agent '%s' queue full — task rejected", agent_id)
        return False

    agent = get_agent(agent_id)
    agent_name = agent.name if agent else agent_id

    item = QueuedMessage(
        chat_id=chat_id,
        text=task,
        msg_type=MessageType.TEXT,
        instance_id=inst.id,
        user_id=0,
    )
    _ensure_worker(inst)
    await inst.queue.put(item)
    logger.info("Queued task to agent '%s' (instance #%d): %s...", agent_id, inst.id, task[:60])
    return True


async def run_pipeline(
    agent_ids: list[str],
    task: str,
    chat_id: int,
    instances: InstanceManager,
    send_fn,
    owner_id: int = 0,
) -> str:
    """Run a sequential pipeline: output of agent[n] feeds into agent[n+1].

    Args:
        agent_ids: List of agent IDs in execution order
        task: Original user task description
        chat_id: Telegram chat ID for status updates
        instances: The InstanceManager
        send_fn: async send_fn(chat_id, text) for Telegram updates
        owner_id: Owner for instance management

    Returns:
        Final synthesized result as string
    """
    from runners import create_runner
    import memory_handler
    _runner = create_runner()

    if not agent_ids:
        return "No agents specified for pipeline."

    resolved = []
    for aid in agent_ids:
        agent = resolve_agent(aid)
        if agent is None:
            return f"Agent '{aid}' not found. Check /agent list."
        resolved.append(agent)

    await send_fn(chat_id, f"Pipeline starting: {' → '.join(a.name for a in resolved)}")

    current_input = task
    final_result = ""

    for i, agent in enumerate(resolved, 1):
        inst = get_or_spawn(agent.id, instances, owner_id)
        if inst is None:
            await send_fn(chat_id, f"Failed to spawn {agent.name}. Stopping pipeline.")
            return final_result or "Pipeline failed."

        await send_fn(chat_id, f"[{i}/{len(resolved)}] {agent.name} processing...")

        # Build pipeline-aware prompt
        if i == 1:
            prompt = current_input
        else:
            prev_agent = resolved[i - 2]
            prompt = (
                f"PIPELINE CONTEXT:\n"
                f"Original task: {task}\n\n"
                f"{prev_agent.name} output:\n{current_input}\n\n"
                f"Your job: Based on the above, {task}"
            )

        memory_ctx = await memory_handler.search_memory(prompt[:200])

        async def _progress(text: str, agent_name=agent.name):
            await send_fn(chat_id, f"[{agent_name}] {text}")

        result = await _runner.run(
            prompt,
            instance=inst,
            on_progress=_progress,
            memory_context=memory_ctx,
        )

        # Store in agent's memory
        try:
            from agent_memory import store_agent_work
            store_agent_work(agent.id, task, result)
        except Exception as e:
            logger.warning("Failed to store pipeline work for '%s': %s", agent.id, e)

        await send_fn(chat_id, f"[{i}/{len(resolved)}] {agent.name} done.")
        current_input = result
        final_result = result

    return final_result


def schedule_agent_task(agent_id: str, time_str: str, task_desc: str) -> str:
    """Add a scheduled recurring task for this agent to SCHEDULE.md.

    Args:
        agent_id: Agent ID (e.g. "research")
        time_str: Time in HH:MM format (NYC timezone)
        task_desc: What the agent should do

    Returns:
        Confirmation string or error message
    """
    agent = get_agent(agent_id)
    if agent is None:
        return f"Agent '{agent_id}' not found."

    # Validate time format
    try:
        h, m = map(int, time_str.split(":"))
        assert 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        return f"Invalid time '{time_str}'. Use HH:MM format (e.g. 09:00)."

    now = datetime.now(LOCAL_TZ)
    run_date = now.strftime("%Y-%m-%d")
    scheduled_time = f"{run_date} {h:02d}:{m:02d}"

    # Read current schedule to get next ID
    try:
        with open(SCHEDULE_FILE, "r") as f:
            lines = f.readlines()
        max_id = 0
        for line in lines[3:]:
            if not line.strip() or not line.startswith("|"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                try:
                    max_id = max(max_id, int(parts[1]))
                except ValueError:
                    pass
        new_id = str(max_id + 1).zfill(3)
    except FileNotFoundError:
        new_id = "001"

    task_id = f"AGT-{agent_id.upper()[:6]}-{new_id}"

    # Append to SCHEDULE.md
    row = (
        f"| {task_id} | {scheduled_time} | {task_desc} "
        f"| Agent:{agent_id} | [ ] Pending | [ ] Waiting | | daily {h:02d}:{m:02d} |\n"
    )

    try:
        with open(SCHEDULE_FILE, "a") as f:
            f.write(row)
    except Exception as e:
        return f"Failed to write schedule: {e}"

    logger.info("Scheduled agent '%s' task '%s' at %s daily", agent_id, task_id, time_str)
    return f"Scheduled: {agent.name} runs '{task_desc}' daily at {h:02d}:{m:02d} NYC (ID: {task_id})"


def format_agent_list(instances: InstanceManager) -> str:
    """Return HTML-formatted list of all agents with their runtime status."""
    agents = list_agents()
    if not agents:
        return "No agents. Create one with /agent create &lt;type&gt; &lt;name&gt;"

    lines = [f"<b>Agents ({len(agents)}):</b>"]
    for agent in agents:
        running_inst = get_running_instance(agent.id, instances)
        if running_inst:
            status = "busy" if running_inst.processing else "active"
            inst_label = f"[#{running_inst.id}: {status}]"
        else:
            inst_label = "[idle]"

        skills_label = ", ".join(agent.skills) if agent.skills else "none"
        proactive_badge = " 🤖 [PROACTIVE]" if agent.proactive else ""
        if agent.proactive and agent.proactive_schedule:
            from proactive_worker import schedule_label
            sched_info = f" | Schedule: {schedule_label(agent.proactive_schedule)}"
        else:
            sched_info = ""
        lines.append(
            f"  <b>{agent.name}</b>{proactive_badge} {inst_label}\n"
            f"    ID: {agent.id} | Type: {agent.agent_type} | Skills: {skills_label}{sched_info}"
        )

    lines.append("\nCommands: /agent talk &lt;name&gt; | /agent task &lt;name&gt; &lt;task&gt; | /agent create &lt;type&gt; &lt;name&gt;")
    return "\n".join(lines)


def ensure_default_agents() -> None:
    """Seed the default agents and skills on startup if they don't exist."""
    try:
        skill_count = seed_default_skills()
        if skill_count:
            logger.info("Seeded %d default skills on startup", skill_count)
    except Exception as e:
        logger.error("Failed to seed default skills: %s", e)
    try:
        count = seed_default_agents()
        if count:
            logger.info("Seeded %d default agents on startup", count)
    except Exception as e:
        logger.error("Failed to seed default agents: %s", e)


async def fix_agent_prompt(agent_id: str, rule: str, instances: InstanceManager | None = None) -> str:
    """Smart-merge a new rule into an agent's system prompt using Claude.

    Instead of blindly appending, Claude reads the current prompt and integrates
    the rule — replacing contradictions, merging duplicates, inserting in context.
    Updates agents.db and any live running instance.

    Returns a status message.
    """
    from agent_registry import update_agent
    from runners import create_runner; _qrunner = create_runner()

    agent = get_agent(agent_id)
    if agent is None:
        return f"Agent '{agent_id}' not found."

    merge_prompt = (
        f"You are editing an AI agent's system prompt. The agent is: {agent.name}\n\n"
        f"CURRENT SYSTEM PROMPT:\n{agent.system_prompt}\n\n"
        f"NEW RULE TO INTEGRATE:\n{rule}\n\n"
        f"Instructions:\n"
        f"1. If the rule contradicts an existing instruction, replace the old one\n"
        f"2. If the rule is a more specific version of an existing instruction, merge them\n"
        f"3. If the rule is new, add it in the most relevant section\n"
        f"4. Keep the prompt concise — do not bloat it unnecessarily\n"
        f"5. Return ONLY the updated system prompt text, nothing else"
    )

    updated_prompt = await _qrunner.run_query(merge_prompt, timeout=60)

    # Fallback: if Claude failed, just append the rule with a datestamp
    if not updated_prompt or updated_prompt.startswith('{"error"') or "(no response)" in updated_prompt:
        logger.warning("fix_agent_prompt: Claude merge failed, appending rule directly")
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        updated_prompt = f"{agent.system_prompt}\n\nRULE (added {today}):\n{rule}"

    updated = update_agent(agent_id, system_prompt=updated_prompt)
    if not updated:
        return f"Failed to save updated prompt for {agent.name}."

    # Update any running instance so the change takes effect immediately
    if instances is not None:
        running = get_running_instance(agent_id, instances)
        if running:
            skill_section = build_skills_prompt(updated.skills)
            running.agent_system_prompt = updated_prompt + ("\n\n" + skill_section if skill_section else "")
            logger.info("Updated live instance #%d system prompt for agent '%s'", running.id, agent_id)

    logger.info("Fixed agent '%s' prompt with rule: %s...", agent_id, rule[:80])
    return f"Rule integrated into <b>{agent.name}</b>'s prompt:\n<i>{rule[:200]}</i>"


def configure_proactive(
    agent_id: str,
    enabled: bool,
    schedule: str = "",
    task: str = "",
) -> str:
    """Enable or disable proactive mode for an agent.

    Args:
        agent_id: Agent ID or name.
        enabled: True to enable, False to disable.
        schedule: HH:MM (NYC time) — required when enabling.
        task: The prompt to run automatically — required when enabling.

    Returns a status message string.
    """
    from agent_registry import update_agent as _update

    agent = get_agent(agent_id)
    if agent is None:
        return f"Agent '{agent_id}' not found."

    if enabled:
        if not schedule:
            return (
                "Schedule required.\n"
                "Examples:\n"
                "  <code>09:00</code> — daily at 9am NYC\n"
                "  <code>every 2h</code> — every 2 hours\n"
                "  <code>every 30m</code> — every 30 minutes"
            )
        if not task:
            return "Task required. Example: /agent proactive research set 09:00 summarize top AI news"
        # Validate schedule format via proactive_worker parser
        try:
            from proactive_worker import parse_schedule, schedule_label
            parse_schedule(schedule)
            label = schedule_label(schedule)
        except ValueError as e:
            return f"❌ {e}"

        _update(agent_id, proactive=True, proactive_schedule=schedule, proactive_task=task)
        logger.info("Proactive ENABLED for agent '%s' — %s — task: %s", agent_id, label, task[:80])
        return (
            f"✅ <b>{agent.name}</b> is now <b>proactive</b>.\n"
            f"Schedule: <code>{label}</code>\n"
            f"Task: <i>{task[:200]}</i>\n\n"
            f"Start the worker when ready: /agent proactive start"
        )
    else:
        _update(agent_id, proactive=False)
        logger.info("Proactive DISABLED for agent '%s'", agent_id)
        return f"⏸ <b>{agent.name}</b> proactive mode <b>disabled</b>. Schedule and task preserved."


def clear_proactive(agent_id: str) -> str:
    """Disable proactive mode and wipe the schedule/task for an agent."""
    from agent_registry import update_agent as _update

    agent = get_agent(agent_id)
    if agent is None:
        return f"Agent '{agent_id}' not found."
    _update(agent_id, proactive=False, proactive_schedule="", proactive_task="")
    logger.info("Proactive CLEARED for agent '%s'", agent_id)
    return f"🗑 <b>{agent.name}</b> proactive config cleared."


async def _diagnose_mistake(agent_name: str, task_response_text: str, feedback: str) -> str:
    """Diagnose the root cause of an agent mistake via Gemini Flash REST API.

    Uses httpx + GEMINI_API_KEY (already configured in .env) to avoid subprocess
    timeout issues. Returns a 1-2 sentence root cause, or empty string on failure.
    """
    import os
    import httpx

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("_diagnose_mistake: GEMINI_API_KEY not set — skipping diagnosis")
        return ""

    prompt = (
        f"You are a quality analyst diagnosing why an AI agent made a mistake.\n\n"
        f"AGENT: {agent_name}\n\n"
        f"AGENT'S LAST TASK AND RESPONSE:\n{task_response_text[:2000]}\n\n"
        f"USER'S CORRECTION:\n{feedback}\n\n"
        f"In 1-2 sentences, identify the specific reasoning error or failure mode that caused this mistake. "
        f"Name the exact decision point where the agent went wrong (e.g. 'hallucinated data', "
        f"'skipped source verification', 'misread the task scope', etc.). "
        f"Start with 'Root cause:' and do not add any preamble or explanation."
    )
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 512, "temperature": 0.2},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
            resp.raise_for_status()
            data = resp.json()
            result = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            logger.info("[_diagnose_mistake] root cause: %s", result[:120])
            return result
    except Exception as e:
        logger.warning("_diagnose_mistake failed: %s", e)
        return ""


async def record_agent_feedback(agent_id: str, feedback: str, instances: InstanceManager | None = None) -> str:
    """Record user feedback about an agent's behavior, then improve the agent.

    Steps:
    1. Retrieve last response from ChromaDB so we have the raw output to diagnose
    2. Diagnose the root cause via Claude (what specific error caused the mistake?)
    3. Record Outcome in KuzuDB with root cause + raw feedback combined
    4. Derive a concrete rule from the feedback and smart-merge into the agent's prompt

    Returns a status message.
    """
    from agent_memory import record_outcome, get_last_agent_response
    from runners import create_runner; _qrunner = create_runner()

    agent = get_agent(agent_id)
    if agent is None:
        return f"Agent '{agent_id}' not found."

    # Step 1: Retrieve last response for diagnosis
    last_response_text = get_last_agent_response(agent_id)

    # Step 2: Diagnose root cause
    root_cause = ""
    if last_response_text:
        logger.info("[feedback:diagnose] agent=%s — running diagnosis on last response (%d chars)",
                    agent_id, len(last_response_text))
        root_cause = await _diagnose_mistake(agent.name, last_response_text, feedback)
        if root_cause:
            logger.info("[feedback:diagnosis] agent=%s — %s", agent_id, root_cause[:120])
            diagnosis_status = f"🔍 <b>Root cause diagnosed:</b>\n<i>{root_cause[:400]}</i>\n\n"
        else:
            logger.warning("[feedback:diagnose] agent=%s — diagnosis returned empty", agent_id)
            diagnosis_status = "<i>Diagnosis failed — no root cause extracted. Saving raw feedback.</i>\n\n"
    else:
        logger.info("[feedback:diagnose] agent=%s — no prior response in ChromaDB to diagnose", agent_id)
        diagnosis_status = "<i>No prior response found to diagnose — saving raw feedback only.</i>\n\n"

    # Step 3: Record outcome in KuzuDB — include root cause so future context is richer
    outcome_text = f"Root cause: {root_cause}\nUser feedback: {feedback}" if root_cause else feedback
    graph_ok = record_outcome(agent_id, outcome_text, outcome_type="corrected")
    graph_status = "Recorded in graph." if graph_ok else "Graph recording failed (KuzuDB unavailable)."

    # Step 4: Derive a concrete, actionable rule from the raw feedback
    derive_prompt = (
        f"Convert this user feedback about an AI agent named '{agent.name}' into a single concrete, "
        f"actionable rule to add to its system prompt.\n\n"
        f"Feedback: {feedback}\n\n"
        f"Return only the rule text, starting with a verb like 'Always', 'Never', or 'When X, do Y'. "
        f"Keep it under 2 sentences. Do not include explanations or preamble."
    )
    derived_rule = await _qrunner.run_query(derive_prompt, timeout=30)

    if not derived_rule or derived_rule.startswith('{"error"') or "(no response)" in derived_rule:
        logger.warning("record_agent_feedback: rule derivation failed, using raw feedback")
        derived_rule = feedback  # use raw feedback as the rule

    # Step 5: Smart-merge derived rule into agent's system prompt
    fix_msg = await fix_agent_prompt(agent_id, derived_rule, instances=instances)

    return f"{diagnosis_status}{graph_status}\n\n{fix_msg}"


async def auto_critique(agent_id: str, task: str, response: str) -> list[str]:
    """Run a self-critique pass on an agent's output against its own rules.

    Asks Claude to read the agent's system prompt rules + the task + the response,
    then list any rule violations. Returns an empty list if the output passes.

    Designed to run async in the background — doesn't block the user response.
    """
    import time
    from runners import create_runner; _qrunner = create_runner()

    agent = get_agent(agent_id)
    if agent is None:
        logger.warning("[auto_critique] agent '%s' not found in registry", agent_id)
        return []

    prompt_excerpt = (agent.system_prompt or "")[:1500]
    logger.info(
        "[auto_critique:start] agent=%s task=%r rules_len=%d response_len=%d",
        agent_id, task[:60], len(prompt_excerpt), len(response),
    )

    critique_prompt = (
        f"You are a strict quality reviewer for an AI agent named '{agent.name}'.\n\n"
        f"AGENT'S RULES (from its system prompt):\n{prompt_excerpt}\n\n"
        f"TASK THE AGENT WAS GIVEN:\n{task[:500]}\n\n"
        f"AGENT'S RESPONSE:\n{response[:2000]}\n\n"
        f"Did the agent violate any of its rules in this response?\n"
        f"If YES: list each violation on its own line, starting with '- '\n"
        f"If NO violations: reply with exactly: PASS\n"
        f"Be strict but fair. Ignore stylistic choices — flag only concrete rule violations."
    )

    t0 = time.time()
    raw = await _qrunner.run_query(critique_prompt, timeout=45)
    elapsed = round(time.time() - t0, 1)

    logger.info(
        "[auto_critique:raw] agent=%s elapsed=%ss raw=%r",
        agent_id, elapsed, (raw or "")[:200],
    )

    if not raw or raw.startswith('{"error"'):
        logger.warning("[auto_critique:error] agent=%s raw=%r", agent_id, raw)
        return []

    if "PASS" in raw.upper():
        logger.info("[auto_critique:result] agent=%s → PASS", agent_id)
        return []

    violations = [
        line.lstrip("- ").strip()
        for line in raw.splitlines()
        if line.strip().startswith("-")
    ]
    logger.info(
        "[auto_critique:result] agent=%s → %d violation(s): %s",
        agent_id, len(violations), violations,
    )
    return violations


async def _run_post_task_critique(
    agent_id: str,
    task: str,
    response: str,
    chat_id: int,
    send_fn,
    instances: InstanceManager | None = None,
) -> None:
    """Background task: critique agent response and log every step to server logs + KuzuDB.

    Flow:
      PASS  → approved outcome recorded. INFO log. Telegram ping: "✅ passed all checks".
      FAIL  → each violation recorded in KuzuDB. INFO log per violation.
              Telegram ping with violation list (non-blocking, informational).
              On next task, _query_graph_context() injects violations into context window.
    """
    from agent_memory import record_outcome
    import time

    started_at = time.time()
    agent = get_agent(agent_id)
    agent_name = agent.name if agent else agent_id

    logger.info(
        "[critique:start] agent=%s task=%r response_len=%d",
        agent_id, task[:80], len(response),
    )

    try:
        violations = await auto_critique(agent_id, task, response)
        elapsed = round(time.time() - started_at, 1)

        if not violations:
            record_outcome(
                agent_id,
                f"Output passed self-critique for task: {task[:120]}",
                outcome_type="approved",
            )
            logger.info(
                "[critique:pass] agent=%s elapsed=%ss — no violations, approved outcome recorded",
                agent_id, elapsed,
            )
            await send_fn(
                chat_id,
                f"✅ <b>{agent_name}</b> — passed all critique checks ({elapsed}s). No violations.",
                parse_mode="HTML",
            )
            return

        # Log every violation individually
        logger.info(
            "[critique:fail] agent=%s elapsed=%ss — %d violation(s) found",
            agent_id, elapsed, len(violations),
        )
        for i, v in enumerate(violations, 1):
            graph_ok = record_outcome(agent_id, v, outcome_type="self_corrected")
            logger.info(
                "[critique:violation:%d/%d] agent=%s graph=%s — %s",
                i, len(violations), agent_id,
                "recorded" if graph_ok else "GRAPH_FAIL",
                v,
            )

        # Telegram ping — informational, no action needed from the user
        violation_lines = "\n".join(f"• {v}" for v in violations[:3])
        suffix = f"\n+ {len(violations) - 3} more" if len(violations) > 3 else ""
        await send_fn(
            chat_id,
            f"🔍 <b>{agent_name}</b> — {len(violations)} violation(s) logged ({elapsed}s):\n"
            f"{violation_lines}{suffix}\n\n"
            f"<i>Injected into context before next task.</i>",
            parse_mode="HTML",
        )
        logger.info("[critique:done] agent=%s ping_sent=True", agent_id)

    except Exception as e:
        elapsed = round(time.time() - started_at, 1)
        logger.warning(
            "[critique:error] agent=%s elapsed=%ss error=%s",
            agent_id, elapsed, e,
        )


def parse_pipeline_command(args: str) -> tuple[list[str], str]:
    """Parse pipeline command args into (agent_ids, task).

    Formats supported:
      Research → Analytics "task desc"
      Research Analytics "task desc"
      research → analytics → writer "task desc"
    """
    # Extract quoted task at end
    task_match = re.search(r'"([^"]+)"\s*$', args)
    task = task_match.group(1) if task_match else ""
    agents_part = args[:task_match.start()].strip() if task_match else args

    # Split on → or ->
    parts = re.split(r"\s*(?:→|->)\s*|\s+", agents_part)
    agent_ids = [p.strip().lower() for p in parts if p.strip()]

    return agent_ids, task
