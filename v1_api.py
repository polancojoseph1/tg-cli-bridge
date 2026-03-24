"""
Bridge Cloud v1 API
Adds /v1/chat (NDJSON streaming) and /v1/health endpoints.
These are separate from the Telegram webhook chain.
"""
import asyncio
import json
import os
import random
import re
import secrets as _secrets
import tempfile
import uuid
from typing import Optional, AsyncGenerator

import httpx

# ── Model routing pools ───────────────────────────────────────────────────────
# Benchmarks sourced from model-arena (SWE-bench + Chatbot Arena, March 2026)
#
# Value Index (SWE-bench score ÷ blended cost/1M tokens):
#   Qwen3.5 Plus    newer gen flagship @ ~$0.50/1M   ← confirmed valid 2026-03-23
#   DeepSeek V3.2   74.4% @ $0.35/1M  → 212x        ← best all-rounder
#   Kimi K2.5       76.8% @ $1.35/1M  →  56x        ← thinking model, needs max_tokens>200
#   Claude Sonnet   72.7% @ $9.00/1M  →   8x
#   Claude Opus     80.9% @ $15.0/1M  →   5x

# ── FREE tier: zero-credit :free models on OpenRouter ───────────────────────
# Rate limit: 20 req/min | 1,000 req/day (with $10+ credits loaded)
# Verified 2026-03-18: returns HTTP 200 with tool-use + in FreeCode registry
# Qwen3 models (coder/80b) confirmed alive but rate-limited at Venice (429 upstream).
# NVIDIA Nemotron + Stepfun + GLM models confirmed HTTP 200 with tool-use.
_FREE_MODELS_GENERAL = [
    "stepfun/step-3.5-flash:free",                 # Flash model — fast, 200 OK with tools
    "z-ai/glm-4.5-air:free",                       # GLM-4.5 Air — strong Chinese model, 200 OK
    "nvidia/nemotron-3-nano-30b-a3b:free",         # 30B MoE — 200 OK with tools
    "arcee-ai/trinity-large-preview:free",         # Trinity Large — 200 OK with tools
    "qwen/qwen3-next-80b-a3b-instruct:free",       # 80B fallback (rate-limited but valid)
    "qwen/qwen3-coder:free",                       # Coder fallback (rate-limited but valid)
]

_FREE_MODELS_CODING = [
    "nvidia/nemotron-3-nano-30b-a3b:free",         # NVIDIA 30B — fast, 200 OK with tools
    "stepfun/step-3.5-flash:free",                 # Flash — fast, 200 OK with tools
    "nvidia/nemotron-nano-9b-v2:free",             # 9B — smallest, fastest, 200 OK with tools
    "qwen/qwen3-coder:free",                       # Qwen3 Coder — coding specialist (rate-limited fallback)
    "qwen/qwen3-next-80b-a3b-instruct:free",       # 80B fallback (rate-limited but valid)
]

# ── PRO tier Chinese models (90% of pro requests) ───────────────────────────
# Ordered by value index; routing picks based on task type
_PRO_CHINESE_GENERAL = [
    "deepseek/deepseek-v3.2",        # $0.26/$0.40 | 74.4% SWE | BEST default (confirmed valid)
    "qwen/qwen3.5-plus-02-15",     # newer gen flagship | confirmed valid 2026-03-23
    "moonshotai/kimi-k2.5",          # $0.45/$2.20 | 99% HumanEval | strong all-rounder (confirmed valid)
]

_PRO_CHINESE_CODING = [
    "moonshotai/kimi-k2.5",          # $0.45/$2.20 | 99% HumanEval | coding king (confirmed valid)
    "deepseek/deepseek-v3.2",        # $0.26/$0.40 | strong coder, best value (confirmed valid)
    "qwen/qwen3.5-plus-02-15",    # newer gen flagship | confirmed valid 2026-03-23
]

