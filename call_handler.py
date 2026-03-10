"""Voice call handler — group voice chat via Pyrogram bot + pytgcalls.

Manages the full audio pipeline:
  User speaks -> silence detection -> Whisper STT -> Claude -> Edge TTS -> play back

Architecture:
  pytgcalls records incoming audio from the group call via a custom ffmpeg pipeline
  that writes raw PCM s16le to a temp file. The conversation loop reads from that file
  progressively to detect speech, without depending on frame callbacks (which do not
  fire for incoming audio in SHELL/ffmpeg mode).

Requires:
  - py-tgcalls + pyrofork (pip install)
  - A Telegram group with the bot added as admin
  - TG_API_ID, TG_API_HASH, CALL_GROUP_ID in .env
"""

import asyncio
import logging
import os
import struct
import tempfile
import time
import wave

from config import (
    TG_API_ID, TG_API_HASH, TG_SESSION_NAME, CALL_GROUP_ID,
    CALL_SILENCE_THRESHOLD, CALL_SILENCE_DURATION, CALL_MAX_SPEECH_DURATION,
)

logger = logging.getLogger("bridge.call")

# Audio format constants — must match ffmpeg output in _build_ffmpeg_cmd()
SAMPLE_RATE = 48000
CHANNELS = 1          # mono output from ffmpeg (converted from stereo input)
BYTES_PER_SAMPLE = 2  # s16le

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


class CallState:
    IDLE = "idle"
    JOINING = "joining"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    STOPPING = "stopping"


def _build_ffmpeg_cmd(output_path: str) -> str:
    """Build ffmpeg command: ntgcalls stereo PCM pipe -> mono PCM file.

    ntgcalls pipes s16le 48000Hz stereo to ffmpeg stdin.
    ffmpeg converts to mono and writes raw PCM s16le to output_path.
    """
    return (
        f"ffmpeg -y -loglevel quiet "
        f"-f s16le -ar 48000 -ac 2 "
        f"-i pipe:0 "
        f"-f s16le -ar {SAMPLE_RATE} -ac {CHANNELS} "
        f"{output_path}"
    )


