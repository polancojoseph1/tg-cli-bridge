"""Rotating OpenAI-compatible proxy for Free Bot.

Handles both endpoints FreeCode uses:
- POST /v1/chat/completions  — standard Chat Completions API
- POST /v1/responses         — OpenAI Responses API (used by FreeCode 1.2+)

Transparently rotates through all configured free providers on 429 or errors.
Logs every provider switch for debugging.

FreeCode points here via:
    OPENAI_API_KEY=free-proxy
    OPENAI_BASE_URL=http://127.0.0.1:8592/v1
    FREECODE_MODEL=freecode/free-bot  (model name is ignored — proxy uses actual provider models)
"""

import json
import logging
import os
import time
import uuid

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from runners.free import _build_providers, Provider, QwenCLIProvider

logger = logging.getLogger("bridge.free_proxy")

# --- Lazy singletons (initialised after env vars are loaded) ---

_providers: list | None = None
_client: httpx.AsyncClient | None = None


def _get_providers() -> list:
    global _providers
    if _providers is None:
        _providers = _build_providers()
        available = [
            p.name for p in _providers
            if (isinstance(p, QwenCLIProvider) and p.is_configured())
            or (isinstance(p, Provider) and p.api_key)
        ]
        logger.info("[free-proxy] Loaded %d providers: %s", len(available), available)
    return _providers


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


# --- Core rotation logic (shared by both endpoints) ---

async def _call_providers(
    messages: list[dict],
    max_tokens: int | None = None,
    tools: list | None = None,
    tool_choice=None,
) -> dict | None:
    """Try providers in order, rotate on 429/error. Returns Chat Completions response or None."""
    providers = _get_providers()
    client = _get_client()
    timeout = int(os.environ.get("CLI_TIMEOUT", "120"))
    provider_timeout = int(os.environ.get("FREE_PROXY_PROVIDER_TIMEOUT", "30"))
    deadline = time.time() + timeout
    max_tokens = max_tokens or int(os.environ.get("FREE_MAX_TOKENS", "4096"))
    temperature = float(os.environ.get("FREE_TEMPERATURE", "0.7"))

    available = [p for p in providers if isinstance(p, Provider) and p.is_available()]
    if not available:
        return None

    last_error = ""
    for provider in available:
        remaining = int(deadline - time.time())
        if remaining <= 5:
            break

        request_body: dict = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            request_body["tools"] = tools
        if tool_choice is not None:
            request_body["tool_choice"] = tool_choice

        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        if "openrouter" in provider.base_url:
            headers["HTTP-Referer"] = "https://bridgebot.local"
            headers["X-Title"] = "Bridgebot"

        logger.info("[free-proxy] → %s (%s)", provider.name, provider.model)
        t0 = time.time()

        call_timeout = min(provider_timeout, remaining)
        try:
            resp = await client.post(
                f"{provider.base_url}/chat/completions",
                json=request_body,
                headers=headers,
                timeout=float(call_timeout),
            )
        except httpx.TimeoutException:
            last_error = f"{provider.name} timed out"
            logger.warning("[free-proxy] %s timed out — skipping", provider.name)
            provider._cooldown_until = time.time() + 15.0
            continue
        except httpx.HTTPError as e:
            last_error = f"{provider.name} network error: {type(e).__name__}"
            logger.warning("[free-proxy] %s network error — skipping", provider.name)
            provider._cooldown_until = time.time() + 15.0
            continue

        if resp.status_code in (429, 413):
            provider.mark_rate_limited()
            logger.info("[free-proxy] %s → %d — switching to next provider", provider.name, resp.status_code)
            last_error = f"{provider.name} rate/token limited"
            continue

        if resp.status_code != 200:
            try:
                err_body = resp.text[:300]
            except Exception:
                err_body = ""
            last_error = f"{provider.name} HTTP {resp.status_code}"
            logger.warning("[free-proxy] %s → %d — body: %s — skipping", provider.name, resp.status_code, err_body)
            provider._cooldown_until = time.time() + 15.0
            continue

        try:
            data = resp.json()
        except Exception:
            last_error = f"{provider.name} non-JSON"
            continue

        elapsed = time.time() - t0
        usage = data.get("usage", {})
        tokens = usage.get("total_tokens", "?")
        logger.info("[free-proxy] %s → success (%.1fs, %s tokens)", provider.name, elapsed, tokens)
        provider.mark_success()
        return data

    logger.error("[free-proxy] All providers failed. Last error: %s", last_error)
    return None


# --- /v1/chat/completions ---