_PRO_CHINESE_COMPLEX = [
    "moonshotai/kimi-k2.5",          # 99% HumanEval — best Chinese model on hard tasks (confirmed valid)
    "qwen/qwen3.5-plus-02-15",    # newer gen flagship | confirmed valid 2026-03-23
    "deepseek/deepseek-v3.2",        # reliable fallback (confirmed valid)
]

# ── Power tier flagship (10% — Sonnet only) ──────────────────────────────────
_POWER_FLAGSHIP_MODELS = [
    "anthropic/claude-sonnet-4-6",   # Best quality-to-cost flagship
]

# ── Obsessed tier flagship (15% — Sonnet + GPT-5.2) ─────────────────────────
_OBSESSED_FLAGSHIP_MODELS = [
    "anthropic/claude-sonnet-4-6",   # 8% weight
    "openai/gpt-5.2",                # 7% weight
]

# Obsessed Chinese mix: 40% Qwen · 20% DeepSeek · 25% Kimi (sums to 85%)
_OBSESSED_CHINESE_MODELS  = ["qwen/qwen3.5-plus-02-15", "deepseek/deepseek-v3.2", "moonshotai/kimi-k2.5"]
_OBSESSED_CHINESE_WEIGHTS = [0.47, 0.24, 0.29]  # normalised to 85% Chinese slice

# ── Task classification patterns ─────────────────────────────────────────────
_CODING_PATTERN = re.compile(
    r"\b(code|function|debug|bug|implement|class|method|algorithm|script|"
    r"programming|python|javascript|typescript|react|api|sql|database|"
    r"refactor|test|unit.test|error|exception|compile|deploy|docker|"
    r"git|regex|parse|json|xml|html|css|async|await|loop|array|object)\b",
    re.IGNORECASE,
)
_CREATIVE_PATTERN = re.compile(
    r"\b(write|draft|compose|email|essay|story|letter|poem|speech|script|"
    r"blog|article|caption|copy|marketing|ad|slogan|headline|narrative|"
    r"professional|tone|voice|persuade|persuasive|creative|brand|pitch)\b",
    re.IGNORECASE,
)
_COMPLEX_PATTERN = re.compile(
    r"\b(analyze|comprehensive|detailed|step.by.step|compare|contrast|"
    r"strategy|roadmap|plan|architecture|design|evaluate|critique|research|"
    r"explain.why|nuanced|multiple|constraints|requirements|tradeoffs)\b",
    re.IGNORECASE,
)


def _classify_task(message: str) -> str:
    """Classify message as 'coding', 'creative', 'complex', or 'general'."""
    coding_hits = len(_CODING_PATTERN.findall(message))
    creative_hits = len(_CREATIVE_PATTERN.findall(message))
    complex_hits = len(_COMPLEX_PATTERN.findall(message))

    # Weighted: multiple coding signals = definitely coding
    if coding_hits >= 2 or (coding_hits >= 1 and len(message) > 200):
        return "coding"
    if creative_hits >= 2:
        return "creative"
    if complex_hits >= 2 or len(message) > 1500:
        return "complex"
    if coding_hits == 1:
        return "coding"
    if creative_hits == 1:
        return "creative"
    return "general"