class VoiceCallManager:
    """State machine for a group voice chat session.

    Joins using a Pyrogram userbot session (TG_SESSION_NAME.session) — bots cannot join voice chats.
    States: IDLE -> JOINING -> LISTENING <-> TRANSCRIBING -> THINKING -> SPEAKING -> LISTENING
    Any state -> /endcall -> STOPPING -> IDLE

    Audio capture uses file-based PCM reading:
    - pytgcalls records incoming audio via a custom ffmpeg pipeline to a .pcm temp file
    - The conversation loop reads from that .pcm file progressively
    - Silence detection is done on raw s16le samples from the file
    """

    def __init__(self, on_status=None):
        self._state = CallState.IDLE
        self._on_status = on_status
        self._group_id = 0

        self._app = None
        self._call_py = None

        # PCM recording file tracking
        self._record_path: str | None = None
        self._read_pos: int = 0

        # Playback synchronisation
        self._is_playing = False
        self._playback_done = asyncio.Event()

        self._loop_task = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state not in (CallState.IDLE, CallState.STOPPING)

    async def _notify(self, text: str):
        if self._on_status:
            try:
                await self._on_status(text)
            except Exception as e:
                logger.error("Status notification failed: %s", e)

    # ── Public API ────────────────────────────────────────────────

    async def start(self, chat_id: int = 0) -> bool:
        """Join the group voice chat and begin listening."""
        if self._state != CallState.IDLE:
            await self._notify("Already in a call.")
            return False

        self._group_id = chat_id or CALL_GROUP_ID
        if not self._group_id:
            await self._notify("\u274c CALL_GROUP_ID not configured in .env")
            return False
        if not TG_API_ID or not TG_API_HASH:
            await self._notify("\u274c TG_API_ID / TG_API_HASH not configured in .env")
            return False
        session_path = os.path.join(_PROJECT_DIR, f"{TG_SESSION_NAME}.session")
        if not os.path.exists(session_path):
            await self._notify(
                f"\u274c Userbot session not found: {TG_SESSION_NAME}.session\n"
                "Run: python setup_session.py  to authenticate once."
            )
            return False

        self._state = CallState.JOINING
        await self._notify("\U0001f4de Joining voice chat...")

        try:
            from pyrogram import Client
            from pytgcalls import PyTgCalls, filters
            from pytgcalls.types import GroupCallConfig, MediaStream
            from pytgcalls.types.raw import Stream, AudioStream, AudioParameters
            from ntgcalls import MediaSource

            self._app = Client(
                name=TG_SESSION_NAME,
                api_id=TG_API_ID,
                api_hash=TG_API_HASH,
                workdir=_PROJECT_DIR,
            )
            self._call_py = PyTgCalls(self._app)

            # Detect when TTS playback finishes
            @self._call_py.on_update(filters.stream_end())
            async def _on_stream_end(_client, _update):
                logger.info("Playback stream ended")
                self._playback_done.set()

            await self._call_py.start()
            logger.info("PyTgCalls started (bot token mode)")

            chat = await self._app.get_chat(self._group_id)
            logger.info("Resolved group: %s (type=%s)", chat.title, chat.type)

            # Create temp PCM file for incoming audio
            fd, self._record_path = tempfile.mkstemp(suffix=".pcm")
            os.close(fd)
            self._read_pos = 0

            # Custom Stream: incoming audio piped through ffmpeg -> raw mono PCM file
            # This is the key fix: RecordStream uses SHELL mode internally and does NOT
            # fire Python frame callbacks. By using a custom Stream we get the same
            # ffmpeg pipeline but with a raw PCM output we can read progressively.
            ffmpeg_cmd = _build_ffmpeg_cmd(self._record_path)
            logger.info("Recording cmd: %s", ffmpeg_cmd)

            custom_stream = Stream(
                microphone=AudioStream(
                    media_source=MediaSource.SHELL,
                    path=ffmpeg_cmd,
                    # Tell ntgcalls what format it sends to ffmpeg stdin:
                    # native rate is 48000Hz stereo
                    parameters=AudioParameters(bitrate=48000, channels=2),
                )
            )

            gc_config = GroupCallConfig(auto_start=False)
            await self._call_py.record(
                self._group_id,
                custom_stream,
                config=gc_config,
            )
            logger.info("Joined voice chat in group %s, PCM file: %s",
                        self._group_id, self._record_path)

            self._state = CallState.LISTENING
            await self._notify(
                "\U0001f399\ufe0f In voice chat \u2014 listening...\n"
                "Speak normally; I\u2019ll respond after you pause."
            )

            self._loop_task = asyncio.create_task(self._conversation_loop())
            return True

        except Exception as e:
            logger.exception("Failed to join voice chat")
            err_str = str(e)
            err_type = type(e).__name__
            if "NoActiveGroupCall" in err_type or "NoActiveGroupCall" in err_str:
                await self._notify(
                    "\u274c No active voice chat found in the group.\n\n"
                    "Please start a voice chat in the group first, "
                    "then send /call again."
                )
            elif "BOT_METHOD_INVALID" in err_str or "CreateGroupCall" in err_str:
                await self._notify(
                    "\u274c Bots can\u2019t start voice chats \u2014 only join existing ones.\n\n"
                    "Please start a voice chat in the group first, "
                    "then send /call again."
                )
            elif "CHAT_ADMIN_REQUIRED" in err_str:
                await self._notify(
                    "\u274c The bot needs admin rights in the group to join voice chats.\n"
                    "Make the bot an admin and try again."
                )
            else:
                await self._notify(f"\u274c Failed to join: {e}")
            self._state = CallState.IDLE
            await self._cleanup()
            return False

    async def stop(self):
        """Leave the voice chat and clean up."""
        if self._state == CallState.IDLE:
            return
        self._state = CallState.STOPPING

        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        await self._cleanup()
        self._state = CallState.IDLE
        await self._notify("\U0001f4f4 Left voice chat.")

    # ── Internal helpers ──────────────────────────────────────────

    async def _cleanup(self):
        try:
            if self._call_py and self._group_id:
                await self._call_py.leave_call(self._group_id)
        except Exception:
            pass
        try:
            if self._app:
                await self._app.stop()
        except Exception:
            pass
        self._call_py = None
        self._app = None
        self._is_playing = False
        if self._record_path:
            try:
                os.unlink(self._record_path)
            except OSError:
                pass
        self._record_path = None
        self._read_pos = 0

    @staticmethod
    def _compute_rms(data: bytes) -> float:
        """Compute RMS energy of PCM s16le audio."""
        if len(data) < 2:
            return 0.0
        count = len(data) // 2
        shorts = struct.unpack(f"<{count}h", data[: count * 2])
        sum_sq = sum(s * s for s in shorts)
        return (sum_sq / count) ** 0.5

    @staticmethod
    def _pcm_to_wav(pcm_data: bytes) -> str:
        """Convert raw PCM buffer to a WAV file for Whisper. Returns path."""
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(BYTES_PER_SAMPLE)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_data)
        return path

    async def _play_tts(self, ogg_path: str):
        """Play a TTS audio file into the voice chat."""
        from pytgcalls.types import MediaStream

        self._is_playing = True
        self._playback_done.clear()
        try:
            await self._call_py.play(
                self._group_id,
                MediaStream(ogg_path, video_flags=MediaStream.Flags.IGNORE),
            )
            try:
                await asyncio.wait_for(self._playback_done.wait(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning("Playback timeout — continuing")
        finally:
            self._is_playing = False

    def _read_new_pcm(self) -> bytes:
        """Read any new PCM bytes written to the recording file since last read."""
        if not self._record_path:
            return b""
        try:
            file_size = os.path.getsize(self._record_path)
        except OSError:
            return b""
        if file_size <= self._read_pos:
            return b""
        try:
            with open(self._record_path, "rb") as f:
                f.seek(self._read_pos)
                data = f.read(file_size - self._read_pos)
            self._read_pos = file_size
            return data
        except (OSError, IOError) as e:
            logger.warning("Error reading PCM file: %s", e)
            return b""

    # ── Main conversation loop ────────────────────────────────────

    async def _conversation_loop(self):
        """Read PCM file -> detect speech -> transcribe -> Claude -> TTS -> play back."""
        from voice_handler import transcribe_audio, text_to_speech_local, cleanup_file
        from claude_runner import run_claude

        POLL_INTERVAL = 0.1  # 100ms
        chunk_bytes = int(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * POLL_INTERVAL)
        min_speech_bytes = int(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 0.5)

        speech_buffer = bytearray()
        speech_detected = False
        speech_start = 0.0
        silence_start = 0.0
        debug_counter = 0

        while self._state not in (CallState.STOPPING, CallState.IDLE):
            await asyncio.sleep(POLL_INTERVAL)

            if self._state != CallState.LISTENING:
                continue

            # Periodic debug log every 5 seconds
            debug_counter += 1
            if debug_counter % 50 == 0:
                try:
                    file_size = os.path.getsize(self._record_path) if self._record_path else 0
                except OSError:
                    file_size = 0
                logger.info(
                    "Loop alive: pcm_file=%d bytes, read_pos=%d, speech_buf=%d bytes",
                    file_size, self._read_pos, len(speech_buffer),
                )

            # Read new PCM data written by ffmpeg since last poll
            new_data = self._read_new_pcm()
            if not new_data:
                continue

            now = time.time()

            # Use the most recent chunk for RMS analysis
            analysis_chunk = new_data[-chunk_bytes:] if len(new_data) >= chunk_bytes else new_data
            rms = self._compute_rms(analysis_chunk)

            if rms > CALL_SILENCE_THRESHOLD:
                if not speech_detected:
                    speech_detected = True
                    speech_start = now
                    speech_buffer = bytearray()
                    logger.info("Speech start (RMS=%.0f)", rms)
                speech_buffer.extend(new_data)
                silence_start = 0.0
            elif speech_detected:
                speech_buffer.extend(new_data)
                if silence_start == 0.0:
                    silence_start = now

            if not speech_detected:
                continue

            speech_duration = now - speech_start
            silence_elapsed = (now - silence_start) if silence_start else 0.0

            if (silence_elapsed < CALL_SILENCE_DURATION
                    and speech_duration < CALL_MAX_SPEECH_DURATION):
                continue

            # ── Process recorded speech ───────────────────────────

            pcm_data = bytes(speech_buffer)
            speech_buffer = bytearray()
            speech_detected = False
            silence_start = 0.0

            if len(pcm_data) < min_speech_bytes:
                logger.info("Audio too short (%d bytes), skipping", len(pcm_data))
                continue

            # Step 1: Transcribe
            self._state = CallState.TRANSCRIBING
            wav_path = None
            try:
                wav_path = self._pcm_to_wav(pcm_data)
                transcribed = await transcribe_audio(wav_path)
            except Exception as e:
                logger.error("Transcription error: %s", e)
                self._state = CallState.LISTENING
                continue
            finally:
                if wav_path:
                    cleanup_file(wav_path)

            if not transcribed.strip():
                logger.info("Empty transcription, back to listening")
                self._state = CallState.LISTENING
                continue

            await self._notify(f'\U0001f3a4 "{transcribed}"')
            logger.info("Transcribed: %s", transcribed)

            # Step 2: Claude
            self._state = CallState.THINKING
            try:
                response = await run_claude(
                    transcribed,
                    memory_context=(
                        "VOICE CALL MODE — CRITICAL: Your response will be read aloud by text-to-speech. "
                        "You MUST follow these rules or the output will sound broken and unlistenable:\n"
                        "- Write ONLY plain spoken English sentences. Nothing else.\n"
                        "- NEVER use: tables, pipes (|), backticks, asterisks, underscores, hashtags, "
                        "brackets, dashes as separators, bullet points, numbered lists, code blocks, or URLs.\n"
                        "- When listing files or items, say them as natural sentences: "
                        "'The files are pycache, call handler, claude runner, and config.' "
                        "NOT a table or bulleted list.\n"
                        "- File names: drop extensions and underscores. Say 'call handler' not 'call_handler.py'.\n"
                        "- Keep responses short — 1 to 3 sentences maximum.\n"
                        "- Imagine you are leaving a voicemail. That is the tone and format required."
                    ),
                )
            except Exception as e:
                logger.error("Claude error: %s", e)
                await self._notify(f"\u274c Claude error: {e}")
                self._state = CallState.LISTENING
                continue

            if not response or not response.strip():
                response = "Sorry, I couldn\u2019t generate a response."

            short = response[:500] + ("..." if len(response) > 500 else "")
            await self._notify(f"\U0001f4ac {short}")
            logger.info("Claude response (%d chars): %s", len(response), response[:200])

            # Step 3: TTS -> play back
            self._state = CallState.SPEAKING
            ogg_path = None
            try:
                ogg_path = await text_to_speech_local(response)
                await self._play_tts(ogg_path)
            except Exception as e:
                logger.error("Playback error: %s", e)
            finally:
                if ogg_path:
                    cleanup_file(ogg_path)

            # Drain any audio accumulated during playback (echo suppression)
            self._read_new_pcm()

            self._state = CallState.LISTENING
            logger.info("Back to listening")


# ── Module-level API ──────────────────────────────────────────────

_manager: VoiceCallManager | None = None


def get_manager() -> VoiceCallManager | None:
    return _manager


async def start_call(on_status=None, chat_id: int = 0) -> bool:
    """Start a voice chat session. Returns True on success."""
    global _manager
    if _manager and _manager.is_active:
        if on_status:
            await on_status("Already in a call. Use /endcall first.")
        return False
    _manager = VoiceCallManager(on_status=on_status)
    return await _manager.start(chat_id=chat_id)


async def end_call():
    """End the active voice chat session."""
    global _manager
    if _manager:
        await _manager.stop()
        _manager = None
