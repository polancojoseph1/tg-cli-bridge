import html
import logging
import os
import re
import tempfile
import httpx

from config import TELEGRAM_API, TELEGRAM_MAX_MESSAGE_LENGTH

logger = logging.getLogger("bridge.telegram")

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def markdown_to_telegram_html(text: str) -> str:
    """Convert GitHub-flavored markdown to Telegram-compatible HTML."""
    # Escape HTML entities first so Claude's output can't inject tags
    text = html.escape(text)

    # Code blocks (```lang\n...\n```) → <pre>...</pre>
    text = re.sub(r"```\w*\n(.*?)```", r"<pre>\1</pre>", text, flags=re.DOTALL)

    # Inline code (`...`) → <code>...</code>, but URLs → <a href>
    def _inline_code(m: re.Match) -> str:
        inner = m.group(1).strip()
        # html.escape was already applied, so check for http/https in escaped form
        if re.match(r"https?://", inner):
            return f'<a href="{inner}">{inner}</a>'
        return f"<code>{inner}</code>"
    text = re.sub(r"`([^`]+)`", _inline_code, text)

    # Bare URLs (not already inside an href) → <a href>
    text = re.sub(r'(?<!href=")(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', text)

    # Headers (## ...) → bold line
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Bold (**...**) → <b>...</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Italic (*...*) → <i>...</i>
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)

    # Horizontal rules
    text = re.sub(r"^---+$", "", text, flags=re.MULTILINE)

    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def strip_html_tags(text: str) -> str:
    """Remove HTML tags for plain-text fallback."""
    return re.sub(r"<[^>]+>", "", text)


def split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message into chunks that fit within Telegram's limit.

    Tries to split at newlines first, then at spaces, to avoid cutting mid-word.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        # Try to find a newline to split at
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            # No good newline — try a space
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            # No good split point — hard cut
            split_at = limit

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


async def send_message(chat_id: int, text: str, format_markdown: bool = False, parse_mode: str | None = None) -> int | None:
    """Send a text message to a Telegram chat. Splits if too long.

    If format_markdown is True, converts GitHub markdown to Telegram HTML.
    If parse_mode is provided, it is used directly (e.g., "HTML" or "MarkdownV2").
    Returns the message_id of the first chunk on success, None on failure.
    """
    if format_markdown:
        text = markdown_to_telegram_html(text)
        parse_mode = "HTML"

    if not text.strip():
        logger.warning("Attempted to send empty message to chat %d", chat_id)
        return None

    chunks = split_message(text)
    client = await get_client()
    first_id: int | None = None

    for chunk in chunks:
        msg_id = await _send_single(client, chat_id, chunk, parse_mode=parse_mode)
        if first_id is None:
            first_id = msg_id

    return first_id


async def send_voice(chat_id: int, ogg_path: str, caption: str | None = None) -> bool:
    """Send a voice message (OGG/Opus) to a Telegram chat.

    Returns True on success. Falls back silently on failure.
    """
    client = await get_client()
    url = f"{TELEGRAM_API}/sendVoice"

    with open(ogg_path, "rb") as f:
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]  # Telegram caption limit
        files = {"voice": ("voice.ogg", f, "audio/ogg")}

        try:
            resp = await client.post(url, data=data, files=files, timeout=60.0)
            if resp.status_code == 200:
                return True
            logger.error("sendVoice error %d: %s", resp.status_code, resp.text)
        except httpx.HTTPError as exc:
            logger.error("sendVoice HTTP error: %s", exc)

    return False


async def send_photo(chat_id: int, photo_path: str, caption: str | None = None) -> bool:
    """Send a photo file to a Telegram chat. Returns True on success."""
    client = await get_client()
    url = f"{TELEGRAM_API}/sendPhoto"

    with open(photo_path, "rb") as f:
        data = {"chat_id": str(chat_id)}
        if caption:
            caption = caption[:1024]  # Telegram caption limit
            data["caption"] = caption
        files = {"photo": ("image.png", f, "image/png")}

        try:
            resp = await client.post(url, data=data, files=files, timeout=60.0)
            if resp.status_code == 200:
                return True
            logger.error("sendPhoto error %d: %s", resp.status_code, resp.text)
        except httpx.HTTPError as exc:
            logger.error("sendPhoto HTTP error: %s", exc)

    return False


async def send_video(chat_id: int, video_path: str, caption: str | None = None) -> bool:
    """Send a video file to a Telegram chat. Returns True on success."""
    client = await get_client()
    url = f"{TELEGRAM_API}/sendVideo"

    with open(video_path, "rb") as f:
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        filename = os.path.basename(video_path)
        files = {"video": (filename, f, "video/mp4")}

        try:
            resp = await client.post(url, data=data, files=files, timeout=120.0)
            if resp.status_code == 200:
                return True
            logger.error("sendVideo error %d: %s", resp.status_code, resp.text)
        except httpx.HTTPError as exc:
            logger.error("sendVideo HTTP error: %s", exc)

    return False