def _pick_model(tier: str, message: str) -> str:
    """
    Select the optimal model based on tier.

    FREE:     :free pool only. Zero API cost.
    CASUAL:   Chinese paid models only. 0% flagship. 96% margin.
    REGULAR:  Chinese paid models only. 0% flagship. 90% margin.
    POWER:    Chinese 90% + 10% Sonnet. No Opus, no GPT.
    OBSESSED: 40% Qwen · 20% DeepSeek · 25% Kimi · 8% Sonnet · 7% GPT-5.2.
              750/day cap — more than 2× Claude Max 20x at 40% less cost.
    """
    task = _classify_task(message)

    # ── Free tier ─────────────────────────────────────────────────────────────
    if tier == "free":
        if task == "coding":
            return random.choice(_FREE_MODELS_CODING)
        return random.choice(_FREE_MODELS_GENERAL)

    # ── Casual / Regular — 0% flagship, pure Chinese ─────────────────────────
    if tier in ("casual", "regular"):
        if task == "coding":
            return random.choice(_PRO_CHINESE_CODING)
        elif task == "complex":
            return random.choice(_PRO_CHINESE_COMPLEX)
        else:
            weights = [0.50, 0.30, 0.20]  # DS V3.2, Qwen3.5 397B, GLM-5
            return random.choices(_PRO_CHINESE_GENERAL, weights=weights, k=1)[0]

    # ── Power — 10% Sonnet, 90% Chinese ──────────────────────────────────────
    if tier == "power":
        if random.random() < 0.10:
            return random.choice(_POWER_FLAGSHIP_MODELS)  # Sonnet only
        if task == "coding":
            return random.choice(_PRO_CHINESE_CODING)
        elif task == "complex":
            return random.choice(_PRO_CHINESE_COMPLEX)
        else:
            weights = [0.50, 0.30, 0.20]
            return random.choices(_PRO_CHINESE_GENERAL, weights=weights, k=1)[0]

    # ── Obsessed — 40% Qwen · 20% DeepSeek · 25% Kimi · 8% Sonnet · 7% GPT ─
    # tier == "obsessed"
    roll = random.random()
    if roll < 0.08:
        return "anthropic/claude-sonnet-4-6"
    elif roll < 0.15:
        return "openai/gpt-5.2"
    # Remaining 85% → Chinese mix (40/20/25 normalised)
    return random.choices(_OBSESSED_CHINESE_MODELS, weights=_OBSESSED_CHINESE_WEIGHTS, k=1)[0]

from typing import Literal  # noqa: E402
from fastapi import APIRouter, Header, HTTPException, Request, UploadFile, File  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

router = APIRouter(prefix="/v1", tags=["bridge-cloud"])

# ── Tier definitions ──────────────────────────────────────────────────────────
# Daily caps (None = unlimited). Monthly charges are handled by billing layer.
DAILY_CAPS: dict[str, int] = {
    "free":     20,    # More than most competitors give paid users
    "casual":   50,    # More than Claude Pro ($20) gives daily
    "regular":  150,   # Heavy daily driver territory
    "power":    500,   # More than Claude Max 20x ($200) gives daily
    "obsessed": 750,   # 2× Claude Max 20x, $80 cheaper — the headline feature
}

# Credit limits for per-user OpenRouter key provisioning.
# Sized to cover worst-case monthly usage (daily cap × 31 days) + 20% buffer.
# Free/Casual/Regular: 0% flagship → pure Chinese cost, margins are fat.
# Power: 10% Sonnet. Obsessed: 15% Sonnet + GPT-5.2.
PLAN_CREDIT_LIMITS: dict[str, float] = {
    "free":     0.0,   # No key — hits :free pool only
    "casual":   1.0,   # Worst case $0.43 + buffer
    "regular":  4.0,   # Worst case $2.52 + buffer
    "power":    30.0,  # Worst case $23.59 + buffer
    "obsessed": 56.0,  # Worst case $45.76 + buffer
}

# Maps Bridge Cloud conversation_id → dedicated instance_id
# Each BC conversation gets its own isolated CLI session, never shared with Telegram.
_bc_conv_instances: dict[str, int] = {}

# Singleton FreeCode runner for Bridge Cloud (lazy-initialized)
_bc_freecode_runner = None

def _set_bc_freecode_runner(runner):
    global _bc_freecode_runner
    _bc_freecode_runner = runner


def _get_or_create_bc_instance(conversation_id: str) -> object:
    """Return (or create) a dedicated instance for this Bridge Cloud conversation."""
    import server as _server

    inst_id = _bc_conv_instances.get(conversation_id)
    if inst_id:
        inst = _server.instances.get(inst_id)
        if inst:
            return inst
        # Instance was deleted — create a fresh one below
        del _bc_conv_instances[conversation_id]

    short = conversation_id[:8]
    inst = _server.instances.create(f"BC:{short}", owner_id=0, switch_active=False)
    _bc_conv_instances[conversation_id] = inst.id
    _server._ensure_worker(inst)
    return inst


