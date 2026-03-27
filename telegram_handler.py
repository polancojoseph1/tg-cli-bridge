import asyncio
import html
import logging
import os
import re
import tempfile
import httpx

from config import TELEGRAM_API, TELEGRAM_BOT_TOKEN, TELEGRAM_MAX_MESSAGE_LENGTH

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


def _convert_markdown_tables(text: str) -> str:
    """Convert markdown pipe tables to numbered lists for Telegram.

    Rule: tables are ALWAYS rendered as numbered lists (one entry per data row,
    headers used as bold labels). <pre> table blocks are never used because they
    wrap and look broken on mobile regardless of column count.
    """
    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Table detected: current line has pipes AND next line is a separator row
        if (
            "|" in line
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s\-|:]+\|\s*$", lines[i + 1])
        ):
            # Collect all consecutive pipe-containing lines
            table_lines = []
            while i < len(lines) and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            # Parse rows, skip separator rows
            rows = []
            for tl in table_lines:
                if re.match(r"^\s*\|[\s\-|:]+\|\s*$", tl):
                    continue
                cells = [c.strip() for c in tl.strip().strip("|").split("|")]
                rows.append(cells)
            if not rows:
                continue
            headers = rows[0]
            data_rows = rows[1:]
            # Always render as numbered list
            rendered = []
            for idx, row in enumerate(data_rows, 1):
                lines_out = [f"<b>{idx}.</b>"]
                for j, header in enumerate(headers):
                    val = row[j] if j < len(row) else ""
                    if val:
                        lines_out.append(f"  <b>{header}:</b> {val}")
                rendered.append("\n".join(lines_out))
            result.append("\n\n".join(rendered))
        else:
            result.append(line)
            i += 1
    return "\n".join(result)


_RE_CODE_BLOCK = re.compile(r"```\w*\n(.*?)```", flags=re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_URL = re.compile(r"https?://")
_RE_BARE_URL = re.compile(r'(?<!href=")(https?://[^\s<>"]+)')
_RE_HEADERS = re.compile(r"^#{1,6}\s+(.+)$", flags=re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_RE_ITALIC = re.compile(r"(?<!\w)\*([^*]+?)\*(?!\w)")
_RE_HRULE = re.compile(r"^---+$", flags=re.MULTILINE)
_RE_BLANK_LINES = re.compile(r"\n{3,}")
_RE_STRIP_HTML = re.compile(r"<[^>]+>")


def markdown_to_telegram_html(text: str) -> str:
    """Convert GitHub-flavored markdown to Telegram-compatible HTML.

    Converts bold/italic/code to HTML tags. Strips headers to plain text.
    """
    # Escape HTML entities first so Claude's output can't inject tags
    text = html.escape(text)

    # Convert markdown tables to numbered lists (Telegram has no table support — always use lists)
    text = _convert_markdown_tables(text)

    # Code blocks (```lang\n...\n```) → <pre>...</pre>
    text = _RE_CODE_BLOCK.sub(r"<pre>\1</pre>", text)

    # Inline code (`...`) → <code>...</code>, but URLs → <a href>
    def _inline_code(m: re.Match) -> str:
        inner = m.group(1).strip()
        if _RE_URL.match(inner):
            return f'<a href="{inner}">{inner}</a>'
        return f"<code>{inner}</code>"
    text = _RE_INLINE_CODE.sub(_inline_code, text)

    # Bare URLs (not already inside an href) → <a href>
    text = _RE_BARE_URL.sub(r'<a href="\1">\1</a>', text)

    # Headers (## ...) → plain text, strip the # prefix
    text = _RE_HEADERS.sub(r"\1", text)

    # Bold (**...**) → <b>...</b>
    text = _RE_BOLD.sub(r"<b>\1</b>", text)

    # Italic (*...*) → <i>...</i>
    text = _RE_ITALIC.sub(r"<i>\1</i>", text)

    # Horizontal rules → remove
    text = _RE_HRULE.sub("", text)

    # Collapse 3+ blank lines to 2
    text = _RE_BLANK_LINES.sub("\n\n", text)

    return text.strip()


def strip_html_tags(text: str) -> str:
    """Remove HTML tags for plain-text fallback."""
    return _RE_STRIP_HTML.sub("", text)


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
        if msg_id is None and parse_mode:
            # Formatted send failed — wait briefly then retry as plain text (last resort)
            await asyncio.sleep(1.0)
            plain = strip_html_tags(chunk)
            if plain.strip():
                msg_id = await _send_single(client, chat_id, plain, retry=True, parse_mode=None)
        if msg_id is None:
            logger.error("Failed to deliver message chunk to chat %d (%d chars)", chat_id, len(chunk))
        if first_id is None and msg_id is not None:
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
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    resp = await client.get(download_url)
    resp.raise_for_status()

    # Save to temp file with correct extension
    ext = os.path.splitext(file_path)[1] or ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="tg_photo_")
    tmp.write(resp.content)
    tmp.close()
    os.chmod(tmp.name, 0o600)
    logger.info("Downloaded photo to %s (%d bytes)", tmp.name, len(resp.content))
    return tmp.name


async def download_document(file_id: str, dest_path: str) -> str:
    """Download a document from Telegram and save to dest_path. Returns the saved path."""
    client = await get_client()

    resp = await client.post(f"{TELEGRAM_API}/getFile", json={"file_id": file_id})
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
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


async def register_bot_commands(commands: list[tuple[str, str]]) -> bool:
    """Register bot commands with Telegram for the autocomplete menu.

    Args:
        commands: List of (command, description) tuples, e.g. [("help", "Show help")]
    Returns True on success.
    """
    client = await get_client()
    payload = [{"command": cmd, "description": desc} for cmd, desc in commands]
    try:
        resp = await client.post(
            f"{TELEGRAM_API}/setMyCommands",
            json={"commands": payload},
        )
        data = resp.json()
        if data.get("ok"):
            logger.info("Bot commands registered (%d commands)", len(commands))
            return True
        logger.error("Bot command registration failed: %s", data)
    except httpx.HTTPError as exc:
        logger.error("Bot command registration HTTP error: %s", exc)
    return False


def _webhook_secret_token(bot_token: str) -> str:
    """Derive a stable secret token from the bot token (SHA-256, first 32 hex chars).
    Telegram requires: 1-256 chars, only a-z A-Z 0-9 _ -"""
    import hashlib
    return hashlib.sha256(bot_token.encode()).hexdigest()[:32]


async def register_webhook(url: str) -> bool:
    """Register a webhook URL with Telegram including a secret token. Returns True on success."""
    normalized_url = url.rstrip("/")
    if not normalized_url.endswith("/webhook"):
        normalized_url = f"{normalized_url}/webhook"

    secret = _webhook_secret_token(TELEGRAM_BOT_TOKEN)
    client = await get_client()
    try:
        resp = await client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": normalized_url, "secret_token": secret},
        )
        data = resp.json()
        if data.get("ok"):
            logger.info("Webhook registered: %s (with secret token)", normalized_url)
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


