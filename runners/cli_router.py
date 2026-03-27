"""CLI Router — unified runner that rotates across multiple CLI backends.

Wraps Claude, Gemini, Codex, and Qwen runners with automatic failover on
rate limits. Use /cli to pin a specific CLI or set auto-rotation.

Configuration:
  CLI_RUNNER=router
  CLI_ORDER=claude,gemini,codex,qwen  (env, comma-separated priority order)
"""

import json
import logging
import os
import time
from typing import Any, Callable, Awaitable

from runners.base import RunnerBase

logger = logging.getLogger("bridge.cli-router")

# Rate limit / overload patterns per CLI name
_RATE_LIMIT_PATTERNS: dict[str, list[str]] = {
    "claude": [
        "overloaded_error", "rate_limit_error", "too_many_requests",
    ],
    "gemini": [
        "quota exhausted", "resource_exhausted",
        "429", "too many requests",
    ],
    "codex": [
        "429", "rate_limit", "quota exceeded",
    ],
    "qwen": [
        "quota reached", "quota exceeded", "429",
    ],
}

# Responses matching these are NEVER treated as rate limits
_NOT_RATE_LIMIT_PATTERNS = [
    "auth expired", "cli not found", "not installed",
    "authentication_error", "please obtain a new token",
    "plan mode is not supported",
    "timed out", "took too long",       # slow != rate limited
    "session error", "exited with an error",  # crash != rate limited
    "empty response", "no text response",     # blank != rate limited
    "\U0001f6d1 stopped",                     # user-initiated stop
]

RATE_LIMIT_COOLDOWN = 30.0  # seconds
ERROR_COOLDOWN = 15.0       # seconds

# Default context windows for CLIs that don't report their own
_DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    "claude": 200_000,
    "gemini": 1_000_000,
    "codex":  200_000,
    "qwen":   128_000,
}

# Persistence file for CLI preference
_PREF_FILE = os.path.join(
    os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot")),
    "cli_preference.json",
)


def _create_runner_by_name(name: str) -> "RunnerBase | None":
    """Create a single runner by name. Returns None if binary not in PATH."""
    try:
        if name == "claude":
            from runners.claude import ClaudeRunner
            r = ClaudeRunner()
            return r if r.is_available() else None
        elif name == "gemini":
            from runners.gemini import GeminiRunner
            r = GeminiRunner()
            return r if r.is_available() else None
        elif name == "codex":
            from runners.codex import CodexRunner
            r = CodexRunner()
            return r if r.is_available() else None
        elif name == "qwen":
            from runners.qwen import QwenRunner
            r = QwenRunner()
            return r if r.is_available() else None
    except Exception as e:
        logger.warning("[cli-router] Failed to create %s runner: %s", name, e)
    return None