# ── Auth helper ──────────────────────────────────────────────────────────────

def _require_auth(x_api_key: str = "") -> None:
    """Validate against BRIDGE_CLOUD_API_KEY or OPENROUTER_API_KEY (unified key).
    Raises 401 if invalid."""
    from config import BRIDGE_CLOUD_API_KEY
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if BRIDGE_CLOUD_API_KEY and _secrets.compare_digest(x_api_key, BRIDGE_CLOUD_API_KEY):
        return
    if openrouter_key and _secrets.compare_digest(x_api_key, openrouter_key):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


# ── Pydantic models ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=32_000)
    conversation_id: str = Field(..., min_length=1, max_length=128)
    stream: bool = True
    instance_id: int = 0
    system_prompt: Optional[str] = ""
    openrouter_key: Optional[str] = None  # Per-user OpenRouter key (Bridge Cloud)
    model: Optional[str] = None           # Explicit model override — skips auto-routing
    tier: Literal["free", "casual", "regular", "power", "obsessed", "pro"] = "free"


# ── /v1/health ───────────────────────────────────────────────────────────────

@router.get("/health")
async def v1_health():
    """Public health check — no auth required so users can test connection."""
    import health as _health
    from config import CLI_RUNNER, BOT_NAME, is_cli_available

    try:
        h = _health.get_health()
    except Exception:
        h = {}

    return {
        "status": "ok",
        "agent_id": CLI_RUNNER,
        "runner": CLI_RUNNER,
        "bot_name": BOT_NAME,
        "version": "1.0.0",
        "uptime_seconds": h.get("uptime_seconds", 0),
        "cli_available": is_cli_available(),
        "message_count": h.get("message_count", 0),
        "last_message_time": h.get("last_message_time"),
        "is_processing": h.get("is_processing", False),
        "queue_depth": h.get("queue_depth", 0),
    }


# ── /v1/chat ─────────────────────────────────────────────────────────────────

