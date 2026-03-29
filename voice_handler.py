import asyncio
import logging
import os
import re
import tempfile

import httpx

from config import TELEGRAM_API, TELEGRAM_BOT_TOKEN, WHISPER_MODEL, EDGE_TTS_VOICE

logger = logging.getLogger("bridge.voice")

# Lazy-loaded whisper model (loaded on first use)
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        logger.info("Loading Whisper model '%s' (first call may download it)...", WHISPER_MODEL)
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        logger.info("Whisper model ready")
    return _whisper_model


async def download_voice(file_id: str) -> str:
    """Download a voice/audio file from Telegram. Returns local file path."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get file path from Telegram
        resp = await client.get(
            f"{TELEGRAM_API}/getFile", params={"file_id": file_id}
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        file_path = result.get("file_path")
        if not file_path:
            raise ValueError("Telegram returned no file_path")

        # Download the actual file
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
        resp = await client.get(download_url)
        resp.raise_for_status()

        ext = os.path.splitext(file_path)[1] or ".ogg"
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        try:
            os.chmod(tmp_path, 0o600)
            os.write(fd, resp.content)
        finally:
            os.close(fd)

        logger.info("Downloaded voice file: %s (%d bytes)", tmp_path, len(resp.content))
        return tmp_path


def _transcribe_sync(file_path: str) -> str:
    """Synchronous transcription (runs in executor)."""
    model = _get_whisper_model()
    segments, info = model.transcribe(file_path, beam_size=5)
    text = " ".join(seg.text.strip() for seg in segments)
    logger.info("Transcribed %.1fs of audio -> %d chars", info.duration, len(text))
    return text


async def transcribe_audio(file_path: str) -> str:
    """Transcribe an audio file using faster-whisper. Returns text."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, file_path)


# Pre-compile regular expressions for TTS cleanup to improve performance
_RE_HTML_TAGS = re.compile(r"<[^>]+>")
_RE_CODE_BLOCKS = re.compile(r"```[\s\S]*?```")
_RE_INLINE_CODE = re.compile(r"`([^`]*)`")
_RE_MD_BOLD_ITALIC = re.compile(r"\*+")
_RE_MD_HEADERS = re.compile(r"^#{1,6}\s*", flags=re.MULTILINE)
_RE_MD_LINKS = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_RE_URLS = re.compile(r"https?://\S+")
_RE_IPS = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?")
_RE_TABLE_PIPES_SEP = re.compile(r"^\s*\|.*\|\s*$", flags=re.MULTILINE)
_RE_PIPES = re.compile(r"\|")
_RE_DASH_FLAGS = re.compile(r"--\S+")
_RE_MULTI_DASHES = re.compile(r"-{2,}")
_RE_LIST_MARKERS = re.compile(r"^\s*[-*•]\s+", flags=re.MULTILINE)
_RE_NUM_LIST_MARKERS = re.compile(r"^\s*\d+\.\s+", flags=re.MULTILINE)
_RE_EXT_PY = re.compile(r"\.py\b")
_RE_EXT_MD = re.compile(r"\.md\b")
_RE_EXT_TXT = re.compile(r"\.txt\b")
_RE_EXT_JSON = re.compile(r"\.json\b")
_RE_EXT_SH = re.compile(r"\.sh\b")
_RE_SLASH = re.compile(r"/\b")
_RE_BRACKETS = re.compile(r"[\[\](){}<>]")
_RE_SYMBOLS = re.compile(r"[#@$%^&*+=~\\|/<>]")
_RE_WHITESPACE = re.compile(r"\s+")

