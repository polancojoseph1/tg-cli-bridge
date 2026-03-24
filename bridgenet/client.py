"""HTTP client for BridgeNet peer and relay calls.

Outbound peer calls always send X-Collab-Token: {peer["token"]} for backward
compatibility with old collab/ nodes, plus X-BridgeNet-Token for new ones.

URL resolution order:
  1. Try /bridgenet/<endpoint> (new BridgeNet nodes)
  2. Fall back to /collab/<endpoint> (old collab nodes)

New in BridgeNet:
  submit_task_via_relay() — routes through relay if configured
  get_online_nodes()      — queries relay for the current node list
"""

import logging

import httpx

from .config import BRIDGENET_RELAY_URL

logger = logging.getLogger("bridge.bridgenet.client")

_DELEGATE_TIMEOUT = 60.0       # tasks can take a while
_BORROW_MESSAGE_TIMEOUT = 120.0  # borrow messages can be slow
_DEFAULT_TIMEOUT = 10.0
_BORROW_TIMEOUT = 15.0


# ── Auth headers ──────────────────────────────────────────────────────────────


def _headers(peer: dict) -> dict[str, str]:
    """Build auth headers for an outbound request to this peer.

    Sends both header names so old collab/ nodes and new BridgeNet nodes
    both recognise the auth token.
    """
    token = peer.get("token", "")
    return {
        "X-Collab-Token": token,
        "X-BridgeNet-Token": token,
    }


# ── URL helpers ───────────────────────────────────────────────────────────────


async def _get_with_fallback(
    client: httpx.AsyncClient,
    peer: dict,
    path: str,
    **kwargs,
) -> httpx.Response:
    """GET /bridgenet/<path>, fall back to /collab/<path> on 404/connection error."""
    base = peer.get("url", "").rstrip("/")
    try:
        resp = await client.get(f"{base}/bridgenet/{path}", **kwargs)
        if resp.status_code != 404:
            return resp
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    return await client.get(f"{base}/collab/{path}", **kwargs)


async def _post_with_fallback(
    client: httpx.AsyncClient,
    peer: dict,
    path: str,
    **kwargs,
) -> httpx.Response:
    """POST /bridgenet/<path>, fall back to /collab/<path> on 404/connection error."""
    base = peer.get("url", "").rstrip("/")
    try:
        resp = await client.post(f"{base}/bridgenet/{path}", **kwargs)
        if resp.status_code != 404:
            return resp
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    return await client.post(f"{base}/collab/{path}", **kwargs)


async def _delete_with_fallback(
    client: httpx.AsyncClient,
    peer: dict,
    path: str,
    **kwargs,
) -> httpx.Response:
    """DELETE /bridgenet/<path>, fall back to /collab/<path>."""
    base = peer.get("url", "").rstrip("/")
    try:
        resp = await client.delete(f"{base}/bridgenet/{path}", **kwargs)
        if resp.status_code != 404:
            return resp
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
    return await client.delete(f"{base}/collab/{path}", **kwargs)


# ── Existing peer endpoints (unchanged signatures) ────────────────────────────


async def fetch_profile(peer: dict) -> dict | None:
    """GET /bridgenet/profile (falls back to /collab/profile) — public, no auth."""
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await _get_with_fallback(client, peer, "profile")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.debug("fetch_profile failed for %s: %s", peer.get("url"), e)
        return None