@router.post("/chat")
async def v1_chat(
    body: ChatRequest,
    x_api_key: str = Header(default=""),
):
    """Authenticated streaming chat endpoint for Bridge Cloud."""
    _require_auth(x_api_key)

    async def generate() -> AsyncGenerator[str, None]:
        try:
            # Import the global runner and instance manager from server module
            import server as _server

            # Resolve instance: explicit instance_id wins; otherwise use a
            # dedicated per-conversation instance so Bridge Cloud is fully
            # isolated from the Telegram session.
            instance: object | None = None
            if body.instance_id:
                instance = _server.instances.get(body.instance_id)
            if instance is None:
                instance = _get_or_create_bc_instance(body.conversation_id)

            if instance is None:
                err = {
                    "type": "error",
                    "conversation_id": body.conversation_id,
                    "code": "runner_not_available",
                    "message": "No active runner instance",
                }
                yield json.dumps(err) + "\n"
                return

            # Model routing: explicit override wins; otherwise auto-route by tier
            # Map "pro" → "regular" (Bridge Cloud frontend uses "pro" for paid tier)
            effective_tier = body.tier or "free"
            if effective_tier == "pro":
                effective_tier = "regular"
            # Auto-upgrade from free to casual when a paid OpenRouter key is present
            # (not the server-wide OPENROUTER_API_KEY, but a per-user provisioned key)
            if effective_tier == "free" and body.openrouter_key:
                effective_tier = "casual"

            # Inject per-user credentials onto the instance for this request.
            # Priority: explicit body key > x-api-key header (only if it's an OR key, not the BC key) > server env key
            # FREE TIER EXCEPTION: don't inject the server-wide OR key for free tier.
            # Free tier uses the local free proxy (OPENAI_BASE_URL=127.0.0.1:8592) which
            # rotates through Groq/Cerebras/Gemini/etc. — far more reliable than :free OR models.
            from config import BRIDGE_CLOUD_API_KEY as _bc_key
            _x_as_or = x_api_key if (x_api_key and x_api_key != _bc_key) else ""
            if effective_tier == "free" and not body.openrouter_key:
                # Free tier: clear any stale OR key; FreeCode will use env proxy
                instance.bc_openrouter_key = None
                or_key = ""
            else:
                or_key = body.openrouter_key or _x_as_or or os.environ.get("OPENROUTER_API_KEY", "")
                if or_key:
                    instance.bc_openrouter_key = or_key

            if body.model:
                instance.model = body.model
            elif or_key:
                # Only apply tier routing when using OpenRouter
                instance.model = _pick_model(effective_tier, body.message)
            else:
                # Free tier: clear model so FreeCode falls back to FREECODE_MODEL env var
                instance.model = ""

            # Clean message
            message = body.message.replace("\x00", "").strip()

            result_text = ""
            error_msg = None

            # Route: FreeCode CLI or fallback to server runner
            or_key = getattr(instance, "bc_openrouter_key", None)
            chosen_model = getattr(instance, "model", None)

            # Use FreeCode streaming path (with progress/steps) when:
            # 1. Paid tier: OR key + model set (uses OpenRouter backend)
            # 2. Free tier: no OR key, FreeCode uses env proxy (Groq/Cerebras/etc.)
            # Fall back to raw CLI runner only for other bot types (Claude/Gemini/etc.)
            use_freecode_path = bool(or_key and chosen_model) or (effective_tier == "free")

            if use_freecode_path:
                # ── FreeCode CLI (OpenRouter for paid tiers, env proxy for free) ──
                # FreeCode has full tool access (files, shell, search, etc.)
                from runners.freecode import FreeCodeBaseRunner

                _fc = _bc_freecode_runner
                if _fc is None:
                    _fc = FreeCodeBaseRunner()
                    _set_bc_freecode_runner(_fc)

                # Set system prompt: explicit body value wins, otherwise inject
                # a Bridge Cloud-appropriate default that does NOT demand tool use
                # on every response (the Telegram bot's CLI_SYSTEM_PROMPT does, which
                # causes free-proxy models to loop indefinitely on simple questions).
                _BC_SYSTEM_PROMPT = (
                    "You are Bridge, an intelligent AI assistant. "
                    "Answer questions directly and helpfully. "
                    "Use tools (shell, file read/write, web search) when the task genuinely requires them — "
                    "for example, reading a file, running code, or looking something up. "
                    "For conversational questions or general knowledge, respond directly without using tools. "
                    "Be concise and clear."
                )
                if body.system_prompt:
                    instance.agent_system_prompt = body.system_prompt
                elif not instance.agent_system_prompt:
                    # First message on this instance and no explicit prompt — use BC default
                    instance.agent_system_prompt = _BC_SYSTEM_PROMPT

                # Use a queue to bridge FreeCode's async progress callbacks into
                # this generator — each queue.get() yields control to the event loop
                # which flushes buffered HTTP chunks before waiting for the next event.
                progress_q: asyncio.Queue = asyncio.Queue()

                async def _on_fc_progress(text: str) -> None:
                    await progress_q.put({"type": "progress", "text": text})

                async def _run_fc() -> None:
                    try:
                        text = await _fc.run(
                            message=message,
                            instance=instance,
                            on_progress=_on_fc_progress,
                        )
                        await progress_q.put({"type": "result", "text": text})
                    except Exception as exc:
                        await progress_q.put({"type": "fc_error", "text": str(exc)})

                asyncio.create_task(_run_fc())

                # Initial heartbeat (immediately delivered since queue.get() follows)
                yield json.dumps({"type": "progress", "conversation_id": body.conversation_id, "text": "⏳ Thinking..."}) + "\n"

                # Drain queue: relay progress events and wait for final result
                while True:
                    try:
                        item = await asyncio.wait_for(progress_q.get(), timeout=25.0)
                    except asyncio.TimeoutError:
                        # Keepalive: prevents iOS from killing a stalled connection
                        yield json.dumps({"type": "progress", "conversation_id": body.conversation_id, "text": "⏳ Still working..."}) + "\n"
                        continue

                    if item["type"] == "progress":
                        yield json.dumps({"type": "progress", "conversation_id": body.conversation_id, "text": item["text"]}) + "\n"
                    elif item["type"] == "result":
                        result_text = item["text"]
                        break
                    elif item["type"] == "fc_error":
                        error_msg = item["text"]
                        break
            else:
                # ── Fallback to CLI runner (Claude/Gemini/Qwen — non-freecode bots) ──
                try:
                    result_text = await _server.runner.run(message, instance)
                except Exception as exc:
                    error_msg = str(exc)

            if error_msg:
                err = {
                    "type": "error",
                    "conversation_id": body.conversation_id,
                    "code": "runner_error",
                    "message": error_msg,
                }
                yield json.dumps(err) + "\n"
                return

            # Stream result in chunks (~4 chars each for natural feel)
            chunk_size = 4
            for i in range(0, len(result_text), chunk_size):
                chunk = result_text[i : i + chunk_size]
                event = {
                    "type": "delta",
                    "conversation_id": body.conversation_id,
                    "text": chunk,
                }
                yield json.dumps(event) + "\n"
                await asyncio.sleep(0.015)  # ~15ms per chunk = natural streaming speed

            # Done
            done = {
                "type": "done",
                "conversation_id": body.conversation_id,
                "finish_reason": "stop",
            }
            yield json.dumps(done) + "\n"

        except Exception as exc:
            err = {
                "type": "error",
                "conversation_id": body.conversation_id,
                "code": "internal_error",
                "message": str(exc),
            }
            yield json.dumps(err) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


