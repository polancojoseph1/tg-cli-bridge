#!/usr/bin/env python3
"""Bridgebot Setup Wizard — Web UI

Run:  python setup_wizard_ui.py
Opens http://localhost:7891 in your browser automatically.

Walks you through every setting step by step.
Re-run anytime to update your configuration.
"""

import os
import sys
import shutil
import webbrowser
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-activate venv
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.resolve()
_VENV_PYTHON = (
    _PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if sys.platform == "win32"
    else _PROJECT_ROOT / ".venv" / "bin" / "python"
)

if _VENV_PYTHON.exists() and not sys.executable.startswith(str(_PROJECT_ROOT / ".venv")):
    if sys.platform == "win32":
        import subprocess as _sp
        result = _sp.run([str(_VENV_PYTHON)] + sys.argv)
        sys.exit(result.returncode)
    else:
        os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON)] + sys.argv)

# ---------------------------------------------------------------------------
# Imports (after venv activation)
# ---------------------------------------------------------------------------

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel
    import uvicorn
    from dotenv import set_key, dotenv_values
except ImportError as e:
    print(f"\n  Missing dependency: {e}")
    print("  Run:  pip install -r requirements.txt\n")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).parent.resolve()
ENV_PATH = PROJECT_DIR / ".env"
WA_ENV_PATH = Path.home() / ".jefe" / "secrets" / ".env.whatsapp"
WA_RUNNER_URL = "http://127.0.0.1:8591"
PORT = 7891
_PLACEHOLDERS = {"your-bot-token-here", "your-telegram-user-id", ""}