class CLIRouterRunner(RunnerBase):
    """Routes messages across multiple CLI runners with automatic failover."""

    name = "router"
    cli_command = ""  # virtual — delegates to child runners

    def __init__(self):
        order_str = os.environ.get("CLI_ORDER", "claude,gemini,codex,qwen")
        self._order: list[str] = []
        self._runners: dict[str, RunnerBase] = {}

        for name in order_str.split(","):
            name = name.strip().lower()
            if name and name not in self._runners:
                runner = _create_runner_by_name(name)
                if runner:
                    self._runners[name] = runner
                    self._order.append(name)
                    logger.info("[cli-router] Loaded: %s", name)
                else:
                    logger.info("[cli-router] Skipped %s (not installed)", name)

        # Cooldown tracking: runner_name -> expiry timestamp
        self._cooldowns: dict[str, float] = {}

        # Per-instance: which runner is currently active
        self._instance_active: dict[int, str] = {}

        # Global preference: "auto" or a specific runner name
        self._preference: str = "auto"
        self._load_preferences()

        logger.info(
            "[cli-router] Ready. Order: %s, Preference: %s",
            self._order, self._preference,
        )

    # ── Preference persistence ──────────────────────────────────────────

    def _load_preferences(self):
        try:
            with open(_PREF_FILE, "r") as f:
                data = json.load(f)
            pref = data.get("preference", "auto")
            if pref == "auto" or pref in self._runners:
                self._preference = pref
        except (OSError, json.JSONDecodeError):
            pass

    def _save_preferences(self):
        os.makedirs(os.path.dirname(_PREF_FILE), exist_ok=True)
        try:
            with open(_PREF_FILE, "w") as f:
                json.dump({"preference": self._preference}, f)
        except OSError as e:
            logger.warning("[cli-router] Failed to save preferences: %s", e)

    def set_preference(self, pref: str) -> bool:
        """Set CLI preference. Returns True if valid."""
        if pref == "auto" or pref in self._runners:
            self._preference = pref
            self._save_preferences()
            return True
        return False

    @property
    def preference(self) -> str:
        return self._preference

    @property
    def runner_names(self) -> list[str]:
        return list(self._order)

    def get_active_for(self, instance_id: int) -> str | None:
        return self._instance_active.get(instance_id)

    def skip_to_next(self, instance_id: int) -> str | None:
        """Put the current CLI on cooldown and return the next available one, or None."""
        current = self._instance_active.get(instance_id)
        if current:
            self._cooldowns[current] = time.time() + RATE_LIMIT_COOLDOWN
            logger.info("[cli-router] Manual skip: %s → cooldown", current)
        now = time.time()
        for name in self._order:
            if name != current and now >= self._cooldowns.get(name, 0):
                self._instance_active[instance_id] = name
                return name
        return None

    def get_runner(self, name: str) -> "RunnerBase | None":
        return self._runners.get(name)

    def get_status(self) -> dict[str, dict]:
        """Status dict per runner: name -> {available, cooldown_remaining}."""
        now = time.time()
        status = {}
        for name in self._order:
            cd = self._cooldowns.get(name, 0)
            remaining = max(0, cd - now)
            status[name] = {
                "available": remaining == 0,
                "cooldown_remaining": round(remaining),
            }
        return status

    # ── Session state per runner ────────────────────────────────────────

    # Fields saved/restored per CLI so each CLI shows its own usage stats
    _SESSION_FIELDS = (
        "session_id", "session_started", "session_cost",
        "context_window", "last_input_tokens", "last_cache_read_tokens",
        "last_cache_creation_tokens", "last_output_tokens", "last_total_tokens",
    )

    def _save_session(self, instance, runner_name: str):
        """Snapshot instance session state for a specific runner."""
        rd = instance.adapter_data.setdefault("cli_router", {})
        sessions = rd.setdefault("sessions", {})
        state = {f: getattr(instance, f, 0) for f in self._SESSION_FIELDS}
        # Codex stores thread_id in adapter_data
        if runner_name == "codex":
            state["thread_id"] = instance.adapter_data.get("thread_id")
        sessions[runner_name] = state

    def _restore_session(self, instance, runner_name: str):
        """Restore instance session state for a specific runner, or init fresh."""
        rd = instance.adapter_data.get("cli_router", {})
        state = rd.get("sessions", {}).get(runner_name)
        if state:
            for f in self._SESSION_FIELDS:
                setattr(instance, f, state.get(f, 0))
            if runner_name == "codex" and "thread_id" in state:
                instance.adapter_data["thread_id"] = state["thread_id"]
        else:
            self._runners[runner_name].new_session(instance)
            # Zero out usage stats for a fresh CLI
            instance.context_window = 0
            instance.last_input_tokens = 0
            instance.last_cache_read_tokens = 0
            instance.last_cache_creation_tokens = 0
            instance.last_output_tokens = 0
            instance.last_total_tokens = 0
            instance.session_cost = 0.0

    # ── Rate limit detection ────────────────────────────────────────────

    @staticmethod
    def _is_rate_limit(runner_name: str, response: str) -> bool:
        if not response or len(response) < 10:
            return False
        lower = response.lower()
        # Exclude known non-rate-limit responses first
        for excl in _NOT_RATE_LIMIT_PATTERNS:
            if excl in lower:
                return False
        for pattern in _RATE_LIMIT_PATTERNS.get(runner_name, []):
            if pattern in lower:
                return True
        return False

    # ── Try order ───────────────────────────────────────────────────────

    def _build_try_order(self, instance_id: int) -> list[str]:
        now = time.time()
        available = [n for n in self._order if now >= self._cooldowns.get(n, 0)]
        if not available:
            # Everything on cooldown — try all anyway (shortest cooldown first)
            return sorted(self._order, key=lambda n: self._cooldowns.get(n, 0))

        preferred = self._preference if self._preference != "auto" else None
        last_used = self._instance_active.get(instance_id)

        result = []
        # 1. Global preference wins when explicitly set via /cli
        if preferred and preferred in available:
            result.append(preferred)
        # 2. Instance-level last used (fallback for auto mode)
        if last_used and last_used in available and last_used not in result:
            result.append(last_used)
        # 3. Remaining in configured order
        for name in available:
            if name not in result:
                result.append(name)
        return result

    # ── RunnerBase interface ────────────────────────────────────────────

    def is_available(self) -> bool:
        return bool(self._runners)

    def new_session(self, instance) -> None:
        active = self._instance_active.get(instance.id)
        if active and active in self._runners:
            self._runners[active].new_session(instance)
        elif self._order:
            self._runners[self._order[0]].new_session(instance)
        # Clear all router state for this instance
        rd = instance.adapter_data.get("cli_router", {})
        rd.pop("active_runner", None)
        rd.pop("sessions", None)
        self._instance_active.pop(instance.id, None)

    async def stop(self, instance) -> bool:
        active = self._instance_active.get(instance.id)
        if active and active in self._runners:
            return await self._runners[active].stop(instance)
        for r in self._runners.values():
            if await r.stop(instance):
                return True
        return False

    async def kill_all(self) -> int:
        total = 0
        for r in self._runners.values():
            total += await r.kill_all()
        return total

    async def stop_all(self, instances: list) -> int:
        import asyncio
        results = await asyncio.gather(*(self.stop(inst) for inst in instances))
        return sum(1 for r in results if r)

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """One-shot stateless query with rotation."""
        for name in self._order:
            now = time.time()
            if now < self._cooldowns.get(name, 0):
                continue
            try:
                result = await self._runners[name].run_query(prompt, timeout)
                if self._is_rate_limit(name, result):
                    self._cooldowns[name] = time.time() + RATE_LIMIT_COOLDOWN
                    logger.info("[cli-router] %s rate limited (query) — rotating", name)
                    continue
                return result
            except Exception as e:
                logger.warning("[cli-router] %s query error: %s", name, e)
                self._cooldowns[name] = time.time() + ERROR_COOLDOWN
                continue
        return '{"error": "All CLIs rate-limited or unavailable"}'

    async def run(
        self,
        message: str,
        instance: Any,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        image_path: str | list | None = None,
        memory_context: str = "",
        on_subprocess_started: Callable[[int, str, str], None] | None = None,
        chat_id: int = 0,
        user_is_owner: bool = True,
    ) -> str:
        try_order = self._build_try_order(instance.id)
        if not try_order:
            return "\u274c No CLI runners available. Install claude, gemini, codex, or qwen."

        last_runner = self._instance_active.get(instance.id)
        last_error = ""

        for i, name in enumerate(try_order):
            runner = self._runners[name]

            # Restore this runner's session state
            self._restore_session(instance, name)

            # Notify on rotation (not for the first attempt)
            if i > 0 and on_progress:
                await on_progress(f"\u26a1 {try_order[i-1]} unavailable \u2014 trying {name}...")

            try:
                result = await runner.run(
                    message,
                    instance=instance,
                    on_progress=on_progress,
                    image_path=image_path,
                    memory_context=memory_context,
                    on_subprocess_started=on_subprocess_started,
                    chat_id=chat_id,
                    user_is_owner=user_is_owner,
                )
            except Exception as e:
                logger.error("[cli-router] %s raised: %s", name, e)
                self._save_session(instance, name)
                self._cooldowns[name] = time.time() + ERROR_COOLDOWN
                last_error = str(e)
                continue

            # Stopped by user — don't rotate
            if instance.was_stopped:
                self._save_session(instance, name)
                self._instance_active[instance.id] = name
                instance.adapter_data.setdefault("cli_router", {})["active_runner"] = name
                return result

            # Check for rate limit in response
            if self._is_rate_limit(name, result):
                self._save_session(instance, name)
                self._cooldowns[name] = time.time() + RATE_LIMIT_COOLDOWN
                logger.info("[cli-router] %s \u2192 rate limit, rotating", name)
                last_error = f"{name} rate limited"
                continue

            # Success — fill in context_window if the runner didn't set one
            if not instance.context_window and name in _DEFAULT_CONTEXT_WINDOWS:
                instance.context_window = _DEFAULT_CONTEXT_WINDOWS[name]

            self._save_session(instance, name)
            self._instance_active[instance.id] = name
            instance.adapter_data.setdefault("cli_router", {})["active_runner"] = name

            if last_runner and name != last_runner:
                logger.info("[cli-router] Switched instance #%d: %s \u2192 %s", instance.id, last_runner, name)

            return result

        # All failed — restore last known session
        if last_runner:
            self._restore_session(instance, last_runner)

        return f"\u274c All CLIs unavailable. Last error: {last_error}"
