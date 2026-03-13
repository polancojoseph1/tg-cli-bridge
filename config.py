"""Unified configuration for bridgebot.

All settings are loaded from environment variables (via .env file).
Users select their CLI runner with CLI_RUNNER=claude|gemini|codex|generic.
"""

import os
import shutil
import logging
from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV_FILE", ".env"))

logger = logging.getLogger("bridge")

# === Required ===
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID: int = int(os.environ.get("ALLOWED_USER_ID", "0"))

# Comma-separated list of allowed Telegram user IDs (e.g. "123456,789012")
# Falls back to just ALLOWED_USER_ID if not set
def _parse_allowed_user_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_USER_IDS", "")
    if raw:
        return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}
    return {ALLOWED_USER_ID} if ALLOWED_USER_ID else set()

ALLOWED_USER_IDS: set[int] = _parse_allowed_user_ids()

# Display names per user ID. Format: "123456:Alice,789012:Bob"
def _parse_user_names() -> dict[int, str]:
    raw = os.environ.get("USER_NAMES", "")
    if not raw:
        return {}
    result = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            uid_str, name = entry.split(":", 1)
            try:
                result[int(uid_str.strip())] = name.strip()
            except ValueError:
                pass
    return result

USER_NAMES: dict[int, str] = _parse_user_names()

# === CLI Selection ===
CLI_RUNNER: str = os.environ.get("CLI_RUNNER", "claude").lower()
CLI_COMMAND: str = os.environ.get("CLI_COMMAND", "")  # auto-detected if empty
CLI_TIMEOUT: int = int(os.environ.get("CLI_TIMEOUT", "1800"))
CLI_SYSTEM_PROMPT: str = os.environ.get("CLI_SYSTEM_PROMPT", "")

# Default model name passed to --model flag when creating agents.
# Override with DEFAULT_AGENT_MODEL env var. Falls back to CLI_RUNNER-specific defaults.
_RUNNER_DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-4-6",
    "gemini": "gemini-2.5-pro",
    "codex":  "gpt-4o",
    "qwen":   "qwen2.5-coder:7b",
}
DEFAULT_AGENT_MODEL: str = os.environ.get(
    "DEFAULT_AGENT_MODEL",
    _RUNNER_DEFAULT_MODELS.get(os.environ.get("CLI_RUNNER", "claude").lower(), "claude-sonnet-4-6"),
)

# Bot display name — derived from CLI_RUNNER unless overridden
BOT_NAME: str = os.environ.get("BOT_NAME", "")

# Bot emoji — prepended to every response. Derived from CLI_RUNNER unless overridden
BOT_EMOJI: str = os.environ.get("BOT_EMOJI", "")

HOST: str = os.environ.get("HOST", "0.0.0.0")
PORT: int = int(os.environ.get("PORT", "8588"))
WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "")

# === Optional Features ===
MEMORY_ENABLED: bool = os.environ.get("MEMORY_ENABLED", "true").lower() in ("true", "1", "yes")
MEMORY_DIR: str = os.environ.get("MEMORY_DIR", os.path.expanduser("~/memories"))
MEMORY_COLLECTION: str = os.environ.get("MEMORY_COLLECTION", "telegram_bridge")
MEMORY_TOP_K: int = int(os.environ.get("MEMORY_TOP_K", "5"))

# Data directory for runtime files (session DB, logs, pid files)
# Override with TG_BRIDGE_DATA_DIR env var. Defaults to ~/.bridgebot
DATA_DIR: str = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))

# Your first name — used to personalize the bot's greetings and memory context
USER_NAME: str = os.environ.get("USER_NAME", "")

# Chrome extension (Claude only)
CHROME_ENABLED: bool = os.environ.get("CHROME_ENABLED", "false").lower() in ("true", "1", "yes")

# Playwright browser automation — enables /screenshot and /browse commands
PLAYWRIGHT_ENABLED: bool = os.environ.get("PLAYWRIGHT_ENABLED", "true").lower() in ("true", "1", "yes")