FREE_PROVIDERS = [
    {
        "id": "GROQ_API_KEY",
        "name": "Groq",
        "badge": "EASIEST + MOST GENEROUS",
        "badge_color": "green",
        "why": "Simplest signup of all 11. Email + password, key ready in 60 seconds.",
        "free_tier": "~14,400 requests/day — the most generous free tier",
        "steps": [
            "Go to <a href='https://console.groq.com' target='_blank'>console.groq.com</a>",
            "Click <strong>Sign Up</strong> — enter your email and a password",
            "Check your inbox and click the confirmation link",
            "Click <strong>API Keys</strong> in the left sidebar",
            "Click <strong>Create API Key</strong>, name it anything, copy it",
            "Paste it in the field below",
        ],
        "is_cli": False,
    },
    {
        "id": "COHERE_API_KEY",
        "name": "Cohere",
        "badge": "KEY AUTO-PROVIDED",
        "badge_color": "blue",
        "why": "Signs you up AND hands you a key automatically — no hunting for it.",
        "free_tier": "Generous free trial, no credit card required",
        "steps": [
            "Go to <a href='https://dashboard.cohere.com' target='_blank'>dashboard.cohere.com</a>",
            "Click <strong>Sign Up</strong> — email or Google login",
            "Your default API key is already visible on the dashboard when you land",
            "Copy it and paste it below — that's it",
        ],
        "is_cli": False,
    },
    {
        "id": "OPENROUTER_API_KEY",
        "name": "OpenRouter",
        "badge": "SOCIAL LOGIN",
        "badge_color": "purple",
        "why": "One login unlocks dozens of free AI models at once. Google or GitHub login.",
        "free_tier": "Multiple completely free models — no credits or payment required",
        "steps": [
            "Go to <a href='https://openrouter.ai/keys' target='_blank'>openrouter.ai/keys</a>",
            "Click <strong>Sign In</strong> — use Google or GitHub (no new password needed)",
            "Click <strong>Create Key</strong>, give it any name",
            "Copy the key and paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "GEMINI_API_KEY",
        "name": "Google Gemini",
        "badge": "USE YOUR GMAIL",
        "badge_color": "orange",
        "why": "If you have Gmail, this is literally 3 clicks.",
        "free_tier": "1,500 requests/day, 15 per minute — no credit card",
        "steps": [
            "Go to <a href='https://aistudio.google.com/app/apikey' target='_blank'>aistudio.google.com/app/apikey</a>",
            "Sign in with your Google account (Gmail works)",
            "Click <strong>Create API key</strong>",
            "Copy the key and paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "TOGETHER_API_KEY",
        "name": "Together AI",
        "badge": "FREE CREDITS",
        "badge_color": "teal",
        "why": "Email or Google signup, free credits added automatically on signup.",
        "free_tier": "Free credits on signup + permanently free Llama models",
        "steps": [
            "Go to <a href='https://api.together.xyz' target='_blank'>api.together.xyz</a>",
            "Click <strong>Sign Up</strong> — email or Google login",
            "Go to <strong>Settings</strong> in the top right",
            "Click <strong>API Keys</strong>, copy your key, paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "MISTRAL_API_KEY",
        "name": "Mistral",
        "badge": "EUROPEAN AI",
        "badge_color": "orange",
        "why": "Straightforward signup. Clean dashboard, key in under 2 minutes.",
        "free_tier": "Free tier on Mistral Small — no credit card required",
        "steps": [
            "Go to <a href='https://console.mistral.ai' target='_blank'>console.mistral.ai</a>",
            "Click <strong>Sign Up</strong> — email or Google",
            "Go to <strong>API Keys</strong> in the left menu",
            "Click <strong>Create new key</strong>, copy it, paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "CEREBRAS_API_KEY",
        "name": "Cerebras",
        "badge": "2,000 TOKENS/SEC",
        "badge_color": "yellow",
        "why": "Ultra-fast AI (~2,000 tokens/sec). Email signup with verification.",
        "free_tier": "Free tier — fastest AI provider in the world",
        "steps": [
            "Go to <a href='https://cloud.cerebras.ai' target='_blank'>cloud.cerebras.ai</a>",
            "Click <strong>Sign Up</strong> — enter your email and a password",
            "Verify your email, then log in",
            "Go to <strong>API Keys</strong> and click <strong>Create new key</strong>",
            "Copy the key and paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "HF_API_KEY",
        "name": "Hugging Face",
        "badge": "HUNDREDS OF MODELS",
        "badge_color": "yellow",
        "why": "Access to hundreds of open-source AI models. Slightly more steps for the token.",
        "free_tier": "Free inference API — hundreds of open-source models",
        "steps": [
            "Go to <a href='https://huggingface.co' target='_blank'>huggingface.co</a> and create a free account",
            "Once logged in, go to <a href='https://huggingface.co/settings/tokens' target='_blank'>huggingface.co/settings/tokens</a>",
            "Click <strong>New token</strong>",
            "Give it any name, set the role to <strong>Read</strong>, click <strong>Generate</strong>",
            "Copy the token and paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "SAMBANOVA_API_KEY",
        "name": "SambaNova",
        "badge": "400 REQ/DAY",
        "badge_color": "blue",
        "why": "Fast free tier on powerful models. Less known but easy signup.",
        "free_tier": "400 requests/day free on fast Llama models",
        "steps": [
            "Go to <a href='https://cloud.sambanova.ai' target='_blank'>cloud.sambanova.ai</a>",
            "Click <strong>Sign Up</strong> — enter your email and a password",
            "Verify your email, then log in",
            "Find the <strong>API Key</strong> section in your dashboard",
            "Copy the key and paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "NVIDIA_API_KEY",
        "name": "NVIDIA NIM",
        "badge": "POWERFUL",
        "badge_color": "green",
        "why": "Most steps of the API providers, but NVIDIA's models are excellent.",
        "free_tier": "Free credits on signup — good for ~1,000 requests",
        "steps": [
            "Go to <a href='https://build.nvidia.com' target='_blank'>build.nvidia.com</a>",
            "Click <strong>Login</strong> in the top right",
            "Create an NVIDIA account if you don't have one (email signup)",
            "Once logged in, click on any model (e.g. Llama 3.3 70B)",
            "Click <strong>Get API Key</strong> on the right side of the page",
            "Copy the key and paste it below",
        ],
        "is_cli": False,
    },
    {
        "id": "QWEN_CLI",
        "name": "Qwen Coder",
        "badge": "NO API KEY NEEDED",
        "badge_color": "purple",
        "why": "Install once via npm, log in with your browser — no key to copy.",
        "free_tier": "1,000 free requests/day via qwen.ai OAuth",
        "steps": [
            "You need <strong>Node.js</strong> installed — check with: <code>node --version</code>",
            "Open your terminal and run: <code>npm install -g @qwen-code/qwen-code</code>",
            "After install, run: <code>qwen</code>",
            "A browser window will open — sign up or log in at qwen.ai (Alibaba account)",
            "Once logged in, come back here and click <strong>Check Installation</strong>",
        ],
        "is_cli": True,
    },
]

CLI_OPTIONS = {
    "claude":  {"label": "Claude Code",  "company": "Anthropic",        "command": "claude",  "install": "npm install -g @anthropic-ai/claude-code"},
    "gemini":  {"label": "Gemini CLI",   "company": "Google",           "command": "gemini",  "install": "npm install -g @google/gemini-cli"},
    "codex":   {"label": "Codex CLI",    "company": "OpenAI",           "command": "codex",   "install": "npm install -g @openai/codex"},
    "qwen":    {"label": "Qwen Coder",   "company": "Alibaba / QwenLM", "command": "qwen",    "install": "npm install -g @qwen-code/qwen-code"},
    "freecode": {"label": "FreeCode",    "company": "FreeCode",          "command": "freecode", "install": "Install from https://github.com/polancojoseph1/freecode"},
}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Bridgebot Setup", docs_url=None, redoc_url=None)


class SaveRequest(BaseModel):
    key: str
    value: str


def read_env() -> dict:
    if ENV_PATH.exists():
        return dict(dotenv_values(str(ENV_PATH)))
    return {}


def mask(value: str) -> str:
    if not value or len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-3:]


def is_set(value: str | None) -> bool:
    return bool(value) and value not in _PLACEHOLDERS


@app.get("/api/config")
def get_config():
    env = read_env()
    result = {}
    for p in FREE_PROVIDERS:
        pid = p["id"]
        if p["is_cli"]:
            result[pid] = {"configured": shutil.which("qwen") is not None, "value": "", "masked": ""}
        else:
            val = env.get(pid, "")
            result[pid] = {"configured": is_set(val), "value": "", "masked": mask(val) if is_set(val) else ""}

    # Core settings
    for key in ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID", "CLI_RUNNER"]:
        val = env.get(key, "")
        result[key] = {"configured": is_set(val), "value": val if key == "CLI_RUNNER" else "", "masked": mask(val) if is_set(val) else ""}

    result["CLI_RUNNER"]["value"] = env.get("CLI_RUNNER", "")

    # Bridge Cloud settings
    for key in ["BRIDGE_CLOUD_API_KEY", "OPENROUTER_MASTER_KEY"]:
        val = env.get(key, "")
        result[key] = {"configured": is_set(val), "value": "", "masked": mask(val) if is_set(val) else ""}

    # CLI detection
    result["_cli_detect"] = {name: shutil.which(info["command"]) is not None for name, info in CLI_OPTIONS.items()}

    return JSONResponse(result)


@app.post("/api/save")
def save_config(req: SaveRequest):
    set_key(str(ENV_PATH), req.key, req.value)
    return {"ok": True}


@app.get("/api/detect-qwen")
def detect_qwen():
    found = shutil.which("qwen") is not None
    return {"installed": found}


@app.post("/api/clear")
def clear_config(req: SaveRequest):
    from dotenv import unset_key
    unset_key(str(ENV_PATH), req.key)
    config_cache = read_env()  # noqa: F841
    return {"ok": True}


@app.get("/api/generate-bc-key")
def generate_bc_key():
    """Generate a random BRIDGE_CLOUD_API_KEY and save it to .env."""
    import secrets as _sec
    key = "bc_live_" + _sec.token_urlsafe(32)
    set_key(str(ENV_PATH), "BRIDGE_CLOUD_API_KEY", key)
    return JSONResponse({"key": key, "masked": mask(key)})


@app.get("/api/bc-info")
def bc_info():
    """Return Bridge Cloud connection info (server URL detection)."""
    import socket
    env = read_env()
    # Try to detect Tailscale URL from env
    tailscale_url = env.get("TAILSCALE_URL", "") or env.get("EXTERNAL_URL", "")
    if not tailscale_url:
        try:
            hostname = socket.gethostname()
        except Exception:
            hostname = "your-server"
        tailscale_url = f"https://{hostname}"
    tailscale_url = tailscale_url.rstrip("/")
    bc_key = env.get("BRIDGE_CLOUD_API_KEY", "")
    or_key = env.get("OPENROUTER_MASTER_KEY", "")
    return JSONResponse({
        "server_url": tailscale_url,
        "bc_key_set": is_set(bc_key),
        "bc_key_masked": mask(bc_key) if is_set(bc_key) else "",
        "or_key_set": is_set(or_key),
    })


@app.get("/api/wa-status")
async def wa_status():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{WA_RUNNER_URL}/wa/status")
            return JSONResponse(r.json())
    except Exception as e:
        return JSONResponse({"bridge_reachable": False, "connected": False, "error": str(e)})


@app.get("/api/wa-qr.png")
async def wa_qr_proxy():
    import httpx
    from fastapi.responses import Response
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{WA_RUNNER_URL}/wa/qr.png")
            if r.status_code == 200:
                return Response(content=r.content, media_type="image/png")
    except Exception:
        pass
    return JSONResponse({"error": "QR not ready"}, status_code=404)


class WaSaveRequest(BaseModel):
    key: str
    value: str


@app.post("/api/save-wa")
def save_wa_config(req: WaSaveRequest):
    WA_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(WA_ENV_PATH), req.key, req.value)
    return {"ok": True}


@app.get("/api/wa-pairing-code")
async def wa_pairing_code():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{WA_RUNNER_URL}/wa/pairing-code")
            # The bridgebot endpoint returns plain text; parse it out
            text = r.text.strip()
            # Match the code in XXXX-XXXX format
            import re
            match = re.search(r'\b([A-Z0-9]{4}-[A-Z0-9]{4})\b', text)
            if match:
                return JSONResponse({"code": match.group(1)})
    except Exception:
        pass
    return JSONResponse({"code": None})


@app.post("/api/restart-wa-bridge")
async def restart_wa_bridge():
    import subprocess
    import os
    plist = os.path.expanduser("~/Library/LaunchAgents/jefe.whatsapp-bridge.plist")
    auth_dir = os.path.expanduser("~/.jefe/wa-auth")
    try:
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        import time
        time.sleep(1)
        # Clear stale pairing code so poll detects the new one
        for f in ["pairing_code.txt", "pairing_code.ready"]:
            p = os.path.join(auth_dir, f)
            if os.path.exists(p):
                os.remove(p)
        subprocess.run(["launchctl", "load", plist], capture_output=True)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.get("/api/config-wa")
def get_wa_config():
    wa_env = dict(dotenv_values(str(WA_ENV_PATH))) if WA_ENV_PATH.exists() else {}
    phone = wa_env.get("WA_PHONE_NUMBER", "")
    owner_jid = wa_env.get("WA_OWNER_JID", "")
    return JSONResponse({
        "phone_set": bool(phone.strip()),
        "phone_masked": mask(phone) if phone.strip() else "",
        "owner_jid": owner_jid,
    })


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    providers_json = __import__("json").dumps(FREE_PROVIDERS)
    cli_options_json = __import__("json").dumps(CLI_OPTIONS)
    return HTMLResponse(content=build_html(providers_json, cli_options_json))


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def build_html(providers_json: str, cli_options_json: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bridgebot Setup</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #3fb950;
    --green-dim: #1a3a21;
    --blue: #58a6ff;
    --blue-dim: #0d2137;
    --purple: #bc8cff;
    --yellow: #d29922;
    --orange: #f0883e;
    --teal: #39d353;
    --red: #f85149;
    --accent: #3fb950;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, 'Segoe UI', sans-serif;
    min-height: 100vh;
    padding-bottom: 60px;
  }}

  /* Header */
  .header {{
    background: var(--card);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
    position: sticky;
    top: 0;
    z-index: 100;
  }}
  .header-logo {{
    font-size: 22px;
    font-weight: 700;
    color: var(--green);
    font-family: monospace;
  }}
  .header-sub {{ color: var(--muted); font-size: 13px; }}

  /* Steps nav */
  .steps-nav {{
    display: flex;
    gap: 0;
    padding: 20px 24px 0;
    max-width: 860px;
    margin: 0 auto;
  }}
  .step-item {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    cursor: pointer;
    opacity: 0.4;
    transition: opacity 0.2s;
  }}
  .step-item.active {{ opacity: 1; }}
  .step-item.done {{ opacity: 0.7; cursor: pointer; }}
  .step-item.done:hover {{ opacity: 1; }}
  .step-num {{
    width: 28px;
    height: 28px;
    border-radius: 50%;
    border: 2px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    font-weight: 700;
    flex-shrink: 0;
    transition: all 0.2s;
  }}
  .step-item.active .step-num {{ border-color: var(--green); color: var(--green); background: var(--green-dim); }}
  .step-item.done .step-num {{ border-color: var(--green); background: var(--green); color: #000; }}
  .step-label {{ font-size: 13px; font-weight: 500; }}
  .step-connector {{ flex: 1; height: 1px; background: var(--border); margin: 0 8px; max-width: 40px; }}

  /* Step content animation */
  @keyframes fadeSlideIn {{
    from {{ opacity: 0; transform: translateY(10px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}

  /* Main content */
  .main {{ max-width: 860px; margin: 24px auto; padding: 0 24px; animation: fadeSlideIn 0.25s ease; }}

  /* Cards */
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 28px;
    margin-bottom: 16px;
  }}
  .card-title {{ font-size: 20px; font-weight: 700; margin-bottom: 8px; }}
  .card-sub {{ color: var(--muted); font-size: 14px; line-height: 1.6; }}

  /* Welcome hero */
  .hero-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 12px;
    margin-top: 20px;
  }}
  .hero-stat {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    text-align: center;
  }}
  .hero-stat-num {{ font-size: 28px; font-weight: 700; color: var(--green); font-family: monospace; }}
  .hero-stat-label {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

  /* Buttons */
  .btn {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 10px 20px;
    border-radius: 8px;
    border: none;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }}
  .btn-primary {{ background: var(--green); color: #000; }}
  .btn-primary:hover {{ background: #4ac963; transform: translateY(-1px); }}
  .btn-secondary {{ background: transparent; border: 1px solid var(--border); color: var(--text); }}
  .btn-secondary:hover {{ border-color: var(--green); color: var(--green); }}
  .btn-ghost {{ background: transparent; color: var(--muted); font-size: 13px; padding: 6px 12px; }}
  .btn-ghost:hover {{ color: var(--text); }}
  .btn-sm {{ padding: 6px 14px; font-size: 13px; }}
  .btn-row {{ display: flex; gap: 10px; align-items: center; margin-top: 20px; }}
  .btn.loading {{ opacity: 0.7; pointer-events: none; }}

  /* Form fields */
  .field {{ margin-bottom: 20px; }}
  .field-label {{ font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px; display: block; text-transform: uppercase; letter-spacing: 0.05em; }}
  .field-input {{
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
    color: var(--text);
    font-family: monospace;
    font-size: 14px;
    transition: border-color 0.15s;
    outline: none;
  }}
  .field-input:focus {{ border-color: var(--blue); }}
  .field-input.saved {{ border-color: var(--green); }}
  .field-hint {{ font-size: 12px; color: var(--muted); margin-top: 6px; }}
  .saved-badge {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--green);
    background: var(--green-dim);
    border-radius: 4px;
    padding: 3px 8px;
    margin-top: 6px;
  }}

  /* Input wrapper with eye toggle */
  .input-wrapper {{ position: relative; }}
  .input-wrapper .field-input {{
    padding-right: 72px;
  }}
  .input-wrapper .toggle-eye {{
    position: absolute; right: 12px; top: 50%; transform: translateY(-50%);
    background: none; border: none; color: var(--muted); cursor: pointer; font-size: 16px; padding: 4px;
  }}
  .input-wrapper .toggle-eye:hover {{ color: var(--text); }}
  .input-wrapper .clear-btn {{
    position: absolute; right: 40px; top: 50%; transform: translateY(-50%);
    background: none; border: none; color: var(--muted); cursor: pointer; font-size: 16px; padding: 4px;
  }}
  .input-wrapper .clear-btn:hover {{ color: var(--red); }}

  /* Runner cards */
  .runner-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }}
  .runner-card {{
    background: var(--bg);
    border: 2px solid var(--border);
    border-radius: 10px;
    padding: 18px;
    cursor: pointer;
    transition: all 0.15s;
  }}
  .runner-card:hover {{ border-color: var(--blue); transform: translateY(-2px); box-shadow: 0 4px 16px rgba(0,0,0,0.3); }}
  .runner-card.selected {{ border-color: var(--green); background: var(--green-dim); }}
  .runner-name {{ font-size: 16px; font-weight: 700; }}
  .runner-company {{ font-size: 12px; color: var(--muted); }}
  .runner-desc {{ font-size: 13px; color: var(--muted); margin-top: 6px; }}
  .runner-status {{ font-size: 11px; margin-top: 8px; }}
  .status-ok {{ color: var(--green); }}
  .status-missing {{ color: var(--yellow); }}
  .runner-free-preview {{
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 10px;
  }}
  .provider-pip {{
    font-size: 11px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 8px;
    color: var(--muted);
    font-family: monospace;
  }}
  .provider-pip.set {{ border-color: var(--green); color: var(--green); background: var(--green-dim); }}

  /* Free hero card */
  .free-hero {{
    background:
      linear-gradient(var(--card), var(--card)) padding-box,
      linear-gradient(135deg, #3fb950, #58a6ff, #bc8cff) border-box;
    border: 2px solid transparent;
    border-radius: 10px;
    padding: 22px;
    cursor: pointer;
    transition: all 0.15s;
    margin-bottom: 0;
    box-shadow: 0 0 20px rgba(63,185,80,0.08);
  }}
  .free-hero:hover {{ box-shadow: 0 0 24px rgba(63,185,80,0.14); transform: translateY(-2px); }}
  .free-hero.selected {{ box-shadow: 0 0 28px rgba(63,185,80,0.18); }}

  /* Divider label */
  .divider-label {{
    text-align: center; font-size: 12px; color: var(--muted);
    margin: 16px 0; display: flex; align-items: center; gap: 10px;
  }}
  .divider-label::before, .divider-label::after {{
    content: ''; flex: 1; height: 1px; background: var(--border);
  }}

  /* Provider walkthrough */
  .provider-progress {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .progress-bar-wrap {{
    flex: 1;
    background: var(--border);
    border-radius: 4px;
    height: 8px;
    overflow: hidden;
  }}
  @keyframes progressGrow {{ from {{ width: 0; }} }}
  .progress-bar-fill {{
    height: 100%;
    background: var(--green);
    border-radius: 4px;
    transition: width 0.4s;
    animation: progressGrow 0.5s ease;
  }}
  .progress-text {{ font-size: 13px; font-weight: 700; color: var(--green); font-family: monospace; white-space: nowrap; }}
  .progress-label {{ font-size: 12px; color: var(--muted); white-space: nowrap; }}

  /* All-done banner */
  .all-done-banner {{
    background: linear-gradient(135deg, var(--green-dim), var(--blue-dim));
    border: 1px solid var(--green);
    border-radius: 10px;
    padding: 16px 20px;
    text-align: center;
    font-size: 15px;
    color: var(--green);
    font-weight: 700;
    margin-bottom: 16px;
    animation: fadeSlideIn 0.3s ease;
  }}

  .provider-card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 12px;
    overflow: hidden;
    transition: all 0.2s;
  }}
  .provider-card.configured {{ border-left: 3px solid var(--green); border-color: var(--green); }}
  .provider-card.skipped {{ opacity: 0.5; }}
  .provider-header {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 16px 20px;
    cursor: pointer;
    user-select: none;
  }}
  .provider-name {{ font-size: 15px; font-weight: 700; flex: 1; }}
  .provider-badge {{
    font-size: 10px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 4px;
    letter-spacing: 0.05em;
  }}
  .badge-green {{ background: var(--green-dim); color: var(--green); border: 1px solid var(--green); }}
  .badge-blue {{ background: #0d2137; color: var(--blue); border: 1px solid var(--blue); }}
  .badge-purple {{ background: #1a0f2e; color: var(--purple); border: 1px solid var(--purple); }}
  .badge-yellow {{ background: #2d2008; color: var(--yellow); border: 1px solid var(--yellow); }}
  .badge-orange {{ background: #2d1608; color: var(--orange); border: 1px solid var(--orange); }}
  .badge-teal {{ background: #0a2d1a; color: var(--teal); border: 1px solid var(--teal); }}

  @keyframes checkPop {{
    0%   {{ transform: scale(0.5); opacity: 0; }}
    70%  {{ transform: scale(1.2); }}
    100% {{ transform: scale(1); opacity: 1; }}
  }}
  .provider-check {{ font-size: 18px; }}
  .provider-check.animate {{ animation: checkPop 0.35s ease forwards; }}
  .provider-chevron {{ color: var(--muted); transition: transform 0.2s; font-size: 14px; }}
  .provider-card.open .provider-chevron {{ transform: rotate(90deg); }}

  /* Accordion with smooth max-height transition */
  .provider-body {{
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.35s ease, padding 0.2s;
    padding: 0 20px;
    border-top: 0px solid var(--border);
  }}
  .provider-card.open .provider-body {{
    max-height: 900px;
    padding: 0 20px 20px;
    border-top: 1px solid var(--border);
  }}

  .provider-meta {{
    display: flex;
    gap: 16px;
    padding: 12px 0;
    font-size: 13px;
  }}
  .meta-item {{ display: flex; flex-direction: column; gap: 2px; }}
  .meta-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; }}
  .meta-value {{ color: var(--text); }}

  .steps-list {{ margin: 12px 0; padding-left: 0; list-style: none; }}
  .steps-list li {{
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 6px 0;
    font-size: 14px;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }}
  .steps-list li:last-child {{ border-bottom: none; }}
  .step-dot {{
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 700;
    color: var(--muted);
    flex-shrink: 0;
    margin-top: 1px;
  }}
  .steps-list li a {{ color: var(--blue); }}

  .key-input-row {{ display: flex; gap: 8px; margin-top: 16px; align-items: flex-start; }}
  .key-input {{ flex: 1; }}
  .cli-note {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
    margin-top: 16px;
    font-size: 13px;
    color: var(--muted);
  }}
  .cli-note code {{
    background: var(--border);
    border-radius: 4px;
    padding: 2px 6px;
    font-family: monospace;
    color: var(--text);
  }}

  /* Clear link in free keys */
  .clear-link {{
    font-size: 12px;
    color: var(--muted);
    background: none;
    border: none;
    cursor: pointer;
    padding: 0 4px;
    margin-left: 6px;
    text-decoration: underline;
    transition: color 0.15s;
  }}
  .clear-link:hover {{ color: var(--red); }}

  /* Summary */
  .summary-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 16px; }}
  .summary-row {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px 16px;
    display: flex;
    align-items: center;
    gap: 10px;
  }}
  .summary-row.ok {{ border-color: var(--green); }}
  .summary-row.warn {{ border-color: var(--yellow); }}
  .summary-icon {{ font-size: 18px; flex-shrink: 0; }}
  .summary-row-text {{ flex: 1; }}
  .summary-row-label {{ font-size: 13px; font-weight: 600; }}
  .summary-row-val {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}

  .launch-box {{
    background: var(--green-dim);
    border: 1px solid var(--green);
    border-radius: 12px;
    padding: 24px;
    margin-top: 20px;
    text-align: center;
  }}
  .launch-title {{ font-size: 18px; font-weight: 700; color: var(--green); }}
  .launch-sub {{ font-size: 14px; color: var(--muted); margin-top: 6px; margin-bottom: 16px; }}
  .launch-cmd {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    font-family: monospace;
    font-size: 14px;
    color: var(--green);
    display: inline-block;
    cursor: pointer;
    margin-top: 8px;
  }}
  .launch-cmd:hover {{ border-color: var(--green); }}

  /* Toasts */
  .toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--card);
    border: 1px solid var(--green);
    border-radius: 8px;
    padding: 12px 18px;
    font-size: 14px;
    color: var(--green);
    transform: translateY(100px);
    opacity: 0;
    transition: all 0.3s;
    z-index: 999;
    pointer-events: none;
  }}
  .toast.show {{ transform: translateY(0); opacity: 1; }}
  .toast.error {{ border-color: var(--red); color: var(--red); }}

  /* Utils */
  .mt-4 {{ margin-top: 16px; }}
  .mt-2 {{ margin-top: 8px; }}
  .text-green {{ color: var(--green); }}
  .text-muted {{ color: var(--muted); }}
  .text-sm {{ font-size: 13px; }}
  .hidden {{ display: none !important; }}
  code {{ font-family: monospace; background: var(--border); border-radius: 4px; padding: 2px 6px; font-size: 13px; }}

  /* Mobile responsive */
  @media (max-width: 640px) {{
    .steps-nav {{ gap: 4px; overflow-x: auto; }}
    .step-label {{ display: none; }}
    .hero-grid {{ grid-template-columns: 1fr 1fr; }}
    .runner-grid {{ grid-template-columns: 1fr; }}
    .summary-grid {{ grid-template-columns: 1fr; }}
    .main {{ padding: 0 12px; }}
    .card {{ padding: 20px 16px; }}
    .btn-row {{ flex-direction: column; }}
    .btn-row .btn {{ width: 100%; justify-content: center; }}
    .key-input-row {{ flex-direction: column; }}
    .key-input-row .btn {{ width: 100%; }}
    .header {{ padding: 12px 16px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-logo">⚡ bridgebot</div>
  <div class="header-sub">Setup Wizard</div>
</div>

<div class="steps-nav" id="stepsNav">
  <!-- Rendered by JS -->
</div>

<div class="main" id="mainContent">
  <!-- Rendered by JS -->
</div>

<div class="toast" id="toast"></div>

<script>
const PROVIDERS = {providers_json};
const CLI_OPTIONS = {cli_options_json};

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let config = {{}};
let waConfig = {{}};
let tgExpanded = true;
let waExpanded = false;
let currentStep = 0;
const STEPS = ['Welcome', 'Platform', 'AI Runner', 'Free Keys', 'Bridge Cloud', 'Done'];

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {{
  [config, waConfig] = await Promise.all([
    fetch('/api/config').then(r => r.json()),
    fetch('/api/config-wa').then(r => r.json()).catch(() => ({{}})),
  ]);
  // Determine starting step
  if (!config.TELEGRAM_BOT_TOKEN?.configured) currentStep = 1;
  else if (!config.ALLOWED_USER_ID?.configured) currentStep = 1;
  else if (!config.CLI_RUNNER?.value) currentStep = 2;
  else if (config.CLI_RUNNER?.value === 'free') currentStep = 3;
  else if (!config.BRIDGE_CLOUD_API_KEY?.configured) currentStep = 4;
  else currentStep = 5;
  renderAll();
}}

function renderAll() {{
  renderStepsNav();
  renderStep();
}}

// ---------------------------------------------------------------------------
// Steps nav
// ---------------------------------------------------------------------------
function renderStepsNav() {{
  const nav = document.getElementById('stepsNav');
  let html = '';
  STEPS.forEach((label, i) => {{
    const cls = i === currentStep ? 'active' : (i < currentStep ? 'done' : '');
    const checkmark = i < currentStep ? '✓' : (i + 1);
    html += `<div class="step-item ${{cls}}" onclick="goStep(${{i}})">
      <div class="step-num">${{checkmark}}</div>
      <div class="step-label">${{label}}</div>
    </div>`;
    if (i < STEPS.length - 1) html += '<div class="step-connector"></div>';
  }});
  nav.innerHTML = html;
}}

function goStep(i) {{
  if (i <= currentStep || i < currentStep + 1) {{
    currentStep = i;
    renderAll();
    window.scrollTo(0, 0);
  }}
}}

function nextStep() {{
  currentStep = Math.min(currentStep + 1, STEPS.length - 1);
  renderAll();
  window.scrollTo(0, 0);
}}

// ---------------------------------------------------------------------------
// Render step
// ---------------------------------------------------------------------------
function renderStep() {{
  const el = document.getElementById('mainContent');
  if (currentStep === 0) el.innerHTML = renderWelcome();
  else if (currentStep === 1) {{
    el.innerHTML = renderPlatform();
    if (selectedPlatform === 'whatsapp') setTimeout(checkWaStatus, 100);
  }}
  else if (currentStep === 2) el.innerHTML = renderRunner();
  else if (currentStep === 3) el.innerHTML = renderFreeKeys();
  else if (currentStep === 4) {{ el.innerHTML = renderBridgeCloud(); setTimeout(loadBcInfo, 50); }}
  else el.innerHTML = renderDone();
}}

// ---------------------------------------------------------------------------
// Step 0: Welcome
// ---------------------------------------------------------------------------
function renderWelcome() {{
  const freeCount = countConfigured();
  return `
  <div class="card">
    <div class="card-title">Welcome to Bridgebot 👋</div>
    <div class="card-sub">
      Bridgebot connects your Telegram to AI agents — Claude, Gemini, Codex, Qwen, or a pool of 11 free providers that rotate automatically so you never hit a limit.
      <br><br>
      This wizard will walk you through everything. It takes about 5 minutes to get the basic setup running, and another 10-15 minutes if you want all 11 free API keys (which means you can run your bot at <strong style="color:var(--green)">$0/month</strong>).
    </div>
    <div class="hero-grid">
      <div class="hero-stat">
        <div class="hero-stat-num">11</div>
        <div class="hero-stat-label">Free AI Providers</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-num">$0</div>
        <div class="hero-stat-label">Monthly Cost</div>
      </div>
      <div class="hero-stat">
        <div class="hero-stat-num">${{freeCount}}/11</div>
        <div class="hero-stat-label">Configured</div>
      </div>
    </div>
    <div class="btn-row">
      <button class="btn btn-primary" onclick="nextStep()">Get Started →</button>
    </div>
  </div>`;
}}

// ---------------------------------------------------------------------------
// Step 1: Platform
// ---------------------------------------------------------------------------
function renderPlatform() {{
  const tgReady = config.TELEGRAM_BOT_TOKEN?.configured && config.ALLOWED_USER_ID?.configured;
  const waConnected = waConfig.connected;
  return `
  <div class="card">
    <div class="card-title">Configure Your Platforms</div>
    <div class="card-sub">
      Set up one or both platforms — click a card to expand its settings. Telegram and WhatsApp can run side by side.
    </div>
    <div class="runner-grid mt-4">
      <div class="runner-card ${{tgExpanded ? 'selected' : ''}}" onclick="togglePlatformSection('telegram')">
        <div style="font-size:28px;margin-bottom:8px">✈️</div>
        <div class="runner-name">Telegram</div>
        <div class="runner-desc">Create a bot via @BotFather. Fast setup, rich features, works worldwide.</div>
        ${{tgReady ? '<div class="runner-status status-ok" style="margin-top:8px">✓ Configured</div>' : ''}}
      </div>
      <div class="runner-card ${{waExpanded ? 'selected' : ''}}" onclick="togglePlatformSection('whatsapp')">
        <div style="font-size:28px;margin-bottom:8px">💬</div>
        <div class="runner-name">WhatsApp</div>
        <div class="runner-desc">Link your existing WhatsApp. QR or phone number pairing.</div>
        ${{waConnected
          ? '<div class="runner-status status-ok" style="margin-top:8px">✓ Connected</div>'
          : '<div style="font-size:11px;color:var(--muted);margin-top:8px">Optional</div>'}}
      </div>
    </div>
  </div>
  ${{tgExpanded ? renderTelegramFields() : ''}}
  ${{waExpanded ? renderWhatsAppSetup() : ''}}
  <div class="card">
    <div class="btn-row">
      ${{tgReady
        ? `<button class="btn btn-primary" onclick="nextStep()">Continue →</button>`
        : `<button class="btn btn-secondary" onclick="saveAndContinue()">Save & Continue →</button>`
      }}
      <button class="btn btn-ghost" onclick="nextStep()">Skip for now</button>
    </div>
  </div>`;
}}

function togglePlatformSection(p) {{
  if (p === 'telegram') tgExpanded = !tgExpanded;
  else waExpanded = !waExpanded;
  renderStep();
}}

function renderTelegramFields() {{
  const tokenSet = config.TELEGRAM_BOT_TOKEN?.configured;
  const uidSet = config.ALLOWED_USER_ID?.configured;
  const tokenMasked = config.TELEGRAM_BOT_TOKEN?.masked || '';
  const uidMasked = config.ALLOWED_USER_ID?.masked || '';

  return `
  <div class="card">
    <div class="card-title">Telegram Setup</div>
    <div class="card-sub">
      You need a Telegram bot token and your personal user ID. The bot will only respond to your user ID — it's private by default.
    </div>

    <div class="mt-4"></div>

    <div class="field">
      <label class="field-label">Bot Token</label>
      <div class="input-wrapper">
        <input class="field-input ${{tokenSet ? 'saved' : ''}}" id="botToken" type="password"
          placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz..."
          value="${{tokenSet ? tokenMasked : ''}}"
          oninput="onTokenChange(this)"
          onfocus="if(this.value===this.getAttribute('data-masked'))this.value=''"
          data-masked="${{tokenMasked}}"
        />
        ${{tokenSet ? `<button class="clear-btn" onclick="clearCoreKey('TELEGRAM_BOT_TOKEN')" title="Clear">✕</button>` : ''}}
        <button class="toggle-eye" onclick="toggleEye('botToken', this)" title="Show/hide">👁</button>
      </div>
      ${{tokenSet ? `<div class="saved-badge">✓ Saved: ${{tokenMasked}}</div>` : `
      <div class="field-hint">
        Get one from <a href="https://t.me/BotFather" target="_blank" style="color:var(--blue)">@BotFather</a> on Telegram.
        Send /newbot, follow the prompts, copy the token it gives you.
      </div>`}}
    </div>

    <div class="field">
      <label class="field-label">Your Telegram User ID</label>
      <div class="input-wrapper">
        <input class="field-input ${{uidSet ? 'saved' : ''}}" id="userId" type="text"
          placeholder="123456789"
          value="${{uidSet ? uidMasked : ''}}"
          oninput="onUserIdChange(this)"
          onfocus="if(this.value===this.getAttribute('data-masked'))this.value=''"
          data-masked="${{uidMasked}}"
        />
        ${{uidSet ? `<button class="clear-btn" onclick="clearCoreKey('ALLOWED_USER_ID')" title="Clear">✕</button>` : ''}}
        <button class="toggle-eye" onclick="toggleEye('userId', this)" title="Show/hide">👁</button>
      </div>
      ${{uidSet ? `<div class="saved-badge">✓ Saved: ${{uidMasked}}</div>` : `
      <div class="field-hint">
        Don't know your ID? Message <a href="https://t.me/userinfobot" target="_blank" style="color:var(--blue)">@userinfobot</a> on Telegram — it replies with your numeric ID.
      </div>`}}
    </div>

  </div>`;
}}

function renderWhatsAppSetup() {{
  const phoneSet = waConfig.phone_set;
  const phoneMasked = waConfig.phone_masked || '';
  return `
  <div class="card" id="wa-setup-card">
    <div class="card-title">Link WhatsApp</div>
    <div class="card-sub">
      Two ways to link your WhatsApp account. Enter your number below for a pairing code, or scan the QR code directly.
    </div>

    <div id="wa-status-banner" style="margin:16px 0;padding:12px 16px;border-radius:8px;font-size:14px;background:var(--bg);border:1px solid var(--border);color:var(--muted)">
      Checking connection...
    </div>

    <div style="margin-top:20px">
      <div style="font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px">Option B — Phone number pairing</div>
      <div class="field">
        <label class="field-label">WhatsApp Phone Number</label>
        <div class="input-wrapper">
          <input class="field-input ${{phoneSet ? 'saved' : ''}}" id="waPhone" type="text"
            placeholder="16465551234 (digits only, include country code)"
            value="${{phoneSet ? phoneMasked : ''}}"
            onfocus="if(this.value===this.getAttribute('data-masked'))this.value=''"
            data-masked="${{phoneMasked}}"
          />
        </div>
        <div class="field-hint">Include country code, digits only — e.g. US: 16465551234.</div>
      </div>
      <button class="btn btn-secondary btn-sm" onclick="saveWaPhone()">Save &amp; Get Pairing Code</button>

      <div id="wa-pairing-box" style="display:none;margin-top:20px;padding:20px;background:var(--bg);border:2px solid var(--blue);border-radius:12px;text-align:center">
        <div style="font-size:13px;color:var(--muted);margin-bottom:10px">Open WhatsApp → <strong>Linked Devices</strong> → <strong>Link a Device</strong> → <strong>Link with phone number instead</strong> → enter this code</div>
        <div id="wa-pairing-code" style="font-size:40px;font-weight:700;letter-spacing:.2em;color:var(--fg);font-family:monospace">——————</div>
        <div style="margin-top:12px;display:flex;align-items:center;justify-content:center;gap:12px">
          <div id="wa-pairing-countdown" style="font-size:13px;color:var(--yellow)"></div>
          <button class="btn btn-ghost btn-sm" onclick="refreshPairingCode()">↻ New Code</button>
        </div>
      </div>
    </div>

    <div style="margin:24px 0 12px;font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">Option A — QR Code</div>
    <div style="text-align:center;padding:20px;background:var(--bg);border:1px solid var(--border);border-radius:10px">
      <img id="wa-qr-img" src="/api/wa-qr.png?t=${{Date.now()}}" style="width:220px;height:220px;border-radius:8px;display:block;margin:0 auto"
        onerror="this.style.display='none';document.getElementById('wa-qr-none').style.display='block'"
        onload="this.style.display='block';document.getElementById('wa-qr-none').style.display='none'"
      />
      <div id="wa-qr-none" style="display:none;padding:32px 16px;color:var(--muted);font-size:14px">
        QR not ready yet. Make sure the WhatsApp bridge is running, then click Refresh.
      </div>
      <div style="margin-top:14px;display:flex;gap:8px;justify-content:center">
        <button class="btn btn-secondary btn-sm" onclick="refreshWaQr()">↻ Refresh QR</button>
        <button class="btn btn-secondary btn-sm" onclick="checkWaStatus()">Check Status</button>
      </div>
      <div style="font-size:12px;color:var(--muted);margin-top:8px">QR expires every ~20s — click Refresh if it expires before you scan</div>
    </div>

  </div>`;
}}

async function checkWaStatus() {{
  const banner = document.getElementById('wa-status-banner');
  if (!banner) return;
  try {{
    const r = await fetch('/api/wa-status').then(r => r.json());
    waConfig.connected = r.connected;
    if (r.connected) {{
      banner.style.cssText = 'margin:16px 0;padding:12px 16px;border-radius:8px;font-size:14px;background:var(--green-dim);border:1px solid var(--green);color:var(--green)';
      banner.innerHTML = '✅ WhatsApp connected! Your bot is ready to receive messages on WhatsApp.';
    }} else if (!r.bridge_reachable) {{
      banner.style.cssText = 'margin:16px 0;padding:12px 16px;border-radius:8px;font-size:14px;background:var(--bg);border:1px solid var(--yellow);color:var(--yellow)';
      banner.innerHTML = '⚠ WhatsApp bridge not reachable. Make sure <strong>jefe.whatsapp-bridge</strong> is running (<code>launchctl list | grep jefe</code>).';
    }} else {{
      banner.style.cssText = 'margin:16px 0;padding:12px 16px;border-radius:8px;font-size:14px;background:var(--blue-dim);border:1px solid var(--blue);color:var(--blue)';
      banner.innerHTML = '⏳ Bridge is running but not yet linked. Scan the QR code below to connect your WhatsApp account.';
    }}
  }} catch(e) {{
    if (banner) banner.innerHTML = '⚠ Could not reach WhatsApp runner at port 8591.';
  }}
}}

function refreshWaQr() {{
  const img = document.getElementById('wa-qr-img');
  const none = document.getElementById('wa-qr-none');
  if (!img) return;
  img.style.display = 'block';
  if (none) none.style.display = 'none';
  img.src = `/api/wa-qr.png?t=${{Date.now()}}`;
}}

let _pairingPollTimer = null;
let _pairingCountdownTimer = null;
let _pairingExpiry = 0;

async function saveWaPhone() {{
  const input = document.getElementById('waPhone');
  if (!input) return;
  const val = input.value.replace(/\\D/g, '');
  if (!val || val.length < 7) return showToast('Enter a valid phone number (digits only)', true);
  await fetch('/api/save-wa', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key: 'WA_PHONE_NUMBER', value: val}})
  }});
  waConfig.phone_set = true;
  waConfig.phone_masked = val.slice(0,3) + '...' + val.slice(-2);
  input.classList.add('saved');
  showToast('✓ Phone number saved — fetching pairing code…');
  startPairingCodePoll();
}}

async function refreshPairingCode() {{
  // Restart the bridge to generate a fresh code, then poll
  await fetch('/api/restart-wa-bridge', {{ method: 'POST' }}).catch(() => {{}});
  showToast('↻ Requesting fresh code…');
  startPairingCodePoll();
}}

function startPairingCodePoll() {{
  const box = document.getElementById('wa-pairing-box');
  const codeEl = document.getElementById('wa-pairing-code');
  if (box) box.style.display = 'block';
  if (codeEl) codeEl.textContent = '…';
  clearInterval(_pairingPollTimer);
  clearInterval(_pairingCountdownTimer);
  _pairingPollTimer = setInterval(async () => {{
    try {{
      const r = await fetch('/api/wa-pairing-code').then(r => r.json());
      if (r.code) {{
        clearInterval(_pairingPollTimer);
        if (codeEl) codeEl.textContent = r.code;
        _pairingExpiry = Date.now() + 58000; // ~58s countdown
        clearInterval(_pairingCountdownTimer);
        _pairingCountdownTimer = setInterval(() => {{
          const secs = Math.max(0, Math.round((_pairingExpiry - Date.now()) / 1000));
          const cd = document.getElementById('wa-pairing-countdown');
          if (cd) cd.textContent = secs > 0 ? `Expires in ${{secs}}s` : 'Expired — click New Code';
          if (secs === 0) clearInterval(_pairingCountdownTimer);
        }}, 1000);
      }}
    }} catch(e) {{}}
  }}, 2000);
  // Stop polling after 30s if no code appears
  setTimeout(() => clearInterval(_pairingPollTimer), 30000);
}}

async function onTokenChange(el) {{
  const val = el.value.trim();
  if (val.length > 20) {{
    await saveKey('TELEGRAM_BOT_TOKEN', val);
    el.classList.add('saved');
    config.TELEGRAM_BOT_TOKEN = {{configured: true, masked: val.slice(0,4)+'...'+val.slice(-3)}};
    showToast('✓ Bot token saved');
  }}
}}

async function onUserIdChange(el) {{
  const val = el.value.trim();
  if (/^\\d{{5,}}$/.test(val)) {{
    await saveKey('ALLOWED_USER_ID', val);
    el.classList.add('saved');
    config.ALLOWED_USER_ID = {{configured: true, masked: val}};
    showToast('✓ User ID saved');
  }}
}}

function saveAndContinue() {{
  const token = document.getElementById('botToken')?.value.trim();
  const uid = document.getElementById('userId')?.value.trim();
  if (token && token.length > 20) saveKey('TELEGRAM_BOT_TOKEN', token);
  if (uid && /^\\d{{5,}}$/.test(uid)) saveKey('ALLOWED_USER_ID', uid);
  nextStep();
}}

// ---------------------------------------------------------------------------
// Step 2: AI Runner
// ---------------------------------------------------------------------------
function renderRunner() {{
  const current = config.CLI_RUNNER?.value || '';
  const detect = config._cli_detect || {{}};
  const freeCount = countConfigured();
  const freeSel = current === 'free';

  const provPips = PROVIDERS.map(p => {{
    const set = config[p.id]?.configured;
    return `<div class="provider-pip ${{set ? 'set' : ''}}">${{p.name}}</div>`;
  }}).join('');

  let html = `
  <div class="card">
    <div class="card-title">Choose Your AI</div>
    <div class="card-sub">
      Pick which AI powers your bot. <strong>Free Bot (FreeCode)</strong> rotates across 11 free providers automatically — perfect if you don't want to pay anything.
    </div>

    <div class="mt-4">
      <div class="free-hero ${{freeSel ? 'selected' : ''}}" onclick="selectRunner('free')">
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <div style="flex:1;min-width:200px">
            <div class="runner-name" style="font-size:18px">
              ⚡ Free Bot (FreeCode)
              <span style="font-size:11px;color:var(--green);background:var(--green-dim);padding:2px 8px;border-radius:4px;margin-left:8px;font-weight:700;letter-spacing:0.05em">RECOMMENDED</span>
            </div>
            <div class="runner-company" style="margin-top:4px">11 Providers · Rotates Automatically · $0/month · Powered by FreeCode &amp; OpenRouter</div>
            <div class="runner-desc" style="margin-top:6px">When one provider hits its limit, it instantly switches to the next. The more keys you add, the harder it is to ever hit a wall. Uses <strong>FreeCode</strong> for code tasks and routes through <strong>OpenRouter</strong> for model access.</div>
          </div>
          <div style="font-size:26px;font-weight:700;color:var(--green);font-family:monospace;white-space:nowrap;padding:8px 16px;background:var(--green-dim);border-radius:8px">${{freeCount}}/11</div>
        </div>
        <div class="runner-free-preview mt-2">${{provPips}}</div>
      </div>

      <div class="divider-label">Or choose a premium AI</div>

      <div class="runner-grid">`;

  for (const [key, info] of Object.entries(CLI_OPTIONS)) {{
    const installed = detect[key];
    const sel = current === key;
    html += `
      <div class="runner-card ${{sel ? 'selected' : ''}}" onclick="selectRunner('${{key}}')">
        <div class="runner-name">${{info.label}}</div>
        <div class="runner-company">${{info.company}}</div>
        <div class="runner-status ${{installed ? 'status-ok' : 'status-missing'}}">
          ${{installed ? '✓ Installed' : '⚠ Not installed — ' + info.install}}
        </div>
      </div>`;
  }}

  html += `
      </div>
    </div>

    <div class="btn-row">
      <button class="btn btn-primary" onclick="confirmRunner()">Continue →</button>
    </div>
  </div>`;

  return html;
}}

async function selectRunner(key) {{
  config.CLI_RUNNER = {{value: key, configured: true}};
  renderStep();
}}

async function confirmRunner() {{
  const runner = config.CLI_RUNNER?.value;
  if (!runner) return showToast('Pick an AI runner first', true);
  await saveKey('CLI_RUNNER', runner);
  showToast(`✓ Runner set to ${{runner}}`);
  if (runner === 'free') {{
    currentStep = 3;
    renderAll();
    window.scrollTo(0,0);
  }} else {{
    currentStep = 4;
    renderAll();
    window.scrollTo(0,0);
  }}
}}

// ---------------------------------------------------------------------------
// Step 3: Free Keys
// ---------------------------------------------------------------------------
function renderFreeKeys() {{
  const configured = countConfigured();
  const total = PROVIDERS.length;
  const pct = Math.round((configured / total) * 100);

  let html = `
  <div class="card" style="padding:20px 28px">
    <div class="card-title">Free API Keys</div>
    <div class="card-sub">
      Add as many providers as possible. Each one takes ~2 minutes and is completely free.
      <strong style="color:var(--green)">The more you add, the harder it is to ever hit a limit.</strong>
      You need at least one to get started.
    </div>
  </div>

  ${{configured === total ? `<div class="all-done-banner">🎉 All 11 providers configured — maximum coverage!</div>` : ''}}

  <div class="provider-progress">
    <div class="progress-label">Providers</div>
    <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:${{pct}}%"></div></div>
    <div class="progress-text">${{configured}}/${{total}}</div>
  </div>`;

  PROVIDERS.forEach((p, i) => {{
    const isConfigured = config[p.id]?.configured;
    const badgeClass = `badge-${{p.badge_color}}`;
    const statusIcon = isConfigured ? '✅' : '⬜';

    html += `
    <div class="provider-card ${{isConfigured ? 'configured' : ''}}" id="pcard-${{i}}">
      <div class="provider-header" onclick="toggleProvider(${{i}})">
        <div class="provider-check" id="pcheck-${{i}}">${{statusIcon}}</div>
        <div class="provider-name">${{p.name}}</div>
        <div class="provider-badge ${{badgeClass}}">${{p.badge}}</div>
        <div class="provider-chevron">▶</div>
      </div>
      <div class="provider-body">
        <div class="provider-meta">
          <div class="meta-item">
            <div class="meta-label">Free Tier</div>
            <div class="meta-value">${{p.free_tier}}</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Why Easy</div>
            <div class="meta-value">${{p.why}}</div>
          </div>
        </div>
        <ol class="steps-list">
          ${{p.steps.map((s, si) => `<li><div class="step-dot">${{si+1}}</div><div>${{s}}</div></li>`).join('')}}
        </ol>
        ${{p.is_cli ? renderQwenInput(i, isConfigured) : renderApiKeyInput(i, p, isConfigured)}}
      </div>
    </div>`;
  }});

  html += `
  <div class="btn-row" style="margin-top:24px">
    <button class="btn btn-primary" onclick="nextStep()">Continue to Summary →</button>
    <button class="btn btn-ghost" onclick="nextStep()">Skip remaining</button>
  </div>`;

  return html;
}}

function renderApiKeyInput(i, p, isConfigured) {{
  const masked = config[p.id]?.masked || '';
  return `
  <div class="key-input-row">
    <div class="input-wrapper" style="flex:1">
      <input class="field-input key-input ${{isConfigured ? 'saved' : ''}}" id="key-${{i}}"
        type="password" placeholder="Paste your ${{p.name}} API key here..."
        value="${{isConfigured ? masked : ''}}"
        onfocus="if(this.value===this.getAttribute('data-masked'))this.value=''"
        data-masked="${{masked}}"
        onpaste="setTimeout(()=>autoSaveKey(this, ${{i}}, '${{p.id}}'), 100)"
      />
      <button class="toggle-eye" onclick="toggleEye('key-${{i}}', this)" title="Show/hide">👁</button>
    </div>
    <button class="btn btn-primary btn-sm" onclick="saveProviderKey(${{i}}, '${{p.id}}')">Save</button>
    ${{isConfigured ? '' : `<button class="btn btn-ghost btn-sm" onclick="skipProvider(${{i}})">Skip</button>`}}
  </div>
  ${{isConfigured ? `<div class="saved-badge mt-2">✓ Configured: ${{masked}} <button class="clear-link" onclick="clearKey('${{p.id}}', ${{i}})">× Clear</button></div>` : ''}}`;
}}

function renderQwenInput(i, isConfigured) {{
  if (isConfigured) {{
    return `<div class="saved-badge mt-2">✓ Qwen CLI detected <button class="clear-link" onclick="clearKey('QWEN_CLI', ${{i}})">× Clear</button></div>`;
  }}
  return `
  <div class="cli-note">
    No API key needed — just install the CLI and log in once.<br><br>
    <strong>Terminal commands:</strong><br>
    <code>npm install -g @qwen-code/qwen-code</code><br>
    <code>qwen</code> — opens browser to log in at qwen.ai
  </div>
  <div class="btn-row">
    <button class="btn btn-secondary btn-sm" onclick="checkQwen(${{i}})">Check Installation</button>
    <button class="btn btn-ghost btn-sm" onclick="skipProvider(${{i}})">Skip</button>
  </div>`;
}}

function toggleProvider(i) {{
  const card = document.getElementById(`pcard-${{i}}`);
  card.classList.toggle('open');
}}

async function saveProviderKey(i, envKey) {{
  const input = document.getElementById(`key-${{i}}`);
  const val = input?.value.trim();
  if (!val || val.length < 10) return showToast('Key looks too short', true);
  input.classList.add('loading');
  await saveKey(envKey, val);
  input.classList.remove('loading');
  input.classList.add('saved');
  config[envKey] = {{configured: true, masked: val.slice(0,4)+'...'+val.slice(-3)}};
  const checkEl = document.getElementById(`pcheck-${{i}}`);
  if (checkEl) checkEl.classList.add('animate');
  showToast(`✓ ${{PROVIDERS[i].name}} saved!`);
  setTimeout(() => {{ renderStep(); }}, 400);
}}

async function autoSaveKey(el, i, envKey) {{
  const val = el.value.trim();
  if (val.length >= 20) {{
    el.classList.add('loading');
    await saveKey(envKey, val);
    el.classList.remove('loading');
    config[envKey] = {{configured: true, masked: val.slice(0,4)+'...'+val.slice(-3)}};
    showToast(`✓ ${{PROVIDERS[i].name}} saved!`);
    setTimeout(() => renderStep(), 300);
  }}
}}

function toggleEye(inputId, btn) {{
  const inp = document.getElementById(inputId);
  if (!inp) return;
  inp.type = inp.type === 'password' ? 'text' : 'password';
}}

async function clearKey(envKey, i) {{
  await fetch('/api/clear', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key: envKey, value: ''}})
  }});
  config[envKey] = {{configured: false, masked: ''}};
  showToast(`Cleared ${{PROVIDERS[i]?.name || envKey}}`);
  setTimeout(() => renderStep(), 200);
}}