# ── /v1/provision ────────────────────────────────────────────────────────────
# Called by Bridge Cloud at user signup to create a scoped OpenRouter key.
# Requires OPENROUTER_MASTER_KEY env var (your main OpenRouter account key).

class ProvisionRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=128)
    plan: Literal["free", "casual", "regular", "power", "obsessed"] = "casual"
    credit_limit_usd: Optional[float] = None  # Override plan default if provided
    label: Optional[str] = None


@router.post("/provision")
async def v1_provision(
    body: ProvisionRequest,
    x_api_key: str = Header(default=""),
):
    """Create a scoped OpenRouter API key for a new Bridge Cloud user.

    Credit limit is automatically sized to the plan unless overridden.
    Free tier returns immediately — no key needed (uses :free model pool).
    """
    _require_auth(x_api_key)

    # Free tier users don't get an OpenRouter key — they hit :free models only
    if body.plan == "free":
        return {
            "user_id": body.user_id,
            "plan": "free",
            "openrouter_key": None,
            "key_hash": None,
            "key_name": None,
            "credit_limit_usd": 0.0,
            "daily_cap": DAILY_CAPS["free"],
        }

    from config import OPENROUTER_MASTER_KEY
    if not OPENROUTER_MASTER_KEY:
        raise HTTPException(status_code=503, detail="OPENROUTER_MASTER_KEY not configured")

    credit_limit = body.credit_limit_usd if body.credit_limit_usd is not None else PLAN_CREDIT_LIMITS[body.plan]
    key_name = body.label or f"bridge-cloud-{body.plan}-{body.user_id[:16]}-{uuid.uuid4().hex[:6]}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/keys",
            headers={
                "Authorization": f"Bearer {OPENROUTER_MASTER_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "name": key_name,
                "limit": credit_limit,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"OpenRouter provisioning failed: {resp.status_code} {resp.text[:200]}",
        )

    data = resp.json()
    return {
        "user_id": body.user_id,
        "plan": body.plan,
        "openrouter_key": data.get("key"),
        "key_hash": data.get("key_hash"),
        "key_name": key_name,
        "credit_limit_usd": credit_limit,
        "daily_cap": DAILY_CAPS[body.plan],
    }


