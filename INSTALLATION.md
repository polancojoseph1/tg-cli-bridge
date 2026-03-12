# Installation Guide

Platform-specific setup instructions for tg-cli-bridge.

## Prerequisites (all platforms)

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| Python | 3.11+ | 3.12 recommended |
| Node.js | 18+ | Required for all AI CLIs |
| pip | any | comes with Python |

Optional:
- **ffmpeg** — voice message support (send/receive audio)
- **tailscale** — stable webhook URLs (recommended)
- **chromadb** — persistent conversation memory

---

## macOS

### 1. Install dependencies

```bash
# Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python and Node
brew install python@3.12 node

# Optional: voice support
brew install ffmpeg
```

### 2. Clone and install

```bash
git clone https://github.com/your-username/tg-cli-bridge.git
cd tg-cli-bridge
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Install your AI CLI

```bash
# Pick one:
npm install -g @anthropic-ai/claude-code   # Claude
npm install -g @google/gemini-cli          # Gemini
npm install -g @qwen-code/qwen-code        # Qwen (free, 1000/day)
npm install -g @openai/codex               # Codex
```

Authenticate the CLI once:
```bash
claude   # or gemini / qwen / codex
```

### 4. Configure

```bash
python setup_wizard.py
```

### 5. Run as a background service (optional)

```bash
# Install as a LaunchAgent (auto-starts on login)
python cli.py install --name claude --port 8588

# Check status
launchctl list | grep tg-cli-bridge

# View logs
tail -f ~/Library/Logs/tg-cli-bridge/claude.log
```

---

## Linux

### 1. Install dependencies

**Ubuntu / Debian:**
```bash
sudo apt update
sudo apt install python3.12 python3.12-venv python3-pip nodejs npm
# Optional voice support:
sudo apt install ffmpeg
```

**Fedora / RHEL:**
```bash
sudo dnf install python3.12 nodejs npm ffmpeg
```

**Arch:**
```bash
sudo pacman -S python nodejs npm ffmpeg
```

### 2. Clone and install

```bash
git clone https://github.com/your-username/tg-cli-bridge.git
cd tg-cli-bridge
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Install your AI CLI

```bash
npm install -g @anthropic-ai/claude-code   # or gemini-cli / qwen-code / codex
```

### 4. Configure

```bash
python setup_wizard.py
```

### 5. Run as a systemd service (optional)

```bash
# Install as a user service (auto-starts on login)
python cli.py install --name claude --port 8588

# Check status
systemctl --user status tg-cli-bridge-claude

# View logs
journalctl --user -u tg-cli-bridge-claude -f
# or:
tail -f ~/.local/share/tg-cli-bridge/logs/claude.log
```

---

## Windows

### 1. Install dependencies

1. [Python 3.12](https://www.python.org/downloads/windows/) — check "Add to PATH"
2. [Node.js LTS](https://nodejs.org/en/download/) — includes npm
3. Optional: [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) — add to PATH for voice support

### 2. Clone and install

```cmd
git clone https://github.com/your-username/tg-cli-bridge.git
cd tg-cli-bridge
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Install your AI CLI

```cmd
npm install -g @anthropic-ai/claude-code
```

### 4. Configure

```cmd
python setup_wizard.py
```

### 5. Run as a scheduled task (optional)

```cmd
python cli.py install --name claude --port 8588
```

This creates a Task Scheduler entry that starts on login. If [NSSM](https://nssm.cc) is installed, it will offer crash-restart support.

---

## Docker

```bash
# Copy and edit your .env file
cp .env.example .env
# edit .env with your bot token, user ID, and CLI_RUNNER

docker build -t tg-cli-bridge .
docker run -d \
  --name tg-cli-bridge \
  -p 8588:8588 \
  -v ~/.tg-cli-bridge:/data \
  --env-file .env \
  -e TG_BRIDGE_DATA_DIR=/data \
  tg-cli-bridge
```

> Note: Docker mode does not include an AI CLI binary. Mount one into the container or use `CLI_RUNNER=generic` with a CLI accessible from inside the container.

---

## Webhook Setup

The bot needs a public HTTPS URL so Telegram can deliver messages.

### Option A: Tailscale Funnel (recommended — stable, free)

```bash
# Enable Funnel once (browser prompt)
tailscale funnel --bg --https=443 http://localhost:8588

# Your URL: https://<hostname>.tail<id>.ts.net
# Run setup_wizard.py — it auto-detects the URL
```

### Option B: Cloudflared quick tunnel (ephemeral)

```bash
cloudflared tunnel --url http://localhost:8588
# Copy the trycloudflare.com URL into setup_wizard.py option 4
```

### Option C: Manual

Set `WEBHOOK_URL=https://your-domain.example.com` in your `.env` file.

---

## Verifying the Installation

```bash
# Check bot is reachable
curl http://localhost:8588/health

# Check webhook is registered
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo"
```

Send `/status` to your bot in Telegram to see runtime info.
