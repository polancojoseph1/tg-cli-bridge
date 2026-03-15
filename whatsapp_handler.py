"""WhatsApp transport adapter — relays sends through the local Baileys bridge.

Mirrors the telegram_handler.py public API so transport.py can swap them
transparently. Functions with no WhatsApp equivalent are safe no-ops.

The Baileys Node.js bridge (whatsapp-bridge/) runs on WA_BRIDGE_URL:
  POST /send          { jid, message }
  POST /send-image    { jid, path, caption }
  POST /send-audio    { jid, path }
  GET  /status
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import httpx

logger = logging.getLogger("bridge.whatsapp")

WA_BRIDGE_URL: str = os.environ.get("WA_BRIDGE_URL", "http://127.0.0.1:3001")

_client: httpx.AsyncClient | None = None

# Stable int → WA JID mapping so all of server.py can use int chat_ids
_jid_map: dict[int, str] = {}


def jid_to_int(jid: str) -> int:
    """Stable mapping from WhatsApp JID to a positive int (used as chat_id / user_id)."""
    return int(hashlib.md5(jid.encode()).hexdigest(), 16) % (2 ** 31 - 1)


def register_jid(chat_id: int, jid: str) -> None:
    _jid_map[chat_id] = jid


def lookup_jid(chat_id: int) -> str | None:
    return _jid_map.get(chat_id)


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


# ── Send functions (mirroring telegram_handler signatures) ───────────────────

async def send_message(
    chat_id: int,
    text: str,
    format_markdown: bool = False,
    parse_mode: str | None = None,
) -> int | None:
    """Send a text message to a WhatsApp JID. Strips HTML if format_markdown was requested."""
    jid = _jid_map.get(chat_id)
    if not jid:
        logger.warning("[wa] send_message: no JID registered for chat_id=%d", chat_id)
        return None

    if format_markdown or parse_mode == "HTML":
        # Strip HTML tags for WhatsApp — WA uses *bold*, _italic_ but we just send plain text
        import re
        text = re.sub(r"<[^>]+>", "", text)

    if not text.strip():
        return None

    # WhatsApp has no hard message length limit but stay practical
    MAX_CHUNK = 4000
    parts = [text[i:i + MAX_CHUNK] for i in range(0, len(text), MAX_CHUNK)]

    client = await _get_client()
    for part in parts:
        try:
            resp = await client.post(f"{WA_BRIDGE_URL}/send", json={"jid": jid, "message": part})
            if resp.status_code != 200:
                logger.error("[wa] send error %d: %s", resp.status_code, resp.text[:200])
                return None
        except httpx.HTTPError as exc:
            logger.error("[wa] send HTTP error: %s", exc)
            return None

    return 1  # Truthy message ID (WA IDs are strings; we return int 1 as a sentinel)


async def delete_message(chat_id: int, message_id: int) -> bool:
    """No-op — WhatsApp doesn't support deleting others' messages."""
    return True


async def send_chat_action(chat_id: int, action: str) -> None:
    """No-op — could send WA typing presence but not needed for basic operation."""
    pass


async def send_voice(chat_id: int, ogg_path: str, caption: str | None = None) -> bool:
    """Send a voice note (OGG/Opus) as a WhatsApp PTT message."""
    jid = _jid_map.get(chat_id)
    if not jid:
        return False
    client = await _get_client()
    try:
        resp = await client.post(f"{WA_BRIDGE_URL}/send-audio", json={"jid": jid, "path": ogg_path})
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.error("[wa] send_voice error: %s", exc)
        return False


async def send_photo(chat_id: int, photo_path: str, caption: str | None = None) -> bool:
    """Send an image file as a WhatsApp photo."""
    jid = _jid_map.get(chat_id)
    if not jid:
        return False
    client = await _get_client()
    try:
        resp = await client.post(
            f"{WA_BRIDGE_URL}/send-image",
            json={"jid": jid, "path": photo_path, "caption": caption or ""},
        )
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.error("[wa] send_photo error: %s", exc)
        return False


async def send_video(chat_id: int, video_path: str, caption: str | None = None) -> bool:
    """WhatsApp video requires re-encoding; just send a text notice for now."""
    msg = f"📹 (video generated)" + (f"\n{caption}" if caption else "")
    result = await send_message(chat_id, msg)
    return result is not None


async def download_photo(file_id: str) -> str:
    """For WA transport, the bridge already saves photos to /tmp.
    If file_id is a local path, return it directly; otherwise raise.
    """
    if os.path.exists(file_id):
        return file_id
    raise NotImplementedError(f"WA photo download: expected local path, got: {file_id!r}")


async def download_document(file_id: str, dest_path: str) -> str:
    """WA documents forwarded as local paths by the bridge."""
    if os.path.exists(file_id):
        import shutil
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(file_id, dest_path)
        return dest_path
    raise NotImplementedError(f"WA document download: expected local path, got: {file_id!r}")


# ── Telegram-specific stubs (no-ops for WA) ──────────────────────────────────

async def register_webhook(url: str) -> bool:
    logger.info("[wa] register_webhook: no-op (WA transport handled by Baileys bridge)")
    return True


async def delete_webhook() -> bool:
    return True


async def get_updates(offset: int = 0, timeout: int = 30) -> list[dict]:
    """WA is webhook-only (Node.js bridge POSTs to us). Sleep to throttle the
    polling loop that server.py starts when WEBHOOK_URL is unset."""
    await asyncio.sleep(float(timeout))
    return []


async def register_bot_commands(commands: list) -> bool:
    return True  # No-op — WA has no bot command menus


async def send_inline_keyboard(
    chat_id: int, text: str, buttons: list[list[dict]]
) -> int | None:
    """WA has no inline keyboards — flatten buttons into a numbered text menu."""
    lines = [text, ""]
    n = 1
    for row in buttons:
        for btn in row:
            label = btn.get("text", "")
            lines.append(f"{n}. {label}")
            n += 1
    return await send_message(chat_id, "\n".join(lines))


async def answer_callback_query(
    callback_query_id: str, text: str = "", show_alert: bool = False
) -> None:
    pass  # No-op