async function clearCoreKey(envKey) {{
  await fetch('/api/clear', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key: envKey, value: ''}})
  }});
  config[envKey] = {{configured: false, masked: '', value: ''}};
  showToast(`Cleared`);
  setTimeout(() => renderStep(), 200);
}}

async function checkQwen(i) {{
  const res = await fetch('/api/detect-qwen').then(r => r.json());
  if (res.installed) {{
    config['QWEN_CLI'] = {{configured: true}};
    showToast('✓ Qwen CLI found!');
    setTimeout(() => {{ renderStep(); }}, 400);
  }} else {{
    showToast('Qwen CLI not found — install it first', true);
  }}
}}

function skipProvider(i) {{
  const card = document.getElementById(`pcard-${{i}}`);
  card.classList.add('skipped');
  card.classList.remove('open');
}}

// ---------------------------------------------------------------------------
// Step 4: Bridge Cloud
// ---------------------------------------------------------------------------
let bcInfo = null;

async function loadBcInfo() {{
  bcInfo = await fetch('/api/bc-info').then(r => r.json()).catch(() => ({{}}));
  config = await fetch('/api/config').then(r => r.json());
  document.getElementById('mainContent').innerHTML = renderBridgeCloud();
}}

async function generateBcKey() {{
  const btn = document.getElementById('bcGenBtn');
  btn.disabled = true; btn.textContent = 'Generating…';
  const data = await fetch('/api/generate-bc-key').then(r => r.json());
  config.BRIDGE_CLOUD_API_KEY = {{ configured: true, masked: data.masked }};
  document.getElementById('bcKeyDisplay').textContent = data.key;
  document.getElementById('bcKeyRow').style.display = 'flex';
  document.getElementById('bcKeyStatus').innerHTML = '<span style="color:var(--green)">✓ Key generated and saved</span>';
  btn.textContent = 'Regenerate'; btn.disabled = false;
}}

