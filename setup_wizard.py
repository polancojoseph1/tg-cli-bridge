#!/usr/bin/env python3
"""Interactive setup wizard for bridgebot.

Run:  python setup_wizard.py

Walks you through configuring your .env file step by step.
Re-run anytime to change settings — your existing config is preserved.
"""

import os
import sys
import shutil
import subprocess
import platform
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-activate venv — re-exec with venv Python if we're not already in it
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.resolve()
# venv Python path differs by OS: bin/python on Unix, Scripts/python.exe on Windows
_VENV_PYTHON = (
    _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if sys.platform == "win32"
    else _PROJECT_ROOT / ".venv" / "bin" / "python"
)

if _VENV_PYTHON.exists() and not sys.executable.startswith(str(_PROJECT_ROOT / ".venv")):
    if sys.platform == "win32":
        # os.execv is unreliable on Windows — spawn a new process instead
        import subprocess as _sp
        result = _sp.run([str(_VENV_PYTHON)] + sys.argv)
        sys.exit(result.returncode)
    else:
        os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON)] + sys.argv)

# ---------------------------------------------------------------------------
# Dependency check — give a helpful message if requirements aren't installed
# ---------------------------------------------------------------------------

_missing = []
try:
    import httpx
except ImportError:
    _missing.append("httpx")
try:
    from dotenv import set_key, dotenv_values
except ImportError:
    _missing.append("python-dotenv")

if _missing:
    print()
    print("  It looks like some dependencies aren't installed yet.")
    print(f"  Missing: {', '.join(_missing)}")
    print()
    print("  To fix this, run:")
    print()
    print("    pip install -r requirements.txt")
    print()
    print("  Then run this wizard again:")
    print()
    print("    python setup_wizard.py")
    print()
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent.resolve()
ENV_PATH = PROJECT_DIR / ".env"
ENV_EXAMPLE = PROJECT_DIR / ".env.example"

# Placeholder values from .env.example that count as "not set"
_PLACEHOLDERS = {"your-bot-token-here", "your-telegram-user-id", ""}

