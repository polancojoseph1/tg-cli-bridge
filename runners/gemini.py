"""Gemini CLI runner adapter.

Subprocess: gemini -p --yolo --output-format stream-json
Session: UUID from init event, --resume <id>
System prompt: writes temp .md file, sets GEMINI_SYSTEM_MD env var
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Callable, Awaitable

from runners.base import RunnerBase

logger = logging.getLogger("bridge.gemini")


class GeminiRunner(RunnerBase):
    name = "gemini"
    cli_command = "gemini"

    def __init__(self):
        from config import CLI_TIMEOUT, CLI_SYSTEM_PROMPT, MEMORY_DIR, MEMORY_ENABLED
        self.timeout = CLI_TIMEOUT
        self.system_prompt = CLI_SYSTEM_PROMPT
        self.memory_dir = MEMORY_DIR
        self.memory_enabled = MEMORY_ENABLED

    def new_session(self, instance) -> None:
        instance.session_started = False
        instance.session_id = None

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
        return self._kill_processes("gemini -p")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query via gemini CLI."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "gemini CLI not found"}'

        env = dict(os.environ)
        cmd = [binary, "--yolo", "--output-format", "stream-json", "-p", prompt]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            return f'{{"error": "Failed to start gemini: {exc}"}}'

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

        # Parse stream-json output for text
        text_parts = []
        for line in stdout_data.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "message" and data.get("role") == "assistant":
                    content = data.get("content", "")
                    if content:
                        text_parts.append(content)
            except json.JSONDecodeError:
                continue

        if text_parts:
            return "".join(text_parts)

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

        session_started = instance.session_started
        resume_id = instance.session_id if session_started else None

        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return "\u274c Error: gemini CLI not found. Is Gemini CLI installed?"

        env = dict(os.environ)
        system_prompt_file = None

        # Build system prompt
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
                    f"update the appropriate file in {self.memory_dir}/ using the edit_file or write_new_file tool. "
                    "For user profile changes update USER.md. For project/system changes update MEMORY.md. "
                    "For new topics, create a new .md file with a descriptive name. "
                    "Only update when there's genuinely new durable information — not for transient questions."
                )
            if self.system_prompt:
                system_parts.append(self.system_prompt)
        if memory_context:
            system_parts.append(memory_context)

        if system_parts:
            system_prompt_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', prefix='gemini_sys_', delete=False
            )
            system_prompt_file.write("\n\n".join(system_parts))
            system_prompt_file.close()
            env["GEMINI_SYSTEM_MD"] = system_prompt_file.name

        # Build prompt
        if image_path:
            if message:
                prompt = f"Look at the image file at: {image_path}\n\n{message}"
            else:
                prompt = f"Look at the image file at: {image_path}\n\nDescribe what you see in the image."
        else:
            prompt = message

        cmd = [binary, "--yolo", "--output-format", "stream-json"]
        if resume_id:
            cmd += ["--resume", resume_id]
        cmd += ["-p", prompt]

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
            logger.exception("OS error running gemini")
            if system_prompt_file:
                try:
                    os.remove(system_prompt_file.name)
                except OSError:
                    pass
            return f"\u274c Error starting gemini: {exc}"

        assistant_text_parts: list[str] = []
        stderr_output = b""
        captured_session_id: str | None = None
        _usage = {"input": 0, "output": 0, "total": 0}

        async def drain_stderr():
            nonlocal stderr_output
            stderr_output = await proc.stderr.read()

        async def process_stdout():
            nonlocal captured_session_id
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "init":
                    sid = data.get("session_id")
                    if sid:
                        captured_session_id = sid

                elif msg_type == "tool_use" and on_progress:
                    progress = self.format_tool_progress(
                        data.get("tool_name", ""), data.get("parameters", {}))
                    if progress:
                        await on_progress(progress)

                elif msg_type == "message":
                    role = data.get("role", "")
                    content = data.get("content", "")
                    if role == "assistant" and content:
                        assistant_text_parts.append(content)

                elif msg_type == "result":
                    stats = data.get("stats", {})
                    if stats:
                        _usage["input"] = stats.get("input_tokens", 0)
                        _usage["output"] = stats.get("output_tokens", 0)
                        _usage["total"] = stats.get("total_tokens", 0)

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
            return "\u23f0 Gemini took too long to respond (timed out)."
        finally:
            if system_prompt_file:
                try:
                    os.remove(system_prompt_file.name)
                except OSError:
                    pass

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if captured_session_id:
                instance.session_id = captured_session_id
            if _usage["total"]:
                instance.last_input_tokens = _usage["input"]
                instance.last_output_tokens = _usage["output"]
                instance.last_total_tokens = _usage["total"]

        if proc.returncode != 0:
            err = stderr_output.decode(errors="replace").strip()
            logger.error("gemini exited %d: %s", proc.returncode, err)
            err_lower = err.lower()
            if any(ind in err_lower for ind in [
                "terminalquotaerror", "resource_exhausted", "quota", "429", "too many requests"
            ]):
                return "\u26a0\ufe0f Gemini API quota exhausted. Wait a few minutes and try again."
            return f"\u274c Gemini error:\n{err}" if err else "\u274c Gemini exited with an error."

        if assistant_text_parts:
            return "".join(assistant_text_parts)
        return "(empty response from Gemini)"
