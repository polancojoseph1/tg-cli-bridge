"""Claude Code CLI runner adapter.

Subprocess: claude -p --output-format stream-json --resume <uuid>
Session: UUID-based (--session-id first call, --resume subsequent)
System prompt: --append-system-prompt CLI flag
"""

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from typing import Callable, Awaitable

from runners.base import RunnerBase, _SUBPROCESS_LOGGER

logger = logging.getLogger("bridge.claude")

_CLAUDE_AUTH_ERROR_PATTERNS = (
    "failed to authenticate",
    "authentication_error",
    "oauth token has expired",
    "please obtain a new token",
    "refresh your existing token",
)


class ClaudeRunner(RunnerBase):
    name = "claude"
    cli_command = "claude"

    def __init__(self):
        from config import CLI_TIMEOUT, CLI_SYSTEM_PROMPT, CHROME_ENABLED, MEMORY_DIR, MEMORY_ENABLED, USER_NAME
        self.timeout = CLI_TIMEOUT
        self.memory_dir = MEMORY_DIR
        self.system_prompt = (CLI_SYSTEM_PROMPT.replace("{MEMORY_DIR}", MEMORY_DIR).replace("{OWNER_NAME}", USER_NAME or "the user") if CLI_SYSTEM_PROMPT else CLI_SYSTEM_PROMPT)
        self.chrome_enabled = CHROME_ENABLED
        self.memory_enabled = MEMORY_ENABLED

    def new_session(self, instance) -> None:
        instance.session_id = str(uuid.uuid4())
        instance.session_started = False
        instance.session_cost = 0.0

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

    # Env vars stripped from subprocess when running on behalf of a non-owner user
    _SENSITIVE_ENV_PATTERNS = re.compile(
        r"^(AWS_|GOOGLE_|GCP_|GCLOUD_|GITHUB_|GH_|GITLAB_|AZURE_|STRIPE_|"
        r"TWILIO_|SENDGRID_|CLOUDFLARE_|DIGITALOCEAN_|HEROKU_|VERCEL_|NETLIFY_|"
        r"OPENAI_|GEMINI_|COHERE_|MISTRAL_|TOGETHER_)",
        re.IGNORECASE,
    )
    _SENSITIVE_ENV_EXACT = {
        "SSH_AUTH_SOCK", "SSH_AGENT_PID",
        "INTERNAL_API_KEY", "TELEGRAM_BOT_TOKEN", "COLLAB_TOKEN",
        "ALLOWED_USER_ID", "ALLOWED_USER_IDS", "USER_NAMES",
    }

    def _build_env(self, user_is_owner: bool) -> dict:
        """Build subprocess environment. Strip sensitive vars for non-owner users."""
        base = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if user_is_owner:
            return base
        return {
            k: v for k, v in base.items()
            if k not in self._SENSITIVE_ENV_EXACT
            and not self._SENSITIVE_ENV_PATTERNS.match(k)
        }

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
            return "\u274c Error: claude CLI not found. Is Claude Code installed?"

        env = self._build_env(user_is_owner)
        session_id = instance.session_id
        session_started = instance.session_started

        _CLI_RUNNER_NAMES = {"claude", "gemini", "codex", "qwen", "free", "freecode"}
        model = instance.model if instance.model and instance.model not in _CLI_RUNNER_NAMES else "claude-sonnet-4-6"
        cmd = [binary, "-p", "--model", model,
               "--dangerously-skip-permissions",
               "--verbose", "--output-format", "stream-json"]

        # Non-owner users cannot trigger shell (Bash) tool execution.
        # This is enforced at the CLI level — the LLM cannot override it.
        if not user_is_owner:
            cmd += ["--disallowed-tools", "Bash"]

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
            paths = image_path if isinstance(image_path, list) else [image_path]
            if len(paths) == 1:
                read_instructions = f"First, use the Read tool to view the image file at: {paths[0]}"
                suffix = "Describe what you see in the image." if not message else f"Then respond to the user's request: {message}"
            else:
                read_instructions = "First, use the Read tool to view each image file:\n" + "\n".join(f"- {p}" for p in paths)
                suffix = "Describe what you see in each image." if not message else f"Then respond to the user's request: {message}"
            prompt = f"{read_instructions}\n\n{suffix}"
            cmd.append(prompt)
        else:
            cmd.append(message.replace("\x00", ""))

        log_path = self.make_log_path(self.name, chat_id, instance.id)
        log_start_offset = os.path.getsize(log_path) if os.path.exists(log_path) else 0

        # Spawn the wrapper as a detached process so it survives server crashes.
        # The wrapper reads CLI stdout+stderr line by line and writes to log_path with flush.
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
            logger.exception("OS error running claude")
            return f"\u274c Error starting claude: {exc}"

        # Record subprocess info on the instance for crash recovery
        instance.subprocess_pid = proc.pid
        instance.subprocess_log_file = log_path
        instance.subprocess_start_time = self.get_pid_start_time(proc.pid) or ""
        if on_subprocess_started:
            on_subprocess_started(proc.pid, log_path, instance.subprocess_start_time)

        final_result = ""
        result_is_error = False
        _pending_text: str = ""
        _got_result = False
        _usage = {"turn": {}, "context_window": 0, "cost": 0.0}
        _agent_calls: dict[str, dict] = {}
        _agent_counter = 0

        def _is_auth_error(text: str) -> bool:
            lowered = text.lower()
            return any(pattern in lowered for pattern in _CLAUDE_AUTH_ERROR_PATTERNS)

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
            nonlocal final_result, result_is_error, _agent_counter, _pending_text, _got_result
            async for line, _offset in self.tail_log_file(log_path, start_offset=log_start_offset, proc=proc):
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
                    content_blocks = data.get("message", {}).get("content", [])
                    for block in content_blocks:
                        block_type = block.get("type", "")
                        if block_type == "tool_use":
                            # A tool call means the pending text was narrative — flush it as progress
                            await _flush_text_as_progress()
                            tool_name = block.get("name", "")
                            tool_inp = block.get("input", {})
                            tool_id = block.get("id", "")
                            if tool_name in ("EnterPlanMode", "ExitPlanMode"):
                                # Plan mode requires interactive approval — kill immediately
                                # instead of waiting for the 30-min timeout
                                try:
                                    proc.kill()
                                except ProcessLookupError:
                                    pass
                                self.new_session(instance)
                                raise asyncio.CancelledError("plan_mode_detected")
                            elif tool_name == "Agent":
                                _agent_counter += 1
                                desc = tool_inp.get("description", tool_inp.get("prompt", ""))[:100]
                                _agent_calls[tool_id] = {"n": _agent_counter, "description": desc}
                                if on_progress:
                                    await on_progress(f"\U0001f916 [Sub-agent {_agent_counter}] {desc}")
                            elif on_progress:
                                progress = self.format_tool_progress(tool_name, tool_inp)
                                if progress:
                                    await on_progress(progress)
                        elif block_type == "thinking":
                            pass  # thinking mode removed — drop silently
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text:
                                # Flush the previous text turn as progress, hold this one pending.
                                # The last text turn will become the final response.
                                await _flush_text_as_progress()
                                _pending_text = text

                elif msg_type == "user":
                    for block in data.get("message", {}).get("content", []):
                        if block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "")
                            if tool_use_id in _agent_calls and on_progress:
                                info = _agent_calls.pop(tool_use_id)
                                await on_progress(f"\u2705 [Sub-agent {info['n']} done]")

                elif msg_type == "result":
                    result_is_error = bool(data.get("is_error"))
                    final_result = data.get("result", "")
                    if result_is_error and not final_result:
                        errors = data.get("errors") or []
                        if isinstance(errors, list):
                            final_result = "\n".join(str(err) for err in errors if err)
                    _usage["cost"] = data.get("total_cost_usd", 0.0)
                    for model_data in data.get("modelUsage", {}).values():
                        _usage["context_window"] = model_data.get("contextWindow", 0)
                        break
                    _got_result = True
                    break  # Don't wait for subprocess exit — chrome keeps the pipe open

            # Kill wrapper if still alive (chrome inherits stdout pipe, keeps it open)
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()

        try:
            await asyncio.wait_for(process_stream(), timeout=self.timeout)
        except asyncio.CancelledError as _ce:
            if str(_ce) == "plan_mode_detected":
                await proc.wait()
                instance.process = None
                instance.subprocess_pid = 0
                instance.subprocess_log_file = ""
                instance.subprocess_start_time = ""
                return "\u26a0\ufe0f Plan mode is not supported in this context \u2014 session reset. Please resend your request."
            raise
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
            self.new_session(instance)  # reset session so next call doesn't hit "already in use"
            return "\u23f0 Claude took too long to respond (timed out)."

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            return "\U0001f6d1 Stopped."

        if (proc.returncode == 0 or _got_result) and not result_is_error:
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
            # Clear subprocess tracking — process finished cleanly
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""

        if proc.returncode != 0 and not _got_result:
            logger.error("claude exited %d (see log: %s)", proc.returncode, log_path)
            # Check log for session-related error indicators
            try:
                with open(log_path, "r", errors="replace") as _f:
                    _log_tail = _f.read()[-2000:]
            except OSError:
                _log_tail = ""
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            if _is_auth_error(_log_tail):
                self.new_session(instance)
                return "\u274c Claude auth expired. Run `claude` in a terminal on this Mac to sign in again, then resend your message."
            if "session" in _log_tail.lower():
                self.new_session(instance)
                return "\u274c Session error. New conversation started \u2014 please resend your message."
            return "\u274c Claude exited with an error." if not _log_tail else "\u274c Claude error (check logs)"

        if result_is_error:
            lowered_result = final_result.lower()
            instance.subprocess_pid = 0
            instance.subprocess_log_file = ""
            instance.subprocess_start_time = ""
            if _is_auth_error(final_result):
                self.new_session(instance)
                return "\u274c Claude auth expired. Run `claude` in a terminal on this Mac to sign in again, then resend your message."
            if "no conversation found with session id" in lowered_result or "session" in lowered_result:
                self.new_session(instance)
                return "\u274c Session error. New conversation started \u2014 please resend your message."
            return "\u274c Claude error." if not final_result else f"\u274c Claude error: {final_result}"

        if final_result:
            if _is_auth_error(final_result):
                self.new_session(instance)
                return "\u274c Claude auth expired. Run `claude` in a terminal on this Mac to sign in again, then resend your message."
            return final_result
        if _pending_text:
            if _is_auth_error(_pending_text):
                self.new_session(instance)
                return "\u274c Claude auth expired. Run `claude` in a terminal on this Mac to sign in again, then resend your message."
            return _pending_text
        return "(empty response from Claude)"
