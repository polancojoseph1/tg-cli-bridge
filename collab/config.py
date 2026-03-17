"""Collab peer registry — load/save/manage peer definitions.

Peers are stored in TG_BRIDGE_DATA_DIR/collab_peers.json.
Each peer has a tier ("family" | "friend" | "acquaintance") and a
shared token that THEY send in X-Collab-Token to authenticate as that peer.
"""

import hmac
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("bridge.collab.config")

# ── Module-level settings ────────────────────────────────────────────────────

COLLAB_ENABLED: bool = os.environ.get("COLLAB_ENABLED", "true").lower() in ("true", "1", "yes")
COLLAB_INSTANCE_NAME: str = os.environ.get("COLLAB_INSTANCE_NAME", "")
COLLAB_TOKEN: str = os.environ.get("COLLAB_TOKEN", "")  # inbound auth token others send to reach us

# Peer file location
_DATA_DIR = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))
_PEERS_FILE = os.path.join(_DATA_DIR, "collab_peers.json")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ensure_data_dir() -> None:
    Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)


def load_peers() -> dict:
    """Load peer registry from disk. Returns empty dict if file does not exist."""
    try:
        with open(_PEERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error("Failed to load collab peers from %s: %s", _PEERS_FILE, e)
        return {}


def save_peers(peers: dict) -> None:
    """Save peer registry to disk."""
    _ensure_data_dir()
    try:
        with open(_PEERS_FILE, "w", encoding="utf-8") as f:
            json.dump(peers, f, indent=2)
    except Exception as e:
        logger.error("Failed to save collab peers to %s: %s", _PEERS_FILE, e)


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
