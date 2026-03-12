# Changelog

All notable changes to tg-cli-bridge are documented here.

Format: [Semantic Versioning](https://semver.org/)

---

## [Unreleased]

### Added
- `TG_BRIDGE_DATA_DIR` env var — override default runtime data directory (`~/.tg-cli-bridge`)
- `EDGAR_CONTACT` warning when unset, avoids silent fake-email fallback
- `check_system_deps()` in setup wizard — checks for node/npm, ffmpeg, tailscale, cloudflared
- `/help` now shows memory status (ON/OFF), ffmpeg availability, and voice TTS status
- `cli.py install` dynamically detects Homebrew prefix via `brew --prefix` — fixes cross-architecture PATH on Apple Silicon vs Intel
- Windows Task Scheduler support in `cli.py install`
- Voice TTS error cleanup — temp OGG files are removed even when conversion fails
- Gemini `list_directory` progress now correctly reads `dir_path` parameter (was always blank)
- Dockerfile now exposes port 8588 (was incorrectly set to 8585)
- `.gitignore` excludes per-runner `.env.*` files while preserving `.env.example`

### Changed
- All hardcoded personal paths replaced with `TG_BRIDGE_DATA_DIR`-aware paths
- Gemini API key now sent via `x-goog-api-key` header (was URL query param — less secure)
- `proactive_worker.py` generic copy (no project-specific names in user-facing strings)
- `.env.example` cleaned of personal data; added `EDGAR_CONTACT` and `OLLAMA_URL` examples

### Security
- Gemini API key moved from URL param to request header in `image_handler.py` and `agent_manager.py`
- EDGAR contact no longer falls back to a hardcoded email address

---

## [0.3.0] — 2026-02

### Added
- Multi-instance support — run parallel CLI sessions in one Telegram chat
- `@<id>` quick-message routing between instances
- Agent system (`/agent create|list|talk|task|fix|feedback|delete`)
- Proactive task worker — runs tasks in background, notifies on completion
- `/orch` — orchestration command (breaks tasks into parallel sub-agents)
- `/objective` — research who is pursuing a goal and what each company is doing
- `/model sonnet|opus` — switch model per instance
- Windows support (Task Scheduler via `cli.py install`)
- Qwen Coder runner (1000 free requests/day via qwen.ai OAuth)
- `/inst` family of subcommands (new, list, switch, rename, end)

### Changed
- Unified codebase: Claude, Gemini, Codex, Qwen runners all in one repo
- Session store now persists across restarts (SQLite)
- Memory handler uses per-user ChromaDB collections

---

## [0.2.0] — 2026-01

### Added
- Gemini CLI runner
- Codex CLI runner
- Voice message support (faster-whisper transcription + edge-tts or macOS `say`)
- Image generation via Gemini API (`/imagine`)
- SEC EDGAR research (`/research`, `/objective`)
- ChromaDB vector memory (`/remember`, `/memory`)
- LaunchAgent / systemd install via `cli.py`
- setup_wizard.py interactive setup
- Tailscale Funnel auto-detection in wizard
- Cloudflared quick-tunnel support

---

## [0.1.0] — 2025-12

### Added
- Initial release: Claude Code runner over Telegram
- Long-polling and webhook modes
- Markdown-to-Telegram HTML conversion
- Message splitting for long replies
- Voice message download + transcription
