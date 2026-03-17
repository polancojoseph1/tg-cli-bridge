"""Collab API router — federated peer endpoints.

Endpoints:
    GET  /collab/profile          — public, no auth
    GET  /collab/peers            — owner auth (COLLAB_TOKEN)
    POST /collab/delegate         — peer auth (X-Collab-Token)
    GET  /collab/memory/search    — peer auth
    POST /collab/broadcast        — peer auth
    GET  /collab/feed             — peer auth
"""

import asyncio
import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import get_peer
from .config import COLLAB_INSTANCE_NAME, COLLAB_TOKEN, load_peers
from .feed import append_event, get_feed
from .permissions import can, check_agent_access, check_bot_access, get_memory_scope
from . import client as collab_client
from . import borrow as borrow_mgr

logger = logging.getLogger("bridge.collab.router")

router = APIRouter(prefix="/collab", tags=["collab"])

COLLAB_VERSION = "1.0.0"

# ── Pydantic models ──────────────────────────────────────────────────────────


class DelegateRequest(BaseModel):
    task: str
    agent_id: str | None = None
    bot: str | None = None
    context: str = ""


class BroadcastRequest(BaseModel):
    message: str
    from_name: str


class BorrowStartRequest(BaseModel):
    bot: str | None = None


class BorrowMessageRequest(BaseModel):
    session_id: str
    text: str


# ── Owner auth helper ────────────────────────────────────────────────────────


