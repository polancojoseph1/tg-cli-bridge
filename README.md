# bridgebot

![GitHub stars](https://img.shields.io/github/stars/polancojoseph1/bridgebot?style=flat-square)
![GitHub forks](https://img.shields.io/github/forks/polancojoseph1/bridgebot?style=flat-square)
![GitHub issues](https://img.shields.io/github/issues/polancojoseph1/bridgebot?style=flat-square)
![GitHub license](https://img.shields.io/github/license/polancojoseph1/bridgebot?style=flat-square)
![GitHub last commit](https://img.shields.io/github/last-commit/polancojoseph1/bridgebot?style=flat-square)
![GitHub commit activity](https://img.shields.io/github/commit-activity/m/polancojoseph1/bridgebot?style=flat-square)
![GitHub release](https://img.shields.io/github/v/release/polancojoseph1/bridgebot?style=flat-square)

![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=flat-square&logo=telegram&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
![ChromaDB](https://img.shields.io/badge/ChromaDB-FF6F00?style=flat-square&logo=databricks&logoColor=white)

**Talk to any AI CLI through Telegram.** One codebase, plug-and-play CLI adapters.

Pick your AI CLI (Claude Code, Gemini CLI, Codex CLI, or any custom tool), set your Telegram bot token, and start chatting from your phone.

## Supported CLIs

| CLI | `CLI_RUNNER` | What it wraps |
| --- | --- | --- |
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude` | `claude -p --output-format stream-json` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `gemini` | `gemini -p --yolo --output-format stream-json` |
| [Qwen Coder](https://github.com/QwenLM/qwen-code) | `qwen` | `qwen --yolo --output-format stream-json` |
| [Codex CLI](https://github.com/openai/codex) | `codex` | `codex exec --json` |
| Any CLI | `generic` | `<your-binary> <prompt>` |

## Quick Start (< 5 minutes)

### 1. Install your AI CLI

Pick one and install it:

```
# Claude Code (Anthropic) — requires Anthropic account
npm install -g @anthropic-ai/claude-code
claude  # authenticate

# Gemini CLI (Google) — requires Google account
npm install -g @google/gemini-cli
gemini  # authenticate

# Qwen Coder (Alibaba) — 1000 free requests/day, no credit card
npm install -g @qwen-code/qwen-code
qwen  # authenticate via browser (qwen.ai account)

# Codex CLI (OpenAI) — requires OpenAI API key
npm install -g @openai/codex
```

### 2. Clone and set up

```
git clone https://github.com/polancojoseph1/bridgebot.git
cd bridgebot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Run the setup wizard

```
python setup_wizard.py
```

The wizard walks you through everything:

* **Telegram bot token** — create a bot with [@BotFather](https://t.me/BotFather), then paste the token
* **Your Telegram user ID** — get it from [@userinfobot](https://t.me/userinfobot)
* **Which AI CLI to use** — auto-detects what's installed, lets you pick
* **Webhook URL** — auto-detected from Tailscale if installed, or paste any URL

Re-run `python setup_wizard.py` anytime to change settings.

### 4. Expose to the internet (Tailscale Funnel — recommended)

The bot needs a public HTTPS URL for Telegram webhooks. Tailscale Funnel gives you a **stable, permanent URL** with no rate limits — free forever.

**One-time setup:**

```
# 1. Install Tailscale
brew install tailscale

# 2. Start the daemon and authenticate
sudo tailscaled &
tailscale up   # opens browser for login

# 3. Enable Funnel (one-time — click the link it prints if needed)
tailscale funnel --bg --https=443 http://localhost:8588

# 4. Get your stable webhook URL
tailscale status --json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print('https://'+d['Self']['DNSName'].rstrip('.'))"
# → https://your-machine.tail-xxxx.ts.net

# 5. Set it in .env (or re-run setup_wizard.py — it auto-detects Tailscale)
# WEBHOOK_URL=https://your-machine.tail-xxxx.ts.net/webhook
```

Your URL never changes, survives reboots, and Tailscale manages the tunnel automatically.

**Alternative:** paste any HTTPS URL when the wizard asks (ngrok, cloudflared, etc.).

```
curl -s "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<YOUR_URL>/webhook"
```

### 5. Start the bot

Press `r` in the setup wizard, or run directly:

```
python -m uvicorn server:app --host 0.0.0.0 --port 8588
```

Message your bot on Telegram — it should respond.

## Features

* **Multi-instance sessions** — Run multiple conversations simultaneously (`/claude list`, `/claude new`)
* **Voice messages** — Send voice notes, get transcribed and answered (requires `faster-whisper` + `ffmpeg`)
* **Voice replies** — Bot responds with audio using Edge TTS
* **Image support** — Send photos for vision analysis
* **Image generation** — Generate images with `/imagine` (requires Gemini API key)
* **Vector memory** — ChromaDB-powered conversation memory with `/remember` (requires `chromadb`)
* **Task tracking** — Shared todo list with `/task add` and `/task done`
* **Agent system** — Named specialist agents with custom system prompts and skill packs
* **Specialist agents** — Create domain-specific agents and talk to them directly
* **Task orchestration** — Break complex tasks into parallel sub-agents with `/orch`
* **Proactive agents** — Schedule agents to run recurring tasks on a cron-like schedule
* **Company research** — SEC filings, government contracts, and news synthesis with `/research`
* **Screen recording** — Capture and send screen recordings with `/record` (macOS)
* **Group voice chat** — Join Telegram group voice chat with `/call` (requires Pyrogram setup)
* **Smart routing** — Ollama-based message routing across instances (requires local Ollama)
* **Live tool updates** — See what tools the AI is using in real-time

## Commands

| Command | Description |
| --- | --- |
| `/start` | Show welcome message |
| `/help` | Detailed help |
| `/stop` | Stop current response |
| `/new` | Start fresh conversation |
| `/kill` | Force-kill all CLI processes |
| `/voice` | Toggle voice replies |
| `/model <n>` | Switch model (Claude only) |
| `/remember <text>` | Save to memory |
| `/memory <query>` | Search memory |
| `/task add <text>` | Add a task |
| `/task list` | Show tasks |
| `/task done <n>` | Complete task |
| `/imagine <prompt>` | Generate an image |
| `/chrome` | Toggle Chrome browser integration (Claude only) |
| `/status` | Show bot status |
| `/server` | Restart the server |
| **Agents** | |
| `/agent create <type> <n>` | Create a specialist agent |
| `/agent list` | Show all agents |
| `/agent talk <n>` | Switch to an agent instance |
| `/agent task <n> <task>` | Assign a one-off task to an agent |
| `/agent proactive <n> set <HH:MM> <task>` | Schedule a recurring agent task |
| `/agent proactive start` | Start the proactive worker |
| `/agent proactive stop` | Stop the proactive worker |
| `/agent proactive status` | Show scheduled tasks |
| **Research** | |
| `/research <company>` | Company intel: SEC filings, contracts, news |
| `/objective <goal>` | Who is pursuing a goal + what each company is doing |
| **Orchestration** | |
| `/orch <task>` | Break task into parallel agents, synthesize results |
| **Multi-instance** | |
| `/claude new <title>` | Start a new named Claude instance |
| `/claude list` | Show all instances |
| `/claude switch <id or title>` | Switch active instance |
| `/claude rename <id> <title>` | Rename an instance |
| `/claude end <id>` | Close an instance |
| **Voice chat** | |
| `/call` | Join Telegram group voice chat |
| `/endcall` | Leave voice chat |
| **Screen** | |
| `/record` | Start screen recording (macOS only) |
| `/stoprecord` | Stop and send recording |

## Configuration

All settings are in `.env`. See [`.env.example`](.env.example) for the full list.

### Required

* `TELEGRAM_BOT_TOKEN` — From BotFather
* `ALLOWED_USER_ID` — Your Telegram user ID (restricts access)
* `CLI_RUNNER` — Which CLI to use (`claude`, `gemini`, `codex`, `generic`)

### Optional Features

* `MEMORY_ENABLED=true` — ChromaDB vector memory
* `GEMINI_API_KEY` — For image generation
* `CHROME_ENABLED=true` — Chrome browser extension (Claude only)
* `EDGAR_CONTACT` — Email for SEC EDGAR User-Agent (e.g. `research@example.com`)
* `TIMEZONE` — Timezone for scheduler in IANA format, defaults to UTC (e.g. `America/New_York`, `Europe/London`)
* `TASK_TIMEOUT` — Max seconds per scheduled task (default: `300`)

## Optional Features Guide

Some features require additional modules and/or Python packages. All optional modules use graceful `try/except ImportError` — the server starts without them.

| Feature | Module | pip packages | System deps |
| --- | --- | --- | --- |
| Vector memory | `memory_handler.py` | `chromadb` | — |
| Voice transcription | `voice_handler.py` | `faster-whisper`, `edge-tts` | `ffmpeg` |
| Screen recording | `screen_recorder.py` | `Pillow` | macOS only |
| Group voice chat | `call_handler.py` | `pyrogram`, `pytgcalls` | `ffmpeg`, Pyrogram userbot session |
| Company research | `research_handler.py` | — (uses `httpx`, already core) | — |
| Task orchestration | `task_orchestrator.py` | — | — |
| Proactive agents | `proactive_worker.py` | — | — |
| Background scheduler | `scheduler.py` | — | — |

To install optional packages, uncomment the relevant lines in `requirements.txt` and run `pip install -r requirements.txt`.

## Architecture

```
bridgebot/
├── setup_wizard.py        # Interactive setup — run this first
├── server.py              # FastAPI webhook server
├── config.py              # Environment config
├── instance_manager.py    # Multi-instance session manager
├── telegram_handler.py    # Telegram API client
├── runners/
│   ├── base.py            # Abstract runner interface
│   ├── claude.py          # Claude Code adapter
│   ├── gemini.py          # Gemini CLI adapter
│   ├── qwen.py            # Qwen Coder adapter
│   ├── codex.py           # Codex CLI adapter
│   └── generic.py         # Any-CLI fallback
├── voice_handler.py       # Whisper + Edge TTS
├── memory_handler.py      # ChromaDB vector memory
├── health.py              # Uptime tracking
├── agent_registry.py      # Agent definitions and storage
├── agent_manager.py       # Agent lifecycle management
├── agent_skills.py        # Skill packs for agents
├── agent_memory.py        # Per-agent memory
├── router.py              # Ollama-based message router
├── task_handler.py        # Shared task list
├── image_handler.py       # Gemini image generation
├── health.py              # Uptime + message tracking
└── .env.example           # Config template

Optional modules (auto-detected at startup):
├── research_handler.py    # /research + /objective — SEC, contracts, news
├── task_orchestrator.py   # /orch — parallel sub-agent decomposition
├── proactive_worker.py    # /agent proactive — scheduled recurring tasks
├── scheduler.py           # Background file-based task scheduler
├── screen_recorder.py     # /record — macOS screen capture
└── call_handler.py        # /call — Pyrogram group voice chat
```

Each runner adapter handles:

* CLI subprocess management (spawn, stream, kill)
* Session tracking (resume conversations)
* System prompt injection (CLI-specific methods)
* Output parsing (stream-json, JSONL, plain text)

## Adding a Custom CLI

1. Create `runners/my_cli.py` extending `RunnerBase`
2. Implement `run()`, `run_query()`, `stop()`, `new_session()`
3. Add it to `runners/__init__.py`
4. Set `CLI_RUNNER=my_cli` in `.env`

See [`runners/generic.py`](runners/generic.py) for a minimal example.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, how to add runners and optional modules, and the PR checklist.

## License

MIT
