"""Free runner — thin wrapper that spawns the freecode CLI.

freecode is a CLI agent pre-configured with free-tier provider
rotation (Groq, Cerebras, SambaNova, Gemini, OpenRouter, Together,
Mistral, Hugging Face, NVIDIA NIM, Ollama). Provider selection and
rotation happen inside the freecode CLI — this runner just talks to it.

Binary lookup: FREECODE_BIN_PATH env var, then PATH lookup for "freecode".
Model override: FREECODE_MODEL env var.

Output: NDJSON events identical to freecode — text, tool_use, step_finish, error.
"""

import logging
import os
import shutil
import time

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

