"""BridgeNet relay client — registration, heartbeat, and task submission.

Every authenticated request to the relay is signed with an HMAC-SHA256
signature so the relay can verify this node's identity without exposing
the relay_token in plain-text query parameters.

Signature headers (sent on every signed request):
  X-BridgeNet-Node:      {node_id}
  X-BridgeNet-Timestamp: {unix seconds (int)}
  X-BridgeNet-Nonce:     {16-byte hex}
  X-BridgeNet-Signature: HMAC-SHA256(relay_token, "{ts}:{nonce}:{sha256(body)}")

Relay-forwarded tasks carry:
  X-BridgeNet-Relay-Sig: HMAC-SHA256(BRIDGENET_RELAY_VERIFY_KEY, body)
"""

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time

import httpx

from .config import (
    BRIDGENET_RELAY_URL,
    BRIDGENET_RELAY_VERIFY_KEY,
    get_or_create_node_id,
    get_relay_token,
    save_relay_token,
)

logger = logging.getLogger("bridge.bridgenet.relay_client")

# How long to wait for relay HTTP calls (seconds)
_DEFAULT_TIMEOUT = 10.0
_TASK_TIMEOUT = 60.0

# Heartbeat interval and tolerated consecutive failures before marking offline
_HEARTBEAT_INTERVAL = 30  # seconds
_MAX_HEARTBEAT_FAILURES = 3

# Module-level state
_relay_online: bool = False
_consecutive_failures: int = 0


# ── Request signing ───────────────────────────────────────────────────────────


