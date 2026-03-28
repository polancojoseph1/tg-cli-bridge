"""Antigravity runner — wraps opencode with Antigravity-flavored defaults.

Subprocess: opencode run --format json -m <model> "prompt"
Session: opencode ses_... IDs, --session for continuation
System prompt: prepended to first user message
Output format: NDJSON events — same as freecode/opencode

Models (set via /model command):
  pro / high / g3p     → google/gemini-3-pro-preview
  flash / low / g3f    → google/gemini-3-flash-preview
  sonnet               → openrouter/anthropic/claude-sonnet-4.6
  opus                 → openrouter/anthropic/claude-opus-4.6
  gpt / gpt-oss        → groq/openai/gpt-oss-120b

Auth: one-time setup required — run in terminal:
  OPENCODE_CONFIG_DIR=~/.jefe/antigravity-opencode opencode auth login
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Callable, Awaitable

from runners.base import RunnerBase, _SUBPROCESS_LOGGER

logger = logging.getLogger("bridge.antigravity")

# Map /model shortcut → full model ID
_AG_MODELS: dict[str, str] = {
    "pro":    "google/gemini-3-pro-preview",
    "high":   "google/gemini-3-pro-preview",
    "g3p":    "google/gemini-3-pro-preview",
    "gemini": "google/gemini-3-pro-preview",
    "flash":  "google/gemini-3-flash-preview",
    "low":    "google/gemini-3-flash-preview",
    "g3f":    "google/gemini-3-flash-preview",
    "sonnet": "openrouter/anthropic/claude-sonnet-4.6",
    "claude": "openrouter/anthropic/claude-sonnet-4.6",
    "opus":   "openrouter/anthropic/claude-opus-4.6",
    "gpt":    "groq/openai/gpt-oss-120b",
    "gpt-oss":"groq/openai/gpt-oss-120b",
}

# Where the Antigravity-specific opencode config + auth tokens live
_AG_CONFIG_DIR = os.path.expanduser(
    os.environ.get("AG_OPENCODE_CONFIG_DIR", "~/.jefe/antigravity-opencode")
)


class AntigravityRunner(RunnerBase):
    name = "antigravity"
    cli_command = "opencode"

    def discover_binary(self) -> str:
        import shutil
        custom = os.environ.get("OPENCODE_BIN_PATH")
        if custom:
            expanded = os.path.expanduser(custom)
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
        path = shutil.which("opencode")
        if path is None:
            raise FileNotFoundError("opencode CLI not found in PATH")
        return path

    def resolve_model(self, alias: str) -> str | None:
        """Resolve a user-supplied alias to a full Antigravity model ID."""
        lower = alias.lower().strip()
        if lower in _AG_MODELS:
            return _AG_MODELS[lower]
        # Accept full provider/model names directly
        if "/" in alias:
            return alias
        return None

    @property
    def model_shortcuts(self) -> str:
        return "pro, flash, sonnet, opus, gpt-oss"

    def new_session(self, instance) -> None:
        instance.session_id = ""
        instance.session_started = False
        instance.session_cost = 0.0

    def is_available(self) -> bool:
        try:
            self.discover_binary()
            return True
        except FileNotFoundError:
            return False

    async def kill_all(self) -> int:
        return self._kill_processes("opencode run")

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return '{"error": "opencode CLI not found"}'

        env = self._build_ag_env(dict(os.environ), True)
        model = os.environ.get("AG_MODEL", "google/gemini-3-flash-preview")
        cmd = [binary, "run", "--format", "json"]
        if model:
            cmd += ["-m", model]
        cmd.append(prompt)

        stdout_data, stderr_data, err_msg = await self._run_cmd_with_timeout(
            cmd, float(timeout), env, "antigravity"
        )
        if err_msg:
            return err_msg

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
                err = data.get("error", {}).get("data", {}).get("message", "")
                if err:
                    return f"[error] {err}"
        return self.format_query_result(text_parts, None, stderr_data, join_char="\n\n")

    def _build_ag_env(self, base_env: dict, user_is_owner: bool) -> dict:
        env = self.build_env(base_env, user_is_owner)
        env["OPENCODE_CONFIG_DIR"] = _AG_CONFIG_DIR
        env["OPENCODE_CONFIG"] = os.path.join(_AG_CONFIG_DIR, "opencode.json")
        return env

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
            return "\u274c Error: opencode CLI not found."

        env = self._build_ag_env(dict(os.environ), user_is_owner)
        session_started = instance.session_started

        # Resolve model — instance.model takes priority, then env default
        model = getattr(instance, "model", None) or os.environ.get("AG_MODEL", "google/gemini-3-flash-preview")

        cmd = [binary, "run", "--format", "json"]

        if model:
            cmd += ["-m", model]

        if session_started and instance.session_id:
            cmd += ["--session", instance.session_id]

        # Build prompt
        system_parts = self.build_system_prompt(instance, memory_context)

        if image_path:
            prompt = f"Look at the image file at: {image_path}\n\n{message}" if message else f"Look at the image file at: {image_path}\n\nDescribe what you see."
        else:
            prompt = message

        if system_parts and not session_started:
            prompt = "[System Instructions]\n" + "\n\n".join(system_parts) + "\n\n[User Message]\n" + prompt

        cmd.append(prompt)

        log_path = self.make_log_path(self.name, chat_id, instance.id)
        log_start_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0

        wrapper_cmd = [sys.executable, _SUBPROCESS_LOGGER, log_path] + cmd

        try:
            proc = await asyncio.create_subprocess_exec(
                *wrapper_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=os.path.expanduser("~"),
                start_new_session=True,
            )
            instance.process = proc
        except OSError as exc:
            logger.exception("OS error running opencode/antigravity")
            return f"\u274c Error starting Antigravity: {exc}"

        instance.subprocess_pid = proc.pid
        instance.subprocess_log_file = log_path
        instance.subprocess_start_time = self.get_pid_start_time(proc.pid) or ""
        if on_subprocess_started:
            on_subprocess_started(proc.pid, log_path, instance.subprocess_start_time)

        _pending_text: str = ""
        _usage: dict = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        _captured_session_id: str = ""
        _session_corrupt: bool = False
        _last_progress_time: list[float] = [time.monotonic()]

        async def _flush_text_as_progress():
            nonlocal _pending_text
            if _pending_text and on_progress:
                text = _pending_text
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
                sid = data.get("sessionID", "")
                if sid and not _captured_session_id:
                    _captured_session_id = sid

                if event_type == "text":
                    text = data.get("part", {}).get("text", "")
                    if text:
                        await _flush_text_as_progress()
                        _pending_text = text

                elif event_type == "tool_use":
                    await _flush_text_as_progress()
                    part = data.get("part", {})
                    state = part.get("state", {})
                    tool_name = part.get("tool", "")
                    tool_input = state.get("input", {})
                    title = state.get("title", "")

                    if on_progress:
                        progress = self._format_tool(tool_name, tool_input, title)
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
                    logger.error("[antigravity] %s: %s", err_name, err_msg)
                    if any(k in err_msg.lower() for k in ("invalid_request_message_order", "not the same number of function calls")):
                        _session_corrupt = True
                    else:
                        _pending_text = f"\u274c {err_name}: {err_msg[:200]}"
                        if on_progress:
                            await on_progress(_pending_text)

            await proc.wait()

        _keepalive_task = self.start_keepalive_task(on_progress, _last_progress_time)
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
            self._clear_subprocess_info(instance)
            return "\u23f0 Antigravity took too long to respond (timed out)."
        finally:
            _keepalive_task.cancel()

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            self._clear_subprocess_info(instance)
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True
            if _captured_session_id:
                instance.session_id = _captured_session_id
            instance.last_input_tokens = _usage.get("input_tokens", 0)
            instance.last_output_tokens = _usage.get("output_tokens", 0)
            instance.session_cost += _usage.get("cost", 0.0)
            self._clear_subprocess_info(instance)

        if proc.returncode != 0:
            logger.error("opencode/antigravity exited %d (log: %s)", proc.returncode, log_path)
            try:
                with open(log_path, "r", errors="replace") as _f:
                    _log_tail = _f.read()[-2000:]
            except OSError:
                _log_tail = ""
            self._clear_subprocess_info(instance)
            _log_lower = _log_tail.lower()
            if "auth" in _log_lower or "login" in _log_lower or "unauthorized" in _log_lower:
                return (
                    "\u274c Antigravity auth error.\n"
                    "Run this once in Terminal to set up:\n"
                    "<code>OPENCODE_CONFIG_DIR=~/.jefe/antigravity-opencode opencode auth login</code>"
                )
            if "session" in _log_lower:
                self.new_session(instance)
                return "\u274c Session error. New conversation started \u2014 please resend."
            return "\u274c Antigravity exited with an error."

        if _session_corrupt:
            self.new_session(instance)
            return "\u26a0\ufe0f Session had corrupt tool history. Reset \u2014 please resend."

        if _pending_text:
            return _pending_text
        if _usage.get("output_tokens", 0) > 0 or _usage.get("cost", 0.0) > 0:
            return "\u2705 Done."
        return "(empty response from Antigravity)"

    @staticmethod
    def _format_tool(tool_name: str, tool_input: dict, title: str) -> str:
        name_lower = tool_name.lower()

        def _get(*keys: str) -> str:
            for k in keys:
                v = tool_input.get(k, "")
                if v:
                    return str(v)
            return ""

        if name_lower in ("bash", "shell", "run_shell_command", "exec_command"):
            cmd = _get("command", "cmd")
            return f"\u26a1 Shell: {title or cmd[:200]}" if (title or cmd) else "\u26a1 Shell"
        elif name_lower in ("edit", "edit_file", "write", "write_file"):
            path = _get("filePath", "file_path", "path")
            return f"\u270f\ufe0f Edit: {path}" if path else f"\u270f\ufe0f {title or tool_name}"
        elif name_lower in ("read", "read_file"):
            path = _get("filePath", "file_path", "path")
            return f"\U0001f4c4 Read: {path}" if path else "\U0001f4c4 Read"
        elif name_lower in ("list_directory", "ls", "glob"):
            target = _get("pattern", "dirPath", "dir_path", "path")
            return f"\U0001f4c2 Glob: {target}" if target else "\U0001f4c2 List"
        elif name_lower in ("grep", "search"):
            query = _get("query", "pattern", "regex")
            return f"\U0001f50d Grep: {query[:80]}" if query else "\U0001f50d Search"
        elif name_lower in ("web_fetch", "fetch"):
            url = _get("url")
            return f"\U0001f310 Fetch: {url}" if url else "\U0001f310 Fetch"
        elif name_lower in ("web_search", "google_web_search"):
            query = _get("query", "search_query")
            return f"\U0001f50e Search: {query}" if query else "\U0001f50e Web search"
        elif tool_name:
            return f"\U0001f527 {title or tool_name}"
        return ""
