"""Free runner — thin wrapper that spawns the freecode CLI.

freecode is a CLI agent pre-configured with free-tier provider
rotation (Groq, Cerebras, SambaNova, Gemini, OpenRouter, Together,
Mistral, Hugging Face, NVIDIA NIM, Ollama). Provider selection and
rotation happen inside the freecode CLI — this runner just talks to it.

Binary lookup: FREECODE_BIN_PATH env var, then PATH lookup for "freecode".
Model override: FREECODE_MODEL env var.

Output: NDJSON events identical to freecode — text, tool_use, step_finish, error.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from typing import Callable, Awaitable

from runners.base import _SUBPROCESS_LOGGER
from runners.freecode import FreeCodeBaseRunner

logger = logging.getLogger("bridge.free")

# ---------------------------------------------------------------------------
# Provider rotation infrastructure (used by free_proxy.py)
# ---------------------------------------------------------------------------

RATE_LIMIT_COOLDOWN = 60.0   # seconds before retrying a rate-limited provider
ERROR_COOLDOWN      = 15.0   # seconds before retrying after a transient error


class Provider:
    """A single free-tier OpenAI-compatible API provider with cooldown tracking."""

    def __init__(self, name: str, base_url: str, api_key: str, model: str):
        self.name = name
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self._cooldown_until: float = 0.0
        self._success_count: int = 0

    def is_available(self) -> bool:
        return bool(self.api_key) and time.time() >= self._cooldown_until

    def mark_rate_limited(self):
        self._cooldown_until = time.time() + RATE_LIMIT_COOLDOWN
        logger.info("[free] %s → rate limited, cooldown %.0fs", self.name, RATE_LIMIT_COOLDOWN)

    def mark_success(self):
        self._success_count += 1


class QwenCLIProvider:
    """Qwen CLI provider — no API key, auto-detected if qwen binary is in PATH."""

    name = "qwen"

    def is_configured(self) -> bool:
        return shutil.which("qwen") is not None

    def is_available(self) -> bool:
        return self.is_configured()


def _build_providers() -> list:
    """Build the ordered list of free-tier providers from environment variables."""
    providers: list = []

    def _add(name: str, base_url: str, key_env: str, model_env: str, default_model: str,
             extra_key_env: str = ""):
        api_key = os.environ.get(key_env, "")
        if not api_key and extra_key_env:
            api_key = os.environ.get(extra_key_env, "")
        model = os.environ.get(model_env, default_model)
        providers.append(Provider(name=name, base_url=base_url, api_key=api_key, model=model))

    # Priority order: fastest / most generous first
    _add("groq",        "https://api.groq.com/openai/v1",
         "GROQ_API_KEY",       "GROQ_MODEL",       "llama-3.3-70b-versatile")
    _add("cerebras",    "https://api.cerebras.ai/v1",
         "CEREBRAS_API_KEY",   "CEREBRAS_MODEL",   "qwen-3-235b-a22b-instruct-2507")
    _add("sambanova",   "https://api.sambanova.ai/v1",
         "SAMBANOVA_API_KEY",  "SAMBANOVA_MODEL",  "Meta-Llama-3.3-70B-Instruct")
    _add("gemini",      "https://generativelanguage.googleapis.com/v1beta/openai",
         "GEMINI_API_KEY",     "GEMINI_FREE_MODEL","gemini-2.0-flash",
         extra_key_env="GOOGLE_GENERATIVE_AI_API_KEY")
    _add("openrouter",  "https://openrouter.ai/api/v1",
         "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    _add("together",    "https://api.together.xyz/v1",
         "TOGETHER_API_KEY",   "TOGETHER_MODEL",   "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free")
    _add("mistral",     "https://api.mistral.ai/v1",
         "MISTRAL_API_KEY",    "MISTRAL_MODEL",    "mistral-small-latest")
    _add("cohere",      "https://api.cohere.com/compatibility/v1",
         "COHERE_API_KEY",     "COHERE_MODEL",     "command-r-plus-08-2024")
    _add("huggingface", "https://api-inference.huggingface.co/v1",
         "HF_API_KEY",         "HF_MODEL",         "meta-llama/Llama-3.3-70B-Instruct")
    _add("nvidia",      "https://integrate.api.nvidia.com/v1",
         "NVIDIA_API_KEY",     "NVIDIA_MODEL",     "meta/llama-3.3-70b-instruct")

    # Qwen CLI — no key needed
    providers.append(QwenCLIProvider())

    configured = [p.name for p in providers
                  if (isinstance(p, QwenCLIProvider) and p.is_configured())
                  or (isinstance(p, Provider) and p.api_key)]
    logger.info("[free] Providers with keys: %s", configured)
    return providers


class FreeCodeRunner(FreeCodeBaseRunner):
    name = "free"
    cli_command = "freecode"

    def discover_binary(self) -> str:
        """Use FREECODE_BIN_PATH if set, otherwise fall back to PATH lookup."""
        custom = os.environ.get("FREECODE_BIN_PATH")
        if custom:
            expanded = os.path.expanduser(custom)
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
            raise FileNotFoundError(f"FREECODE_BIN_PATH={custom!r} not found or not executable")
        path = shutil.which(self.cli_command)
        if path is None:
            raise FileNotFoundError(
                f"{self.cli_command} CLI not found in PATH. "
                "Install from https://github.com/polancojoseph1/freecode"
            )
        return path

    async def kill_all(self) -> int:
        return self._kill_processes("freecode run")

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

        env = self.build_env(dict(os.environ), user_is_owner)
        session_started = instance.session_started

        model = getattr(instance, "model", None) or os.environ.get("FREECODE_MODEL", "")

        cmd = [binary, "run", "--format", "json"]

        max_steps = os.environ.get("FREECODE_MAX_STEPS") or os.environ.get("OPENCODE_MAX_STEPS")
        if max_steps:
            cmd += ["--steps", max_steps]

        if model:
            cmd += ["-m", model]

        if session_started and instance.session_id:
            cmd += ["--session", instance.session_id]

        # Build system prompt
        system_parts = self.build_system_prompt(instance, memory_context)

        if image_path:
            prompt = (
                f"Look at the image file at: {image_path}\n\n{message}"
                if message
                else f"Look at the image file at: {image_path}\n\nDescribe what you see in the image."
            )
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
            logger.exception("OS error running freecode")
            return f"\u274c Error starting freecode: {exc}"

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

            async for line, _offset in self.tail_log_file(log_path, start_offset=log_start_offset, proc=proc):
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
                    _corrupt_keywords = ("invalid_request_message_order", "not the same number of function calls")
                    if any(k in err_msg.lower() for k in _corrupt_keywords):
                        _session_corrupt = True
                    elif on_progress:
                        await on_progress(f"\u274c {err_name}: {err_msg[:200]}")
                    else:
                        # No progress callback (Bridge Cloud path) — surface error as response
                        _pending_text = f"\u274c {err_name}: {err_msg[:200]}"

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
            return "\u23f0 FreeCode took too long to respond (timed out)."
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
            logger.error("freecode exited %d (see log: %s)", proc.returncode, log_path)
            try:
                with open(log_path, "r", errors="replace") as _f:
                    _log_tail = _f.read()[-2000:]
            except OSError:
                _log_tail = ""
            self._clear_subprocess_info(instance)
            _log_lower = _log_tail.lower()
            if "auth" in _log_lower or "unauthorized" in _log_lower:
                return "\u274c FreeCode auth error. Check your API keys or run `freecode` to configure."
            if "session" in _log_lower:
                self.new_session(instance)
                return "\u274c Session error. New conversation started \u2014 please resend your message."
            return "\u274c FreeCode exited with an error."

        if _session_corrupt:
            self.new_session(instance)
            return "\u26a0\ufe0f Session had corrupt tool call history. Session has been reset \u2014 please resend your message."

        if _pending_text:
            return _pending_text
        return "(empty response from FreeCode)"