async function saveBcOrKey() {{
  const val = document.getElementById('bcOrInput').value.trim();
  if (!val) return;
  await fetch('/api/save', {{ method: 'POST', headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{key: 'OPENROUTER_MASTER_KEY', value: val}}) }});
  config.OPENROUTER_MASTER_KEY = {{ configured: true }};
  document.getElementById('bcOrStatus').innerHTML = '<span style="color:var(--green)">✓ Saved</span>';
  document.getElementById('bcOrInput').value = '';
}}

function copyBcKey() {{
  const key = document.getElementById('bcKeyDisplay').textContent;
  if (key && key !== '—') navigator.clipboard.writeText(key).then(() => showToast('✓ API key copied!'));
}}

function renderBridgeCloud() {{
  const bcKeyOk = config.BRIDGE_CLOUD_API_KEY?.configured;
  const orKeyOk = config.OPENROUTER_MASTER_KEY?.configured;
  const serverUrl = bcInfo?.server_url || 'https://your-server.tailXXXX.ts.net';
  const bcKeyMasked = config.BRIDGE_CLOUD_API_KEY?.masked || '';

  return `
  <div class="card">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <span style="font-size:22px">☁️</span>
      <h2 style="font-size:20px;font-weight:700">Bridge Cloud</h2>
    </div>
    <p style="color:var(--muted);font-size:14px;margin-bottom:24px">
      Bridge Cloud is the web UI that lets users chat with your bots from any browser.
      Set up your server credentials so Bridge Cloud can connect.
    </p>

    <!-- Section 1: BC API Key -->
    <div style="border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <h3 style="font-size:15px;font-weight:600">Server API Key</h3>
        <span style="font-size:12px;padding:2px 8px;background:var(--blue-dim);color:var(--blue);border-radius:20px">REQUIRED</span>
      </div>
      <p style="font-size:13px;color:var(--muted);margin-bottom:14px">
        A secret key that secures your <code>/v1/</code> endpoints.
        Bridge Cloud sends this with every request — users can't connect without it.
      </p>
      <div id="bcKeyStatus" style="margin-bottom:10px;font-size:13px">
        ${{bcKeyOk
          ? `<span style="color:var(--green)">✓ Key is set (${{bcKeyMasked}})</span>`
          : `<span style="color:var(--muted)">No key set yet — generate one below.</span>`
        }}
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button id="bcGenBtn" class="btn btn-primary btn-sm" onclick="generateBcKey()">
          ${{bcKeyOk ? 'Regenerate Key' : '⚡ Generate Key'}}
        </button>
        <div id="bcKeyRow" style="display:${{bcKeyOk ? 'none' : 'none'}};align-items:center;gap:8px;flex:1;min-width:200px">
          <code id="bcKeyDisplay" style="font-size:12px;color:var(--green);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">—</code>
          <button class="btn btn-ghost btn-sm" onclick="copyBcKey()">Copy</button>
        </div>
      </div>
    </div>

    <!-- Section 2: OpenRouter Master Key -->
    <div style="border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <h3 style="font-size:15px;font-weight:600">OpenRouter Key</h3>
        <span style="font-size:12px;padding:2px 8px;background:${{orKeyOk ? 'var(--green-dim)' : 'rgba(240,136,62,0.15)'}};color:${{orKeyOk ? 'var(--green)' : 'var(--orange)'}};border-radius:20px">
          ${{orKeyOk ? '✓ SET' : 'RECOMMENDED'}}
        </span>
      </div>
      <p style="font-size:13px;color:var(--muted);margin-bottom:14px">
        Your master OpenRouter key. When users sign up on Bridge Cloud, the server
        automatically provisions them a scoped API key using this.
        <strong>One key to rule all your users — they never need to create their own.</strong>
      </p>
      <ol style="font-size:13px;color:var(--muted);padding-left:18px;margin-bottom:14px;line-height:1.8">
        <li>Go to <a href="https://openrouter.ai/keys" target="_blank" style="color:var(--blue)">openrouter.ai/keys</a> — sign in with Google or GitHub</li>
        <li>Click <strong style="color:var(--text)">Create Key</strong>, name it <code>bridge-cloud-master</code></li>
        <li>Copy the key (starts with <code>sk-or-v1-</code>) and paste below</li>
      </ol>
      <div id="bcOrStatus" style="margin-bottom:10px;font-size:13px">
        ${{orKeyOk ? '<span style="color:var(--green)">✓ OpenRouter key is configured</span>' : ''}}
      </div>
      <div style="display:flex;gap:8px">
        <input id="bcOrInput" type="password" placeholder="sk-or-v1-..." style="flex:1;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px" />
        <button class="btn btn-primary btn-sm" onclick="saveBcOrKey()">Save</button>
      </div>
    </div>

    <!-- Section 3: Your Server URL -->
    <div style="border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:24px">
      <h3 style="font-size:15px;font-weight:600;margin-bottom:8px">Your Server URL</h3>
      <p style="font-size:13px;color:var(--muted);margin-bottom:12px">
        This is what you (or your users) paste into Bridge Cloud's <strong>Server URL</strong> field.
        Set <code>TAILSCALE_URL</code> or <code>EXTERNAL_URL</code> in your .env to show the correct URL here.
      </p>
      <div style="display:flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 16px">
        <code style="flex:1;font-size:13px;color:var(--green);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="bcServerUrl">${{serverUrl}}</code>
        <button class="btn btn-ghost btn-sm" onclick="navigator.clipboard.writeText(document.getElementById('bcServerUrl').textContent).then(()=>showToast('✓ URL copied!'))">Copy</button>
      </div>
      <p style="font-size:12px;color:var(--muted);margin-top:8px">
        In Bridge Cloud → click <strong>Connect Server</strong> → select <strong>Local</strong> → paste this URL + your API key above.
      </p>
    </div>

    <div class="btn-row">
      <button class="btn btn-secondary" onclick="goStep(3)">← Back</button>
      <button class="btn btn-primary" onclick="nextStep()">Continue →</button>
    </div>
  </div>`;
}}