# ── /v1/upload ────────────────────────────────────────────────────────────────

@router.post("/upload")
async def v1_upload(
    file: UploadFile = File(...),
    x_api_key: str = Header(default=""),
):
    """Save an uploaded file to a temp path and return the path for Claude to read."""
    _require_auth(x_api_key)

    original_name = file.filename or "upload"
    ext = os.path.splitext(original_name)[1] or ".bin"

    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="bc_upload_") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    return {"path": tmp_path, "filename": original_name, "size": len(content)}


# ── /api/chat — static-export proxy ──────────────────────────────────────────
# The Next.js static export can't run server-side API routes, so the browser
# hits /api/chat directly on this FastAPI server.  We replicate what the
# Next.js route.ts did: pick the right bot URL + key and stream /v1/chat.

_API_AGENT_URLS: dict[str, str] = {
    "claude": os.environ.get("BRIDGEBOT_CLAUDE_URL", "http://localhost:8585"),
    "gemini": os.environ.get("BRIDGEBOT_GEMINI_URL", "http://localhost:8586"),
    "codex":  os.environ.get("BRIDGEBOT_CODEX_URL",  "http://localhost:8587"),
    "qwen":   os.environ.get("BRIDGEBOT_QWEN_URL",   "http://localhost:8588"),
    "free":   os.environ.get("BRIDGEBOT_FREE_URL",   "http://localhost:8590"),
}

_API_AGENT_KEYS: dict[str, str] = {
    "claude": os.environ.get("BRIDGEBOT_CLAUDE_KEY", ""),
    "gemini": os.environ.get("BRIDGEBOT_GEMINI_KEY", ""),
    "codex":  os.environ.get("BRIDGEBOT_CODEX_KEY",  ""),
    "qwen":   os.environ.get("BRIDGEBOT_QWEN_KEY",   ""),
    "free":   os.environ.get("BRIDGEBOT_FREE_KEY",   ""),
}

api_router = APIRouter(prefix="/api", tags=["proxy"])

@api_router.post("/chat")
@api_router.post("/chat/")
async def api_chat_proxy(request: Request):
    """Proxy /api/chat → the correct bot's /v1/chat (replicates Next.js route.ts)."""
    try:
        body = await request.json()
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse({"type": "error", "message": "Invalid JSON"}, status_code=400)

    agent_id = body.get("agent_id", "claude")
    target_url = _API_AGENT_URLS.get(agent_id, _API_AGENT_URLS["claude"])
    # Read key at request time so env changes take effect without restart.
    # Falls back to BRIDGE_CLOUD_API_KEY (set in .env.claude) for self-hosted bots.
    api_key = (
        os.environ.get(f"BRIDGEBOT_{agent_id.upper()}_KEY", "")
        or os.environ.get("BRIDGE_CLOUD_API_KEY", "")
        or _API_AGENT_KEYS.get(agent_id, "")
    )

    # Ensure tier defaults to "pro" so Bridge Cloud always uses quality models.
    # The frontend may send tier="free" (default) which would route to slow tool-heavy models.
    forwarded_body = dict(body)
    if not forwarded_body.get("tier") or forwarded_body["tier"] == "free":
        forwarded_body["tier"] = "pro"

    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{target_url}/v1/chat",
                    json=forwarded_body,
                    headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        except Exception as exc:
            import json as _json
            yield (_json.dumps({"type": "error", "message": str(exc)}) + "\n").encode()

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── /api/proxy — static-export proxy (Next.js route.ts replacement) ──────────
# The static export calls /api/proxy (matching Next.js route.ts naming).
# FastAPI replaces this since Next.js API routes can't be statically exported.

