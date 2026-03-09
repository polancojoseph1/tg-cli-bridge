"""Claude Code CLI runner adapter.

Subprocess: claude -p --output-format stream-json --resume <uuid>
Session: UUID-based (--session-id first call, --resume subsequent)
System prompt: --append-system-prompt CLI flag
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Callable, Awaitable

from runners.base import RunnerBase

logger = logging.getLogger("bridge.claude")


class ClaudeRunner(RunnerBase):
    name = "claude"
    cli_command = "claude"

    def __init__(self):
        from config import CLI_TIMEOUT, CLI_SYSTEM_PROMPT, CHROME_ENABLED, MEMORY_DIR, MEMORY_ENABLED
        self.timeout = CLI_TIMEOUT
        self.system_prompt = CLI_SYSTEM_PROMPT
        self.chrome_enabled = CHROME_ENABLED
        self.memory_dir = MEMORY_DIR
        self.memory_enabled = MEMORY_ENABLED

    def new_session(self, instance) -> None:
        instance.session_id = str(uuid.uuid4())
        instance.session_started = False
        instance.session_cost = 0.0

    async def stop(self, instance) -> bool:
        proc = instance.process
        if proc is not None and proc.returncode is None:
            instance.was_stopped = True
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            instance.process = None
            return True
        return False

    async def kill_all(self) -> int:
        return self._kill_processes("claude -p")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query. No session, no memory, no progress."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "claude CLI not found"}'

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        cmd = [
            binary, "-p", "--model", "claude-haiku-4-5-20251001",
            "--dangerously-skip-permissions",
            "--output-format", "text",
            prompt,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            return f'{{"error": "Failed to start claude: {exc}"}}'

        stdout_data = b""
        stderr_data = b""

        async def _read():
            nonlocal stdout_data, stderr_data
            stdout_data, stderr_data = await proc.communicate()

        try:
            await asyncio.wait_for(_read(), timeout=float(timeout))
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return '{"error": "timed out"}'

        result = stdout_data.decode(errors="replace").strip()
        if result:
            return result
        err = stderr_data.decode(errors="replace").strip()
        if err:
            return f"[stderr] {err}"
        return "(no response)"

    async def run(
        self,
        message: str,
        instance,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        image_path: str | None = None,
        memory_context: str = "",
    ) -> str:
        instance.was_stopped = False

        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return "\u274c Error: claude CLI not found. Is Claude Code installed?"

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        session_id = instance.session_id
        session_started = instance.session_started

        model = instance.model or "claude-sonnet-4-6"
        cmd = [binary, "-p", "--model", model,
               "--dangerously-skip-permissions",
               "--verbose", "--output-format", "stream-json"]

        if self.chrome_enabled:
            cmd.append("--chrome")

        if session_started:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]

        # Build combined system prompt
        system_parts = []
        if instance.agent_system_prompt:
            system_parts.append(instance.agent_system_prompt)
        else:
            if self.memory_enabled:
                system_parts.append(
                    f"You have a persistent memory system at {self.memory_dir}/. "
                    f"At the start of a session, read {self.memory_dir}/USER.md to understand who you're talking to, "
                    f"and {self.memory_dir}/MEMORY.md for project context and instructions. "
                    "If you learn new important facts during this conversation "
                    "(new projects, decisions, preferences, contacts, or corrections to existing info), "
                    f"update the appropriate file in {self.memory_dir}/ using the Edit or Write tool. "
                    "For user profile changes update USER.md. For project/system changes update MEMORY.md. "
                    "For new topics, create a new .md file with a descriptive name. "
                    "Only update when there's genuinely new durable information — not for transient questions."
                )
            if self.system_prompt:
                system_parts.append(self.system_prompt)
        if memory_context:
            system_parts.append(memory_context)
        if system_parts:
            cmd += ["--append-system-prompt", "\n\n".join(system_parts)]

        if image_path:
            if message:
                prompt = f"First, use the Read tool to view the image file at: {image_path}\n\nThen respond to the user's request: {message}"
            else:
                prompt = f"Use the Read tool to view the image file at: {image_path}\n\nDescribe what you see in the image."
            cmd.append(prompt)
        else:
            cmd.append(message)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=10 * 1024 * 1024,
            )
            instance.process = proc
        except OSError as exc:
            logger.exception("OS error running claude")
            return f"\u274c Error starting claude: {exc}"

        final_result = ""
        assistant_text_parts: list[str] = []
        stderr_output = b""
        _usage = {"turn": {}, "context_window": 0, "cost": 0.0}
        _agent_calls: dict[str, dict] = {}
        _agent_counter = 0

        async def drain_stderr():
            nonlocal stderr_output
            stderr_output = await proc.stderr.read()

        async def process_stdout():
            nonlocal final_result, _agent_counter
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "assistant":
                    turn_usage = data.get("message", {}).get("usage")
                    if turn_usage:
                        _usage["turn"] = turn_usage
                    for block in data.get("message", {}).get("content", []):
                        block_type = block.get("type", "")
                        if block_type == "tool_use":
                            tool_name = block.get("name", "")
                            tool_inp = block.get("input", {})
                            tool_id = block.get("id", "")
                            if tool_name == "Agent":
                                _agent_counter += 1
                                desc = tool_inp.get("description", tool_inp.get("prompt", ""))[:100]
                                _agent_calls[tool_id] = {"n": _agent_counter, "description": desc}
                                if on_progress:
                                    await on_progress(f"\U0001f916 [Sub-agent {_agent_counter}] {desc}")
                            elif on_progress:
                                progress = self.format_tool_progress(tool_name, tool_inp)
                                if progress:
                                    await on_progress(progress)
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text:
                                assistant_text_parts.append(text)

                elif msg_type == "user":
                    for block in data.get("message", {}).get("content", []):
                        if block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "")
                            if tool_use_id in _agent_calls and on_progress:
                                info = _agent_calls.pop(tool_use_id)
                                await on_progress(f"\u2705 [Sub-agent {info['n']} done]")

                elif msg_type == "result":
                    final_result = data.get("result", "")
                    _usage["cost"] = data.get("total_cost_usd", 0.0)
                    for model_data in data.get("modelUsage", {}).values():
                        _usage["context_window"] = model_data.get("contextWindow", 0)
                        break

        async def process_stream():
            stderr_task = asyncio.create_task(drain_stderr())
            await process_stdout()
            await proc.wait()
            await stderr_task

        try:
            await asyncio.wait_for(process_stream(), timeout=self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            instance.process = None
            return "\u23f0 Claude took too long to respond (timed out)."

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if _usage["context_window"]:
                instance.context_window = _usage["context_window"]
            turn = _usage["turn"]
            if turn:
                instance.last_input_tokens = turn.get("input_tokens", 0)
                instance.last_cache_read_tokens = turn.get("cache_read_input_tokens", 0)
                instance.last_cache_creation_tokens = turn.get("cache_creation_input_tokens", 0)
                instance.last_output_tokens = turn.get("output_tokens", 0)
            instance.session_cost += _usage["cost"]

        if proc.returncode != 0:
            err = stderr_output.decode(errors="replace").strip()
            logger.error("claude exited %d: %s", proc.returncode, err)
            if "session" in err.lower():
                self.new_session(instance)
                return "\u274c Session error. New conversation started \u2014 please resend your message."
            return f"\u274c Claude error:\n{err}" if err else "\u274c Claude exited with an error."

        if final_result:
            return final_result
        if assistant_text_parts:
            return "\n".join(assistant_text_parts)
        return "(empty response from Claude)"
