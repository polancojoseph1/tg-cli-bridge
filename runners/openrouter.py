"""OpenRouter HTTP runner for Bridge Cloud.

Direct HTTP calls to OpenRouter API — no CLI subprocess needed.
Handles conversation history, streaming, model routing, and fallback.
"""

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"

# Max conversation history per session (messages, not turns)
MAX_HISTORY = 40

# Per-conversation message store: { conversation_id: [messages] }
_histories: dict[str, list[dict]] = {}
_last_access: dict[str, float] = {}

# Cleanup conversations older than 2 hours
_HISTORY_TTL = 7200


def _cleanup_stale():
    """Remove conversations not accessed in the last 2 hours."""
    now = time.time()
    stale = [cid for cid, ts in _last_access.items() if now - ts > _HISTORY_TTL]
    for cid in stale:
        _histories.pop(cid, None)
        _last_access.pop(cid, None)


def _get_history(conversation_id: str) -> list[dict]:
    """Get or create conversation history."""
    _cleanup_stale()
    _last_access[conversation_id] = time.time()
    if conversation_id not in _histories:
        _histories[conversation_id] = []
    return _histories[conversation_id]


def _trim_history(messages: list[dict]) -> list[dict]:
    """Trim to last MAX_HISTORY messages, keeping system prompt if present."""
    if len(messages) <= MAX_HISTORY:
        return messages
    # Keep system message if it's first
    if messages and messages[0].get("role") == "system":
        return [messages[0]] + messages[-(MAX_HISTORY - 1):]
    return messages[-MAX_HISTORY:]


def clear_history(conversation_id: str) -> None:
    """Clear conversation history for a session reset."""
    _histories.pop(conversation_id, None)
    _last_access.pop(conversation_id, None)


async def run(
    message: str,
    model: str,
    api_key: str,
    conversation_id: str,
    system_prompt: str | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> str:
    """Send a message to OpenRouter and return the response.

    Maintains per-conversation history. Streams the response.

    Args:
        message:          User message text.
        model:            OpenRouter model ID (e.g. "qwen/qwen3-30b-a3b:free").
        api_key:          OpenRouter API key (per-user or server-wide).
        conversation_id:  Unique conversation ID for history tracking.
        system_prompt:    Optional system prompt (injected as first message).
        on_progress:      Async callback for streaming progress updates.
        temperature:      Sampling temperature.
        max_tokens:       Max response tokens.

    Returns:
        The assistant's response text.
    """
    if not api_key:
        raise ValueError("No OpenRouter API key provided")

    history = _get_history(conversation_id)

    # Inject system prompt if provided and not already present
    if system_prompt and (not history or history[0].get("role") != "system"):
        history.insert(0, {"role": "system", "content": system_prompt})

    # Add user message
    history.append({"role": "user", "content": message})

    # Trim to keep context manageable
    trimmed = _trim_history(history)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bridge.bot",
        "X-Title": "Bridge Cloud",
    }

    payload = {
        "model": model,
        "messages": trimmed,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if on_progress:
        await on_progress(f"\U0001f30f {model.split('/')[-1]}")

    full_response = ""
    retry_models = []

    # Build fallback list for free models — merge both pools, deduplicate
    if ":free" in model:
        from v1_api import _FREE_MODELS_GENERAL, _FREE_MODELS_CODING
        seen = {model}
        for m in _FREE_MODELS_CODING + _FREE_MODELS_GENERAL:
            if m not in seen:
                retry_models.append(m)
                seen.add(m)

    models_to_try = [model] + retry_models  # Try all available free models

    last_error = None
    for idx, try_model in enumerate(models_to_try):
        payload["model"] = try_model
        try:
            full_response = await _stream_request(headers, payload, on_progress)
            if full_response:
                break
        except Exception as exc:
            last_error = exc
            logger.warning("OpenRouter model %s failed: %s", try_model, exc)
            # Brief back-off between retries to avoid hammering rate limits
            if idx < len(models_to_try) - 1:
                await asyncio.sleep(1.0)
            continue

    if not full_response:
        if last_error:
            raise RuntimeError(
                f"All {len(models_to_try)} OpenRouter model(s) failed. "
                f"Last error: {last_error}. Free-tier rate limits may be exhausted — try again in a minute."
            )
        raise RuntimeError("All models failed to produce a response")

    # Save assistant response to history
    history.append({"role": "assistant", "content": full_response})

    # Update the stored history (trimmed)
    _histories[conversation_id] = _trim_history(history)

    return full_response


async def _stream_request(
    headers: dict,
    payload: dict,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Make a streaming request to OpenRouter and return the full response text."""
    full_response = ""

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        async with client.stream(
            "POST",
            OPENROUTER_BASE,
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code == 429:
                raise httpx.HTTPStatusError(
                    "Rate limited",
                    request=resp.request,
                    response=resp,
                )
            if resp.status_code != 200:
                body = await resp.aread()
                raise httpx.HTTPStatusError(
                    f"OpenRouter {resp.status_code}: {body.decode()[:500]}",
                    request=resp.request,
                    response=resp,
                )

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    text = delta.get("content", "")
                    if text:
                        full_response += text
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

    return full_response


async def run_query(
    prompt: str,
    model: str,
    api_key: str,
    timeout: int = 120,
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> str:
    """Stateless one-shot query — no conversation history."""
    if not api_key:
        raise ValueError("No OpenRouter API key provided")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bridge.bot",
        "X-Title": "Bridge Cloud",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        resp = await client.post(OPENROUTER_BASE, headers=headers, json=payload)
        if resp.status_code != 200:
            raise httpx.HTTPStatusError(
                f"OpenRouter {resp.status_code}: {resp.text[:500]}",
                request=resp.request,
                response=resp,
            )
        data = resp.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