@api_router.post("/proxy")
@api_router.post("/proxy/")
async def api_proxy(request: Request):
    """Proxy /api/proxy → the correct bot's /v1/chat (Next.js route.ts replacement)."""
    from fastapi.responses import JSONResponse as _JSONResponse
    try:
        body = await request.json()
    except Exception:
        return _JSONResponse({"error": "Invalid JSON"}, status_code=400)

    agent_id = (body.get("agentId") or body.get("agent_id") or "claude").lower()
    message = body.get("message", "")
    conversation_id = body.get("conversationId") or body.get("conversation_id") or ""
    server_url = body.get("serverUrl") or body.get("server_url") or ""
    server_key = body.get("serverKey") or body.get("server_key") or ""

    # Env-configured cloud configs take precedence (mirrors Next.js route.ts logic)
    env_url = os.environ.get(f"BRIDGEBOT_{agent_id.upper()}_URL", "")
    env_key = os.environ.get(f"BRIDGEBOT_{agent_id.upper()}_KEY", "")
    target_url = env_url or server_url or _API_AGENT_URLS.get(agent_id, _API_AGENT_URLS["claude"])
    target_key = env_key or server_key or os.environ.get("BRIDGE_CLOUD_API_KEY", "")

    if not target_url:
        return _JSONResponse({"error": "No server configured for this agent"}, status_code=503)

    # Validate URL to prevent SSRF
    try:
        from urllib.parse import urlparse as _urlparse
        _p = _urlparse(target_url)
        if _p.scheme not in ("http", "https"):
            raise ValueError("Invalid protocol")
    except Exception:
        return _JSONResponse({"error": "Invalid server URL"}, status_code=400)

    # Determine OpenRouter key and tier.
    # If server_key is an OR key, use it as per-user key (pro tier).
    # Otherwise fall back to server-wide OPENROUTER_API_KEY (also pro tier).
    is_or_key = bool(server_key and server_key.startswith("sk-or-"))
    server_or_key = os.environ.get("OPENROUTER_API_KEY", "")
    or_key = server_key if is_or_key else server_or_key

    chat_body: dict = {
        "message": message,
        "conversation_id": conversation_id,
        "stream": True,
        "instance_id": 0,
        "system_prompt": "",
        "tier": "pro",  # pro → regular tier (DeepSeek/Qwen3/Kimi) — never free
    }
    if or_key:
        chat_body["openrouter_key"] = or_key

    async def _proxy_stream():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{target_url}/v1/chat",
                    json=chat_body,
                    headers={"X-API-Key": target_key, "Content-Type": "application/json"},
                ) as resp:
                    if not (200 <= resp.status_code < 300):
                        import json as _json
                        yield (_json.dumps({"type": "error", "message": f"Upstream error: {resp.status_code}"}) + "\n").encode()
                        return
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        except Exception as exc:
            import json as _json
            yield (_json.dumps({"type": "error", "message": str(exc)}) + "\n").encode()

    return StreamingResponse(
        _proxy_stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


@api_router.post("/proxy/verify")
async def api_proxy_verify(request: Request):
    """Verify a bot server is reachable — used by Bridge Cloud ConnectForm + health check."""
    from fastapi.responses import JSONResponse as _JSONResponse
    try:
        body = await request.json()
    except Exception:
        return _JSONResponse({"status": "error", "error": "Invalid JSON"}, status_code=400)

    url = (body.get("url") or "").rstrip("/")
    api_key = body.get("apiKey") or body.get("api_key") or ""

    if not url:
        return _JSONResponse({"status": "offline", "error": "No URL provided"})

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{url}/v1/health",
                headers={"X-API-Key": api_key} if api_key else {},
            )
            if resp.status_code == 200:
                data = resp.json()
                return _JSONResponse({
                    "status": "online",
                    "agentId": data.get("agent_id") or data.get("runner") or "claude",
                    "botName": data.get("bot_name", ""),
                })
            elif resp.status_code in (401, 403):
                return _JSONResponse({"status": "auth_error"})
            else:
                return _JSONResponse({"status": "offline"})
    except Exception as exc:
        return _JSONResponse({"status": "offline", "error": str(exc)})