CLI_OPTIONS = {
    "claude": {
        "command": "claude",
        "label": "Claude Code",
        "company": "Anthropic",
        "description": "Anthropic's AI coding agent",
        "install": "npm install -g @anthropic-ai/claude-code",
        "url": "https://docs.anthropic.com/en/docs/claude-code",
    },
    "gemini": {
        "command": "gemini",
        "label": "Gemini CLI",
        "company": "Google",
        "description": "Google's AI coding assistant",
        "install": "npm install -g @google/gemini-cli",
        "url": "https://github.com/google-gemini/gemini-cli",
    },
    "codex": {
        "command": "codex",
        "label": "Codex CLI",
        "company": "OpenAI",
        "description": "OpenAI's coding agent",
        "install": "npm install -g @openai/codex",
        "url": "https://github.com/openai/codex",
    },
    "qwen": {
        "command": "qwen",
        "label": "Qwen Coder",
        "company": "Alibaba / QwenLM",
        "description": "Qwen3-Coder agent — 1000 free requests/day",
        "install": "npm install -g @qwen-code/qwen-code",
        "url": "https://github.com/QwenLM/qwen-code",
    },
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def mask_token(token: str) -> str:
    """Show first 5 and last 3 chars: '71234...efg'"""
    if not token or len(token) <= 10:
        return "***"
    return f"{token[:5]}...{token[-3:]}"


def _is_placeholder(value: str | None) -> bool:
    return not value or value in _PLACEHOLDERS


def detect_clis() -> dict[str, str | None]:
    """Return {runner_name: /path/to/binary or None} for each known CLI."""
    return {
        name: shutil.which(info["command"])
        for name, info in CLI_OPTIONS.items()
    }


def check_system_deps() -> list[tuple[str, bool, str]]:
    """Check for optional system dependencies.

    Returns list of (name, found, install_hint) tuples.
    """
    checks = [
        ("node / npm",  shutil.which("npm") is not None,
         "Install Node.js from https://nodejs.org (required for all AI CLIs)"),
        ("ffmpeg",      shutil.which("ffmpeg") is not None,
         "Install ffmpeg: brew install ffmpeg  (macOS) | apt install ffmpeg  (Linux)\n"
         "     Required for voice message support."),
        ("tailscale",   shutil.which("tailscale") is not None,
         "Install Tailscale from https://tailscale.com  (recommended for stable webhook URLs)"),
        ("cloudflared", shutil.which("cloudflared") is not None,
         "Install cloudflared from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/\n"
         "     Optional: alternative tunnel tool to Tailscale."),
    ]
    return checks


def print_system_deps():
    """Print a system dependency table — called from the main wizard menu."""
    checks = check_system_deps()
    print()
    print("=" * 46)
    print("  System Dependency Check")
    print("=" * 46)
    print()
    all_ok = True
    for name, found, hint in checks:
        status = "OK" if found else "MISSING"
        icon = "" if found else "!"
        print(f"  [{status:^7}] {icon} {name}")
        if not found:
            all_ok = False
            print(f"           {hint}")
            print()
    if all_ok:
        print("  All optional dependencies found.")
    print()


def save_value(key: str, value: str):
    """Write a single key=value to .env (creates file if needed)."""
    set_key(str(ENV_PATH), key, value)


def load_existing() -> dict[str, str | None]:
    """Load current .env values. Returns {} if no file exists."""
    if ENV_PATH.exists():
        return dotenv_values(ENV_PATH)
    return {}


def seed_env_file():
    """On first run, copy .env.example -> .env so comments are preserved."""
    if not ENV_PATH.exists():
        if ENV_EXAMPLE.exists():
            shutil.copy2(ENV_EXAMPLE, ENV_PATH)
        else:
            ENV_PATH.touch()


def is_required_complete(existing: dict) -> bool:
    """Are all 3 required settings configured?"""
    token = existing.get("TELEGRAM_BOT_TOKEN", "")
    uid = existing.get("ALLOWED_USER_ID", "")
    runner = existing.get("CLI_RUNNER", "")
    return (
        not _is_placeholder(token)
        and not _is_placeholder(uid)
        and bool(runner) and runner in ("claude", "gemini", "codex", "qwen", "generic")
    )


# ---------------------------------------------------------------------------
# Input helpers — the core prompting primitives
# ---------------------------------------------------------------------------

def prompt_value(label: str, *, default: str = "", existing: str = "",
                 validator=None, required: bool = False) -> str:
    """Prompt the user for a value with optional validation and defaults.

    - Shows [current: ...] if there's an existing value
    - Shows (default: ...) if there's a default
    - Pressing Enter keeps the existing value or uses the default
    - Retries on validation failure with a helpful message
    """
    while True:
        hint_parts = []
        if existing:
            hint_parts.append(f"current: {existing}")
        if default and default != existing:
            hint_parts.append(f"default: {default}")
        hint = f" [{', '.join(hint_parts)}]" if hint_parts else ""

        try:
            raw = input(f"  {label}{hint}: ").strip()
        except EOFError:
            raw = ""

        value = raw or existing or default

        if required and not value:
            print("    This field is required. Please enter a value.\n")
            continue

        if value and validator:
            ok, msg = validator(value)
            if not ok:
                print(f"    {msg}")
                print()
                continue
            if msg:
                print(f"    {msg}")

        return value


def prompt_yes_no(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        raw = input(f"  {label} [{hint}]: ").strip().lower()
    except EOFError:
        raw = ""
    if not raw:
        return default
    return raw in ("y", "yes")


def wait_for_enter(msg: str = "  Press Enter to continue..."):
    """Pause and wait for the user to press Enter."""
    try:
        input(msg)
    except EOFError:
        pass


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

def validate_telegram_token(token: str) -> tuple[bool, str]:
    """Validate a Telegram bot token by calling the getMe API."""
    token = token.strip()
    if ":" not in token:
        return False, (
            "That doesn't look like a bot token.\n"
            "    Tokens look like: 7123456789:AAHx1234567890abcdefg\n"
            "    (a number, then a colon, then letters and numbers)\n"
            "    You can get one from @BotFather on Telegram."
        )

    parts = token.split(":", 1)
    if not parts[0].isdigit():
        return False, (
            "The part before the colon should be all numbers.\n"
            "    Example: 7123456789:AAHx1234567890abcdefg"
        )

    print("    Checking with Telegram...", end=" ", flush=True)
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if data.get("ok"):
            name = data["result"].get("first_name", "your bot")
            username = data["result"].get("username", "")
            at_name = f" (@{username})" if username else ""
            return True, f"Connected! Your bot: {name}{at_name}"
        desc = data.get("description", "unknown error")
        if "Not Found" in desc or "Unauthorized" in desc:
            return False, (
                f"Telegram says: {desc}\n"
                "    This token might be incorrect or revoked.\n"
                "    Double-check with @BotFather on Telegram."
            )
        return False, f"Telegram says: {desc}"
    except httpx.TimeoutException:
        return True, (
            "Could not reach Telegram right now (timeout).\n"
            "    Token saved — it will be verified when the bot starts."
        )
    except httpx.ConnectError:
        return True, (
            "No internet connection.\n"
            "    Token saved — it will be verified when the bot starts."
        )
    except Exception as e:
        return True, (
            f"Could not verify ({e}).\n"
            "    Token saved — it will be verified when the bot starts."
        )


def validate_user_id(value: str) -> tuple[bool, str]:
    """Validate a Telegram user ID."""
    value = value.strip()
    if not value.isdigit():
        return False, (
            "User IDs are numbers only — no letters or spaces.\n"
            "    To find yours, message @userinfobot on Telegram."
        )
    if len(value) < 5:
        return False, (
            "That seems too short for a Telegram user ID.\n"
            "    IDs are usually 8-10 digits long.\n"
            "    To find yours, message @userinfobot on Telegram."
        )
    return True, ""


# ---------------------------------------------------------------------------
# Status dashboard
# ---------------------------------------------------------------------------

def show_dashboard(existing: dict):
    """Print the settings dashboard with current status for each item."""
    # 1. Token
    token = existing.get("TELEGRAM_BOT_TOKEN", "")
    if not _is_placeholder(token):
        token_display = mask_token(token)
    else:
        token_display = "not set  <-- required"

    # 2. User ID
    uid = existing.get("ALLOWED_USER_ID", "")
    if not _is_placeholder(uid):
        uid_display = uid
    else:
        uid_display = "not set  <-- required"

    # 2b. Name
    user_name = existing.get("USER_NAME", "")
    name_display = user_name if user_name else "not set (optional)"

    # 3. CLI
    runner = existing.get("CLI_RUNNER", "")
    if runner and runner in CLI_OPTIONS:
        cli_info = CLI_OPTIONS[runner]
        path = shutil.which(cli_info["command"])
        cli_display = cli_info["label"]
        if path:
            cli_display += f" (installed)"
        else:
            cli_display += f" (not found in PATH!)"
    elif runner == "generic":
        cmd = existing.get("CLI_COMMAND", "")
        cli_display = f"Custom: {cmd}" if cmd else "Generic (no command set)"
    else:
        cli_display = "not set  <-- required"

    # 4. Server
    host = existing.get("HOST", "0.0.0.0")
    port = existing.get("PORT", "8588")
    webhook = existing.get("WEBHOOK_URL", "")
    server_display = f"{host}:{port}"
    if webhook:
        truncated = webhook[:35] + "..." if len(webhook) > 35 else webhook
        server_display += f" -> {truncated}"

    # 5. Voice
    whisper = existing.get("WHISPER_MODEL", "")
    voice = existing.get("EDGE_TTS_VOICE", "")
    if whisper or voice:
        voice_display = f"model={whisper or 'base'}"
    else:
        voice_display = "not configured"

    # 6. Image generation
    gemini_key = existing.get("GEMINI_API_KEY", "")
    img_display = f"key: {mask_token(gemini_key)}" if gemini_key else "not configured"

    # 7. Memory
    mem_enabled = existing.get("MEMORY_ENABLED", "true")
    if mem_enabled.lower() in ("true", "1", "yes"):
        mem_dir = existing.get("MEMORY_DIR", "~/memories")
        mem_display = f"enabled ({mem_dir})"
    else:
        mem_display = "disabled"

    # 8. System prompt
    sys_prompt = existing.get("CLI_SYSTEM_PROMPT", "")
    if sys_prompt:
        prompt_display = f'"{sys_prompt[:35]}..."' if len(sys_prompt) > 35 else f'"{sys_prompt}"'
    else:
        prompt_display = "not set"

    # 9. Display preferences
    show_tools = existing.get("DISPLAY_SHOW_TOOLS", "true").lower() in ("true", "1", "yes")
    show_thoughts = existing.get("DISPLAY_SHOW_THOUGHTS", "true").lower() in ("true", "1", "yes")
    if show_tools and show_thoughts:
        display_display = "tools + thoughts (default)"
    elif show_tools:
        display_display = "tools only"
    elif show_thoughts:
        display_display = "thoughts only"
    else:
        display_display = "neither (clean output)"

    ready = is_required_complete(existing)

    print()
    print("  Your settings:")
    print()
    print(f"  1. Telegram Bot Token    -- {token_display}")
    print(f"  2. Your Telegram User ID -- {uid_display}")
    print(f"  2b. Your Name            -- {name_display}")
    print(f"  3. AI CLI                -- {cli_display}")
    print("  " + "-" * 44)
    print(f"  4. Server (host, port)   -- {server_display}")
    print(f"  5. Voice messages        -- {voice_display}")
    print(f"  6. Image generation      -- {img_display}")
    print(f"  7. Memory                -- {mem_display}")
    print(f"  8. System prompt         -- {prompt_display}")
    print(f"  9. Display preferences   -- {display_display}")
    print()
    if ready:
        print("  r. Run the bot")
    else:
        print("  r. Run the bot           (finish items 1-3 first)")
    print("  q. Save & quit")
    print()


# ---------------------------------------------------------------------------
# Step 1: Telegram Bot Token
# ---------------------------------------------------------------------------

def step_bot_token(existing: dict):
    print()
    print("=" * 46)
    print("  Step 1: Telegram Bot Token")
    print("=" * 46)
    print()
    print("  Every Telegram bot needs a unique token — it's like")
    print("  a password that lets this app send and receive messages")
    print("  through your bot.")
    print()
    print("  Here's how to get one (takes about 30 seconds):")
    print()
    print("    1. Open Telegram on your phone or computer")
    print("    2. Search for @BotFather and open a chat with it")
    print("    3. Send the message:  /newbot")
    print("    4. BotFather will ask you for a name and username")
    print("    5. After that, it gives you a token like:")
    print("       7123456789:AAHx1234567890abcdefghijk")
    print("    6. Copy that token and paste it below")
    print()

    current = existing.get("TELEGRAM_BOT_TOKEN", "")
    if not _is_placeholder(current):
        display = mask_token(current)
        print(f"  You already have a token set: {display}")
        print("  Press Enter to keep it, or paste a new one.")
        print()
    else:
        current = ""
        display = ""

    token = prompt_value("Paste your bot token here",
                         existing=display if current else "",
                         validator=validate_telegram_token,
                         required=True)

    # If user pressed Enter on the masked value, keep the original
    if token == display and current:
        print("    Keeping current token.\n")
    else:
        save_value("TELEGRAM_BOT_TOKEN", token)
        existing["TELEGRAM_BOT_TOKEN"] = token
        print()


# ---------------------------------------------------------------------------
# Step 1b: Your Name
# ---------------------------------------------------------------------------

def step_user_name(existing: dict):
    print()
    print("=" * 46)
    print("  What's your name?")
    print("=" * 46)
    print()
    print("  The bot will use your name to personalize its")
    print("  memory and greetings. Just your first name is fine.")
    print()

    current = existing.get("USER_NAME", "")
    if current:
        print(f"  Current name: {current}")
        print("  Press Enter to keep it, or type a new one.")
        print()

    name = prompt_value("Your first name", existing=current)
    if name:
        save_value("USER_NAME", name)
        existing["USER_NAME"] = name
    print()


# ---------------------------------------------------------------------------
# Step 2: Telegram User ID
# ---------------------------------------------------------------------------

def step_user_id(existing: dict):
    print()
    print("=" * 46)
    print("  Step 2: Your Telegram User ID")
    print("=" * 46)
    print()
    print("  For security, the bot only responds to YOU.")
    print("  It ignores messages from anyone else.")
    print()
    print("  To find your user ID:")
    print()
    print("    1. Open Telegram")
    print("    2. Search for @userinfobot and open a chat")
    print("    3. Send any message (even just 'hi')")
    print("    4. It replies with your user ID — a number like 987654321")
    print("    5. Copy that number and paste it below")
    print()

    current = existing.get("ALLOWED_USER_ID", "")
    if _is_placeholder(current):
        current = ""
    elif current:
        print(f"  Current user ID: {current}")
        print("  Press Enter to keep it, or type a new one.")
        print()

    uid = prompt_value("Your user ID",
                       existing=current,
                       validator=validate_user_id,
                       required=True)
    save_value("ALLOWED_USER_ID", uid)
    existing["ALLOWED_USER_ID"] = uid
    print()


# ---------------------------------------------------------------------------
# Step 3: AI CLI Selection
# ---------------------------------------------------------------------------

def step_cli_runner(existing: dict):
    print()
    print("=" * 46)
    print("  Step 3: Choose Your AI")
    print("=" * 46)
    print()
    print("  bridgebot connects your Telegram bot to an AI")
    print("  coding assistant that runs on your computer.")
    print()
    print("  Which AI do you want to chat with through Telegram?")
    print("  (The wizard will check if it's installed)")
    print()

    detected = detect_clis()
    current_runner = existing.get("CLI_RUNNER", "")

    # Build numbered menu
    options = []
    for i, (key, info) in enumerate(CLI_OPTIONS.items(), 1):
        path = detected.get(key)
        if path:
            status = "INSTALLED"
        else:
            status = "not installed"
        current_marker = "  <-- current" if key == current_runner else ""
        print(f"  {i}. {info['label']}  ({info['company']})")
        print(f"     {info['description']}")
        print(f"     Status: {status}{current_marker}")
        print()
        options.append(key)

    # Generic option
    generic_num = len(options) + 1
    current_marker = "  <-- current" if current_runner == "generic" else ""
    print(f"  {generic_num}. Other / Custom CLI")
    print("     Use any command-line tool that accepts a text prompt.")
    if current_runner == "generic":
        print(f"     Status: current{current_marker}")
    print()
    options.append("generic")

    # Smart default: current > first installed > claude
    if current_runner in options:
        default_num = options.index(current_runner) + 1
    else:
        default_num = 1
        for i, key in enumerate(options[:-1]):
            if detected.get(key):
                default_num = i + 1
                break

    raw = input(f"  Pick a number [{default_num}]: ").strip()
    choice_num = int(raw) if raw.isdigit() else default_num

    if choice_num < 1 or choice_num > len(options):
        choice_num = default_num

    chosen = options[choice_num - 1]

    # Handle generic — ask for the binary command
    if chosen == "generic":
        print()
        print("  What command runs your AI CLI?")
        print("  For example: 'my-ai-tool' or '/usr/local/bin/my-tool'")
        print()
        current_cmd = existing.get("CLI_COMMAND", "")
        cmd = prompt_value("Command name",
                           existing=current_cmd,
                           required=True)
        path = shutil.which(cmd)
        if path:
            print(f"    Found at: {path}")
        else:
            print(f"    Warning: '{cmd}' not found in your PATH right now.")
            print("    Make sure it's installed before running the bot.")
        save_value("CLI_COMMAND", cmd)
        existing["CLI_COMMAND"] = cmd
    else:
        # Check if the selected binary is installed
        info = CLI_OPTIONS[chosen]
        path = detected.get(chosen)
        if path:
            print(f"    {info['label']} found at: {path}")
        else:
            print()
            print(f"    {info['label']} is not installed yet.")
            print()
            print(f"    To install it, run:")
            print(f"      {info['install']}")
            print()
            print(f"    More info: {info['url']}")
            print()
            if not prompt_yes_no("  Save this choice anyway? (you can install it later)", default=True):
                return step_cli_runner(existing)  # let them pick again

        # Qwen Coder requires a one-time browser login
        if chosen == "qwen" and path:
            print()
            print("    Qwen Coder requires a one-time authentication.")
            print("    If you haven't done this yet, run:  qwen")
            print("    It will open your browser to log in with your qwen.ai account.")
            print("    After that, the bot will work without further login.")
            print("    Free tier: 1000 requests/day via qwen.ai OAuth.")
            print()

    save_value("CLI_RUNNER", chosen)
    existing["CLI_RUNNER"] = chosen
    print()


# ---------------------------------------------------------------------------
# Optional: Server settings
# ---------------------------------------------------------------------------

def config_server(existing: dict):
    print()
    print("=" * 46)
    print("  Server Settings")
    print("=" * 46)
    print()
    print("  The bot runs a small web server on your computer.")
    print("  Telegram sends messages to it via a 'webhook'.")
    print()
    print("  For most setups, the defaults work fine.")
    print()

    host = prompt_value("Host (what address to listen on)",
                        default="0.0.0.0",
                        existing=existing.get("HOST", "0.0.0.0"))
    save_value("HOST", host)
    existing["HOST"] = host

    port = prompt_value("Port (what port to listen on)",
                        default="8588",
                        existing=existing.get("PORT", "8588"))
    save_value("PORT", port)
    existing["PORT"] = port

    print()
    print("  Telegram needs a public URL to reach your bot.")
    print("  The recommended way is Tailscale Funnel (free, stable URL):")
    print()
    print("    1. Install: brew install tailscale && tailscale up")
    print("    2. Enable Funnel in your Tailscale admin console")
    print(f"    3. Run: tailscale serve --funnel --bg --https=443 http://localhost:{port}")
    print("    4. Your URL is: https://<your-machine>.<tailnet>.ts.net")
    print()

    # Try to auto-detect Tailscale URL
    detected_url = _detect_tailscale_url()
    if detected_url:
        print(f"  Tailscale detected! Your URL appears to be:")
        print(f"  {detected_url}")
        print()
        if prompt_yes_no("  Use this URL?", default=True):
            webhook = f"{detected_url}/webhook"
            save_value("WEBHOOK_URL", webhook)
            existing["WEBHOOK_URL"] = webhook
            print(f"    Saved: {webhook}")
            print()
            return
    else:
        print("  (Tailscale not detected — you can also use ngrok or paste any URL)")
        print()

    webhook = prompt_value("Webhook URL (public HTTPS URL + /webhook)",
                           existing=existing.get("WEBHOOK_URL", ""))
    if webhook:
        save_value("WEBHOOK_URL", webhook)
        existing["WEBHOOK_URL"] = webhook

    print()
    print("    Saved!")
    print()


# ---------------------------------------------------------------------------
# Optional: Voice messages
# ---------------------------------------------------------------------------

def config_voice(existing: dict):
    print()
    print("=" * 46)
    print("  Voice Messages")
    print("=" * 46)
    print()
    print("  This lets you send voice notes to the bot and get")
    print("  audio replies back — like talking to your AI.")
    print()
    print("  Requirements (install these first if you want voice):")
    print()
    print("    pip install faster-whisper edge-tts")
    print()
    print("  You also need ffmpeg installed on your system:")
    if platform.system() == "Darwin":
        print("    brew install ffmpeg")
    elif platform.system() == "Linux":
        print("    sudo apt install ffmpeg  (or your package manager)")
    else:
        print("    https://ffmpeg.org/download.html")
    print()

    # Check if ffmpeg is available
    if shutil.which("ffmpeg"):
        print("    ffmpeg: found")
    else:
        print("    ffmpeg: NOT FOUND — voice won't work without it")
    print()

    model = prompt_value("Whisper model for transcription\n"
                         "  (tiny = fastest, large = most accurate)",
                         default="base",
                         existing=existing.get("WHISPER_MODEL", "base"))
    save_value("WHISPER_MODEL", model)
    existing["WHISPER_MODEL"] = model

    print()
    voice = prompt_value("Text-to-speech voice\n"
                         "  (see edge-tts --list-voices for all options)",
                         default="en-US-AndrewNeural",
                         existing=existing.get("EDGE_TTS_VOICE", "en-US-AndrewNeural"))
    save_value("EDGE_TTS_VOICE", voice)
    existing["EDGE_TTS_VOICE"] = voice

    print()
    print("    Saved! Voice messages configured.")
    print()


# ---------------------------------------------------------------------------
# Optional: Image generation
# ---------------------------------------------------------------------------

def config_images(existing: dict):
    print()
    print("=" * 46)
    print("  Image Generation")
    print("=" * 46)
    print()
    print("  Send /imagine <description> in Telegram to generate images.")
    print()
    print("  This uses Google's Gemini API. You need a free API key:")
    print()
    print("    1. Go to https://aistudio.google.com/apikey")
    print("    2. Sign in with your Google account")
    print("    3. Click 'Create API Key'")
    print("    4. Copy the key and paste it below")
    print()

    current = existing.get("GEMINI_API_KEY", "")
    if current:
        display = mask_token(current)
        print(f"  Current key: {display}")
        print("  Press Enter to keep it, or paste a new one.")
        print()
    else:
        display = ""

    key = prompt_value("Gemini API key (leave blank to skip)", existing=display)
    if key and key != display:
        save_value("GEMINI_API_KEY", key)
        existing["GEMINI_API_KEY"] = key
        print("    Saved!")
    elif key == display and current:
        print("    Keeping current key.")
    else:
        print("    Skipped — you can add this later.")
    print()


# ---------------------------------------------------------------------------
# Optional: Memory
# ---------------------------------------------------------------------------

def config_memory(existing: dict):
    print()
    print("=" * 46)
    print("  Conversation Memory")
    print("=" * 46)
    print()
    print("  When enabled, the bot remembers your conversations")
    print("  and can recall relevant context from past chats.")
    print("  Use /remember in Telegram to save important info.")
    print()
    print("  Uses ChromaDB for storage. If not installed:")
    print("    pip install chromadb")
    print()

    current = existing.get("MEMORY_ENABLED", "true")
    enabled = current.lower() in ("true", "1", "yes")
    enabled = prompt_yes_no("Enable conversation memory?", default=enabled)
    save_value("MEMORY_ENABLED", "true" if enabled else "false")
    existing["MEMORY_ENABLED"] = "true" if enabled else "false"

    if enabled:
        print()
        mem_dir = prompt_value("Where to store memory files",
                               default="~/memories",
                               existing=existing.get("MEMORY_DIR", "~/memories"))
        save_value("MEMORY_DIR", mem_dir)
        existing["MEMORY_DIR"] = mem_dir

    print()
    print("    Saved!")
    print()


# ---------------------------------------------------------------------------
# Optional: System prompt
# ---------------------------------------------------------------------------

def config_prompt(existing: dict):
    print()
    print("=" * 46)
    print("  System Prompt")
    print("=" * 46)
    print()
    print("  A system prompt is a set of instructions that the AI")
    print("  follows in every conversation. For example:")
    print()
    print('    "Always respond in Spanish"')
    print('    "You are a Python expert. Keep answers concise."')
    print('    "Focus on security best practices"')
    print()
    print("  Leave blank if you don't need one — the AI will use")
    print("  its default behavior.")
    print()

    current = existing.get("CLI_SYSTEM_PROMPT", "")
    if current:
        print(f"  Current: \"{current[:60]}{'...' if len(current) > 60 else ''}\"")
        print("  Press Enter to keep it, or type a new one.")
        print("  Type 'clear' to remove it.")
        print()

    prompt = prompt_value("System prompt", existing=current)
    if prompt == "clear":
        save_value("CLI_SYSTEM_PROMPT", "")
        existing["CLI_SYSTEM_PROMPT"] = ""
        print("    Cleared!")
    elif prompt:
        save_value("CLI_SYSTEM_PROMPT", prompt)
        existing["CLI_SYSTEM_PROMPT"] = prompt
        print("    Saved!")
    print()


# ---------------------------------------------------------------------------
# Optional: Display preferences
# ---------------------------------------------------------------------------

def config_display(existing: dict):
    print()
    print("=" * 46)
    print("  Display Preferences")
    print("=" * 46)
    print()
    print("  By default your bot shows both tool progress and thinking steps.")
    print("  You can change this anytime with /show or /hide commands.")
    print()
    print("  What do you want to see when your bot is working?")
    print("  1. Both \u2014 code progress AND thinking steps (recommended)")
    print("  2. Code only \u2014 tool progress, no thinking")
    print("  3. Thoughts only \u2014 thinking steps, no tool progress")
    print("  4. Neither \u2014 just the final answer, nothing else")
    print()

    current_tools = existing.get("DISPLAY_SHOW_TOOLS", "true").lower() in ("true", "1", "yes")
    current_thoughts = existing.get("DISPLAY_SHOW_THOUGHTS", "true").lower() in ("true", "1", "yes")
    if current_tools and current_thoughts:
        current_display = "1"
    elif current_tools:
        current_display = "2"
    elif current_thoughts:
        current_display = "3"
    else:
        current_display = "4"

    display_choice = input(f"\n  Enter 1-4 [current: {current_display}]: ").strip() or current_display

    show_tools = display_choice in ("1", "2")
    show_thoughts = display_choice in ("1", "3")

    save_value("DISPLAY_SHOW_TOOLS", "true" if show_tools else "false")
    save_value("DISPLAY_SHOW_THOUGHTS", "true" if show_thoughts else "false")
    existing["DISPLAY_SHOW_TOOLS"] = "true" if show_tools else "false"
    existing["DISPLAY_SHOW_THOUGHTS"] = "true" if show_thoughts else "false"

    labels = []
    if show_tools:
        labels.append("tool indicators")
    if show_thoughts:
        labels.append("thinking blocks")
    what = " + ".join(labels) if labels else "final answers only"
    print(f"\n    Saved! Will show: {what}")
    print("    (Change anytime with /show or /hide in Telegram)")
    print()


# ---------------------------------------------------------------------------
# Run the bot
# ---------------------------------------------------------------------------

def run_bot(existing: dict):
    host = existing.get("HOST", "0.0.0.0")
    port = existing.get("PORT", "8588")

    # Pre-flight checks
    runner = existing.get("CLI_RUNNER", "claude")
    if runner in CLI_OPTIONS:
        cmd = CLI_OPTIONS[runner]["command"]
        if not shutil.which(cmd):
            print()
            print(f"    Warning: '{cmd}' is not installed or not in your PATH.")
            print(f"    The bot will start but won't be able to run AI queries.")
            print(f"    Install: {CLI_OPTIONS[runner]['install']}")
            print()
            if not prompt_yes_no("  Start anyway?", default=False):
                return

    webhook = existing.get("WEBHOOK_URL", "")
    if not webhook:
        # Try Tailscale auto-detect as a convenience
        detected = _detect_tailscale_url()
        if detected:
            webhook = f"{detected}/webhook"
            save_value("WEBHOOK_URL", webhook)
            existing["WEBHOOK_URL"] = webhook
            print(f"    Auto-detected Tailscale URL: {webhook}")

    # Kill any existing process on the port so uvicorn can bind
    _free_port(int(port))

    env = dict(os.environ)
    if webhook:
        env["WEBHOOK_URL"] = webhook

    mode = f"webhook -> {webhook}" if webhook else "polling (no webhook URL set)"
    print()
    print(f"    Starting bridgebot on {host}:{port}...")
    print(f"    Mode: {mode}")
    print("    Press Ctrl+C to stop.")
    print()

    # Use the venv Python if available, otherwise fall back to sys.executable
    venv_python = (
        PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else PROJECT_DIR / ".venv" / "bin" / "python"
    )
    python = str(venv_python) if venv_python.exists() else sys.executable

    # Start uvicorn
    server_proc = subprocess.Popen(
        [python, "-m", "uvicorn", "server:app",
         "--host", host, "--port", port],
        cwd=str(PROJECT_DIR),
        env=env,
    )

    try:
        server_proc.wait()
    except KeyboardInterrupt:
        print("\n\n    Bot stopped.\n")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=3)
        except Exception:
            server_proc.kill()


def _detect_tailscale_url() -> str | None:
    """Try to get the current machine's Tailscale Funnel URL (no port = HTTPS 443)."""
    if not shutil.which("tailscale"):
        return None
    try:
        import json
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        hostname = data.get("Self", {}).get("DNSName", "").rstrip(".")
        if not hostname:
            return None
        return f"https://{hostname}"
    except Exception:
        return None


def _free_port(port: int) -> None:
    """Kill any process listening on port so uvicorn can bind."""
    try:
        if sys.platform == "win32":
            # netstat + taskkill on Windows
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True
            )
            for pid in result.stdout.strip().splitlines():
                try:
                    subprocess.run(["kill", "-9", pid.strip()], capture_output=True)
                except Exception:
                    pass
    except Exception:
        pass


