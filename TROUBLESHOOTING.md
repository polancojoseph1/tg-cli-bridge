# Troubleshooting Guide

## Bot doesn't respond to messages

**Check 1: Is the server running?**
```bash
# macOS (LaunchAgent)
launchctl list | grep tg-cli-bridge

# Linux (systemd)
systemctl --user status tg-cli-bridge-claude

# Manual start
python -m uvicorn server:app --host 0.0.0.0 --port 8588
```

**Check 2: Is the webhook registered?**
```bash
curl "https://api.telegram.org/bot<YOUR_TOKEN>/getWebhookInfo"
```
Look for `"url"` — it should point to your server's `/webhook` endpoint.

**Check 3: Can Telegram reach your server?**
```bash
# From another machine or phone browser, open:
https://your-domain.example.com/health
# Should return: {"status": "ok"}
```

---

## "CLI binary not found" on startup

The AI CLI (claude/gemini/etc) isn't in the PATH that the background service sees.

**Fix for LaunchAgent (macOS):**
```bash
# Re-run the install — it now auto-detects your Homebrew prefix
python cli.py install --name claude --port 8588
```

**Fix for manual runs:**
```bash
# Make sure you're in the venv and the CLI is in PATH
source .venv/bin/activate
which claude  # should print a path
```

---

## Telegram webhook 400 / HTML parse error

The bot sends a message with HTML formatting that Telegram rejects.

This is usually caused by **unescaped `<` or `>` in AI output**. The server automatically falls back to plain text on parse errors, so the message should still arrive, just without formatting.

If it happens frequently, check the logs for `HTML parse rejected` lines and report the failing message pattern as an issue.

---

## Voice messages not working

**Check ffmpeg is installed:**
```bash
which ffmpeg
ffmpeg -version
```

If missing:
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

**Check voice is enabled in .env:**
```bash
grep WHISPER_MODEL .env
# Should have: WHISPER_MODEL=base
```

**First run downloads the Whisper model** (~150MB for `base`). This can take a minute on the first transcription.

---

## Memory not working (`/memory` returns "Memory is disabled")

Check `MEMORY_ENABLED` in your `.env`:
```
MEMORY_ENABLED=true
```

Also check ChromaDB is installed:
```bash
pip install chromadb
```

ChromaDB requires `sqlite3` with a version >= 3.35. On older Ubuntu:
```bash
pip install pysqlite3-binary
```

---

## Image generation returns "API key not set"

Set `GEMINI_API_KEY` in your `.env`:
```
GEMINI_API_KEY=your-key-here
```

Get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

---

## Bot stops responding after a few hours

This is usually a stale webhook or a crashed process.

**Check logs:**
```bash
# macOS LaunchAgent logs
tail -100 ~/Library/Logs/tg-cli-bridge/claude.err.log

# Systemd logs
journalctl --user -u tg-cli-bridge-claude --since "1 hour ago"
```

**Restart the service:**
```bash
# macOS
launchctl unload ~/Library/LaunchAgents/tg-cli-bridge.claude.plist
sleep 2
launchctl load ~/Library/LaunchAgents/tg-cli-bridge.claude.plist

# Linux
systemctl --user restart tg-cli-bridge-claude

# Telegram
/server   (send this to the bot in Telegram)
```

---

## "Address already in use" on startup

Another process is on the port. The setup wizard's "r" option automatically clears the port. For manual runs:

```bash
# Find what's on port 8588
lsof -i :8588      # macOS/Linux
netstat -ano | findstr :8588  # Windows

# Kill it
kill <PID>         # macOS/Linux
taskkill /F /PID <PID>  # Windows
```

---

## Tailscale Funnel not routing correctly

```bash
# Check what's exposed
tailscale funnel status

# Re-enable if needed
tailscale funnel --bg --https=443 http://localhost:8588

# Verify the URL resolves
curl https://<your-hostname>.tail<id>.ts.net/health
```

---

## `pip install -r requirements.txt` fails

**Python version too old** — requires Python 3.11+:
```bash
python3 --version
# If < 3.11, install a newer Python (brew install python@3.12 on macOS)
```

**ChromaDB build failure on Linux** — needs build tools:
```bash
sudo apt install build-essential python3-dev
pip install chromadb
```

**onnxruntime wheel not found for your arch** — try:
```bash
pip install chromadb --no-deps
pip install onnxruntime
```

---

## Getting more debug output

```bash
# Set log level to DEBUG
LOG_LEVEL=DEBUG python -m uvicorn server:app --host 0.0.0.0 --port 8588

# Or tail the subprocess logs (one per CLI process)
ls ~/.tg-cli-bridge/subprocess_logs/
tail -f ~/.tg-cli-bridge/subprocess_logs/*.log
```

---

## Still stuck?

1. Check [existing issues](https://github.com/your-username/tg-cli-bridge/issues)
2. Run `/status` in Telegram for runtime diagnostics
3. Open a new issue with: OS, Python version, CLI runner, and the relevant log lines
