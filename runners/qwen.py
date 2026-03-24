"""Qwen Coder CLI runner adapter.

Subprocess: qwen -p --yolo --output-format stream-json --resume <uuid>
Session: UUID from --session-id on first call, --resume on subsequent
System prompt: writes temp .md file, sets QWEN_SYSTEM_MD env var
Output format: same Claude-style stream-json (type: assistant/result)
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import uuid
from typing import Callable, Awaitable

from runners.base import RunnerBase, _SUBPROCESS_LOGGER

logger = logging.getLogger("bridge.qwen")


class QwenRunner(RunnerBase):
    name = "qwen"
    cli_command = "qwen"

    def __init__(self):
        from config import CLI_TIMEOUT, CLI_SYSTEM_PROMPT, MEMORY_DIR, MEMORY_ENABLED, USER_NAME
        self.timeout = CLI_TIMEOUT
        self.memory_dir = MEMORY_DIR
        self.system_prompt = (CLI_SYSTEM_PROMPT.replace("{MEMORY_DIR}", MEMORY_DIR).replace("{OWNER_NAME}", USER_NAME or "the user") if CLI_SYSTEM_PROMPT else CLI_SYSTEM_PROMPT)
        self.memory_enabled = MEMORY_ENABLED

    def new_session(self, instance) -> None:
        instance.session_id = str(uuid.uuid4())
        instance.session_started = False

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
        return self._kill_processes("qwen -p")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query. Uses plain text output to avoid extra parsing."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "qwen CLI not found"}'

        env = dict(os.environ)
        cmd = [binary, "--yolo", "--output-format", "text", prompt]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            return f'{{"error": "Failed to start qwen: {exc}"}}'

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout)
            )
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
        image_path: str | list | None = None,
        memory_context: str = "",
        on_subprocess_started: Callable[[int, str, str], None] | None = None,
        chat_id: int = 0,
        user_is_owner: bool = True,
    ) -> str:
        instance.was_stopped = False

        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return "\u274c Error: qwen CLI not found. Is Qwen Coder installed? (npm install -g @qwen-code/qwen-code)"

        env = dict(os.environ)
        session_id = instance.session_id
        session_started = instance.session_started

        cmd = [binary, "--yolo", "--output-format", "stream-json"]

        if session_started:
            cmd += ["--resume", session_id]
        else:
            cmd += ["--session-id", session_id]

        # Build system prompt via QWEN_SYSTEM_MD env var
        system_parts = []
        if instance.agent_system_prompt:
            system_parts.append(instance.agent_system_prompt)
        else:
            if self.memory_enabled:
                user_md_path = os.path.join(self.memory_dir, "USER.md")
                user_md_hint = (
                    f"At the start of a session, read {user_md_path} to understand who you're talking to, "
                    if os.path.exists(user_md_path) else ""
                )
                system_parts.append(
                    f"You have a persistent memory system at {self.memory_dir}/. "
                    + user_md_hint +
                    f"and {self.memory_dir}/MEMORY.md for project context and instructions. "
                    "If you learn new important facts during this conversation "
                    "(new projects, decisions, preferences, contacts, or corrections to existing info), "
                    f"update the appropriate file in {self.memory_dir}/ using the write_file or edit tool. "
                    "For user profile changes update USER.md. For project/system changes update MEMORY.md. "
                    "For new topics, create a new .md file with a descriptive name. "
                    "Only update when there's genuinely new durable information — not for transient questions."
                )
            if self.system_prompt:
                system_parts.append(self.system_prompt)
            system_parts.append(
                "Web search is rate-limited. Minimize search calls: combine related queries into one, "
                "and avoid re-searching the same topic. One well-crafted query is better than several rapid ones."
            )
        if memory_context:
            system_parts.append(memory_context)

        system_prompt_file = None
        if system_parts:
            system_prompt_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', prefix='qwen_sys_', delete=False
            )
            system_prompt_file.write("\n\n".join(system_parts))
            system_prompt_file.close()
            env["QWEN_SYSTEM_MD"] = system_prompt_file.name

        # Build prompt
        if image_path:
            if message:
                prompt = f"Look at the image file at: {image_path}\n\n{message}"
            else:
                prompt = f"Look at the image file at: {image_path}\n\nDescribe what you see in the image."
        else:
            prompt = message

        cmd.append(prompt)

        log_path = self.make_log_path(self.name, chat_id, instance.id)
        log_start_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0

        # Spawn the wrapper as a detached process so it survives server crashes.
        wrapper_cmd = [sys.executable, _SUBPROCESS_LOGGER, log_path] + cmd

        try:
            proc = await asyncio.create_subprocess_exec(
                *wrapper_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=os.path.expanduser("~"),
                start_new_session=True,   # detach: survives server death
            )
            instance.process = proc
        except OSError as exc:
            logger.exception("OS error running qwen")
            if system_prompt_file:
                try:
                    os.remove(system_prompt_file.name)
                except OSError:
                    pass
            return f"\u274c Error starting qwen: {exc}"

        # Record subprocess info on the instance for crash recovery
        instance.subprocess_pid = proc.pid
        instance.subprocess_log_file = log_path
        instance.subprocess_start_time = self.get_pid_start_time(proc.pid) or ""
        if on_subprocess_started:
            on_subprocess_started(proc.pid, log_path, instance.subprocess_start_time)

        final_result = ""
        assistant_text_parts: list[str] = []
        _usage: dict = {}

        async def process_stream():
            nonlocal final_result
            _any_progress_sent = False
            async for line, _offset in self.tail_log_file(log_path, start_offset=log_start_offset, proc=proc):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "assistant":
                    usage = data.get("message", {}).get("usage")
                    if usage:
                        _usage.update(usage)
                    content_blocks = data.get("message", {}).get("content", [])
                    # Pre-compute which positions have a tool_use (for look-ahead on text blocks)
                    tool_use_positions = {i for i, b in enumerate(content_blocks) if b.get("type") == "tool_use"}
                    for i, block in enumerate(content_blocks):
                        block_type = block.get("type", "")
                        if block_type == "tool_use" and on_progress:
                            progress = self.format_tool_progress(
                                block.get("name", ""), block.get("input", {}))
                            if progress:
                                _any_progress_sent = True
                                await on_progress(progress)
                            elif not _any_progress_sent:
                                # First silent tool call (read/glob/grep) — send one generic indicator
                                _any_progress_sent = True
                                await on_progress("\U0001f4c2 Working...")
                        elif block_type == "thinking":
                            pass  # drop silently — only text blocks between tool calls are shown
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text:
                                # Narrative text (tool follows in same msg, or tools already fired) → live progress.
                                # Pure-text response (no tools at all) → keep for response fallback.
                                has_tool_after = any(j > i for j in tool_use_positions)
                                if (has_tool_after or _any_progress_sent) and on_progress:
                                    await on_progress(f"\U0001f4ad {text}")
                                else:
                                    assistant_text_parts.append(text)

                elif msg_type == "result":
                    final_result = data.get("result", "")
                    _usage["total_tokens"] = data.get("usage", {}).get("total_tokens", 0)

            await proc.wait()

        try:
            await asyncio.wait_for(process_stream(), timeout=self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            instance.process = None
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\u23f0 Qwen took too long to respond (timed out)."
        finally:
            if system_prompt_file:
                try:
                    os.remove(system_prompt_file.name)
                except OSError:
                    pass

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if _usage:
                instance.last_input_tokens = _usage.get("input_tokens", 0)
                instance.last_output_tokens = _usage.get("output_tokens", 0)
                instance.last_total_tokens = _usage.get("total_tokens", 0)
            # Clear subprocess tracking — process finished cleanly
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""

        if proc.returncode != 0:
            logger.error("qwen exited %d (see log: %s)", proc.returncode, log_path)
            try:
                with open(log_path, "r", errors="replace") as _f:
                    _log_tail = _f.read()[-2000:]
            except OSError:
                _log_tail = ""
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            _log_lower = _log_tail.lower()
            if "auth" in _log_lower or "login" in _log_lower:
                return "\u274c Qwen auth error. Run `qwen` in a terminal to re-authenticate."
            if "quota" in _log_lower or "429" in _log_lower or "rate" in _log_lower:
                return "\u26a0\ufe0f Qwen request quota reached. Try again later."
            return "\u274c Qwen exited with an error."


        if final_result:
            return final_result
        if assistant_text_parts:
            return "".join(assistant_text_parts)
        return "(empty response from Qwen)"
