"""BridgeNet API router — federated peer endpoints.

Exports two APIRouter instances:
  collab_router   — /collab/* endpoints (backward compatible, unchanged)
  bridgenet_router — /bridgenet/* endpoints (new, with sanitization + credits)

Both are exported from __init__.py so server.py can mount them independently.

/collab/* endpoints (backward compat — identical to original collab/router.py):
  GET  /collab/profile
  GET  /collab/peers
  POST /collab/delegate
  GET  /collab/memory/search
  POST /collab/broadcast
  GET  /collab/feed
  POST /collab/borrow/start
  POST /collab/borrow/message
  DELETE /collab/borrow/{session_id}

/bridgenet/* endpoints (new):
  GET  /bridgenet/profile    — extended profile with relay status
  GET  /bridgenet/peers      — same as /collab/peers
  POST /bridgenet/task       — relay-forwarded or peer task (with sanitization)
  POST /bridgenet/result     — receive result forwarded by relay
  GET  /bridgenet/status     — node status, credits, reputation
  POST /bridgenet/broadcast  — same as /collab/broadcast
  GET  /bridgenet/feed       — same as /collab/feed (higher limit)
  POST /bridgenet/borrow/start
  POST /bridgenet/borrow/message
  DELETE /bridgenet/borrow/{session_id}
"""

import asyncio
import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .auth import get_peer, get_relay_or_peer
from .config import (
    BRIDGENET_ENABLED,
    BRIDGENET_NODE_NAME,
    BRIDGENET_RELAY_URL,
    BRIDGENET_TOKEN,
    load_peers,
    get_or_create_node_id,
)
from .feed import append_event, get_feed
from .permissions import can, check_agent_access, check_bot_access, get_memory_scope
from .sanitizer import sanitize_task
from . import reputation as rep
from . import credits as credit_ledger
from . import client as bridgenet_client
from . import borrow as borrow_mgr

logger = logging.getLogger("bridge.bridgenet.router")

BRIDGENET_VERSION = "1.0.0"

# ── In-memory result store (relay-forwarded results) ─────────────────────────
# task_id → {"result": str, "event": asyncio.Event, "ts": float}
_pending_results: dict[str, dict] = {}
_pending_lock = asyncio.Lock()


# ── Pydantic models ───────────────────────────────────────────────────────────


class DelegateRequest(BaseModel):
    task: str
    agent_id: str | None = None
    bot: str | None = None
    context: str = ""


class BridgeNetTaskRequest(BaseModel):
    task_id: str | None = None
    task_type: str = "chat"
    content: str
    agent_id: str | None = None
    bot: str | None = None
    context: str = ""


class BridgeNetResultRequest(BaseModel):
    task_id: str
    result: str
    source_node: str = ""


class BroadcastRequest(BaseModel):
    message: str
    from_name: str


class BorrowStartRequest(BaseModel):
    bot: str | None = None


class BorrowMessageRequest(BaseModel):
    session_id: str
    text: str


# ── Shared owner auth helper ──────────────────────────────────────────────────


def _require_owner_token(request: Request) -> None:
    """Verify the request carries this instance's BRIDGENET_TOKEN (owner-only)."""
    token = BRIDGENET_TOKEN
    if not token:
        raise HTTPException(
            status_code=503,
            detail="BRIDGENET_TOKEN not configured on this instance",
        )
    provided = (
        request.headers.get("X-BridgeNet-Token", "").strip()
        or request.headers.get("X-Collab-Token", "").strip()
    )
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="Invalid owner token")


# ── Shared task runner ────────────────────────────────────────────────────────


async def _run_task(task: str, agent_id: str | None, context: str) -> str:
    """Run a task using the configured runner, optionally via a named agent."""
    prompt = task
    if context:
        prompt = f"Context: {context}\n\n{task}"

    if agent_id:
        try:
            from agent_registry import get_agent
            agent = get_agent(agent_id)
            if agent and agent.system_prompt:
                prompt = f"{agent.system_prompt}\n\n{prompt}"
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


# ═════════════════════════════════════════════════════════════════════════════
# /collab/* router — backward-compatible, logic unchanged from collab/router.py
# ═════════════════════════════════════════════════════════════════════════════

collab_router = APIRouter(prefix="/collab", tags=["collab"])

# Re-use COLLAB_VERSION alias for /collab/* endpoints
_COLLAB_VERSION = "1.0.0"


