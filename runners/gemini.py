"""Gemini CLI runner adapter.

Subprocess: gemini -p --yolo --output-format stream-json
Session: UUID from init event, --resume <id>
System prompt: writes temp .md file, sets GEMINI_SYSTEM_MD env var
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from typing import Callable, Awaitable

from runners.base import RunnerBase, _SUBPROCESS_LOGGER

logger = logging.getLogger("bridge.gemini")


class GeminiRunner(RunnerBase):
    name = "gemini"
    cli_command = "gemini"

    def new_session(self, instance) -> None:
        instance.session_started = False
        instance.session_id = None

    async def kill_all(self) -> int:
        return self._kill_processes("gemini -p")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query via gemini CLI."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "gemini CLI not found"}'

        env = self.build_env(dict(os.environ), True)
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

        try:
            stdout_data, stderr_data = await RunnerBase.read_with_timeout(proc, float(timeout))
        except asyncio.TimeoutError:
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

        return self.format_query_result(text_parts, None, stderr_data)

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

        session_started = instance.session_started
        resume_id = instance.session_id if session_started else None

        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return "\u274c Error: gemini CLI not found. Is Gemini CLI installed?"

        env = self.build_env(dict(os.environ), user_is_owner)
        system_prompt_file = None

        # Build system prompt
        system_parts = self.build_system_prompt(instance, memory_context, memory_tool_names="edit_file or write_new_file")

        if system_parts:
            system_prompt_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.md', prefix='gemini_sys_', delete=False
            )
            system_prompt_file.write("\n\n".join(system_parts))
            system_prompt_file.close()
            env["GEMINI_SYSTEM_MD"] = system_prompt_file.name

        # Build prompt (normalize list → first image)
        if isinstance(image_path, list):
            image_path = image_path[0] if image_path else None
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
            logger.exception("OS error running gemini")
            if system_prompt_file:
                try:
                    os.remove(system_prompt_file.name)
                except OSError:
                    pass
            return f"\u274c Error starting gemini: {exc}"

        # Record subprocess info on the instance for crash recovery
        instance.subprocess_pid = proc.pid
        instance.subprocess_log_file = log_path
        instance.subprocess_start_time = self.get_pid_start_time(proc.pid) or ""
        if on_subprocess_started:
            on_subprocess_started(proc.pid, log_path, instance.subprocess_start_time)

        assistant_text_parts: list[str] = []
        captured_session_id: str | None = None
        _usage = {"input": 0, "output": 0, "total": 0}

        async def process_stream():
            nonlocal captured_session_id
            async for line, _offset in self.tail_log_file(log_path, start_offset=log_start_offset, proc=proc):
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

                elif msg_type == "tool_use":
                    if on_progress:
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
            self._clear_subprocess_info(instance)
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
            self._clear_subprocess_info(instance)
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if captured_session_id:
                instance.session_id = captured_session_id
            if _usage["total"]:
                instance.last_input_tokens = _usage["input"]
                instance.last_output_tokens = _usage["output"]
                instance.last_total_tokens = _usage["total"]
            # Clear subprocess tracking — process finished cleanly
            self._clear_subprocess_info(instance)

        if proc.returncode != 0:
            logger.error("gemini exited %d (see log: %s)", proc.returncode, log_path)
            # Check log for quota-related error indicators
            try:
                with open(log_path, "r", errors="replace") as _f:
                    _log_tail = _f.read()[-2000:]
            except OSError:
                _log_tail = ""
            self._clear_subprocess_info(instance)
            _log_lower = _log_tail.lower()
            if any(ind in _log_lower for ind in [
                "terminalquotaerror", "resource_exhausted", "quota", "429", "too many requests"
            ]):
                return "\u26a0\ufe0f Gemini API quota exhausted. Wait a few minutes and try again."
            return "\u274c Gemini exited with an error."

        if assistant_text_parts:
            return "".join(assistant_text_parts)
        return "(empty response from Gemini)"
