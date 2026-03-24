"""
Borrow session management for Collab.

Host side: tracks active borrow sessions from peers.
Borrower side: tracks which peer's bot this user is currently borrowing.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

logger = logging.getLogger("bridge.bridgenet.borrow")


@dataclass
class BorrowSession:
    """Host side: a peer is borrowing one of our bots."""
    session_id: str
    peer_name: str
    bot: str
    instance_id: int
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)


@dataclass
class BorrowInfo:
    """Borrower side: we are borrowing a peer's bot."""
    peer_name: str
    bot: str
    session_id: str
    started_at: float = field(default_factory=time.time)
    label: str = ""  # e.g. "Gemini @ Diony"


# Host side registry: session_id -> BorrowSession
_active_borrows: dict[str, BorrowSession] = {}
# Borrower side registry: chat_id -> BorrowInfo
_my_borrows: dict[int, BorrowInfo] = {}

BORROW_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
_timeout_lock = asyncio.Lock()


def create_session(peer_name: str, bot: str, instance_id: int) -> BorrowSession:
    session_id = str(uuid4())
    session = BorrowSession(
        session_id=session_id,
        peer_name=peer_name,
        bot=bot,
        instance_id=instance_id,
    )
    _active_borrows[session_id] = session
    logger.info(f"Borrow session created: {session_id} from {peer_name} using {bot}")
    return session


def get_session(session_id: str) -> Optional[BorrowSession]:
    return _active_borrows.get(session_id)


def touch_session(session_id: str) -> bool:
    session = _active_borrows.get(session_id)
    if session:
        session.last_activity = time.time()
        return True
    return False


def end_session(session_id: str) -> Optional[BorrowSession]:
    return _active_borrows.pop(session_id, None)


def list_sessions() -> list[BorrowSession]:
    return list(_active_borrows.values())


def is_borrowing(chat_id: int) -> Optional[BorrowInfo]:
    return _my_borrows.get(chat_id)


def start_borrow(chat_id: int, peer_name: str, session_id: str, bot: str, label: str) -> BorrowInfo:
    info = BorrowInfo(
        peer_name=peer_name,
        bot=bot,
        session_id=session_id,
        label=label,
    )
    _my_borrows[chat_id] = info
    return info


def end_borrow(chat_id: int) -> Optional[BorrowInfo]:
    return _my_borrows.pop(chat_id, None)


async def timeout_checker(instance_manager, notify_fn=None):
    """Background task: clean up idle borrow sessions every 5 minutes."""
    while True:
        await asyncio.sleep(300)  # check every 5 min
        async with _timeout_lock:
            now = time.time()
            expired = [
                sid for sid, s in _active_borrows.items()
                if now - s.last_activity > BORROW_TIMEOUT_SECONDS
            ]
            for sid in expired:
                session = end_session(sid)
                if session:
                    logger.info(f"Borrow session timed out: {sid} from {session.peer_name}")
                    # Clean up guest instance
                    try:
                        instance_manager.remove(session.instance_id)
                    except Exception:
                        pass
                    # Notify owner if notify_fn provided
                    if notify_fn:
                        await notify_fn(
                            f"Borrow session from {session.peer_name} timed out after 30 minutes of inactivity."
                        )