async def send_inline_keyboard(chat_id: int, text: str, buttons: list[list[dict]]) -> int | None:
    """Send a message with an inline keyboard.

    buttons is a list of rows, each row is a list of button dicts with
    keys 'text' and 'callback_data'.
    Returns the message_id on success, None on failure.
    """
    client = await get_client()
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    try:
        resp = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")
        logger.error("send_inline_keyboard error %d: %s", resp.status_code, resp.text)
    except httpx.HTTPError as exc:
        logger.error("send_inline_keyboard HTTP error: %s", exc)
    return None


async def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
    """Acknowledge an inline keyboard button press."""
    client = await get_client()
    try:
        await client.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert},
        )
    except httpx.HTTPError as exc:
        logger.error("answerCallbackQuery HTTP error: %s", exc)


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
    _depth: int = 0,
) -> int | None:
    """Send a single message chunk. Returns message_id on success, None on failure."""
    if _depth >= 2:
        return None
    url = f"{TELEGRAM_API}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = await client.post(url, json=payload)
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")

        # If Telegram rejected our HTML, retry as plain text once
        if parse_mode and resp.status_code == 400:
            logger.warning("HTML parse rejected, falling back to plain text")
            plain = strip_html_tags(text)
            return await _send_single(client, chat_id, plain, retry=False, parse_mode=None, _depth=_depth + 1)

        logger.error("Telegram API error %d: %s", resp.status_code, resp.text)
    except httpx.HTTPError as exc:
        logger.error("Telegram HTTP error: %s", exc)

    # Retry once on network error
    if retry:
        logger.info("Retrying send to chat %d", chat_id)
        return await _send_single(client, chat_id, text, retry=False, parse_mode=parse_mode, _depth=_depth + 1)

    return None
