# Gemini CLI Runner

## Setup

```bash
npm install -g @google/gemini-cli
gemini  # authenticate — opens browser for Google login
```

## .env settings

```env
CLI_RUNNER=gemini
# CLI_COMMAND=gemini          # auto-detected
# BOT_NAME=Gemini             # auto-detected
# GEMINI_API_KEY=...          # only needed for /imagine — not for the CLI itself
```

## Features

| Feature | Status |
|---------|--------|
| Streaming responses | Yes |
| Tool use (file read/write, shell) | Yes |
| Chrome browser integration | No |
| Voice messages | Yes (with ffmpeg) |
| Image generation | Yes (with `GEMINI_API_KEY`) |
| Multi-instance | Yes |

## Authentication

Gemini CLI uses Google OAuth. Run `gemini` once to authenticate. The token is stored in `~/.gemini/`. No API key is required for the CLI; `GEMINI_API_KEY` is only used for the `/imagine` image generation feature.

## Subprocess flags

```bash
gemini -p "<prompt>" --yolo --output-format stream-json
```

`--yolo` auto-approves tool calls without prompting. `--output-format stream-json` enables streaming.

## Notes

- The `list_directory` tool uses `dir_path` as the parameter key (not `path`). The bridge handles this automatically.
- Gemini uses Gemini 2.5 Flash by default. Model selection via `/model` is not yet supported for the Gemini runner.

## Troubleshooting

- **Blank progress for file listings** — update to latest bridgebot (fixed in v0.3.x)
- **Auth expired** — run `gemini` in terminal to re-authenticate
- **"gemini: command not found"** — reinstall: `npm install -g @google/gemini-cli`