def _clean_for_tts(text: str) -> str:
    """Strip markdown and symbols so TTS reads naturally."""
    has_code = "```" in text or "`" in text
    has_link = "http" in text

    # Remove HTML tags
    text = _RE_HTML_TAGS.sub("", text)
    # Remove code blocks entirely
    text = _RE_CODE_BLOCKS.sub(" some code ", text)
    # Remove inline code backticks, keep the inner text
    text = _RE_INLINE_CODE.sub(r"\1", text)
    # Remove any remaining backticks
    text = text.replace("`", "")
    # Remove markdown bold/italic
    text = _RE_MD_BOLD_ITALIC.sub("", text)
    # Replace underscores with space (handles __pycache__, snake_case, etc.)
    text = text.replace("_", " ")
    # Remove markdown headers
    text = _RE_MD_HEADERS.sub("", text)
    # Markdown links: keep label only
    text = _RE_MD_LINKS.sub(r"\1", text)
    # Remove bare URLs and IP addresses
    text = _RE_URLS.sub("", text)
    text = _RE_IPS.sub("", text)
    # Remove table pipes and separator rows
    text = _RE_TABLE_PIPES_SEP.sub("", text)
    text = _RE_PIPES.sub(" ", text)
    # Remove leading dashes used as flags (--flag) or separators
    text = _RE_DASH_FLAGS.sub("", text)
    text = _RE_MULTI_DASHES.sub(" ", text)
    # Remove bullet/numbered list markers
    text = _RE_LIST_MARKERS.sub("", text)
    text = _RE_NUM_LIST_MARKERS.sub("", text)
    # Remove file extensions from filenames so they sound natural
    text = _RE_EXT_PY.sub(" ", text)
    text = _RE_EXT_MD.sub(" ", text)
    text = _RE_EXT_TXT.sub(" ", text)
    text = _RE_EXT_JSON.sub(" ", text)
    text = _RE_EXT_SH.sub(" ", text)
    text = _RE_SLASH.sub(" ", text)
    # Remove brackets and symbols
    text = _RE_BRACKETS.sub(" ", text)
    text = _RE_SYMBOLS.sub(" ", text)
    # Normalize whitespace
    text = _RE_WHITESPACE.sub(" ", text)
    cleaned = text.strip()

    if not cleaned:
        if has_code:
            return "I sent you some code."
        if has_link:
            return "I sent you a link."
        return "I sent you a message."

    return cleaned


async def text_to_speech_local(text: str) -> str:
    """Generate OGG/Opus audio using macOS `say` command — instant, no network. Returns file path."""
    text = _clean_for_tts(text)

    fd_aiff, aiff_path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd_aiff)
    os.chmod(aiff_path, 0o600)
    fd_ogg, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd_ogg)
    os.chmod(ogg_path, 0o600)

    try:
        # macOS say: instant local synthesis
        proc = await asyncio.create_subprocess_exec(
            "say", "-o", aiff_path, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("say command failed")

        # Convert AIFF -> OGG/Opus for pytgcalls
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", aiff_path,
            "-c:a", "libopus", "-b:a", "64k",
            "-application", "voip",
            "-y", ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg AIFF->OGG conversion failed")

        logger.info("Local TTS generated: %d chars -> %s", len(text), ogg_path)
        return ogg_path

    except Exception:
        cleanup_file(ogg_path)
        raise
    finally:
        cleanup_file(aiff_path)


async def text_to_speech(text: str) -> str:
    """Generate OGG/Opus voice audio from text using edge-tts. Returns file path."""
    import edge_tts
    text = _clean_for_tts(text)

    fd_mp3, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd_mp3)
    os.chmod(mp3_path, 0o600)
    fd_ogg, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd_ogg)
    os.chmod(ogg_path, 0o600)

    try:
        # Generate MP3 with Edge TTS
        communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
        await communicate.save(mp3_path)

        # Convert to OGG/Opus (required format for Telegram sendVoice)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", mp3_path,
            "-c:a", "libopus", "-b:a", "64k",
            "-application", "voip",
            "-y", ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        if proc.returncode != 0:
            raise RuntimeError("ffmpeg MP3->OGG conversion failed")

        logger.info("TTS generated: %d chars -> %s", len(text), ogg_path)
        return ogg_path

    except Exception:
        cleanup_file(ogg_path)
        raise
    finally:
        # Always clean up the intermediate MP3
        cleanup_file(mp3_path)


def cleanup_file(path: str) -> None:
    """Remove a temp file if it exists."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