def _require_owner_token(request: Request) -> None:
    """Verify the request carries this instance's COLLAB_TOKEN (owner-only endpoints)."""
    if not COLLAB_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="COLLAB_TOKEN not configured on this instance",
        )
    token = request.headers.get("X-Collab-Token", "").strip()
    if not hmac.compare_digest(token, COLLAB_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid owner token")


# ── GET /collab/profile ──────────────────────────────────────────────────────


@router.get("/profile")
async def get_profile():
    """Public profile endpoint — returns this instance's capabilities."""
    import config as main_config

    # Collect available bots — use CLI_RUNNER from main config
    bots = [main_config.CLI_RUNNER]

    # Collect available agents from agent_registry
    agents: list[str] = []
    try:
        from agent_registry import list_agents
        agents = [a.id for a in list_agents()]
    except Exception as e:
        logger.debug("Could not list agents for profile: %s", e)

    return {
        "instance_name": COLLAB_INSTANCE_NAME,
        "owner": main_config.USER_NAME or COLLAB_INSTANCE_NAME,
        "bots": bots,
        "agents": agents,
        "version": "1.0.0",
        "collab_version": COLLAB_VERSION,
    }


# ── GET /collab/peers ────────────────────────────────────────────────────────


@router.get("/peers")
async def list_peers(request: Request):
    """Owner-only: list all known peers with online status."""
    _require_owner_token(request)

    peers = load_peers()
    result = []

    for name, peer in peers.items():
        # Quick health check — fetch profile with 3s timeout
        profile = None
        try:
            import asyncio
            profile = await asyncio.wait_for(
                collab_client.fetch_profile(peer),
                timeout=3.0,
            )
        except Exception:
            profile = None

        online = profile is not None
        result.append({
            "name": name,
            "url": peer.get("url", ""),
            "tier": peer.get("tier", "acquaintance"),
            "online": online,
            "bots": profile.get("bots", peer.get("bots", [])) if profile else peer.get("bots", []),
            "agents": profile.get("agents", []) if profile else [],
        })

    return {"peers": result, "count": len(result)}


# ── POST /collab/delegate ────────────────────────────────────────────────────


@router.post("/delegate")
async def delegate(
    body: DelegateRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: run a task on this instance and return the result."""
    peer_name, peer = peer_auth

    # Permission checks
    if not can(peer, "delegate"):
        raise HTTPException(status_code=403, detail="Delegate not permitted for your tier")

    if body.agent_id and not check_agent_access(peer, body.agent_id):
        raise HTTPException(
            status_code=403,
            detail=f"Access to agent '{body.agent_id}' not permitted for your tier",
        )

    if body.bot and not check_bot_access(peer, body.bot):
        raise HTTPException(
            status_code=403,
            detail=f"Access to bot '{body.bot}' not permitted for your tier",
        )

    start_ms = int(time.time() * 1000)

    # Run the task via the runner
    result = await _run_task(body.task, body.agent_id, body.context)

    duration_ms = int(time.time() * 1000) - start_ms

    # Record in activity feed
    await append_event(
        bot=body.bot or "default",
        action="delegate",
        summary=f"[{peer_name}] {body.task[:120]}",
        peer_name=peer_name,
    )

    logger.info(
        "Delegate from '%s': agent=%s task=%s... (%dms)",
        peer_name, body.agent_id, body.task[:60], duration_ms,
    )

    return {
        "result": result,
        "agent_id": body.agent_id,
        "duration_ms": duration_ms,
    }


async def _run_task(task: str, agent_id: str | None, context: str) -> str:
    """Run a task using the configured runner, optionally via a named agent.

    Falls back gracefully if agent_id is unknown or runner is unavailable.
    """
    prompt = task
    if context:
        prompt = f"Context: {context}\n\n{task}"

    # If a specific agent is requested, inject its system prompt
    system_prompt: str | None = None
    if agent_id:
        try:
            from agent_registry import get_agent
            agent = get_agent(agent_id)
            if agent and agent.system_prompt:
                system_prompt = agent.system_prompt
                prompt = f"{system_prompt}\n\n{prompt}"
        except Exception as e:
            logger.debug("Could not load agent '%s': %s", agent_id, e)

    try:
        from runners import create_runner
        runner = create_runner()
        result = await runner.run_query(prompt, timeout=120)
        return result or "(no response)"
    except Exception as e:
        logger.error("_run_task failed: %s", e)
        return f"Error running task: {e}"


# ── GET /collab/memory/search ────────────────────────────────────────────────


@router.get("/memory/search")
async def memory_search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=20),
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)] = None,
):
    """Peer-authenticated: search this instance's memory."""
    peer_name, peer = peer_auth

    if not can(peer, "memory_search"):
        raise HTTPException(status_code=403, detail="Memory search not permitted for your tier")

    scope = get_memory_scope(peer)
    results: list[dict] = []

    if scope == "all":
        # Search the full memory collection
        try:
            import memory_handler
            raw = await memory_handler.search_memory(q, n_results=limit)
            # search_memory returns a formatted string — wrap it as a single result
            if raw:
                results = [{"content": raw, "metadata": {}, "score": 1.0}]
        except Exception as e:
            logger.error("memory_search (all) failed: %s", e)

    elif scope == "shared":
        # Search only the Shared/ collection
        try:
            import agent_memory
            results = await agent_memory.search_shared(q, limit)
        except Exception as e:
            logger.error("memory_search (shared) failed: %s", e)

    safe_q = q[:60].replace("\n", "\\n").replace("\r", "\\r")
    logger.info("Memory search by '%s': q=%s scope=%s results=%d", peer_name, safe_q, scope, len(results))

    return {"results": results, "scope": scope or "none"}


# ── POST /collab/broadcast ───────────────────────────────────────────────────


@router.post("/broadcast")
async def broadcast(
    body: BroadcastRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: receive a broadcast message and append to feed."""
    peer_name, peer = peer_auth

    if not can(peer, "broadcast"):
        raise HTTPException(status_code=403, detail="Broadcast not permitted for your tier")

    summary = f"[broadcast from {body.from_name}] {body.message[:200]}"
    await append_event(
        bot="collab",
        action="broadcast",
        summary=summary,
        peer_name=peer_name,
    )

    logger.info("Broadcast received from peer '%s' (from_name=%s)", peer_name, body.from_name)
    return {"ok": True}


# ── GET /collab/feed ─────────────────────────────────────────────────────────


@router.get("/feed")
async def feed_endpoint(
    limit: int = Query(20, ge=1, le=50),
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)] = None,
):
    """Peer-authenticated: return this instance's activity feed."""
    peer_name, peer = peer_auth

    if not can(peer, "feed_read"):
        raise HTTPException(status_code=403, detail="Feed access not permitted for your tier")

    events = await get_feed(limit=limit)
    return {"events": events, "instance_name": COLLAB_INSTANCE_NAME}


# ── POST /collab/borrow/start ─────────────────────────────────────────────────


@router.post("/borrow/start")
async def borrow_start(
    body: BorrowStartRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: start a borrow session on this instance."""
    peer_name, peer = peer_auth

    if not can(peer, "borrow"):
        raise HTTPException(status_code=403, detail="Borrow not permitted for your tier")

    if body.bot and not check_bot_access(peer, body.bot):
        raise HTTPException(
            status_code=403,
            detail=f"Access to bot '{body.bot}' not permitted for your tier",
        )

    # Pick the bot to use
    import config as main_config
    bot = body.bot if body.bot else main_config.CLI_RUNNER

    # Create a guest instance for this borrow session
    try:
        from instance_manager import InstanceManager
        import server as _server_mod
        _instances = _server_mod.instances
        guest_instance = _instances.create(
            f"Borrow:{peer_name}",
            owner_id=0,
            switch_active=False,
        )
        instance_id = guest_instance.id
    except Exception as e:
        logger.error("Failed to create guest instance for borrow: %s", e)
        instance_id = 0

    session = borrow_mgr.create_session(peer_name, bot, instance_id)

    await append_event(
        bot=bot,
        action="borrow_start",
        summary=f"{peer_name} started borrowing {bot}",
        peer_name=peer_name,
    )

    # Notify owner via Telegram
    try:
        from telegram_handler import send_message as _tg_send
        import config as _cfg
        asyncio.create_task(_tg_send(
            _cfg.ALLOWED_USER_ID,
            f"Borrow session started: {peer_name} is now using your {bot} bot.",
        ))
    except Exception as e:
        logger.debug("Could not notify owner of borrow start: %s", e)

    label = f"{bot.title()} @ {COLLAB_INSTANCE_NAME}"
    logger.info("Borrow started: peer=%s bot=%s session=%s", peer_name, bot, session.session_id)

    return {
        "session_id": session.session_id,
        "bot": bot,
        "label": label,
    }


# ── POST /collab/borrow/message ───────────────────────────────────────────────


@router.post("/borrow/message")
async def borrow_message(
    body: BorrowMessageRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: send a message through an active borrow session."""
    peer_name, peer = peer_auth

    session = borrow_mgr.get_session(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Borrow session not found")

    if session.peer_name != peer_name:
        raise HTTPException(status_code=403, detail="This session does not belong to you")

    borrow_mgr.touch_session(body.session_id)

    result = await _run_task(body.text, agent_id=None, context="")

    await append_event(
        bot=session.bot,
        action="borrow_message",
        summary=f"[{peer_name}] {body.text[:120]}",
        peer_name=peer_name,
    )

    logger.info("Borrow message: peer=%s session=%s text=%s...", peer_name, body.session_id, body.text[:60])

    return {
        "response": result,
        "bot": session.bot,
        "instance_name": COLLAB_INSTANCE_NAME,
    }


# ── DELETE /collab/borrow/{session_id} ────────────────────────────────────────


@router.delete("/borrow/{session_id}")
async def borrow_end(
    session_id: str,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: end an active borrow session."""
    peer_name, peer = peer_auth

    session = borrow_mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Borrow session not found")

    if session.peer_name != peer_name:
        raise HTTPException(status_code=403, detail="This session does not belong to you")

    duration = time.time() - session.started_at
    borrow_mgr.end_session(session_id)

    # Clean up guest instance
    try:
        import server as _server_mod
        _server_mod.instances.remove(session.instance_id, owner_id=0)
    except Exception as e:
        logger.debug("Could not remove guest instance %d: %s", session.instance_id, e)

    await append_event(
        bot=session.bot,
        action="borrow_end",
        summary=f"{peer_name} disconnected after {int(duration / 60)} min",
        peer_name=peer_name,
    )

    # Notify owner via Telegram
    try:
        from telegram_handler import send_message as _tg_send
        import config as _cfg
        asyncio.create_task(_tg_send(
            _cfg.ALLOWED_USER_ID,
            f"{peer_name} has disconnected. Session lasted {int(duration / 60)} minutes.",
        ))
    except Exception as e:
        logger.debug("Could not notify owner of borrow end: %s", e)

    logger.info("Borrow ended: peer=%s session=%s duration=%.0fs", peer_name, session_id, duration)

    return {"ok": True, "duration_seconds": int(duration)}
