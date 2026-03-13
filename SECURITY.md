# Security Policy

## Supported Versions

Only the latest release receives security fixes.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Send a private report to the maintainer:
- GitHub: use [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) on this repo
- Response time: within 7 days

## Security Architecture

### Authentication
- All incoming Telegram updates are validated against `ALLOWED_USER_IDS`
- Requests with unknown `from.id` are silently dropped (no error response to avoid leaking info)
- Bot token is never logged or included in error responses

### Secret Management
**Do not store secrets in the project directory.** Recommended layout:

```
~/.config/bridgebot/        # or any path outside the repo
  .env.claude
  .env.gemini
  .env.codex
```

Point each runner at its file:
```bash
export ENV_FILE=~/.config/bridgebot/.env.claude
```

Keep permissions tight:
```bash
chmod 700 ~/.config/bridgebot/
chmod 600 ~/.config/bridgebot/.env.*
```

The project ships a `.gitignore` that excludes `.env`, `.env.*`, and common secret file names. **Never commit real tokens.**

### API Keys
- Gemini API key is sent via `x-goog-api-key` request header, not as a URL query parameter, to prevent accidental exposure in server logs
- OpenAI/Perplexity keys are passed via `Authorization: Bearer` headers

### Subprocess Isolation
- The AI CLI runs as a subprocess under the same OS user
- There is no sandbox — the CLI can read/write any file the user can access
- Restrict `ALLOWED_USER_IDS` to only yourself (or trusted users who should have this level of access)

### Network
- The webhook server binds to `HOST` (default `0.0.0.0`) and `PORT` (default `8588`)
- Use a firewall or VPN (Tailscale recommended) to avoid exposing the port to the open internet
- The server validates that the `Content-Type` is `application/json` and that the payload comes from Telegram's IP ranges (via bot token validation on the first getMe call during startup)

### Memory
- ChromaDB stores conversation embeddings locally in `MEMORY_DIR`
- No data is sent to third-party embedding services — embeddings use local sentence-transformers
- Memory files are stored in plain text on disk; protect them with filesystem permissions

## Known Limitations
- The bot runs as the logged-in OS user — a malicious prompt could instruct the AI CLI to run destructive commands. Only give access to users you fully trust.
- Voice transcription (faster-whisper) runs locally; no audio is sent to external services.
