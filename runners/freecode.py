"""FreeCode CLI runner adapter.

Subprocess: freecode run --format json --session <id> "prompt"
Session: freecode-assigned session IDs (ses_...), --continue for subsequent calls
System prompt: prepended to user message (freecode has no --append-system-prompt flag)
Output format: NDJSON events — step_start, text, tool_use, step_finish, error
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Callable, Awaitable

from runners.base import RunnerBase, _SUBPROCESS_LOGGER

logger = logging.getLogger("bridge.freecode")


class FreeCodeBaseRunner(RunnerBase):
    name = "freecode"
    cli_command = "freecode"

    def __init__(self):
        from config import CLI_TIMEOUT, CLI_SYSTEM_PROMPT, MEMORY_DIR, MEMORY_ENABLED, USER_NAME
        self.timeout = CLI_TIMEOUT
        self.memory_dir = MEMORY_DIR
        self.system_prompt = (CLI_SYSTEM_PROMPT.replace("{MEMORY_DIR}", MEMORY_DIR).replace("{OWNER_NAME}", USER_NAME or "the user") if CLI_SYSTEM_PROMPT else CLI_SYSTEM_PROMPT)
        self.memory_enabled = MEMORY_ENABLED

    def discover_binary(self) -> str:
        """Use FREECODE_BIN_PATH if set, otherwise fall back to PATH lookup."""
        import shutil
        custom = os.environ.get("FREECODE_BIN_PATH")
        if custom:
            expanded = os.path.expanduser(custom)
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
            raise FileNotFoundError(f"FREECODE_BIN_PATH={custom!r} not found or not executable")
        path = shutil.which(self.cli_command)
        if path is None:
            raise FileNotFoundError(
                f"{self.cli_command} CLI not found in PATH. Install from https://github.com/polancojoseph1/freecode"
            )
        return path

    def new_session(self, instance) -> None:
        instance.session_id = ""  # opencode assigns its own ses_... IDs
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
        return self._kill_processes("freecode run")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query. No session, no memory, no progress."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "freecode CLI not found"}'

        env = dict(os.environ)
        cmd = [binary, "run", "--format", "json", prompt]

        stdout_data, stderr_data, err_msg = await self._run_cmd_with_timeout(
            cmd, float(timeout), env, "freecode"
        )
        if err_msg:
            return err_msg

        # Parse NDJSON output — collect all text events
        text_parts = []
        for line in stdout_data.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "text":
                text = data.get("part", {}).get("text", "")
                if text:
                    text_parts.append(text)
            elif data.get("type") == "error":
                err_msg = data.get("error", {}).get("data", {}).get("message", "")
                if err_msg:
                    return f"[error] {err_msg}"

        if text_parts:
            return "\n\n".join(text_parts)
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
            return "\u274c Error: freecode CLI not found. Install from https://github.com/polancojoseph1/freecode"

        env = dict(os.environ)
        session_started = instance.session_started

        # Per-instance OpenRouter credentials (Bridge Cloud per-user keys)
        bc_or_key = getattr(instance, "bc_openrouter_key", None)
        if bc_or_key:
            # Freecode's openrouter provider reads OPENROUTER_API_KEY (not OPENAI_*)
            env["OPENROUTER_API_KEY"] = bc_or_key
            logger.debug("[freecode] Using per-instance OpenRouter key for Bridge Cloud")

        # Determine model — instance.model or env override
        model = getattr(instance, "model", None) or os.environ.get("FREECODE_MODEL", "")

        cmd = [binary, "run", "--format", "json"]

        # Custom fork: pass --steps if FREECODE_MAX_STEPS is set
        max_steps = os.environ.get("FREECODE_MAX_STEPS")
        if max_steps:
            cmd += ["--steps", max_steps]

        if model:
            # Freecode uses "providerID/modelID" format. OpenRouter models from v1_api.py
            # are "org/model" style — must be prefixed with "openrouter/" so freecode
            # routes them through the openrouter provider, not a non-existent org provider.
            if bc_or_key and not model.startswith("openrouter/"):
                fc_model = f"openrouter/{model}"
            else:
                fc_model = model
            cmd += ["-m", fc_model]

        # Session continuity
        if session_started and instance.session_id:
            cmd += ["--session", instance.session_id]

        # Build the prompt with system context prepended
        # (freecode doesn't have --append-system-prompt, so we prepend to the message)
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
                    + user_md_hint
                    + f"and {self.memory_dir}/MEMORY.md for project context and instructions. "
                    "If you learn new important facts during this conversation "
                    "(new projects, decisions, preferences, contacts, or corrections to existing info), "
                    f"update the appropriate file in {self.memory_dir}/ using the edit or write tool. "
                    "For user profile changes update USER.md. For project/system changes update MEMORY.md. "
                    "For new topics, create a new .md file with a descriptive name. "
                    "Only update when there's genuinely new durable information — not for transient questions."
                )
            if self.system_prompt:
                system_parts.append(self.system_prompt)
        if memory_context:
            system_parts.append(memory_context)

        # Build prompt
        if image_path:
            if message:
                prompt = f"Look at the image file at: {image_path}\n\n{message}"
            else:
                prompt = f"Look at the image file at: {image_path}\n\nDescribe what you see in the image."
        else:
            prompt = message

        # Prepend system context to the first message of a session
        if system_parts and not session_started:
            prompt = "[System Instructions]\n" + "\n\n".join(system_parts) + "\n\n[User Message]\n" + prompt

        cmd.append(prompt)

        log_path = self.make_log_path(self.name, chat_id, instance.id)
        log_start_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0

        # Spawn via subprocess logger wrapper (detached, survives server crashes)
        wrapper_cmd = [sys.executable, _SUBPROCESS_LOGGER, log_path] + cmd

        try:
            proc = await asyncio.create_subprocess_exec(
                *wrapper_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=os.path.expanduser("~"),
                start_new_session=True,  # detach: survives server death
            )
            instance.process = proc
        except OSError as exc:
            logger.exception("OS error running freecode")
            return f"\u274c Error starting freecode: {exc}"

        # Record subprocess info for crash recovery
        instance.subprocess_pid = proc.pid
        instance.subprocess_log_file = log_path
        instance.subprocess_start_time = self.get_pid_start_time(proc.pid) or ""
        if on_subprocess_started:
            on_subprocess_started(proc.pid, log_path, instance.subprocess_start_time)

        final_text_parts: list[str] = []  # noqa: F841
        _pending_text: str = ""
        _usage: dict = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        _captured_session_id: str = ""
        _session_corrupt: bool = False
        _last_progress_time: list[float] = [time.monotonic()]  # mutable for closure

        async def _flush_text_as_progress():
            nonlocal _pending_text
            if _pending_text and on_progress:
                text = _pending_text
                # Strip everything from the first code block onward — keep only prose
                for fence in ("```", "~~~"):
                    if fence in text:
                        text = text[:text.index(fence)]
                text = text.strip()
                if text:
                    await on_progress(f"\U0001f4ad {text}")
            _pending_text = ""

        async def process_stream():
            nonlocal _captured_session_id, _pending_text, _session_corrupt
            _any_progress_sent = False

            async for line, _offset in self.tail_log_file(
                log_path, start_offset=log_start_offset, proc=proc
            ):
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type", "")

                # Capture session ID from any event
                sid = data.get("sessionID", "")
                if sid and not _captured_session_id:
                    _captured_session_id = sid

                if event_type == "text":
                    text = data.get("part", {}).get("text", "")
                    if text:
                        # Flush previous text as progress, hold this one pending
                        await _flush_text_as_progress()
                        _pending_text = text

                elif event_type == "tool_use":
                    # Flush any pending narrative text before tool progress
                    await _flush_text_as_progress()
                    part = data.get("part", {})
                    state = part.get("state", {})
                    tool_name = part.get("tool", "")
                    tool_input = state.get("input", {})
                    title = state.get("title", "")

                    if on_progress:
                        progress = self._format_freecode_tool(tool_name, tool_input, title)
                        if progress:
                            _any_progress_sent = True
                            _last_progress_time[0] = time.monotonic()
                            await on_progress(progress)
                        elif not _any_progress_sent:
                            _any_progress_sent = True
                            _last_progress_time[0] = time.monotonic()
                            await on_progress("\U0001f4c2 Working...")

                elif event_type == "step_finish":
                    part = data.get("part", {})
                    tokens = part.get("tokens", {})
                    if tokens:
                        _usage["input_tokens"] += tokens.get("input", 0)
                        _usage["output_tokens"] += tokens.get("output", 0)
                    _usage["cost"] += part.get("cost", 0.0)

                elif event_type == "error":
                    err_data = data.get("error", {})
                    err_name = err_data.get("name", "Error")
                    err_msg = err_data.get("data", {}).get("message", str(err_data))
                    logger.error("[freecode] %s: %s", err_name, err_msg)
                    # Detect Mistral/LiteLLM tool call mismatch — session must be reset
                    _corrupt_keywords = ("invalid_request_message_order", "not the same number of function calls")
                    if any(k in err_msg.lower() for k in _corrupt_keywords):
                        _session_corrupt = True
                    elif on_progress:
                        await on_progress(f"\u274c {err_name}: {err_msg[:200]}")
                    else:
                        # No progress callback (Bridge Cloud path) — surface error as response
                        _pending_text = f"\u274c {err_name}: {err_msg[:200]}"

            await proc.wait()

        _KEEPALIVE_INTERVAL = 180  # seconds between "still working" pings

        async def _keepalive():
            """Send a periodic heartbeat when no progress has been sent for a while."""
            while True:
                await asyncio.sleep(30)
                if on_progress and time.monotonic() - _last_progress_time[0] >= _KEEPALIVE_INTERVAL:
                    _last_progress_time[0] = time.monotonic()
                    await on_progress("\u23f3 Still working...")

        _keepalive_task = asyncio.create_task(_keepalive())
        try:
            await asyncio.wait_for(process_stream(), timeout=self.timeout)
        except asyncio.TimeoutError:
            _keepalive_task.cancel()
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            instance.process = None
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\u23f0 FreeCode took too long to respond (timed out)."
        finally:
            _keepalive_task.cancel()

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if _captured_session_id:
                instance.session_id = _captured_session_id
            instance.last_input_tokens = _usage.get("input_tokens", 0)
            instance.last_output_tokens = _usage.get("output_tokens", 0)
            instance.session_cost += _usage.get("cost", 0.0)
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""

        if proc.returncode != 0:
            logger.error("freecode exited %d (see log: %s)", proc.returncode, log_path)
            try:
                with open(log_path, "r", errors="replace") as _f:
                    _log_tail = _f.read()[-2000:]
            except OSError:
                _log_tail = ""
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            _log_lower = _log_tail.lower()
            if "auth" in _log_lower or "unauthorized" in _log_lower:
                return "\u274c FreeCode auth error. Check your API keys or run `freecode` to configure."
            if "session" in _log_lower:
                self.new_session(instance)
                return "\u274c Session error. New conversation started \u2014 please resend your message."
            return "\u274c FreeCode exited with an error."

        # Corrupt session: tool call / response mismatch rejected by Mistral
        if _session_corrupt:
            self.new_session(instance)
            return "\u26a0\ufe0f Session had corrupt tool call history (Mistral rejected it). Session has been reset \u2014 please resend your message."

        if _pending_text:
            return _pending_text
        return "(empty response from FreeCode)"

    @staticmethod
    def _format_freecode_tool(tool_name: str, tool_input: dict, title: str) -> str:
        """Format a freecode tool call into a human-readable progress string.

        FreeCode events use camelCase keys (filePath, dirPath) while some tools
        use snake_case. We check both to ensure nothing is silently dropped.
        """
        name_lower = tool_name.lower()

        def _get(*keys: str) -> str:
            for k in keys:
                v = tool_input.get(k, "")
                if v:
                    return str(v)
            return ""

        if name_lower in ("bash", "shell", "run_shell_command", "exec_command"):
            cmd = _get("command", "cmd")
            desc = title or cmd[:200]
            return f"\u26a1 Shell: {desc}" if desc else "\u26a1 Shell"
        elif name_lower in ("edit", "edit_file", "write", "write_file", "apply_patch"):
            path = _get("filePath", "file_path", "path")
            return f"\u270f\ufe0f Edit: {path}" if path else f"\u270f\ufe0f {title or tool_name}"
        elif name_lower in ("read", "read_file"):
            path = _get("filePath", "file_path", "path")
            return f"\U0001f4c4 Read: {path}"  if path else "\U0001f4c4 Read"
        elif name_lower in ("list_directory", "ls", "glob"):
            target = _get("pattern", "dirPath", "dir_path", "path")
            return f"\U0001f4c2 Glob: {target}" if target else "\U0001f4c2 List"
        elif name_lower in ("grep", "grep_search", "search"):
            query = _get("query", "pattern", "regex")
            return f"\U0001f50d Grep: {query[:80]}" if query else "\U0001f50d Search"
        elif name_lower in ("web_fetch", "fetch"):
            url = _get("url")
            return f"\U0001f310 Fetch: {url}" if url else "\U0001f310 Fetch"
        elif name_lower in ("web_search", "google_web_search"):
            query = _get("query", "search_query")
            return f"\U0001f50e Search: {query}" if query else "\U0001f50e Web search"
        elif name_lower in ("question", "ask", "ask_followup_question", "ask_user"):
            return ""  # interactive question — not useful as progress
        elif tool_name:
            return f"\U0001f527 {title or tool_name}"
        return ""
