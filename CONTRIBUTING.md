# Contributing to tg-cli-bridge

Thanks for your interest! This guide covers how to set up a dev environment, add new runners or optional modules, and submit a PR.

## Dev Setup

```bash
git clone https://github.com/polancojoseph1/tg-cli-bridge.git
cd tg-cli-bridge

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your bot token, user ID, and CLI runner
python setup_wizard.py          # or edit .env manually
```

Start the server:

```bash
python -m uvicorn server:app --host 0.0.0.0 --port 8585 --reload
```

Expose to Telegram (pick one):

```bash
ngrok http 8585
# or
cloudflared tunnel --url http://localhost:8585
```

Set `WEBHOOK_URL` in `.env` to the public URL.

## Project Structure

The key abstraction is the **runner** — a pluggable adapter that wraps any AI CLI. All runners live in `runners/` and implement the `RunnerBase` interface. `server.py` calls `create_runner()` which picks the right adapter based on `CLI_RUNNER` in `.env`.

Optional features are loaded with `try/except ImportError` in `server.py`. If the module file doesn't exist, that feature is silently disabled at startup. This keeps the core server minimal.

## Adding a New Runner

1. Create `runners/my_cli.py`:

```python
from .base import RunnerBase

class MyCLIRunner(RunnerBase):
    async def run(self, prompt: str, instance, send_fn, chat_id: int) -> None:
        # Stream output to Telegram
        ...

    async def run_query(self, prompt: str, timeout_secs: int = 120) -> str:
        # Return plain-text response (used by orchestrator, research, etc.)
        ...

    def stop(self) -> None: ...
    def new_session(self) -> None: ...
```

2. Register it in `runners/__init__.py`:

```python
from .my_cli import MyCLIRunner

def create_runner():
    runner = os.environ.get("CLI_RUNNER", "claude")
    if runner == "my_cli":
        return MyCLIRunner()
    ...
```

3. Set `CLI_RUNNER=my_cli` in `.env` and test with a simple message.

## Adding an Optional Module

Optional modules follow this pattern so the server degrades gracefully if the module is missing:

**In `server.py`** (already there for existing optional modules — add yours to the same block):

```python
try:
    import my_feature_handler
except ImportError:
    my_feature_handler = None
```

**In your module**, if it needs the runner:

```python
_runner = None

def init(runner) -> None:
    global _runner
    _runner = runner
```

**In `server.py` lifespan** (in the startup block):

```python
if my_feature_handler:
    my_feature_handler.init(runner)
```

**Guard usage in command handlers**:

```python
if not my_feature_handler:
    await send_message(chat_id, "⚠️ my_feature_handler not installed.")
    return
```

## PR Checklist

- [ ] Which CLI runner was tested (`claude` / `gemini` / `codex` / `generic`)?
- [ ] Which command or feature was tested end-to-end from Telegram?
- [ ] New optional deps added to `requirements.txt` with a comment explaining what they enable?
- [ ] New optional modules follow the `try/except ImportError` pattern in `server.py`?
- [ ] No hardcoded personal paths, emails, usernames, or API keys?
- [ ] `python -c "import server"` runs without errors?

## Reporting Bugs

Open a GitHub Issue with:
- Which CLI runner you're using
- The command or message that triggered the bug
- Relevant log output (run with `--log-level debug` for more detail)
- Your OS and Python version