def _start_cloudflared_tunnel(port: str, existing: dict) -> str | None:
    """Start a cloudflared quick tunnel, capture the URL, register the Telegram webhook."""
    import re
    import time
    import threading

    token = existing.get("TELEGRAM_BOT_TOKEN", "")
    print()
    print("    Starting cloudflared tunnel...")

    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        print("    cloudflared not found.")
        return None

    url = None
    deadline = time.time() + 30
    for line in proc.stdout:
        if time.time() > deadline:
            break
        # Strip ANSI escape codes before matching
        clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
        m = re.search(r'https://[a-zA-Z0-9\-]+\.trycloudflare\.com', clean)
        if m:
            url = m.group(0).strip()
            break

    if not url:
        proc.terminate()
        print("    Timed out waiting for tunnel URL.")
        return None

    print(f"    Tunnel: {url}")
    print("    Waiting for tunnel DNS to propagate...", end="", flush=True)
    # Poll /health through the tunnel until Telegram can reach it (max 30s)
    import httpx as _httpx
    for _ in range(10):
        time.sleep(3)
        print(".", end="", flush=True)
        try:
            r = _httpx.get(f"{url}/health", timeout=5)
            if r.status_code < 500:
                break
        except Exception:
            pass
    print()

    # Register webhook with Telegram
    webhook_url = f"{url}/webhook"
    try:
        import httpx
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url},
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            print("    Webhook registered!")
            # Don't persist ephemeral trycloudflare.com URLs to .env
            existing["WEBHOOK_URL"] = webhook_url
        else:
            print(f"    Webhook registration failed: {result.get('description')}")
    except Exception as e:
        print(f"    Could not register webhook: {e}")

    # Keep cloudflared alive in background (exits when wizard process exits)
    threading.Thread(target=proc.wait, daemon=True).start()
    return url


