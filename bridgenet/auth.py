"""FastAPI dependencies for BridgeNet inbound authentication.

Two auth modes are supported:

1. Peer token auth (backward compatible with collab/):
   - Header: X-Collab-Token  (old clients)
   - Header: X-BridgeNet-Token  (new clients)
   Both headers are checked; the first non-empty one wins.

2. Relay-forwarded request auth:
   - The relay signs the raw request body with HMAC-SHA256 using
     BRIDGENET_RELAY_VERIFY_KEY and sends the hex digest in
     X-BridgeNet-Relay-Sig.

Dependencies:
  get_peer(request)           → (peer_name, peer_dict)   — peer token only
  get_relay_or_peer(request)  → (source, info)            — relay sig OR peer token
"""

import logging

from fastapi import HTTPException, Request

from .config import get_peer_by_token
from .relay_client import verify_relay_signature

logger = logging.getLogger("bridge.bridgenet.auth")


# ── Peer token authentication ─────────────────────────────────────────────────


async def get_peer(request: Request) -> tuple[str, dict]:
    """FastAPI dependency: authenticate an inbound peer request.

    Accepts X-BridgeNet-Token (new) or X-Collab-Token (backward compat).
    Returns (peer_name, peer_dict).

    Raises:
        HTTPException 401 — no token provided.
        HTTPException 403 — token not recognised.
    """
    # Check BridgeNet header first, then fall back to collab for backward compat
    token = (
        request.headers.get("X-BridgeNet-Token", "").strip()
        or request.headers.get("X-Collab-Token", "").strip()
    )

    if not token:
        logger.warning("BridgeNet request from %s with no peer token", request.client)
        raise HTTPException(
            status_code=401,
            detail="X-BridgeNet-Token (or X-Collab-Token) header required",
        )

    result = get_peer_by_token(token)
    if result is None:
        logger.warning(
            "BridgeNet request with unknown peer token from %s", request.client
        )
        raise HTTPException(status_code=403, detail="Unknown peer token")

    peer_name, peer_dict = result
    logger.debug("Authenticated peer '%s' from %s", peer_name, request.client)
    return (peer_name, peer_dict)


# ── Relay-forwarded request auth ──────────────────────────────────────────────


async def verify_relay_forward(request: Request, body_bytes: bytes) -> bool:
    """Return True if the request carries a valid relay HMAC signature.

    Checks X-BridgeNet-Relay-Sig against BRIDGENET_RELAY_VERIFY_KEY.
    Does NOT raise — callers decide what to do with False.
    """
    return verify_relay_signature(request.headers, body_bytes)


# ── Combined relay-or-peer dependency ────────────────────────────────────────


async def get_relay_or_peer(request: Request) -> tuple[str, dict]:
    """FastAPI dependency: accept a relay-signed request OR a peer token.

    Tries relay HMAC first (cheaper for relay-forwarded traffic), then
    falls back to peer token lookup.

    Returns:
        ("relay", {})           — if relay signature is valid
        (peer_name, peer_dict)  — if a known peer token is present

    Raises:
        HTTPException 401 — no credentials at all.
        HTTPException 403 — credentials present but not valid.
    """
    # Check whether a relay signature header is present
    relay_sig = (
        request.headers.get("X-BridgeNet-Relay-Sig", "").strip()
    )

    if relay_sig:
        # Relay path — read and verify body
        try:
            body_bytes = await request.body()
        except Exception as e:
            logger.warning("get_relay_or_peer: could not read body for relay verify: %s", e)
            body_bytes = b""

        if verify_relay_signature(request.headers, body_bytes):
            logger.debug(
                "get_relay_or_peer: relay-forwarded request verified from %s",
                request.client,
            )
            return ("relay", {})
        else:
            # Signature header present but invalid — hard reject, no fallback
            logger.warning(
                "get_relay_or_peer: invalid relay signature from %s", request.client
            )
            raise HTTPException(status_code=403, detail="Invalid relay signature")

    # No relay sig — try peer token
    token = (
        request.headers.get("X-BridgeNet-Token", "").strip()
        or request.headers.get("X-Collab-Token", "").strip()
    )

    if not token:
        logger.warning(
            "get_relay_or_peer: no credentials from %s", request.client
        )
        raise HTTPException(
            status_code=401,
            detail="Authentication required: X-BridgeNet-Relay-Sig or X-BridgeNet-Token",
        )

    result = get_peer_by_token(token)
    if result is None:
        logger.warning(
            "get_relay_or_peer: unknown peer token from %s", request.client
        )
        raise HTTPException(status_code=403, detail="Unknown peer token")

    peer_name, peer_dict = result
    logger.debug(
        "get_relay_or_peer: peer token auth for '%s' from %s",
        peer_name,
        request.client,
    )
    return (peer_name, peer_dict)
