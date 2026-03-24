"""BridgeNet peer registry and relay configuration.

Extends collab/config.py with relay support, node identity, and credits.

Peers are stored in TG_BRIDGE_DATA_DIR/collab_peers.json (same file as
collab/ — no migration needed, just reads the existing registry).

Each peer has a tier ("family" | "friend" | "acquaintance") and a shared
token that THEY send in X-Collab-Token / X-BridgeNet-Token to authenticate.
"""

import hmac
import json
import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger("bridge.bridgenet.config")

# ── Module-level settings ─────────────────────────────────────────────────────

# BridgeNet enabled — check BRIDGENET_ENABLED first, fall back to COLLAB_ENABLED
_bn_enabled_raw = os.environ.get("BRIDGENET_ENABLED", "").strip().lower()
_collab_enabled_raw = os.environ.get("COLLAB_ENABLED", "true").strip().lower()

BRIDGENET_ENABLED: bool = (
    _bn_enabled_raw in ("true", "1", "yes")
    if _bn_enabled_raw
    else _collab_enabled_raw in ("true", "1", "yes")
)

# Node name — check BRIDGENET_NODE_NAME first, fall back to COLLAB_INSTANCE_NAME
BRIDGENET_NODE_NAME: str = (
    os.environ.get("BRIDGENET_NODE_NAME", "")
    or os.environ.get("COLLAB_INSTANCE_NAME", "")
)

# Inbound auth token others send to reach us — BRIDGENET_TOKEN first, then COLLAB_TOKEN
BRIDGENET_TOKEN: str = (
    os.environ.get("BRIDGENET_TOKEN", "")
    or os.environ.get("COLLAB_TOKEN", "")
)

# Relay configuration
BRIDGENET_RELAY_URL: str = os.environ.get("BRIDGENET_RELAY_URL", "").rstrip("/")
BRIDGENET_RELAY_VERIFY_KEY: str = os.environ.get("BRIDGENET_RELAY_VERIFY_KEY", "")

# Data directory (shared with collab/)
_DATA_DIR = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))

# Peer file — same as collab/, no migration needed
_PEERS_FILE = os.path.join(_DATA_DIR, "collab_peers.json")

# BridgeNet identity and relay-token files
_IDENTITY_FILE = os.path.join(_DATA_DIR, "bridgenet_identity.json")
_RELAY_TOKEN_FILE = os.path.join(_DATA_DIR, "bridgenet_relay_token.json")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ensure_data_dir() -> None:
    Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)


# ── Peer registry (unchanged from collab/config.py) ──────────────────────────


def load_peers() -> dict:
    """Load peer registry from disk. Returns empty dict if file does not exist."""
    try:
        with open(_PEERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error("Failed to load bridgenet peers from %s: %s", _PEERS_FILE, e)
        return {}


def save_peers(peers: dict) -> None:
    """Save peer registry to disk."""
    _ensure_data_dir()
    try:
        with open(_PEERS_FILE, "w", encoding="utf-8") as f:
            json.dump(peers, f, indent=2)
    except Exception as e:
        logger.error("Failed to save bridgenet peers to %s: %s", _PEERS_FILE, e)


def get_peer_by_token(token: str) -> tuple[str, dict] | None:
    """Find a peer by the token they send. Returns (name, peer_dict) or None.

    Uses hmac.compare_digest and always iterates all peers to prevent
    timing side-channel attacks.
    """
    if not token:
        return None
    peers = load_peers()
    match_name: str | None = None
    match_peer: dict | None = None
    for name, peer in peers.items():
        peer_token = peer.get("token", "")
        if hmac.compare_digest(peer_token, token):
            # Don't break early — iterate all peers to avoid timing leaks
            match_name = name
            match_peer = peer
    if match_name is not None:
        return (match_name, match_peer)
    return None


def add_peer(
    name: str,
    url: str,
    tier: str,
    token: str,
    bots: list[str] | None = None,
    allowed_agents: list[str] | None = None,
    allowed_bots: list[str] | None = None,
) -> dict:
    """Add or update a peer. Returns the peer dict."""
    peers = load_peers()
    peer: dict = {
        "url": url,
        "tier": tier,
        "token": token,
        "bots": bots or [],
        "allowed_agents": allowed_agents or [],
        "allowed_bots": allowed_bots or [],
    }
    peers[name] = peer
    save_peers(peers)
    logger.info("Added/updated peer '%s' (tier=%s url=%s)", name, tier, url)
    return peer


def remove_peer(name: str) -> bool:
    """Remove a peer by name. Returns True if removed, False if not found."""
    peers = load_peers()
    if name not in peers:
        return False
    del peers[name]
    save_peers(peers)
    logger.info("Removed peer '%s'", name)
    return True


# ── Node identity ─────────────────────────────────────────────────────────────


def get_or_create_node_id() -> str:
    """Load this node's persistent UUID from disk, or generate and save one.

    Stored in ~/.bridgebot/bridgenet_identity.json as {node_id, registered_at}.
    """
    import time

    _ensure_data_dir()
    try:
        with open(_IDENTITY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        node_id = data.get("node_id", "")
        if node_id:
            return node_id
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Could not read bridgenet identity file: %s", e)

    # Generate a fresh node ID
    node_id = str(uuid.uuid4())
    try:
        with open(_IDENTITY_FILE, "w", encoding="utf-8") as f:
            json.dump({"node_id": node_id, "registered_at": time.time()}, f, indent=2)
        logger.info("Generated new BridgeNet node ID: %s", node_id)
    except Exception as e:
        logger.error("Could not save bridgenet identity file: %s", e)

    return node_id


# ── Relay token ───────────────────────────────────────────────────────────────


def get_relay_token() -> str | None:
    """Load the relay token received during registration.

    Returns the token string, or None if not yet registered.
    """
    try:
        with open(_RELAY_TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("relay_token") or None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("Could not read relay token file: %s", e)
        return None


def save_relay_token(token: str) -> None:
    """Persist the relay token returned by the relay on registration."""
    import time

    _ensure_data_dir()
    try:
        with open(_RELAY_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"relay_token": token, "saved_at": time.time()}, f, indent=2)
        logger.info("Relay token saved to %s", _RELAY_TOKEN_FILE)
    except Exception as e:
        logger.error("Could not save relay token: %s", e)
