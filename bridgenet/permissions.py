"""Tier-based permission matrix for collab peers.

Tiers:
  family      — full access: delegate, all agents, all bots, memory search, broadcast
  friend      — limited access: delegate + allowed agents/bots, shared memory, feed read
  acquaintance — read-only: feed only, no delegation, no memory, no broadcast
"""

import logging

logger = logging.getLogger("bridge.collab.permissions")

# ── Permission matrix ────────────────────────────────────────────────────────

TIER_PERMISSIONS: dict[str, dict] = {
    "family": {
        "delegate": True,
        "memory_search": "all",
        "agents": "all",
        "bots": "all",
        "feed_read": True,
        "broadcast": True,
        "borrow": True,
    },
    "friend": {
        "delegate": True,
        "memory_search": "shared",   # MEMORY_DIR/Shared/ only
        "agents": "allowed_list",
        "bots": "allowed_list",
        "feed_read": True,
        "broadcast": False,
        "borrow": True,
    },
    "acquaintance": {
        "delegate": False,
        "memory_search": False,
        "agents": False,
        "bots": False,
        "feed_read": True,
        "broadcast": False,
        "borrow": False,
    },
}


# ── Permission helpers ───────────────────────────────────────────────────────


def can(peer: dict, action: str) -> bool:
    """Check whether the peer's tier permits the given action.

    For boolean permissions, returns the bool value.
    For string permissions (e.g. "all" / "shared"), truthy strings count as True.
    Returns False for unknown tiers.
    """
    tier = peer.get("tier", "acquaintance")
    perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["acquaintance"])
    value = perms.get(action, False)
    if isinstance(value, bool):
        return value
    # String values like "all" or "shared" are truthy
    return bool(value)


def check_agent_access(peer: dict, agent_id: str) -> bool:
    """Check whether the peer may use the given agent.

    - family  → always allowed
    - friend  → allowed only if agent_id is in peer["allowed_agents"]
    - acquaintance → never allowed
    """
    tier = peer.get("tier", "acquaintance")
    if tier == "family":
        return True
    if tier == "friend":
        allowed = peer.get("allowed_agents", [])
        return agent_id in allowed
    return False


def check_bot_access(peer: dict, bot_name: str) -> bool:
    """Check whether the peer may target the given bot.

    - family  → always allowed
    - friend  → allowed only if bot_name is in peer["allowed_bots"]
    - acquaintance → never allowed
    """
    tier = peer.get("tier", "acquaintance")
    if tier == "family":
        return True
    if tier == "friend":
        allowed = peer.get("allowed_bots", [])
        return bot_name in allowed
    return False


def get_memory_scope(peer: dict) -> str | None:
    """Return the memory search scope for this peer.

    Returns "all", "shared", or None (no access).
    """
    tier = peer.get("tier", "acquaintance")
    perms = TIER_PERMISSIONS.get(tier, TIER_PERMISSIONS["acquaintance"])
    value = perms.get("memory_search", False)
    if not value:
        return None
    return str(value)  # "all" or "shared"


def can_borrow(peer: dict) -> bool:
    """Convenience: check whether the peer may start a borrow session."""
    return can(peer, "borrow")