# ---------------------------------------------------------------------------
# Summary / quit
# ---------------------------------------------------------------------------

def print_summary(existing: dict):
    host = existing.get("HOST", "0.0.0.0")
    port = existing.get("PORT", "8588")
    ready = is_required_complete(existing)

    print()
    print("=" * 46)
    if ready:
        print("  Setup complete!")
    else:
        print("  Settings saved (not fully configured yet)")
    print("=" * 46)
    print()
    print(f"  Your config is saved at:")
    print(f"  {ENV_PATH}")
    print()

    if ready:
        runner = existing.get("CLI_RUNNER", "claude")
        cli_name = CLI_OPTIONS.get(runner, {}).get("label", runner)

        print(f"  AI:      {cli_name}")
        print(f"  Server:  {host}:{port}")
        print()
        print("  Next steps:")
        print()
        print("  1. Start the bot:")
        print(f"     python -m uvicorn server:app --host {host} --port {port}")
        print()
        print("  2. Expose it via Tailscale Funnel (if not already):")
        print(f"     tailscale serve --funnel --bg --https=443 http://localhost:{port}")
        print("     (then re-run this wizard — it will auto-detect the URL)")
        print()
        print("  3. Set the webhook URL (if you haven't already):")
        print("     Re-run this wizard and choose option 4,")
        print("     or add WEBHOOK_URL=<your-url>/webhook to .env")
        print()
        print("  4. Open Telegram and message your bot!")
        print()
        print("  Tip: Run 'python setup_wizard.py' anytime to change settings.")
    else:
        missing = []
        if _is_placeholder(existing.get("TELEGRAM_BOT_TOKEN", "")):
            missing.append("Telegram bot token (option 1)")
        if _is_placeholder(existing.get("ALLOWED_USER_ID", "")):
            missing.append("Telegram user ID (option 2)")
        runner = existing.get("CLI_RUNNER", "")
        if not runner or runner not in ("claude", "gemini", "codex", "generic"):
            missing.append("AI CLI selection (option 3)")

        print("  Still needed:")
        for item in missing:
            print(f"    - {item}")
        print()
        print("  Run 'python setup_wizard.py' to finish setup.")

    print()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    try:
        _main()
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled. Any values already saved are in .env.")
        print("  Run 'python setup_wizard.py' to continue where you left off.\n")
        sys.exit(0)