async def chat_completions(request: Request) -> Response:
    """POST /v1/chat/completions — standard Chat Completions (streaming + non-streaming)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid JSON body"}}, status_code=400)

    stream = body.get("stream", False)

    data = await _call_providers(
        messages=body.get("messages", []),
        max_tokens=body.get("max_tokens"),
        tools=body.get("tools"),
        tool_choice=body.get("tool_choice"),
    )
    if data is None:
        err = {"error": {"message": "All free providers failed or are temporarily busy."}}
        if stream:
            async def _err_chat_stream():
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_err_chat_stream(), media_type="text/event-stream")
        return _all_busy_error()

    if not stream:
        return JSONResponse(data)

    # Fake-stream: wrap the completed response as SSE chunks
    return StreamingResponse(
        _stream_chat_completion(data),
        media_type="text/event-stream",
    )


async def _stream_chat_completion(data: dict):
    """Convert a completed Chat Completion response into SSE streaming chunks."""
    completion_id = data.get("id", f"chatcmpl-{uuid.uuid4().hex[:20]}")
    model = data.get("model", "free-proxy")
    created = data.get("created", int(time.time()))
    choices = data.get("choices", [{}])
    usage = data.get("usage", {})

    assistant_msg = choices[0].get("message", {}) if choices else {}
    content = assistant_msg.get("content") or ""
    tool_calls = assistant_msg.get("tool_calls") or []
    finish_reason = choices[0].get("finish_reason", "stop") if choices else "stop"

    base = {"id": completion_id, "object": "chat.completion.chunk",
            "created": created, "model": model}

    if tool_calls:
        # Emit tool call chunks
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            chunk = {**base, "choices": [{
                "index": 0,
                "delta": {"tool_calls": [{
                    "index": i,
                    "id": tc.get("id", f"call_{uuid.uuid4().hex[:20]}"),
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "{}")},
                }]},
                "finish_reason": None,
            }]}
            yield f"data: {json.dumps(chunk)}\n\n"
    elif content:
        # Emit content in one chunk
        chunk = {**base, "choices": [{"index": 0,
            "delta": {"role": "assistant", "content": content}, "finish_reason": None}]}
        yield f"data: {json.dumps(chunk)}\n\n"

    # Final chunk with finish_reason + usage
    final = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
             "usage": usage}
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


# --- /v1/responses (OpenAI Responses API — used by FreeCode 1.2+) ---

def _responses_input_to_messages(input_items: list) -> list[dict]:
    """Convert Responses API input array → Chat Completions messages list."""
    messages = []
    for item in input_items:
        item_type = item.get("type", "message")

        # Tool result
        if item_type == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": str(item.get("output", "")),
            })
            continue

        # Prior assistant function_call (tool use) in history
        if item_type == "function_call":
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }],
            })
            continue

        # Regular message
        role = item.get("role", "user")
        if role == "developer":
            role = "system"

        content = item.get("content", "")
        if isinstance(content, list):
            # Flatten content blocks to text
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", block.get("input_text", "")))
                else:
                    parts.append(str(block))
            content = "\n".join(p for p in parts if p)

        messages.append({"role": role, "content": content})

    return messages


def _chat_message_to_responses_output(msg: dict) -> list[dict]:
    """Convert Chat Completions assistant message → Responses API output items."""
    output = []
    tool_calls = msg.get("tool_calls") or []

    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:20]}",
                "call_id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": fn.get("arguments", "{}"),
                "status": "completed",
            })
    else:
        content = msg.get("content") or ""
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:20]}",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content}],
            "status": "completed",
        })

    return output


def _responses_tools_to_chat_tools(tools: list) -> list:
    """Convert Responses API tools format → Chat Completions tools format.

    Responses API:  {"type": "function", "name": "...", "description": "...", "parameters": {...}}
    Chat Completions: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    converted = []
    for tool in tools:
        if tool.get("type") == "function" and "name" in tool:
            # Responses API format — wrap in function object
            fn = {k: v for k, v in tool.items() if k != "type"}
            converted.append({"type": "function", "function": fn})
        else:
            # Already in Chat Completions format (has "function" key)
            converted.append(tool)
    return converted


