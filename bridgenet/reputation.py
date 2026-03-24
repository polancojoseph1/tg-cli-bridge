"""Local reputation tracking per BridgeNet peer.

Scores are floats in [0.0, 1.0]. Unknown peers start at 0.5 (neutral).
Scores are persisted to ~/.bridgebot/bridgenet_reputation.json after every
update so they survive process restarts.

Score adjustments:
  record_success(peer)          +0.02  (max 1.0)
  record_failure(peer)          -0.05  (min 0.0)
  record_user_feedback positive +0.03
  record_user_feedback negative -0.08
  decay_all()                   ×0.995 per call (intended for daily cron)
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("bridge.bridgenet.reputation")

_DEFAULT_SCORE = 0.5
_DATA_DIR = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))
_REP_FILE = os.path.join(_DATA_DIR, "bridgenet_reputation.json")


# ── Disk helpers ──────────────────────────────────────────────────────────────


def _ensure_data_dir() -> None:
    Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)


def _load() -> dict[str, float]:
    """Load reputation dict from disk. Returns {} on missing/corrupt file."""
    try:
        with open(_REP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Coerce all values to float, drop non-numeric entries
            return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}
        return {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error("Failed to load reputation file %s: %s", _REP_FILE, e)
        return {}


def _save(scores: dict[str, float]) -> None:
    """Persist reputation dict to disk."""
    _ensure_data_dir()
    try:
        with open(_REP_FILE, "w", encoding="utf-8") as f:
            json.dump(scores, f, indent=2)
    except Exception as e:
        logger.error("Failed to save reputation file %s: %s", _REP_FILE, e)


def _clamp(value: float) -> float:
    """Clamp a score to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))


def _update(peer_name: str, delta: float) -> float:
    """Apply a delta to a peer's score, clamp, persist, and return new score."""
    scores = _load()
    current = scores.get(peer_name, _DEFAULT_SCORE)
    new_score = _clamp(current + delta)
    scores[peer_name] = new_score
    _save(scores)
    logger.debug(
        "Reputation update for '%s': %.3f → %.3f (delta=%+.3f)",
        peer_name, current, new_score, delta,
    )
    return new_score


# ── Public API ────────────────────────────────────────────────────────────────


def get_reputation(peer_name: str) -> float:
    """Return the reputation score for peer_name.

    Returns _DEFAULT_SCORE (0.5) for unknown peers.
    """
    scores = _load()
    return scores.get(peer_name, _DEFAULT_SCORE)


def record_success(peer_name: str) -> float:
    """Record a successful task execution. Returns updated score."""
    return _update(peer_name, +0.02)


def record_failure(peer_name: str) -> float:
    """Record a failed or errored task execution. Returns updated score."""
    return _update(peer_name, -0.05)


def record_user_feedback(peer_name: str, positive: bool) -> float:
    """Record explicit user feedback for a peer interaction.

    positive=True  → +0.03
    positive=False → -0.08

    Returns updated score.
    """
    delta = +0.03 if positive else -0.08
    return _update(peer_name, delta)


def get_all_reputations() -> dict[str, float]:
    """Return a copy of all stored reputation scores."""
    return dict(_load())


def decay_all() -> dict[str, float]:
    """Apply 0.5% daily decay to every stored score (multiply by 0.995).

    Intended to be called once per day (e.g. from a cron job or startup task).
    Scores naturally drift toward 0 without continued positive interactions,
    which prevents stale high scores from persisting indefinitely.

    Returns the updated scores dict.
    """
    scores = _load()
    if not scores:
        return scores

    updated = {name: _clamp(score * 0.995) for name, score in scores.items()}
    _save(updated)
    logger.info("Reputation decay applied to %d peer(s)", len(updated))
    return updated