def _collab_require_owner(request: Request) -> None:
    """Owner auth for /collab/* endpoints (uses BRIDGENET_TOKEN / COLLAB_TOKEN)."""
    _require_owner_token(request)


@collab_router.get("/profile")
async def collab_get_profile():
    """Public profile endpoint — returns this instance's capabilities."""
    import config as main_config

    bots = [main_config.CLI_RUNNER]
    agents: list[str] = []
    try:
        from agent_registry import list_agents
        agents = [a.id for a in list_agents()]
    except Exception as e:
        logger.debug("Could not list agents for collab profile: %s", e)

    return {
        "instance_name": BRIDGENET_NODE_NAME,
        "owner": getattr(main_config, "USER_NAME", None) or BRIDGENET_NODE_NAME,
        "bots": bots,
        "agents": agents,
        "version": "1.0.0",
        "collab_version": _COLLAB_VERSION,
    }


@collab_router.get("/peers")
async def collab_list_peers(request: Request):
    """Owner-only: list all known peers with online status."""
    _collab_require_owner(request)

    peers = load_peers()
    result = []
    for name, peer in peers.items():
        profile = None
        try:
            profile = await asyncio.wait_for(
                bridgenet_client.fetch_profile(peer), timeout=3.0
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


@collab_router.post("/delegate")
async def collab_delegate(
    body: DelegateRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: run a task on this instance and return the result."""
    peer_name, peer = peer_auth

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
    result = await _run_task(body.task, body.agent_id, body.context)
    duration_ms = int(time.time() * 1000) - start_ms

    await append_event(
        bot=body.bot or "default",
        action="delegate",
        summary=f"[{peer_name}] {body.task[:120]}",
        peer_name=peer_name,
    )

    logger.info(
        "Collab delegate from '%s': agent=%s task=%s... (%dms)",
        peer_name, body.agent_id, body.task[:60], duration_ms,
    )
    return {"result": result, "agent_id": body.agent_id, "duration_ms": duration_ms}


@collab_router.get("/memory/search")
async def collab_memory_search(
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
        try:
            import memory_handler
            raw = await memory_handler.search_memory(q, n_results=limit)
            if raw:
                results = [{"content": raw, "metadata": {}, "score": 1.0}]
        except Exception as e:
            logger.error("collab memory_search (all) failed: %s", e)
    elif scope == "shared":
        try:
            import agent_memory
            results = await agent_memory.search_shared(q, limit)
        except Exception as e:
            logger.error("collab memory_search (shared) failed: %s", e)

    safe_q = q[:60].replace("\n", "\\n").replace("\r", "\\r")
    logger.info(
        "Collab memory search by '%s': q=%s scope=%s results=%d",
        peer_name, safe_q, scope, len(results),
    )
    return {"results": results, "scope": scope or "none"}


@collab_router.post("/broadcast")
async def collab_broadcast(
    body: BroadcastRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: receive a broadcast message and append to feed."""
    peer_name, peer = peer_auth

    if not can(peer, "broadcast"):
        raise HTTPException(status_code=403, detail="Broadcast not permitted for your tier")

    summary = f"[broadcast from {body.from_name}] {body.message[:200]}"
    await append_event(bot="collab", action="broadcast", summary=summary, peer_name=peer_name)
    logger.info("Collab broadcast from peer '%s' (from_name=%s)", peer_name, body.from_name)
    return {"ok": True}


@collab_router.get("/feed")
async def collab_feed(
    limit: int = Query(20, ge=1, le=50),
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)] = None,
):
    """Peer-authenticated: return this instance's activity feed."""
    peer_name, peer = peer_auth

    if not can(peer, "feed_read"):
        raise HTTPException(status_code=403, detail="Feed access not permitted for your tier")

    events = await get_feed(limit=limit)
    return {"events": events, "instance_name": BRIDGENET_NODE_NAME}