def _main():
    seed_env_file()
    existing = load_existing()

    first_run = not is_required_complete(existing)

    # Welcome
    print()
    print("=" * 46)
    print("  bridgebot Setup")
    print("=" * 46)

    if first_run:
        print()
        print("  Welcome! This wizard will help you set up your")
        print("  Telegram bot in just a few steps.")
        print()
        print("  You'll need:")
        print("    - A Telegram account (on your phone or computer)")
        print("    - About 2 minutes")
        print()
        wait_for_enter("  Ready? Press Enter to start...")

        # Auto-walk through required steps
        step_bot_token(existing)
        existing = load_existing()

        step_user_id(existing)
        existing = load_existing()

        step_user_name(existing)
        existing = load_existing()

        step_cli_runner(existing)
        existing = load_existing()

    # Main menu loop
    showed_ready_msg = False
    while True:
        existing = load_existing()

        print()
        print("=" * 46)
        show_dashboard(existing)

        ready = is_required_complete(existing)
        default = "r" if ready else "1"

        if ready and not showed_ready_msg:
            if first_run:
                print("  Your bot is ready to go!")
                print("  Pick a number to configure more, r to run, q to quit.")
            else:
                print("  Pick a number to change, r to run, q to quit.")
            print()
            showed_ready_msg = True

        choice = input(f"  What would you like to do? [{default}]: ").strip().lower()
        if not choice:
            choice = default

        if choice == "1":
            step_bot_token(existing)
            showed_ready_msg = False
        elif choice == "2":
            step_user_id(existing)
            showed_ready_msg = False
        elif choice == "3":
            step_cli_runner(existing)
            showed_ready_msg = False
        elif choice == "4":
            config_server(existing)
        elif choice == "5":
            config_voice(existing)
        elif choice == "6":
            config_images(existing)
        elif choice == "7":
            config_memory(existing)
        elif choice == "8":
            config_prompt(existing)
        elif choice == "9":
            config_display(existing)
        elif choice == "10":
            print_system_deps()
        elif choice == "r":
            if ready:
                run_bot(existing)
            else:
                print()
                missing = []
                if _is_placeholder(existing.get("TELEGRAM_BOT_TOKEN", "")):
                    missing.append("1 (bot token)")
                if _is_placeholder(existing.get("ALLOWED_USER_ID", "")):
                    missing.append("2 (user ID)")
                r = existing.get("CLI_RUNNER", "")
                if not r or r not in ("claude", "gemini", "codex", "generic"):
                    missing.append("3 (AI CLI)")
                print(f"    Can't start yet — finish setting up: {', '.join(missing)}")
                print()
        elif choice == "q":
            print_summary(existing)
            break
        else:
            print("\n    Pick a number 1-10, r to run, or q to quit.\n")


if __name__ == "__main__":
    main()