# Image generation (requires GEMINI_API_KEY)
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")

# Voice settings
WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base")
EDGE_TTS_VOICE: str = os.environ.get("EDGE_TTS_VOICE", "en-US-AndrewNeural")
VOICE_MAX_LENGTH: int = int(os.environ.get("VOICE_MAX_LENGTH", "3000"))

# Telegram
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Voice call settings (Pyrogram userbot — optional)
TG_API_ID: int = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH: str = os.environ.get("TG_API_HASH", "")
TG_SESSION_NAME: str = os.environ.get("TG_SESSION_NAME", "bridge_voice")
CALL_GROUP_ID: int = int(os.environ.get("CALL_GROUP_ID", "0"))
CALL_SILENCE_THRESHOLD: int = int(os.environ.get("CALL_SILENCE_THRESHOLD", "500"))
CALL_SILENCE_DURATION: float = float(os.environ.get("CALL_SILENCE_DURATION", "1.5"))
CALL_MAX_SPEECH_DURATION: int = int(os.environ.get("CALL_MAX_SPEECH_DURATION", "60"))


# === Auto-detection ===

_CLI_DEFAULTS: dict[str, dict] = {
    "claude": {"command": "claude", "bot_name": "Claude", "bot_emoji": "🤖"},
    "gemini": {"command": "gemini", "bot_name": "Gemini", "bot_emoji": "✨"},
    "codex":  {"command": "codex",  "bot_name": "Codex",  "bot_emoji": "⚡"},
    "qwen":   {"command": "qwen",   "bot_name": "Qwen",   "bot_emoji": "🔮"},
}

def _auto_detect():
    """Fill in CLI_COMMAND, BOT_NAME, and BOT_EMOJI from CLI_RUNNER if not explicitly set."""
    global CLI_COMMAND, BOT_NAME, BOT_EMOJI
    defaults = _CLI_DEFAULTS.get(CLI_RUNNER, {})
    if not CLI_COMMAND:
        CLI_COMMAND = defaults.get("command", CLI_RUNNER)
    if not BOT_NAME:
        BOT_NAME = defaults.get("bot_name", CLI_RUNNER.capitalize())
    if not BOT_EMOJI:
        BOT_EMOJI = defaults.get("bot_emoji", "")

_auto_detect()


# === Display Preferences (defaults; overridden per-user via /show and /hide commands) ===
DISPLAY_SHOW_TOOLS: bool = os.environ.get("DISPLAY_SHOW_TOOLS", "true").lower() == "true"
DISPLAY_SHOW_THOUGHTS: bool = os.environ.get("DISPLAY_SHOW_THOUGHTS", "true").lower() == "true"

# === Collab (federated peer networking) ===
COLLAB_ENABLED: bool = os.environ.get("COLLAB_ENABLED", "true").lower() in ("true", "1", "yes")
COLLAB_INSTANCE_NAME: str = os.environ.get("COLLAB_INSTANCE_NAME", "")
COLLAB_TOKEN: str = os.environ.get("COLLAB_TOKEN", "")  # inbound auth token for this instance


def validate_config() -> list[str]:
    """Return a list of configuration errors (empty if all good)."""
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is not set")
    if not ALLOWED_USER_IDS:
        errors.append("ALLOWED_USER_ID is not set")
    if CLI_RUNNER not in ("claude", "gemini", "codex", "qwen", "generic"):
        errors.append(f"CLI_RUNNER='{CLI_RUNNER}' — must be claude, gemini, codex, qwen, or generic")
    if CLI_RUNNER != "generic" and not is_cli_available():
        errors.append(
            f"CLI binary '{CLI_COMMAND}' not found in PATH — "
            f"install it before starting the bot"
        )
    return errors


def is_cli_available() -> bool:
    """Check if the selected CLI binary is found in PATH."""
    return shutil.which(CLI_COMMAND) is not None