async def download_photo(file_id: str) -> str:
    """Download a photo from Telegram and save to a temp file. Returns the file path."""
    client = await get_client()

    # Get file path from Telegram
    resp = await client.post(f"{TELEGRAM_API}/getFile", json={"file_id": file_id})
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    # Download the file
    token = TELEGRAM_API.split("/bot")[1]
    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    resp = await client.get(download_url)
    resp.raise_for_status()

    # Save to temp file with correct extension
    ext = os.path.splitext(file_path)[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="tg_photo_")
    tmp.write(resp.content)
    tmp.close()
    logger.info("Downloaded photo to %s (%d bytes)", tmp.name, len(resp.content))
    return tmp.name


async def download_document(file_id: str, dest_path: str) -> str:
    """Download a document from Telegram and save to dest_path. Returns the saved path."""
    client = await get_client()

    resp = await client.post(f"{TELEGRAM_API}/getFile", json={"file_id": file_id})
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    token = TELEGRAM_API.split("/bot")[1]
    download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    resp = await client.get(download_url)
    resp.raise_for_status()

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    logger.info("Downloaded document to %s (%d bytes)", dest_path, len(resp.content))
    return dest_path


async def delete_webhook() -> bool:
    """Delete any registered webhook, switching Telegram to allow polling. Returns True on success."""
    client = await get_client()
    try:
        resp = await client.post(f"{TELEGRAM_API}/deleteWebhook")
        data = resp.json()
        if data.get("ok"):
            logger.info("Webhook deleted — polling mode active")
            return True
        logger.error("deleteWebhook failed: %s", data)
    except httpx.HTTPError as exc:
        logger.error("deleteWebhook error: %s", exc)
    return False


async def get_updates(offset: int = 0, timeout: int = 30) -> list[dict]:
    """Long-poll Telegram for new updates. Returns a list of update dicts."""
    client = await get_client()
    try:
        resp = await client.post(
            f"{TELEGRAM_API}/getUpdates",
            json={"offset": offset, "timeout": timeout, "limit": 100},
            timeout=timeout + 5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return data.get("result", [])
        logger.warning("getUpdates returned %d: %s", resp.status_code, resp.text[:200])
    except httpx.HTTPError as exc:
        logger.warning("getUpdates HTTP error: %s", exc)
    return []


async def register_webhook(url: str) -> bool:
    """Register a webhook URL with Telegram. Returns True on success."""
    normalized_url = url.rstrip("/")
    if not normalized_url.endswith("/webhook"):
        normalized_url = f"{normalized_url}/webhook"

    client = await get_client()
    try:
        resp = await client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": normalized_url},
        )
        data = resp.json()
        if data.get("ok"):
            logger.info("Webhook registered: %s", normalized_url)
            return True
        logger.error("Webhook registration failed: %s", data)
    except httpx.HTTPError as exc:
        logger.error("Webhook registration HTTP error: %s", exc)
    return False


async def send_chat_action(chat_id: int, action: str) -> None:
    """Send a chat action indicator (typing, record_voice, etc.)."""
    client = await get_client()
    url = f"{TELEGRAM_API}/sendChatAction"
    try:
        await client.post(url, json={"chat_id": chat_id, "action": action})
    except httpx.HTTPError:
        pass


async def delete_message(chat_id: int, message_id: int) -> bool:
    """Delete a message by chat_id and message_id. Returns True on success."""
    client = await get_client()
    url = f"{TELEGRAM_API}/deleteMessage"
    try:
        resp = await client.post(url, json={"chat_id": chat_id, "message_id": message_id})
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        logger.error("deleteMessage HTTP error: %s", exc)
        return False


async def _send_single(
    client: httpx.AsyncClient,
    chat_id: int,
    text: str,
    retry: bool = True,
    parse_mode: str | None = None,
) -> int | None:
    """Send a single message chunk. Returns message_id on success, None on failure."""
    url = f"{TELEGRAM_API}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")

        # If Telegram rejected our HTML, retry as plain text
        if parse_mode and resp.status_code == 400:
            logger.warning("HTML parse rejected, falling back to plain text")
            plain = strip_html_tags(text)
            return await _send_single(client, chat_id, plain, retry=retry, parse_mode=None)

        logger.error("Telegram API error %d: %s", resp.status_code, resp.text)
    except httpx.HTTPError as exc:
        logger.error("Telegram HTTP error: %s", exc)

    # Retry once
    if retry:
        logger.info("Retrying send to chat %d", chat_id)
        return await _send_single(client, chat_id, text, retry=False, parse_mode=parse_mode)

    return None