@collab_router.post("/borrow/start")
async def collab_borrow_start(
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

    import config as main_config
    bot = body.bot if body.bot else main_config.CLI_RUNNER

    try:
        import server as _server_mod
        guest_instance = _server_mod.instances.create(
            f"Borrow:{peer_name}", owner_id=0, switch_active=False
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

    try:
        from telegram_handler import send_message as _tg_send
        import config as _cfg
        asyncio.create_task(_tg_send(
            _cfg.ALLOWED_USER_ID,
            f"Borrow session started: {peer_name} is now using your {bot} bot.",
        ))
    except Exception as e:
        logger.debug("Could not notify owner of borrow start: %s", e)

    label = f"{bot.title()} @ {BRIDGENET_NODE_NAME}"
    logger.info("Borrow started: peer=%s bot=%s session=%s", peer_name, bot, session.session_id)
    return {"session_id": session.session_id, "bot": bot, "label": label}


@collab_router.post("/borrow/message")
async def collab_borrow_message(
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
    logger.info(
        "Borrow message: peer=%s session=%s text=%s...",
        peer_name, body.session_id, body.text[:60],
    )
    return {"response": result, "bot": session.bot, "instance_name": BRIDGENET_NODE_NAME}


@collab_router.delete("/borrow/{session_id}")
async def collab_borrow_end(
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

    try:
        from telegram_handler import send_message as _tg_send
        import config as _cfg
        asyncio.create_task(_tg_send(
            _cfg.ALLOWED_USER_ID,
            f"{peer_name} has disconnected. Session lasted {int(duration / 60)} minutes.",
        ))
    except Exception as e:
        logger.debug("Could not notify owner of borrow end: %s", e)

    logger.info(
        "Borrow ended: peer=%s session=%s duration=%.0fs", peer_name, session_id, duration
    )
    return {"ok": True, "duration_seconds": int(duration)}


# ═════════════════════════════════════════════════════════════════════════════
# /bridgenet/* router — new BridgeNet endpoints
# ═════════════════════════════════════════════════════════════════════════════

router = APIRouter(prefix="/bridgenet", tags=["bridgenet"])


# ── GET /bridgenet/profile ────────────────────────────────────────────────────


@router.get("/profile")
async def bridgenet_get_profile():
    """Public profile endpoint — includes BridgeNet version and relay status."""
    import config as main_config
    from .relay_client import is_relay_online

    bots = [main_config.CLI_RUNNER]
    agents: list[str] = []
    try:
        from agent_registry import list_agents
        agents = [a.id for a in list_agents()]
    except Exception as e:
        logger.debug("Could not list agents for bridgenet profile: %s", e)

    return {
        "instance_name": BRIDGENET_NODE_NAME,
        "node_id": get_or_create_node_id(),
        "owner": getattr(main_config, "USER_NAME", None) or BRIDGENET_NODE_NAME,
        "bots": bots,
        "agents": agents,
        "version": "1.0.0",
        "bridgenet_version": BRIDGENET_VERSION,
        "relay_url": BRIDGENET_RELAY_URL or None,
        "relay_connected": is_relay_online(),
    }


# ── GET /bridgenet/peers ──────────────────────────────────────────────────────


@router.get("/peers")
async def bridgenet_list_peers(request: Request):
    """Owner-only: list all known peers with online status."""
    _require_owner_token(request)

    peers = load_peers()
    result = []
    for name, peer in peers.items():
        profile = None
        try:
            profile = await asyncio.wait_for(
                bridgenet_client.fetch_profile(peer), timeout=3.0
            )
        except Exception:
            profile = None

        online = profile is not None
        result.append({
            "name": name,
            "url": peer.get("url", ""),
            "tier": peer.get("tier", "acquaintance"),
            "online": online,
            "reputation": rep.get_reputation(name),
            "bots": profile.get("bots", peer.get("bots", [])) if profile else peer.get("bots", []),
            "agents": profile.get("agents", []) if profile else [],
        })

    return {"peers": result, "count": len(result)}


# ── POST /bridgenet/task ──────────────────────────────────────────────────────


@router.post("/task")
async def bridgenet_task(
    request: Request,
    auth: Annotated[tuple[str, dict], Depends(get_relay_or_peer)],
):
    """Execute a task from a relay-forwarded request or a direct peer call.

    Accepts either a relay HMAC signature (X-BridgeNet-Relay-Sig) or a
    peer token (X-BridgeNet-Token / X-Collab-Token).

    Runs the prompt sanitizer on the task content before execution.
    Injection violations are logged and stripped — they are NOT returned
    to the caller (so attackers don't learn which patterns are detected).
    """
    source, peer = auth

    # Parse body (already consumed by get_relay_or_peer for relay path,
    # but FastAPI caches body reads so this is safe to call again)
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {e}")

    task_req = BridgeNetTaskRequest(**body)

    # Permission check for direct peer calls
    if source != "relay":
        if not can(peer, "delegate"):
            raise HTTPException(
                status_code=403, detail="Task execution not permitted for your tier"
            )
        if task_req.agent_id and not check_agent_access(peer, task_req.agent_id):
            raise HTTPException(
                status_code=403,
                detail=f"Access to agent '{task_req.agent_id}' not permitted",
            )
        if task_req.bot and not check_bot_access(peer, task_req.bot):
            raise HTTPException(
                status_code=403,
                detail=f"Access to bot '{task_req.bot}' not permitted",
            )

    # Sanitize task content — strip injection attempts silently
    sanitized_content, violations = sanitize_task(task_req.content, task_type=task_req.task_type)
    if violations:
        peer_label = source if source == "relay" else source  # peer_name or "relay"
        logger.warning(
            "Task from '%s' contained %d violation(s): %s",
            peer_label, len(violations), violations,
        )
        # Record as a failure for reputation purposes (peer token path only)
        if source != "relay":
            rep.record_failure(source)

    start_ms = int(time.time() * 1000)
    result = await _run_task(sanitized_content, task_req.agent_id, task_req.context)
    duration_ms = int(time.time() * 1000) - start_ms

    # Record reputation and credits
    if source != "relay":
        rep.record_success(source)

    # Earn 1 credit for serving a relay-forwarded task
    if source == "relay":
        try:
            await credit_ledger.earn(1, f"served_relay_task:{task_req.task_id or 'unknown'}")
        except Exception as e:
            logger.debug("Credit earn failed: %s", e)

    # Append to BridgeNet feed
    await append_event(
        bot=task_req.bot or "bridgenet",
        action="bridgenet_task",
        summary=f"[{source}] {task_req.content[:120]}",
        peer_name=source if source != "relay" else None,
    )

    logger.info(
        "BridgeNet task from '%s': type=%s agent=%s (%dms)",
        source, task_req.task_type, task_req.agent_id, duration_ms,
    )

    return {
        "result": result,
        "task_id": task_req.task_id,
        "agent_id": task_req.agent_id,
        "duration_ms": duration_ms,
    }


# ── POST /bridgenet/result ────────────────────────────────────────────────────


@router.post("/result")
async def bridgenet_result(
    request: Request,
    body: BridgeNetResultRequest,
):
    """Receive a task result forwarded by the relay.

    Auth: relay HMAC signature only (no peer token accepted here).
    Stores the result in _pending_results and wakes any asyncio.Event
    waiter so callers polling for the result are unblocked.
    """
    # Verify relay signature
    body_bytes = await request.body()
    from .relay_client import verify_relay_signature
    if not verify_relay_signature(request.headers, body_bytes):
        raise HTTPException(status_code=403, detail="Invalid relay signature")

    task_id = body.task_id

    async with _pending_lock:
        if task_id not in _pending_results:
            event = asyncio.Event()
            _pending_results[task_id] = {
                "result": body.result,
                "source_node": body.source_node,
                "ts": time.time(),
                "event": event,
            }
            event.set()
        else:
            # Entry was pre-created by a waiter — fill result and signal
            _pending_results[task_id]["result"] = body.result
            _pending_results[task_id]["source_node"] = body.source_node
            _pending_results[task_id]["ts"] = time.time()
            _pending_results[task_id]["event"].set()

    logger.info(
        "Received relay result for task %s from node '%s'",
        task_id,
        body.source_node,
    )
    return {"ok": True, "task_id": task_id}


# ── GET /bridgenet/status ─────────────────────────────────────────────────────


@router.get("/status")
async def bridgenet_status(request: Request):
    """Owner-only: return this node's BridgeNet status.

    Includes relay connectivity, credit balance, peer reputation scores,
    and the count of currently online peers (queried from relay).
    """
    _require_owner_token(request)

    from .relay_client import is_relay_online, list_online_nodes

    balance = await credit_ledger.get_balance()
    all_reps = rep.get_all_reputations()

    # Ask relay how many nodes are online (best-effort)
    online_nodes: list[dict] = []
    try:
        online_nodes = await asyncio.wait_for(list_online_nodes(), timeout=5.0)
    except Exception:
        pass

    return {
        "bridgenet_version": BRIDGENET_VERSION,
        "node_id": get_or_create_node_id(),
        "node_name": BRIDGENET_NODE_NAME,
        "relay_url": BRIDGENET_RELAY_URL or None,
        "relay_connected": is_relay_online(),
        "online_peers_count": len(online_nodes),
        "credits": {
            "balance": balance,
            "history": await credit_ledger.get_history(limit=5),
        },
        "reputations": all_reps,
        "enabled": BRIDGENET_ENABLED,
    }


# ── POST /bridgenet/broadcast ─────────────────────────────────────────────────


@router.post("/broadcast")
async def bridgenet_broadcast(
    body: BroadcastRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: receive a broadcast message."""
    peer_name, peer = peer_auth

    if not can(peer, "broadcast"):
        raise HTTPException(status_code=403, detail="Broadcast not permitted for your tier")

    summary = f"[broadcast from {body.from_name}] {body.message[:200]}"
    await append_event(
        bot="bridgenet", action="broadcast", summary=summary, peer_name=peer_name
    )
    logger.info(
        "BridgeNet broadcast from peer '%s' (from_name=%s)", peer_name, body.from_name
    )
    return {"ok": True}


# ── GET /bridgenet/feed ───────────────────────────────────────────────────────


@router.get("/feed")
async def bridgenet_feed(
    limit: int = Query(20, ge=1, le=500),
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)] = None,
):
    """Peer-authenticated: return this node's BridgeNet activity feed."""
    peer_name, peer = peer_auth

    if not can(peer, "feed_read"):
        raise HTTPException(status_code=403, detail="Feed access not permitted for your tier")

    events = await get_feed(limit=limit)
    return {"events": events, "instance_name": BRIDGENET_NODE_NAME}


# ── POST /bridgenet/borrow/start ──────────────────────────────────────────────


@router.post("/borrow/start")
async def bridgenet_borrow_start(
    body: BorrowStartRequest,
    peer_auth: Annotated[tuple[str, dict], Depends(get_peer)],
):
    """Peer-authenticated: start a borrow session."""
    peer_name, peer = peer_auth

    if not can(peer, "borrow"):
        raise HTTPException(status_code=403, detail="Borrow not permitted for your tier")
    if body.bot and not check_bot_access(peer, body.bot):
        raise HTTPException(
            status_code=403,
            detail=f"Access to bot '{body.bot}' not permitted for your tier",
        )

    import config as main_config
    bot = body.bot if body.bot else main_config.CLI_RUNNER

    try:
        import server as _server_mod
        guest_instance = _server_mod.instances.create(
            f"Borrow:{peer_name}", owner_id=0, switch_active=False
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

    try:
        from telegram_handler import send_message as _tg_send
        import config as _cfg
        asyncio.create_task(_tg_send(
            _cfg.ALLOWED_USER_ID,
            f"BridgeNet borrow started: {peer_name} is using your {bot} bot.",
        ))
    except Exception as e:
        logger.debug("Could not notify owner of borrow start: %s", e)

    label = f"{bot.title()} @ {BRIDGENET_NODE_NAME}"
    logger.info(
        "BridgeNet borrow started: peer=%s bot=%s session=%s",
        peer_name, bot, session.session_id,
    )
    return {"session_id": session.session_id, "bot": bot, "label": label}


# ── POST /bridgenet/borrow/message ────────────────────────────────────────────


@router.post("/borrow/message")
async def bridgenet_borrow_message(
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
    logger.info(
        "BridgeNet borrow message: peer=%s session=%s text=%s...",
        peer_name, body.session_id, body.text[:60],
    )
    return {"response": result, "bot": session.bot, "instance_name": BRIDGENET_NODE_NAME}


# ── DELETE /bridgenet/borrow/{session_id} ─────────────────────────────────────


@router.delete("/borrow/{session_id}")
async def bridgenet_borrow_end(
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

    try:
        from telegram_handler import send_message as _tg_send
        import config as _cfg
        asyncio.create_task(_tg_send(
            _cfg.ALLOWED_USER_ID,
            f"{peer_name} has disconnected. Session lasted {int(duration / 60)} minutes.",
        ))
    except Exception as e:
        logger.debug("Could not notify owner of borrow end: %s", e)

    logger.info(
        "BridgeNet borrow ended: peer=%s session=%s duration=%.0fs",
        peer_name, session_id, duration,
    )
    return {"ok": True, "duration_seconds": int(duration)}
