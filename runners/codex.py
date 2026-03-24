"""OpenAI Codex CLI runner adapter.

Subprocess: codex exec --json --dangerously-bypass-approvals-and-sandbox
Session: thread_id-based (codex exec resume <thread_id>)
System prompt: prepended to prompt text (no CLI flag support)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import date
from typing import Callable, Awaitable

from runners.base import RunnerBase, _SUBPROCESS_LOGGER

logger = logging.getLogger("bridge.codex")


class CodexRunner(RunnerBase):
    name = "codex"
    cli_command = "codex"

    def new_session(self, instance) -> None:
        instance.session_started = False
        instance.adapter_data.pop("thread_id", None)

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
        return self._kill_processes("codex exec")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query via codex CLI."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "codex CLI not found"}'

        env = dict(os.environ)
        cmd = [
            binary, "exec", prompt,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            return f'{{"error": "Failed to start codex: {exc}"}}'

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

        # Parse JSONL for agent_message items
        text_parts = []
        for line in stdout_data.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "item.completed":
                    item = data.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text", "")
                        if text:
                            text_parts.append(text)
            except json.JSONDecodeError:
                continue

        if text_parts:
            return "".join(text_parts)

        err = stderr_data.decode(errors="replace").strip()
        if err:
            return f"[stderr] {err}"
        return "(no response)"

    def _format_codex_progress(self, item: dict) -> str:
        """Format a codex JSONL item into a progress string."""
        item_type = item.get("type", "")

        if item_type == "command_execution":
            cmd = item.get("command", "")
            # Don't show direct Telegram API calls — Codex shouldn't send messages itself
            if "api.telegram.org" in cmd:
                return ""
            for prefix in (
                "/bin/zsh -lc '", '/bin/zsh -lc "',
                "/bin/sh -lc '", '/bin/sh -lc "',
                "cmd /c \"", "cmd /c '",
            ):
                if cmd.startswith(prefix):
                    cmd = cmd[len(prefix):]
                    if cmd.endswith("'") or cmd.endswith('"'):
                        cmd = cmd[:-1]
                    break
            cmd = cmd.strip()
            if not cmd:
                return "\u26a1 Running command..."
            return f"\u26a1 {cmd[:120]}"

        if item_type == "function_call":
            name = item.get("name", "")
            raw_args = item.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args_dict = json.loads(raw_args)
                except Exception:
                    args_dict = {}
            else:
                args_dict = raw_args if isinstance(raw_args, dict) else {}

            if name in ("shell", "bash", "exec_command"):
                cmd = args_dict.get("command", args_dict.get("cmd", ""))
                return f"\u26a1 {cmd[:120]}" if cmd else "\u26a1 Shell"
            elif name in ("write_file", "apply_patch"):
                return f"\U0001f4dd Write: {args_dict.get('path', '')}"
            elif name == "read_file":
                return f"\U0001f4d6 Read: {args_dict.get('path', '')}"
            elif name in ("web.run", "browser_search"):
                return f"\U0001f50d Web: {str(args_dict)[:100]}"
            elif name == "view_image":
                return f"\U0001f441\ufe0f Image: {args_dict.get('path', '')}"
            elif name:
                return f"\U0001f527 {name}"

        if item_type == "reasoning":
            return ""  # thinking mode removed — drop silently

        return ""

    def _build_memory_prefix(self, is_initial: bool, memory_context: str) -> str:
        """Build the memory/context block to prepend to the user's prompt."""
        parts = []

        if self.memory_enabled and is_initial:
            injected = []
            today_fname = date.today().strftime("%Y-%m-%d") + ".md"
            daily_fname = os.path.join("Daily", today_fname)
            for fname in ("USER.md", "MEMORY.md", daily_fname):
                fpath = os.path.join(self.memory_dir, fname)
                try:
                    with open(fpath, "rb") as f_bin:
                        content = f_bin.read().decode("utf-8", errors="replace").strip()
                    if content:
                        if len(content) > 12000:
                            content = content[:12000] + "\n... (truncated)"
                        injected.append(f"=== {fname} ===\n{content}")
                except OSError:
                    pass

            if injected:
                parts.append(
                    f"[MEMORY — auto-loaded from {self.memory_dir}]\n\n"
                    + "\n\n".join(injected)
                )

        if self.system_prompt and is_initial:
            parts.append(f"[SYSTEM]\n{self.system_prompt}")

        if memory_context:
            parts.append(memory_context)

        return "\n\n".join(parts)

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
        thread_id = instance.adapter_data.get("thread_id") if session_started else None
        is_initial = not bool(thread_id)

        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return "\u274c Error: codex CLI not found. Is Codex CLI installed?"

        # Build the base message (normalize list → first image)
        if isinstance(image_path, list):
            image_path = image_path[0] if image_path else None
        if image_path:
            base_message = (f"Look at the image file at: {image_path}\n\n{message}"
                            if message else
                            f"Look at the image file at: {image_path}\n\nDescribe what you see.")
        else:
            base_message = message

        # Prepend memory context to the prompt
        memory_prefix = self._build_memory_prefix(is_initial, memory_context)
        if memory_prefix:
            full_prompt = f"{memory_prefix}\n\n---\n\n{base_message}"
        else:
            full_prompt = base_message

        env = dict(os.environ)

        base_flags = [
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-c", "shell_environment_policy.inherit=all",
        ]

        from pathlib import Path
        init_flags = ["-C", str(Path.home())] + base_flags

        if thread_id:
            cmd = [binary, "exec", "resume", thread_id, full_prompt] + base_flags
        else:
            cmd = [binary, "exec", full_prompt] + init_flags

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
            logger.exception("OS error running codex")
            return f"\u274c Error starting codex: {exc}"

        # Record subprocess info on the instance for crash recovery
        instance.subprocess_pid = proc.pid
        instance.subprocess_log_file = log_path
        instance.subprocess_start_time = self.get_pid_start_time(proc.pid) or ""
        if on_subprocess_started:
            on_subprocess_started(proc.pid, log_path, instance.subprocess_start_time)

        assistant_text_parts: list[str] = []
        captured_thread_id: str | None = None
        _usage = {"input": 0, "output": 0}
        # Pending agent_message: held until next agent_message or end-of-turn.
        # Intermediate messages (planning/status) are flushed as progress; only
        # the last agent_message is kept as the final response.
        _pending_agent_msg: str = ""

        async def _flush_pending_as_progress():
            nonlocal _pending_agent_msg
            if _pending_agent_msg and on_progress:
                await on_progress(f"\U0001f4ad {_pending_agent_msg}")
            _pending_agent_msg = ""

        async def process_stream():
            nonlocal captured_thread_id, _pending_agent_msg
            async for line, _offset in self.tail_log_file(log_path, start_offset=log_start_offset, proc=proc):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type", "")

                if event_type == "thread.started":
                    tid = data.get("thread_id")
                    if tid:
                        captured_thread_id = tid

                elif event_type == "item.started":
                    item = data.get("item", {})
                    if item.get("type") == "command_execution" and on_progress:
                        progress = self._format_codex_progress(item)
                        if progress:
                            await on_progress(progress)

                elif event_type == "item.completed":
                    item = data.get("item", {})
                    item_type = item.get("type", "")
                    if item_type == "agent_message":
                        text = item.get("text", "")
                        if text:
                            # Flush previous agent_message as progress (it was intermediate)
                            await _flush_pending_as_progress()
                            _pending_agent_msg = text
                    elif on_progress:
                        progress = self._format_codex_progress(item)
                        if progress:
                            await on_progress(progress)

                elif event_type == "turn.completed":
                    usage = data.get("usage", {})
                    _usage["input"] = usage.get("input_tokens", 0)
                    _usage["output"] = usage.get("output_tokens", 0)

            # Collect the final (last) agent_message as the response
            if _pending_agent_msg:
                assistant_text_parts.append(_pending_agent_msg)

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
            return "\u23f0 Codex took too long to respond (timed out)."

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if captured_thread_id:
                instance.adapter_data["thread_id"] = captured_thread_id
                instance.session_id = captured_thread_id  # mirror into session_id for crash recovery
            if _usage["input"] or _usage["output"]:
                instance.last_input_tokens = _usage["input"]
                instance.last_output_tokens = _usage["output"]
                instance.last_total_tokens = _usage["input"] + _usage["output"]
            # Clear subprocess tracking — process finished cleanly
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""

        if proc.returncode != 0:
            logger.error("codex exited %d (see log: %s)", proc.returncode, log_path)
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\u274c Codex exited with an error."

        if assistant_text_parts:
            return "".join(assistant_text_parts)
        return "(empty response from Codex)"
