# tg-cli-bridge

**Talk to any AI CLI through Telegram.** One codebase, plug-and-play CLI adapters.

Pick your AI CLI (Claude Code, Gemini CLI, Codex CLI, or any custom tool), set your Telegram bot token, and start chatting from your phone.

## Supported CLIs

| CLI | `CLI_RUNNER` | What it wraps |
|-----|-------------|---------------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | `claude` | `claude -p --output-format stream-json` |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `gemini` | `gemini -p --yolo --output-format stream-json` |
| [Codex CLI](https://github.com/openai/codex) | `codex` | `codex exec --json` |
| Any CLI | `generic` | `<your-binary> <prompt>` |

## Quick Start (< 5 minutes)

### 1. Install your AI CLI

Make sure at least one AI CLI is installed and authenticated:

```bash
# Claude Code (Anthropic)
npm install -g @anthropic-ai/claude-code

# Gemini CLI (Google)
npm install -g @google/gemini-cli

# Codex CLI (OpenAI)
npm install -g @openai/codex
```

### 2. Clone and run the setup wizard

```bash
git clone https://github.com/polancojoseph1/tg-cli-bridge.git
cd tg-cli-bridge
pip install -r requirements.txt

python setup_wizard.py
```

The wizard walks you through everything:
- **Telegram bot token** — get one from [@BotFather](https://t.me/BotFather) (the wizard validates it)
- **Your Telegram user ID** — get it from [@userinfobot](https://t.me/userinfobot)
- **Which AI CLI to use** — the wizard auto-detects what's installed

Optional features (voice, image generation, memory, etc.) are configurable from the same menu. Re-run `python setup_wizard.py` anytime to change settings.

### 3. Start the bot

```bash
python -m uvicorn server:app --host 0.0.0.0 --port 8585
```

Or just press `r` in the setup wizard to launch directly.

### 4. Expose to the internet

The bot needs a public URL for Telegram webhooks. Use any tunneling tool:

```bash
# ngrok
ngrok http 8585

# cloudflared
cloudflared tunnel --url http://localhost:8585
```

Set the `WEBHOOK_URL` in your `.env` to the public URL, or the bot will auto-register on startup if `WEBHOOK_URL` is set.

## Features

- **Multi-instance sessions** — Run multiple conversations simultaneously (`/claude list`, `/claude new`)
- **Voice messages** — Send voice notes, get transcribed and answered (requires `faster-whisper` + `ffmpeg`)
- **Voice replies** — Bot responds with audio using Edge TTS
- **Image support** — Send photos for vision analysis
- **Image generation** — Generate images with `/imagine` (requires Gemini API key)
- **Vector memory** — ChromaDB-powered conversation memory with `/remember` (requires `chromadb`)
- **Task tracking** — Shared todo list with `/task add` and `/task done`
- **Agent system** — Named specialist agents with custom system prompts and skill packs
- **Specialist agents** — Create domain-specific agents and talk to them directly
- **Task orchestration** — Break complex tasks into parallel sub-agents with `/orch`
- **Proactive agents** — Schedule agents to run recurring tasks on a cron-like schedule
- **Company research** — SEC filings, government contracts, and news synthesis with `/research`
- **Screen recording** — Capture and send screen recordings with `/record` (macOS)
- **Group voice chat** — Join Telegram group voice chat with `/call` (requires Pyrogram setup)
- **Smart routing** — Ollama-based message routing across instances (requires local Ollama)
- **Live tool updates** — See what tools the AI is using in real-time

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message |
| `/help` | Detailed help |
| `/stop` | Stop current response |
| `/new` | Start fresh conversation |
| `/kill` | Force-kill all CLI processes |
| `/voice` | Toggle voice replies |
| `/model <name>` | Switch model (Claude only) |
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
| `/agent create <type> <name>` | Create a specialist agent |
| `/agent list` | Show all agents |
| `/agent talk <name>` | Switch to an agent instance |
| `/agent task <name> <task>` | Assign a one-off task to an agent |
| `/agent proactive <name> set <HH:MM> <task>` | Schedule a recurring agent task |
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
- `TELEGRAM_BOT_TOKEN` — From BotFather
- `ALLOWED_USER_ID` — Your Telegram user ID (restricts access)
- `CLI_RUNNER` — Which CLI to use (`claude`, `gemini`, `codex`, `generic`)

### Optional Features
- `MEMORY_ENABLED=true` — ChromaDB vector memory
- `GEMINI_API_KEY` — For image generation
- `CHROME_ENABLED=true` — Chrome browser extension (Claude only)
- `EDGAR_CONTACT` — Email for SEC EDGAR User-Agent (e.g. `research@example.com`)
- `TIMEZONE` — Timezone for scheduler (e.g. `America/New_York`)
- `TASK_TIMEOUT` — Max seconds per scheduled task (default: `300`)

## Optional Features Guide

Some features require additional modules and/or Python packages. All optional modules use graceful `try/except ImportError` — the server starts without them.

| Feature | Module | pip packages | System deps |
|---------|--------|-------------|-------------|
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
tg-cli-bridge/
├── setup_wizard.py        # Interactive setup — run this first
├── server.py              # FastAPI webhook server
├── config.py              # Environment config
├── instance_manager.py    # Multi-instance session manager
├── telegram_handler.py    # Telegram API client
├── runners/
│   ├── base.py            # Abstract runner interface
│   ├── claude.py          # Claude Code adapter
│   ├── gemini.py          # Gemini CLI adapter
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
- CLI subprocess management (spawn, stream, kill)
- Session tracking (resume conversations)
- System prompt injection (CLI-specific methods)
- Output parsing (stream-json, JSONL, plain text)

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
