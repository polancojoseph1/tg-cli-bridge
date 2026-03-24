"""Credit ledger for BridgeNet relay task routing.

Tracks this node's credit balance used to submit tasks through the relay.
Persisted to ~/.bridgebot/bridgenet_credits.json. Thread-safe via asyncio.Lock.

Ledger schema:
  {
    "balance": int,           # current balance
    "lifetime_earned": int,   # total ever earned
    "lifetime_spent": int,    # total ever spent
    "transactions": [         # rolling last-100 log
      {"type": "earn|spend", "amount": int, "reason": str, "ts": float}
    ]
  }
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("bridge.bridgenet.credits")

_DATA_DIR = os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot"))
_CREDITS_FILE = os.path.join(_DATA_DIR, "bridgenet_credits.json")
_TX_HISTORY_MAX = 100

_lock = asyncio.Lock()


# ── Disk helpers ──────────────────────────────────────────────────────────────


def _ensure_data_dir() -> None:
    Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)


def _load_sync() -> dict:
    """Load ledger from disk (sync — call only inside _lock)."""
    try:
        with open(_CREDITS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Ensure required keys with sane defaults
            data.setdefault("balance", 0)
            data.setdefault("lifetime_earned", 0)
            data.setdefault("lifetime_spent", 0)
            data.setdefault("transactions", [])
            return data
        return _empty_ledger()
    except FileNotFoundError:
        return _empty_ledger()
    except Exception as e:
        logger.error("Failed to load credits file %s: %s", _CREDITS_FILE, e)
        return _empty_ledger()


def _save_sync(ledger: dict) -> None:
    """Save ledger to disk (sync — call only inside _lock)."""
    _ensure_data_dir()
    try:
        with open(_CREDITS_FILE, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)
    except Exception as e:
        logger.error("Failed to save credits file %s: %s", _CREDITS_FILE, e)


def _empty_ledger() -> dict:
    return {
        "balance": 0,
        "lifetime_earned": 0,
        "lifetime_spent": 0,
        "transactions": [],
    }


def _append_tx(ledger: dict, tx_type: str, amount: int, reason: str) -> None:
    """Append a transaction record, trimming to the last _TX_HISTORY_MAX entries."""
    tx = {
        "type": tx_type,
        "amount": amount,
        "reason": reason,
        "ts": time.time(),
    }
    ledger["transactions"].append(tx)
    if len(ledger["transactions"]) > _TX_HISTORY_MAX:
        ledger["transactions"] = ledger["transactions"][-_TX_HISTORY_MAX:]


# ── Public API ────────────────────────────────────────────────────────────────


async def get_balance() -> int:
    """Return the current credit balance."""
    async with _lock:
        ledger = _load_sync()
    return int(ledger["balance"])


async def earn(amount: int, reason: str) -> int:
    """Credit the balance by amount. Returns new balance.

    Args:
        amount: Positive integer number of credits to add.
        reason: Human-readable label for the transaction log.
    """
    if amount <= 0:
        raise ValueError(f"earn() requires a positive amount, got {amount}")

    async with _lock:
        ledger = _load_sync()
        ledger["balance"] += amount
        ledger["lifetime_earned"] += amount
        _append_tx(ledger, "earn", amount, reason)
        _save_sync(ledger)
        new_balance = ledger["balance"]

    logger.info("Credits earned: +%d (%s) → balance=%d", amount, reason, new_balance)
    return new_balance


async def spend(amount: int, reason: str) -> int:
    """Deduct amount from the balance. Returns new balance.

    Raises:
        ValueError: if balance is insufficient.
    """
    if amount <= 0:
        raise ValueError(f"spend() requires a positive amount, got {amount}")

    async with _lock:
        ledger = _load_sync()
        if ledger["balance"] < amount:
            raise ValueError(
                f"Insufficient credits: balance={ledger['balance']} < requested={amount}"
            )
        ledger["balance"] -= amount
        ledger["lifetime_spent"] += amount
        _append_tx(ledger, "spend", amount, reason)
        _save_sync(ledger)
        new_balance = ledger["balance"]

    logger.info("Credits spent: -%d (%s) → balance=%d", amount, reason, new_balance)
    return new_balance


async def can_afford(amount: int) -> bool:
    """Return True if the current balance is >= amount."""
    balance = await get_balance()
    return balance >= amount


async def get_history(limit: int = 20) -> list[dict]:
    """Return the most recent transactions, newest last.

    Args:
        limit: Maximum number of transactions to return (capped at _TX_HISTORY_MAX).
    """
    limit = max(1, min(limit, _TX_HISTORY_MAX))
    async with _lock:
        ledger = _load_sync()
    txs = ledger.get("transactions", [])
    return txs[-limit:] if len(txs) > limit else list(txs)