async def delegate_task(
    peer: dict,
    task: str,
    agent_id: str | None = None,
    bot: str | None = None,
    context: str = "",
) -> str:
    """POST /bridgenet/delegate (falls back to /collab/delegate).

    Returns the result string or an error message.
    """
    payload: dict = {"task": task, "context": context}
    if agent_id:
        payload["agent_id"] = agent_id
    if bot:
        payload["bot"] = bot

    try:
        async with httpx.AsyncClient(timeout=_DELEGATE_TIMEOUT) as client:
            resp = await _post_with_fallback(
                client, peer, "delegate", json=payload, headers=_headers(peer)
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("result", "")
    except httpx.HTTPStatusError as e:
        logger.error(
            "delegate_task HTTP %s from %s: %s",
            e.response.status_code,
            peer.get("url"),
            e,
        )
        return f"Error from peer: HTTP {e.response.status_code}"
    except Exception as e:
        logger.error("delegate_task failed for %s: %s", peer.get("url"), e)
        return f"Error reaching peer: {e}"


async def search_peer_memory(peer: dict, query: str) -> list[dict]:
    """GET /bridgenet/memory/search (falls back to /collab/memory/search)."""
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await _get_with_fallback(
                client, peer, "memory/search",
                params={"q": query},
                headers=_headers(peer),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
    except Exception as e:
        logger.debug("search_peer_memory failed for %s: %s", peer.get("url"), e)
        return []


async def fetch_peer_feed(peer: dict) -> list[dict]:
    """GET /bridgenet/feed (falls back to /collab/feed)."""
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await _get_with_fallback(
                client, peer, "feed", headers=_headers(peer)
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("events", [])
    except Exception as e:
        logger.debug("fetch_peer_feed failed for %s: %s", peer.get("url"), e)
        return []


async def broadcast_to_peer(peer: dict, message: str, from_name: str) -> bool:
    """POST /bridgenet/broadcast (falls back to /collab/broadcast)."""
    payload = {"message": message, "from_name": from_name}
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await _post_with_fallback(
                client, peer, "broadcast", json=payload, headers=_headers(peer)
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.debug("broadcast_to_peer failed for %s: %s", peer.get("url"), e)
        return False


async def borrow_start(peer: dict, bot: str | None = None) -> dict | None:
    """POST /bridgenet/borrow/start (falls back to /collab/borrow/start)."""
    payload: dict = {}
    if bot:
        payload["bot"] = bot
    try:
        async with httpx.AsyncClient(timeout=_BORROW_TIMEOUT) as client:
            resp = await _post_with_fallback(
                client, peer, "borrow/start", json=payload, headers=_headers(peer)
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(
            "borrow_start HTTP %s from %s: %s", e.response.status_code, peer.get("url"), e
        )
        return None
    except Exception as e:
        logger.error("borrow_start failed for %s: %s", peer.get("url"), e)
        return None


async def borrow_message(peer: dict, session_id: str, text: str) -> str:
    """POST /bridgenet/borrow/message (falls back to /collab/borrow/message)."""
    payload = {"session_id": session_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=_BORROW_MESSAGE_TIMEOUT) as client:
            resp = await _post_with_fallback(
                client, peer, "borrow/message", json=payload, headers=_headers(peer)
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", "")
    except httpx.HTTPStatusError as e:
        logger.error(
            "borrow_message HTTP %s from %s: %s", e.response.status_code, peer.get("url"), e
        )
        return f"Error from peer: HTTP {e.response.status_code}"
    except Exception as e:
        logger.error("borrow_message failed for %s: %s", peer.get("url"), e)
        return f"Error reaching peer: {e}"


async def borrow_end(peer: dict, session_id: str) -> bool:
    """DELETE /bridgenet/borrow/{session_id} (falls back to /collab/borrow/{session_id})."""
    try:
        async with httpx.AsyncClient(timeout=_BORROW_TIMEOUT) as client:
            resp = await _delete_with_fallback(
                client, peer, f"borrow/{session_id}", headers=_headers(peer)
            )
            resp.raise_for_status()
            return True
    except httpx.HTTPStatusError as e:
        logger.error(
            "borrow_end HTTP %s from %s: %s", e.response.status_code, peer.get("url"), e
        )
        return False
    except Exception as e:
        logger.error("borrow_end failed for %s: %s", peer.get("url"), e)
        return False


# ── New BridgeNet relay functions ─────────────────────────────────────────────


async def submit_task_via_relay(
    task: str,
    capability: str,
    task_type: str = "chat",
    context: str = "",
) -> str:
    """Route a task through the relay if configured, otherwise direct peer call.

    The relay selects the best available node with the required capability.
    If no relay is configured or the relay fails, falls back to a direct call
    to the first peer that advertises the required capability.

    Args:
        task:       The task text to execute.
        capability: Required capability string (e.g. "chat", "code").
        task_type:  Task type label for logging and relay routing.
        context:    Optional context string prepended to the task.

    Returns:
        Result string from the executing node, or an error message.
    """
    import uuid as _uuid
    from . import relay_client

    task_id = str(_uuid.uuid4())

    if BRIDGENET_RELAY_URL:
        content = f"Context: {context}\n\n{task}" if context else task
        result = await relay_client.submit_task_via_relay(
            task_id=task_id,
            task_type=task_type,
            content=content,
            required_capability=capability,
        )
        if result and result.get("ok"):
            logger.info(
                "Task %s relayed to %s", task_id, result.get("routed_to", "unknown")
            )
            # Result will arrive asynchronously via POST /bridgenet/result
            # Return a placeholder — callers should poll or await the result event
            return f"[Task {task_id} relayed to {result.get('routed_to', 'a peer')}]"

    # Relay not configured or failed — fall back to direct peer call
    from .config import load_peers
    peers = load_peers()
    for name, peer in peers.items():
        peer_caps = peer.get("capabilities", [])
        if capability in peer_caps or not peer_caps:  # no caps = try anyway
            logger.info(
                "submit_task_via_relay: relay unavailable, direct call to peer '%s'", name
            )
            return await delegate_task(peer, task, context=context)

    return "Error: no relay configured and no capable peer found"


async def get_online_nodes() -> list[dict]:
    """Ask the relay for the list of currently online nodes.

    Returns a list of node dicts, or [] if relay is not configured or unreachable.
    """
    from . import relay_client
    return await relay_client.list_online_nodes()