// ---------------------------------------------------------------------------
// Step 5: Done
// ---------------------------------------------------------------------------
function renderDone() {{
  const tokenOk = config.TELEGRAM_BOT_TOKEN?.configured;
  const uidOk = config.ALLOWED_USER_ID?.configured;
  const runnerOk = config.CLI_RUNNER?.configured;
  const runner = config.CLI_RUNNER?.value || '';
  const freeCount = countConfigured();
  const waConnected = waConfig.connected;
  const allGood = tokenOk && uidOk && runnerOk;

  return `
  <div class="card">
    <div class="card-title">${{allGood ? '🎉 Setup Complete!' : '⚠ Almost There'}}</div>
    <div class="card-sub">
      Here's your current configuration. You can go back to any step to change things.
    </div>

    <div class="summary-grid mt-4">
      <div class="summary-row ${{tokenOk ? 'ok' : 'warn'}}">
        <div class="summary-icon">${{tokenOk ? '✅' : '⚠️'}}</div>
        <div class="summary-row-text">
          <div class="summary-row-label">Bot Token</div>
          <div class="summary-row-val">${{tokenOk ? config.TELEGRAM_BOT_TOKEN?.masked : 'Not set'}}</div>
        </div>
      </div>
      <div class="summary-row ${{uidOk ? 'ok' : 'warn'}}">
        <div class="summary-icon">${{uidOk ? '✅' : '⚠️'}}</div>
        <div class="summary-row-text">
          <div class="summary-row-label">Your User ID</div>
          <div class="summary-row-val">${{uidOk ? config.ALLOWED_USER_ID?.masked : 'Not set'}}</div>
        </div>
      </div>
      <div class="summary-row ${{runnerOk ? 'ok' : 'warn'}}">
        <div class="summary-icon">${{runnerOk ? '✅' : '⚠️'}}</div>
        <div class="summary-row-text">
          <div class="summary-row-label">AI Runner</div>
          <div class="summary-row-val">${{runner || 'Not set'}}</div>
        </div>
      </div>
      <div class="summary-row ${{waConnected ? 'ok' : ''}}">
        <div class="summary-icon">${{waConnected ? '✅' : '➖'}}</div>
        <div class="summary-row-text">
          <div class="summary-row-label">WhatsApp</div>
          <div class="summary-row-val">${{waConnected ? 'Connected' : 'Not linked (optional)'}}</div>
        </div>
      </div>
      ${{runner === 'free' ? `
      <div class="summary-row ${{freeCount > 0 ? 'ok' : 'warn'}}">
        <div class="summary-icon">${{freeCount >= 4 ? '✅' : freeCount > 0 ? '⚠️' : '❌'}}</div>
        <div class="summary-row-text">
          <div class="summary-row-label">Free Providers</div>
          <div class="summary-row-val">${{freeCount}}/11 configured ${{freeCount < 4 ? '— add more for reliability' : freeCount < 11 ? '— more = better' : '— maximum coverage!'}}</div>
        </div>
      </div>` : ''}}
    </div>

    ${{allGood ? `
    <div class="launch-box">
      <div class="launch-title">Your bot is ready to launch</div>
      <div class="launch-sub">Run this command in your terminal from the bridgebot folder:</div>
      <div class="launch-cmd" onclick="copyCmd(this)">python bridge.py</div>
      <div style="font-size:12px;color:var(--muted);margin-top:10px">Click to copy</div>
      ${{runner === 'free' && freeCount < 4 ? `
      <div style="margin-top:16px;padding:12px;background:var(--bg);border:1px solid var(--yellow);border-radius:8px;font-size:13px;color:var(--yellow)">
        ⚠ You only have ${{freeCount}} provider${{freeCount !== 1 ? 's' : ''}} — add more free keys to avoid hitting limits.
        <button class="btn btn-ghost btn-sm" onclick="goStep(3)" style="margin-left:8px">Add more →</button>
      </div>` : ''}}
    </div>` : `
    <div style="margin-top:20px;padding:16px;background:var(--bg);border:1px solid var(--yellow);border-radius:8px;font-size:14px;color:var(--yellow)">
      ⚠ Go back and complete the missing steps before launching.
    </div>`}}

    <div class="btn-row mt-4">
      <button class="btn btn-secondary" onclick="goStep(1)">← Edit Platform</button>
      <button class="btn btn-secondary" onclick="goStep(2)">← Edit Runner</button>
      ${{runner === 'free' ? `<button class="btn btn-secondary" onclick="goStep(3)">← Edit Free Keys</button>` : ''}}
      <button class="btn btn-secondary" onclick="goStep(4)">← Edit Bridge Cloud</button>
    </div>
  </div>`;
}}

function copyCmd(el) {{
  navigator.clipboard.writeText(el.textContent).then(() => showToast('✓ Command copied!'));
}}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function countConfigured() {{
  return PROVIDERS.filter(p => config[p.id]?.configured).length;
}}

async function saveKey(key, value) {{
  await fetch('/api/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{key, value}})
  }});
}}

function showToast(msg, isError=false) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast' + (isError ? ' error' : '');
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2800);
}}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
boot();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Server start
# ---------------------------------------------------------------------------

def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print("\n  ⚡ Bridgebot Setup Wizard")
    print(f"  Opening http://localhost:{PORT} ...")
    print("  Press Ctrl+C to stop\n")
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