def _sign_request(relay_token: str, body_bytes: bytes) -> dict[str, str]:
    """Build the four BridgeNet authentication headers for a relay request.

    Args:
        relay_token: The token returned by the relay at registration time.
        body_bytes:  The raw request body bytes (b"" for GET requests).

    Returns:
        Dict with keys X-BridgeNet-Node, X-BridgeNet-Timestamp,
        X-BridgeNet-Nonce, X-BridgeNet-Signature.
    """
    node_id = get_or_create_node_id()
    timestamp = int(time.time())
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(body_bytes).hexdigest()

    message = f"{timestamp}:{nonce}:{body_hash}"
    signature = hmac.new(
        relay_token.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return {
        "X-BridgeNet-Node": node_id,
        "X-BridgeNet-Timestamp": str(timestamp),
        "X-BridgeNet-Nonce": nonce,
        "X-BridgeNet-Signature": signature,
    }


# ── Relay-forwarded task verification ────────────────────────────────────────


def verify_relay_signature(request_headers: dict, body_bytes: bytes) -> bool:
    """Verify that a relay-forwarded request carries a valid relay signature.

    The relay signs forwarded tasks with BRIDGENET_RELAY_VERIFY_KEY using
    HMAC-SHA256 over the raw body bytes.  The signature is sent in the
    X-BridgeNet-Relay-Sig header.

    Returns True if the signature matches, False otherwise (including when
    BRIDGENET_RELAY_VERIFY_KEY is not configured — fail-closed).
    """
    if not BRIDGENET_RELAY_VERIFY_KEY:
        logger.warning(
            "verify_relay_signature: BRIDGENET_RELAY_VERIFY_KEY not configured — rejecting"
        )
        return False

    provided_sig = ""
    # Accept both dict-like headers (FastAPI Request.headers) and plain dicts
    for header_name in ("X-BridgeNet-Relay-Sig", "x-bridgenet-relay-sig"):
        if hasattr(request_headers, "get"):
            provided_sig = request_headers.get(header_name, "")
            if provided_sig:
                break

    if not provided_sig:
        logger.warning("verify_relay_signature: missing X-BridgeNet-Relay-Sig header")
        return False

    expected_sig = hmac.new(
        BRIDGENET_RELAY_VERIFY_KEY.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        logger.warning("verify_relay_signature: signature mismatch — possible forgery")
        return False

    return True


# ── Registration ──────────────────────────────────────────────────────────────


async def register_with_relay(
    node_name: str,
    url: str,
    capabilities: list[str],
) -> bool:
    """Register this node with the relay. Saves returned relay_token on success.

    Args:
        node_name:    Human-readable node name (e.g. "jefe-claude").
        url:          The public URL peers can reach this node at.
        capabilities: List of capability strings (e.g. ["chat", "code"]).

    Returns:
        True if registration succeeded and relay_token was saved.
    """
    if not BRIDGENET_RELAY_URL:
        logger.error("register_with_relay: BRIDGENET_RELAY_URL not configured")
        return False

    admin_token = os.environ.get("BRIDGENET_RELAY_ADMIN_TOKEN", "")
    if not admin_token:
        logger.error("register_with_relay: BRIDGENET_RELAY_ADMIN_TOKEN not set")
        return False

    node_id = get_or_create_node_id()
    payload = {
        "node_id": node_id,
        "node_name": node_name,
        "url": url,
        "capabilities": capabilities,
    }

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{BRIDGENET_RELAY_URL}/register",
                json=payload,
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(
            "register_with_relay: HTTP %d from relay: %s",
            e.response.status_code,
            e.response.text[:200],
        )
        return False
    except Exception as e:
        logger.error("register_with_relay: request failed: %s", e)
        return False

    relay_token = data.get("relay_token", "")
    if not relay_token:
        logger.error("register_with_relay: relay returned no relay_token")
        return False

    save_relay_token(relay_token)
    logger.info("BridgeNet registration successful. node_id=%s", node_id)
    return True


# ── Heartbeat ─────────────────────────────────────────────────────────────────


async def heartbeat() -> bool:
    """Send a signed heartbeat to the relay.

    Returns True if the relay acknowledged the heartbeat.
    """
    global _relay_online, _consecutive_failures

    if not BRIDGENET_RELAY_URL:
        return False

    relay_token = get_relay_token()
    if not relay_token:
        logger.debug("heartbeat: no relay_token — not registered yet")
        return False

    body_bytes = b""
    headers = _sign_request(relay_token, body_bytes)

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{BRIDGENET_RELAY_URL}/heartbeat",
                content=body_bytes,
                headers=headers,
            )
            resp.raise_for_status()

        _relay_online = True
        _consecutive_failures = 0
        logger.debug("Heartbeat acknowledged by relay")
        return True

    except Exception as e:
        _consecutive_failures += 1
        logger.warning(
            "Heartbeat failed (%d/%d): %s",
            _consecutive_failures,
            _MAX_HEARTBEAT_FAILURES,
            e,
        )
        if _consecutive_failures >= _MAX_HEARTBEAT_FAILURES:
            _relay_online = False
            logger.warning("Relay marked offline after %d consecutive failures", _consecutive_failures)
        return False


# ── Node discovery ────────────────────────────────────────────────────────────


async def list_online_nodes() -> list[dict]:
    """Fetch the list of currently online nodes from the relay.

    Returns a list of node dicts, or [] on failure.
    """
    if not BRIDGENET_RELAY_URL:
        return []

    relay_token = get_relay_token()
    if not relay_token:
        return []

    body_bytes = b""
    headers = _sign_request(relay_token, body_bytes)

    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                f"{BRIDGENET_RELAY_URL}/nodes",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("nodes", [])
    except Exception as e:
        logger.debug("list_online_nodes failed: %s", e)
        return []


# ── Task submission ───────────────────────────────────────────────────────────


async def submit_task_via_relay(
    task_id: str,
    task_type: str,
    content: str,
    required_capability: str,
    priority: str = "normal",
) -> dict | None:
    """Submit a task through the relay for routing to another node.

    The relay selects the best available node with the required capability
    and forwards the task. This call deducts credits from this node's balance.

    Args:
        task_id:             Caller-supplied unique task identifier.
        task_type:           Task category (e.g. "chat", "code").
        content:             Task payload text.
        required_capability: Capability string the target node must advertise.
        priority:            "low", "normal", or "high".

    Returns:
        Dict with keys {ok, routed_to, task_id} on success, or None on failure.
    """
    from . import credits as credit_ledger

    if not BRIDGENET_RELAY_URL:
        logger.debug("submit_task_via_relay: BRIDGENET_RELAY_URL not configured")
        return None

    relay_token = get_relay_token()
    if not relay_token:
        logger.debug("submit_task_via_relay: not registered with relay")
        return None

    # Check credit balance before sending
    cost = 1  # 1 credit per relayed task (relay may override)
    if not await credit_ledger.can_afford(cost):
        logger.warning(
            "submit_task_via_relay: insufficient credits (balance=%d, cost=%d)",
            await credit_ledger.get_balance(),
            cost,
        )
        return None

    import json as _json

    payload = {
        "task_id": task_id,
        "task_type": task_type,
        "content": content,
        "required_capability": required_capability,
        "priority": priority,
    }
    body_bytes = _json.dumps(payload).encode("utf-8")
    auth_headers = _sign_request(relay_token, body_bytes)
    auth_headers["Content-Type"] = "application/json"

    try:
        async with httpx.AsyncClient(timeout=_TASK_TIMEOUT) as client:
            resp = await client.post(
                f"{BRIDGENET_RELAY_URL}/task",
                content=body_bytes,
                headers=auth_headers,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(
            "submit_task_via_relay: HTTP %d — %s",
            e.response.status_code,
            e.response.text[:200],
        )
        return None
    except Exception as e:
        logger.error("submit_task_via_relay: request failed: %s", e)
        return None

    # Deduct credit only on confirmed success
    actual_cost = data.get("credits_charged", cost)
    try:
        await credit_ledger.spend(actual_cost, f"relay_task:{task_id}")
    except ValueError:
        logger.warning(
            "submit_task_via_relay: credit deduction failed for task %s (balance may be stale)",
            task_id,
        )

    logger.info(
        "Task %s submitted via relay → routed_to=%s (cost=%d)",
        task_id,
        data.get("routed_to", "unknown"),
        actual_cost,
    )
    return {
        "ok": data.get("ok", True),
        "routed_to": data.get("routed_to", ""),
        "task_id": task_id,
    }


# ── Heartbeat background loop ─────────────────────────────────────────────────


async def start_heartbeat_loop() -> None:
    """Async background task: send heartbeat to relay every 30 seconds.

    Marks _relay_online = False after _MAX_HEARTBEAT_FAILURES consecutive
    failures. Runs indefinitely — launch with asyncio.create_task().
    """
    logger.info(
        "BridgeNet heartbeat loop started (interval=%ds)", _HEARTBEAT_INTERVAL
    )
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL)
        try:
            await heartbeat()
        except Exception as e:
            # Belt-and-suspenders — heartbeat() already handles its own errors
            logger.error("Unexpected error in heartbeat loop: %s", e)


def is_relay_online() -> bool:
    """Return the cached relay connectivity status (updated by heartbeat loop)."""
    return _relay_online