async def responses_completions(request: Request) -> Response:
    """POST /v1/responses — OpenAI Responses API for FreeCode 1.2+ compatibility."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid JSON body"}}, status_code=400)

    stream = body.get("stream", False)

    # Convert Responses API → Chat Completions
    messages = _responses_input_to_messages(body.get("input", []))

    # Convert tools: Responses API uses {type, name, description, parameters}
    # Chat Completions needs {type, function: {name, description, parameters}}
    raw_tools = body.get("tools")
    tools = _responses_tools_to_chat_tools(raw_tools) if raw_tools else None
    tool_choice = body.get("tool_choice")
    max_tokens = body.get("max_output_tokens")

    data = await _call_providers(messages, max_tokens, tools, tool_choice)

    if data is None:
        err = {"error": {"message": "All free providers failed or are temporarily busy."}}
        if stream:
            async def _err_stream():
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(_err_stream(), media_type="text/event-stream")
        return JSONResponse(err, status_code=503)

    # Build Responses API response object
    choices = data.get("choices", [{}])
    assistant_msg = choices[0].get("message", {}) if choices else {}
    output_items = _chat_message_to_responses_output(assistant_msg)

    resp_id = f"resp_{uuid.uuid4().hex[:20]}"
    usage_raw = data.get("usage", {})
    response_obj = {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "output": output_items,
        "usage": {
            "input_tokens": usage_raw.get("prompt_tokens", 0),
            "output_tokens": usage_raw.get("completion_tokens", 0),
            "total_tokens": usage_raw.get("total_tokens", 0),
        },
    }

    if not stream:
        return JSONResponse(response_obj)

    # Streaming: emit SSE events (non-streaming upstream, fake-stream the result)
    return StreamingResponse(
        _stream_response_events(response_obj, output_items),
        media_type="text/event-stream",
    )


async def _stream_response_events(response_obj: dict, output_items: list):
    """Yield Responses API SSE events for a completed response."""

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    # response.created (no output yet)
    yield sse("response.created", {
        "type": "response.created",
        "response": {**response_obj, "output": []},
    })

    for idx, item in enumerate(output_items):
        item_id = item.get("id", f"msg_{uuid.uuid4().hex[:16]}")

        if item.get("type") == "message":
            # output_item.added
            yield sse("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": idx,
                "item": {**item, "content": [], "status": "in_progress"},
            })

            for c_idx, part in enumerate(item.get("content", [])):
                text = part.get("text", "")
                # content_part.added
                yield sse("response.content_part.added", {
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": idx,
                    "content_index": c_idx,
                    "part": {"type": "output_text", "text": ""},
                })
                # output_text.delta (single chunk — full text at once)
                yield sse("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": idx,
                    "content_index": c_idx,
                    "delta": text,
                })
                # output_text.done
                yield sse("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "content_index": c_idx,
                    "text": text,
                })

            # output_item.done
            yield sse("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": item,
            })

        elif item.get("type") == "function_call":
            # Tool call
            yield sse("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": idx,
                "item": {**item, "status": "in_progress"},
            })
            yield sse("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta",
                "item_id": item_id,
                "output_index": idx,
                "delta": item.get("arguments", "{}"),
            })
            yield sse("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": item_id,
                "output_index": idx,
                "arguments": item.get("arguments", "{}"),
            })
            yield sse("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": item,
            })

    # response.completed — AI SDK reads this to determine finishReason
    # When incomplete_details is null/absent and no function_call items: finishReason = "stop"
    # When function_call items present: finishReason = "tool-calls"
    yield sse("response.completed", {
        "type": "response.completed",
        "response": {
            "incomplete_details": None,
            "usage": response_obj.get("usage", {"input_tokens": 0, "output_tokens": 0}),
        },
    })
    yield "data: [DONE]\n\n"


# --- /health ---

async def proxy_health(request: Request) -> Response:
    providers = _get_providers()
    now = time.time()
    statuses = []
    for p in providers:
        if isinstance(p, QwenCLIProvider):
            statuses.append({"name": p.name, "type": "cli", "available": p.is_available()})
        else:
            cd = round(max(0.0, p._cooldown_until - now), 1)
            statuses.append({
                "name": p.name,
                "model": p.model,
                "available": p.is_available(),
                "cooldown_remaining_s": cd,
            })
    available_count = sum(1 for s in statuses if s.get("available"))
    return JSONResponse({
        "status": "ok",
        "available_providers": available_count,
        "total_providers": len(statuses),
        "providers": statuses,
    })


async def list_models(request: Request) -> Response:
    """GET /v1/models — return a fake models list so FreeCode can validate the endpoint."""
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": "free-bot", "object": "model", "created": 1677610602, "owned_by": "free-proxy"},
            {"id": "llama-3.3-70b-versatile", "object": "model", "created": 1677610602, "owned_by": "free-proxy"},
            {"id": "qwen-3-235b", "object": "model", "created": 1677610602, "owned_by": "free-proxy"},
        ],
    })


def _all_busy_error() -> JSONResponse:
    providers = _get_providers()
    configured = [p for p in providers if isinstance(p, Provider) and p.api_key]
    if configured:
        soonest = min(configured, key=lambda p: p._cooldown_until)
        wait = max(0.0, soonest._cooldown_until - time.time())
        msg = f"All free providers are temporarily busy. Try again in {wait:.0f}s."
    else:
        msg = "No providers configured. Add API keys to .env.free."
    return JSONResponse({"error": {"message": msg}}, status_code=503)


# --- Starlette app ---

app = Starlette(routes=[
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/v1/responses", responses_completions, methods=["POST"]),
    Route("/v1/models", list_models, methods=["GET"]),
    Route("/health", proxy_health, methods=["GET"]),
])
