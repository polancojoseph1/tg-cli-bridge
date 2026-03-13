import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import health
from config import (
    ALLOWED_USER_ID, ALLOWED_USER_IDS, USER_NAMES, HOST, PORT, VOICE_MAX_LENGTH, WEBHOOK_URL,
    TELEGRAM_BOT_TOKEN, CLI_RUNNER, BOT_NAME, BOT_EMOJI, MEMORY_DIR,
    is_cli_available, validate_config, logger,
    COLLAB_ENABLED,
)
from runners import create_runner
from telegram_handler import send_message, delete_message, send_voice, send_photo, send_video, send_chat_action, download_photo, download_document, register_webhook, delete_webhook, get_updates, close_client, register_bot_commands
from image_handler import generate_image
try:
    import playwright_handler
    _playwright_available = True
except ImportError:
    _playwright_available = False
from voice_handler import download_voice, transcribe_audio, text_to_speech, cleanup_file
import memory_handler
import display_prefs
import task_handler
import daily_report
from instance_manager import InstanceManager, Instance
import router
import agent_manager
from agent_registry import create_agent, resolve_agent, list_agents, update_agent, delete_agent, get_agent, create_skill, get_skill, list_skills_db, update_skill, delete_skill
from agent_skills import SKILL_PACKS, list_skills

# Optional modules (graceful degradation if not present)
try:
    import screen_recorder
except ImportError:
    screen_recorder = None  # type: ignore

try:
    import scheduler
except ImportError:
    scheduler = None  # type: ignore

try:
    import proactive_worker
except ImportError:
    proactive_worker = None  # type: ignore

try:
    import task_orchestrator
except ImportError:
    task_orchestrator = None  # type: ignore

try:
    import trigger_registry
    import trigger_worker
    _triggers_available = True
except ImportError:
    trigger_registry = None  # type: ignore
    trigger_worker = None    # type: ignore
    _triggers_available = False


# Initialize the CLI runner
runner = create_runner()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# Suppress 404 access log noise (e.g. stale browser tabs hitting unknown routes)
class _Suppress404(logging.Filter):
    def filter(self, record):
        return '" 404 ' not in record.getMessage()

logging.getLogger("uvicorn.access").addFilter(_Suppress404())




# -- Prompt dictionary -------------------------------------------------------
# All AI prompts live here. Use {placeholders} for dynamic values.
# View all prompts at GET /prompts

PROMPTS = {}



# -- Instance manager --------------------------------------------------------
instances = InstanceManager()

# -- Session store (crash recovery) ------------------------------------------
import session_store as _ss_mod
_session_store = _ss_mod.SessionStore()
_SHUTDOWN_FLAG = os.path.join(os.path.expanduser(os.environ.get("TG_BRIDGE_DATA_DIR", "~/.bridgebot")), "pids", f"{CLI_RUNNER}.shutdown_clean")

# -- Message types -----------------------------------------------------------

class MessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    IMAGE_GEN = "image_gen"


@dataclass
class QueuedMessage:
    chat_id: int
    msg_type: MessageType
    text: str = ""
    file_id: str = ""
    voice_reply: bool = False
    instance_id: int = 0  # 0 = use active instance
    user_id: int = 0


_processed_updates: set[int] = set()
_voice_reply_mode: bool = False  # Toggle: reply with voice to text messages too


# -- Per-instance queue workers ----------------------------------------------

def _ensure_worker(inst: Instance) -> None:
    """Start a queue worker for the instance if one isn't running."""
    if inst.worker_task is None or inst.worker_task.done():
        inst.worker_task = asyncio.create_task(_instance_queue_worker(inst))
        logger.info("Started worker for instance #%d: %s", inst.id, inst.title)


async def _instance_queue_worker(inst: Instance) -> None:
    """Persistent worker that processes queued messages for a single instance.

    Outer loop ensures the worker always restarts after any crash.
    """
    while True:
        try:
            item = await inst.queue.get()
        except asyncio.CancelledError:
            logger.info("Instance #%d worker cancelled while waiting", inst.id)
            return
        except Exception:
            logger.exception("Instance #%d queue.get() error", inst.id)
            await asyncio.sleep(1)
            continue

        inst.processing = True
        try:
            if item.msg_type == MessageType.TEXT:
                coro = _process_message(item.chat_id, item.text, voice_reply=item.voice_reply, instance=inst, user_id=item.user_id)
            elif item.msg_type == MessageType.PHOTO:
                coro = _process_photo_message(item.chat_id, item.file_id, item.text, instance=inst, user_id=item.user_id)
            elif item.msg_type == MessageType.VOICE:
                coro = _process_voice_message(item.chat_id, item.file_id, item.text, instance=inst, user_id=item.user_id)
            elif item.msg_type == MessageType.IMAGE_GEN:
                coro = _process_image_generation(item.chat_id, item.text)
            else:
                continue

            inst.current_task = asyncio.create_task(coro)
            await inst.current_task
        except asyncio.CancelledError:
            logger.info("Instance #%d task cancelled", inst.id)
        except Exception as e:
            logger.error("Instance #%d worker error processing %s: %s", inst.id, item.msg_type.value, e)
            try:
                await send_message(item.chat_id, f"Error processing your message: {e}")
            except Exception:
                logger.error("Instance #%d failed to send error message", inst.id)
        finally:
            inst.current_task = None
            inst.processing = False
            try:
                inst.queue.task_done()
            except ValueError:
                pass  # task_done called too many times


async def _enqueue_message(item: QueuedMessage) -> None:
    """Add a message to the target instance's queue."""
    inst = instances.get(item.instance_id) if item.instance_id else instances.active
    if inst is None:
        inst = instances.active

    _ensure_worker(inst)

    if inst.queue.full():
        enqueue_owner_id = 0 if item.user_id == ALLOWED_USER_ID else item.user_id
        owner_count = len(instances.list_all(for_owner_id=enqueue_owner_id))
        label = f" [#{instances.display_num(inst.id, enqueue_owner_id)}: {inst.title}]" if owner_count >= 2 else ""
        await send_message(
            item.chat_id,
            f"Queue is full (max 10){label}. Please wait or send /stop to cancel.",
        )
        return

    if inst.processing:
        position = inst.queue.qsize() + 1
        enqueue_owner_id = 0 if item.user_id == ALLOWED_USER_ID else item.user_id
        owner_count = len(instances.list_all(for_owner_id=enqueue_owner_id))
        label = f" [#{instances.display_num(inst.id, enqueue_owner_id)}: {inst.title}]" if owner_count >= 2 else ""
        await send_message(
            item.chat_id,
            f"Queued (position {position}){label}. I'll get to it when the current task finishes.",
        )

    await inst.queue.put(item)


def _is_any_processing() -> bool:
    """Check if any instance is currently processing."""
    return any(inst.processing for inst in instances.list_all())


def _total_queue_size() -> int:
    """Total pending messages across all instance queues."""
    return sum(inst.queue.qsize() for inst in instances.list_all() if inst.queue)




async def _init_memory_background() -> None:
    """Initialize vector memory without blocking API startup."""
    try:
        primary_count = await memory_handler.index_files(0)
        logger.info("Memory initialized: %d chunks indexed", primary_count)
    except Exception as e:
        logger.warning("Memory initialization failed (non-fatal): %s", e)


async def _start_scheduler_background() -> None:
    """Start scheduler after startup has fully completed."""
    await asyncio.sleep(0.2)
    if scheduler:
        scheduler.init(runner, TELEGRAM_BOT_TOKEN, str(next(iter(ALLOWED_USER_IDS), "")))
        await scheduler.scheduler_loop()


async def _notify_startup_background() -> None:
    """Send startup ping without blocking server readiness."""
    await asyncio.sleep(0.2)
    await send_message(ALLOWED_USER_ID, "\u2705 Server restarted and ready.")


async def _restore_sessions_after_crash() -> None:
    """Recreate all instances from stored state after a crash.

    Runs as a background task on startup when no shutdown_clean flag is found.
    Instances are recreated in their original order. Unresolved instances get
    an auto-queued recovery message so the bot resumes the task automatically.

    If a detached subprocess is still running (survived the crash), we reconnect
    to its log file and deliver its output when it finishes. If the subprocess
    already finished while we were down, we deliver unread log output immediately.
    """
    from runners.base import RunnerBase

    await asyncio.sleep(0.5)  # let startup fully complete first

    sessions = _session_store.get_all_sessions(CLI_RUNNER)
    if not sessions:
        return

    _session_store.prune_old_messages()

    unresolved_count = 0
    by_chat: dict[str, list[dict]] = {}
    for s in sessions:
        by_chat.setdefault(s["chat_id"], []).append(s)

    for chat_id_str, chat_sessions in by_chat.items():
        chat_id = int(chat_id_str)
        owner_id = 0 if chat_id == ALLOWED_USER_ID else chat_id

        for s in chat_sessions:  # already ordered by instance_number
            num = s["instance_number"]
            title = s.get("title") or f"Instance {num}"

            inst = instances.create_with_number(num, title, owner_id=owner_id)
            _ensure_worker(inst)

            # Restore session_id so runner uses --resume on recovery
            if s.get("session_id"):
                inst.session_id = s["session_id"]
                inst.session_started = True
                if CLI_RUNNER == "codex":
                    inst.adapter_data["thread_id"] = s["session_id"]

            if s["status"] != "unresolved":
                continue

            # Check for a surviving detached subprocess first
            sub_info = _session_store.get_subprocess_info(chat_id, CLI_RUNNER, num)
            if sub_info:
                pid = sub_info["subprocess_pid"]
                log_file = sub_info["subprocess_log_file"]
                offset = sub_info["subprocess_log_offset"] or 0
                start_time = sub_info["subprocess_start_time"] or ""

                if RunnerBase.is_pid_alive(pid, start_time):
                    # Subprocess still running — reconnect to its log stream
                    logger.info(
                        "Reconnecting to live subprocess PID %d for chat %s inst %d",
                        pid, chat_id, num,
                    )
                    asyncio.create_task(
                        _reconnect_subprocess(chat_id, inst, log_file, offset, pid, num)
                    )
                    unresolved_count += 1
                    continue
                elif log_file and os.path.exists(log_file):
                    # Subprocess finished while server was down — deliver unread output
                    logger.info(
                        "Subprocess for chat %s inst %d already finished; delivering log from offset %d",
                        chat_id, num, offset,
                    )
                    asyncio.create_task(
                        _deliver_subprocess_log(chat_id, inst, log_file, offset, num)
                    )
                    unresolved_count += 1
                    continue

            # No surviving subprocess — fall back to standard crash recovery
            if s.get("original_prompt"):
                unresolved_count += 1
                inst.needs_recovery = True

                context = _session_store.build_recovery_context(chat_id, CLI_RUNNER, num)
                recovery_text = (
                    f"[CRASH RECOVERY] The server restarted unexpectedly. "
                    f"Please resume your previous task.\n\n{context}"
                    if context else
                    f"[CRASH RECOVERY] The server restarted. "
                    f"Please resume: {s['original_prompt']}"
                )
                item = QueuedMessage(
                    chat_id=chat_id,
                    msg_type=MessageType.TEXT,
                    text=recovery_text,
                    instance_id=inst.id,
                    user_id=ALLOWED_USER_ID if owner_id == 0 else owner_id,
                )
                await inst.queue.put(item)

    if unresolved_count:
        await send_message(
            ALLOWED_USER_ID,
            f"\u267b\ufe0f Crash detected. Restoring {unresolved_count} active session(s)...",
        )


def _extract_text_from_event(data: dict, text_parts: list) -> None:
    """Extract assistant text from a stream-json event (handles claude + gemini + codex formats)."""
    msg_type = data.get("type", "")
    # Claude / Qwen format: type=assistant with content blocks
    if msg_type == "assistant":
        for block in data.get("message", {}).get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
    # Gemini format: type=message role=assistant
    elif msg_type == "message" and data.get("role") == "assistant":
        content = data.get("content", "")
        if content:
            text_parts.append(content)
    # Claude result fallback
    elif msg_type == "result":
        result = data.get("result", "")
        if result and not text_parts:  # only if we have nothing else
            text_parts.append(result)
    # Codex format: type=item.completed with agent_message item
    elif msg_type == "item.completed":
        item = data.get("item", {})
        if item.get("type") == "agent_message":
            text = item.get("text", "")
            if text:
                text_parts.append(text)


async def _reconnect_subprocess(
    chat_id: int, inst, log_file: str, start_offset: int, pid: int, inst_num: int
) -> None:
    """Tail a still-running subprocess log file and deliver its output to Telegram."""
    from runners.base import RunnerBase

    class _PidWatcher:
        """Watches a PID and exposes returncode when it exits."""
        def __init__(self, watched_pid):
            self._pid = watched_pid
            self.returncode = None

        def check(self):
            if self.returncode is not None:
                return
            try:
                os.kill(self._pid, 0)  # 0 = just check existence
            except (ProcessLookupError, PermissionError):
                self.returncode = 0  # assume clean exit

    watcher = _PidWatcher(pid)
    text_parts: list[str] = []

    async for line, offset in RunnerBase.tail_log_file(log_file, start_offset=start_offset, proc=watcher):
        watcher.check()
        if not line:
            continue
        # Update offset in DB so we don't re-read on next crash
        _session_store.update_log_offset(chat_id, CLI_RUNNER, inst_num, offset)
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        _extract_text_from_event(data, text_parts)

    if text_parts:
        response = "".join(text_parts)
        labeled = _label(inst, response, 0)
        await send_message(chat_id, labeled, format_markdown=True)

    _session_store.mark_resolved(chat_id, CLI_RUNNER, inst_num)
    _session_store.clear_subprocess(chat_id, CLI_RUNNER, inst_num)
    inst.subprocess_pid = 0
    inst.subprocess_log_file = ""
    inst.subprocess_start_time = ""


async def _deliver_subprocess_log(
    chat_id: int, inst, log_file: str, start_offset: int, inst_num: int
) -> None:
    """Read unprocessed portion of a finished subprocess log and deliver to Telegram."""
    text_parts: list[str] = []

    try:
        with open(log_file, "r", errors="replace") as f:
            f.seek(start_offset)
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _extract_text_from_event(data, text_parts)
    except OSError:
        pass

    if text_parts:
        response = "".join(text_parts)
        labeled = _label(inst, response, 0)
        await send_message(chat_id, labeled, format_markdown=True)

    _session_store.mark_resolved(chat_id, CLI_RUNNER, inst_num)
    _session_store.clear_subprocess(chat_id, CLI_RUNNER, inst_num)


# -- Lifespan ----------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    errors = validate_config()
    for err in errors:
        logger.warning("Config issue: %s", err)
    if is_cli_available():
        logger.info("claude CLI found in PATH")
    else:
        logger.warning("claude CLI NOT found in PATH -- commands will fail")
    health.init()
    if task_orchestrator:
        task_orchestrator.init(runner)
    if WEBHOOK_URL:
        await register_webhook(WEBHOOK_URL)
        logger.info("Webhook registered from WEBHOOK_URL env")
    else:
        await delete_webhook()
        logger.info("No WEBHOOK_URL set — starting long-poll mode")
        asyncio.create_task(_run_polling())

    await register_bot_commands([
        # Core
        ("help",      "Show available commands"),
        ("status",    "Show server status & queue depth"),
        
        # Session Management
        ("new",       "Reset conversation & start fresh"),
        ("stop",      "Stop current task & clear queue"),
        ("kill",      "Force-kill all AI processes"),
        
        # Instances (Multi-session)
        ("inst",      "Manage instances: new/list/switch/rename/end"),
        
        # Agents
        ("agent",     "Manage specialist agents: list/create/talk/fix/feedback"),
        ("orch",      "Break task into parallel agents, synthesize results"),
        
        # Scheduling
        ("schedule",   "Schedule a recurring or one-time task"),
        ("schedules",  "List active schedules"),
        ("unschedule", "Cancel a schedule by ID"),
        
        # Memory & Tasks
        ("remember",  "Save something to memory"),
        ("memory",    "Memory stats & re-index files"),
        ("task",      "View/manage task list: add/done/list"),
        
        # Media & Voice
        ("imagine",   "Generate an image from prompt"),
        ("voice",     "Toggle voice replies mode"),
        # Browser & Tools
        ("screenshot", "Screenshot a URL and send the image"),
        ("browse",    "Extract readable text from a URL"),
        ("chrome",    "Toggle Chrome browser integration"),
        ("model",     "Switch AI model: sonnet|opus|haiku"),
        
        # System
        ("server",    "Restart the bridge server"),
    ])

    # Crash detection: if shutdown_clean flag is absent, the last run crashed
    _crashed = not os.path.exists(_SHUTDOWN_FLAG)
    if not _crashed:
        try:
            os.remove(_SHUTDOWN_FLAG)
        except OSError:
            pass
        logger.info("Clean boot detected (shutdown_clean flag found)")
    else:
        logger.info("Crash detected (no shutdown_clean flag) — will restore sessions")

    # Start worker for the default instance (primary user)
    _ensure_worker(instances.active)

    # Auto-create dedicated instances for non-primary users and start their workers
    for uid in ALLOWED_USER_IDS:
        if uid == ALLOWED_USER_ID:
            continue
        name = USER_NAMES.get(uid, f"User {uid}")
        inst = instances.ensure_pinned(uid, name)
        _ensure_worker(inst)
        logger.info("Created dedicated instance for %s (user %d)", name, uid)

    logger.info("Instance workers started")

    # Seed default specialist agents
    agent_manager.ensure_default_agents()

    # NOTE: Memory warmup is intentionally disabled here.
    # Chroma initialization can stall startup and block webhook responsiveness.
    logger.info("Telegram-Claude bridge is ready")
    asyncio.create_task(_start_scheduler_background())
    if _triggers_available:
        trigger_registry.init_db()
        trigger_worker.init(instances, send_message)
        logger.info("Trigger system ready")
    asyncio.create_task(_notify_startup_background())
    if _crashed:
        asyncio.create_task(_restore_sessions_after_crash())
    # Start borrow session timeout checker
    if COLLAB_ENABLED and collab_borrow is not None:
        asyncio.create_task(collab_borrow.timeout_checker(
            instances,
            notify_fn=lambda msg: send_message(ALLOWED_USER_ID, msg),
        ))
    # Proactive worker does NOT auto-start — use /agent proactive start to enable
    yield
    # Stop all instance workers
    for inst in instances.list_all():
        if inst.worker_task and not inst.worker_task.done():
            inst.worker_task.cancel()
            try:
                await inst.worker_task
            except asyncio.CancelledError:
                pass
    await proactive_worker.stop()
    await close_client()
    # Only write shutdown_clean flag if no sessions are mid-task.
    # If there are unresolved sessions, skip the flag so the next boot
    # triggers crash recovery and resumes in-flight work.
    if _session_store.has_unresolved(CLI_RUNNER):
        logger.info("Skipping shutdown_clean flag — unresolved sessions exist")
    else:
        try:
            Path(_SHUTDOWN_FLAG).parent.mkdir(parents=True, exist_ok=True)
            Path(_SHUTDOWN_FLAG).write_text(str(int(time.time())))
            logger.info("Clean shutdown flag written")
        except OSError as e:
            logger.warning("Could not write shutdown flag: %s", e)
    logger.info("Bridge shut down")


app = FastAPI(title="Telegram-Claude Bridge", lifespan=lifespan)

# -- Collab router (federated peer networking) --------------------------------
collab_borrow = None  # type: ignore
collab_borrow_start = None  # type: ignore
collab_borrow_message = None  # type: ignore
collab_borrow_end = None  # type: ignore
load_peers = None  # type: ignore

if COLLAB_ENABLED:
    try:
        from collab import collab_router
        from collab import borrow as collab_borrow
        from collab.client import borrow_start as collab_borrow_start, borrow_message as collab_borrow_message, borrow_end as collab_borrow_end
        from collab.config import load_peers
        app.include_router(collab_router)
        logger.info("Collab module loaded and router mounted at /collab")
    except Exception as _collab_err:
        collab_borrow = None  # type: ignore
        collab_borrow_start = None  # type: ignore
        collab_borrow_message = None  # type: ignore
        collab_borrow_end = None  # type: ignore
        load_peers = None  # type: ignore
        logger.warning("Collab module failed to load (non-fatal): %s", _collab_err)


@app.get("/health")
async def health_endpoint():
    return health.get_health()


@app.get("/status")
async def status_endpoint():
    return health.get_status()


class DirectQueryRequest(BaseModel):
    prompt: str
    timeout_secs: int = 120


@app.post("/query")
async def direct_query(req: DirectQueryRequest):
    """Stateless AI query endpoint for automation tools (n8n, scripts).
    Runs Claude Haiku with no session/memory overhead. Returns raw text response."""
    try:
        response = await runner.run_query(req.prompt, timeout_secs=req.timeout_secs)
        return {"ok": True, "response": response}
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"ok": False, "error": f"AI response timed out after {req.timeout_secs}s", "response": ""},
        )
    except Exception as exc:
        logger.error("Direct query error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(exc), "response": ""},
        )


    return result




























@app.get("/prompts")
async def get_prompts(name: Optional[str] = None):
    """Return all prompts from the PROMPTS dictionary, or a single prompt by ?name=key."""
    if name:
        if name not in PROMPTS:
            return JSONResponse(status_code=404, content={"error": f"Prompt '{name}' not found", "available": list(PROMPTS.keys())})
        return {"name": name, "prompt": PROMPTS[name]}
    return {"prompts": {k: {"length": len(v), "preview": v[:120] + "..."} for k, v in PROMPTS.items()}}






async def process_update(body: dict) -> None:
    """Process a single Telegram update dict. Used by both webhook and polling modes."""
    # Deduplicate retries / repeated polling
    update_id = body.get("update_id")
    if update_id:
        if update_id in _processed_updates:
            return
        _processed_updates.add(update_id)
        if len(_processed_updates) > 1000:
            oldest = sorted(_processed_updates)[:500]
            _processed_updates.difference_update(oldest)

    message = body.get("message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", 0)
    text = message.get("text", "")
    voice = message.get("voice")
    audio = message.get("audio")
    photo = message.get("photo")
    document = message.get("document")

    logger.info("Incoming | user=%d chat=%d text=%s voice=%s", user_id, chat_id, text[:80], bool(voice or audio))

    # Auth check
    if user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user %d", user_id)
        await send_message(chat_id, "Unauthorized.")
        return

    # Normalize command to lowercase (preserve args) so all commands are case-insensitive
    if text.startswith("/"):
        _space = text.find(" ")
        text = (text[:_space].lower() + text[_space:]) if _space != -1 else text.lower()

    # Bot commands -- handled directly (fast, no background needed)

    if text.startswith("/"):
        await _handle_command(chat_id, text, user_id=user_id)
        return

    # Photo message -- download and send to Claude with vision
    if photo:
        # Telegram sends multiple sizes; pick the largest (last in array)
        file_id = photo[-1]["file_id"]
        caption = message.get("caption", "")
        health.record_message()

        target_instance = _resolve_target_instance(caption or "photo", user_id)
        asyncio.create_task(_enqueue_message(QueuedMessage(
            chat_id=chat_id,
            msg_type=MessageType.PHOTO,
            text=caption,
            file_id=file_id,
            instance_id=target_instance.id,
            user_id=user_id,
        )))
        return

    # Document upload -- save to uploads folder inside memory dir
    if document:
        file_id = document["file_id"]
        file_name = os.path.basename(document.get("file_name", f"file_{file_id[:8]}"))
        save_dir = os.path.join(MEMORY_DIR, "uploads")
        os.makedirs(save_dir, exist_ok=True)
        dest_path = os.path.join(save_dir, file_name)
        if not os.path.realpath(dest_path).startswith(os.path.realpath(save_dir)):
            logger.warning("Upload path escape blocked: %s", dest_path)
            return
        health.record_message()
        asyncio.create_task(_handle_document_upload(chat_id, file_id, dest_path, file_name))
        return

    # Voice / audio message -- transcribe then process
    if voice or audio:
        file_id = (voice or audio)["file_id"]
        caption = message.get("caption", "")
        health.record_message()
        voice_instance = _resolve_target_instance("", user_id)
        asyncio.create_task(_enqueue_message(QueuedMessage(
            chat_id=chat_id,
            msg_type=MessageType.VOICE,
            text=caption,
            file_id=file_id,
            instance_id=voice_instance.id,
            user_id=user_id,
        )))
        return

    # Skip empty messages
    if not text.strip() and not photo and not voice and not audio and not document:
        return

    # One-shot direct message: @<id or name> <message>
    # Routes to a specific instance WITHOUT changing the active instance.
    # Supports: @2 hey, @Research what's the status?, @ChatGPT summarize this
    import re as _re
    _oneshot_match = _re.match(r'^@(\S+)\s+([\s\S]+)$', text.strip())
    if _oneshot_match:
        target_ref = _oneshot_match.group(1)
        oneshot_text = _oneshot_match.group(2).strip()
        owner_id = 0 if user_id == ALLOWED_USER_ID else user_id

        # Resolve target: try display number first, then title
        target_inst = None
        if target_ref.isdigit():
            target_inst = instances.get_by_display_num(int(target_ref), owner_id)
        if target_inst is None:
            # Partial title match (case-insensitive)
            for inst in instances.list_all(for_owner_id=owner_id):
                if target_ref.lower() in inst.title.lower():
                    target_inst = inst
                    break

        if target_inst is None:
            # Auto-create a new instance with the given name (or a default title if numeric)
            new_title = target_ref if not target_ref.isdigit() else f"Instance {target_ref}"
            target_inst = instances.create(new_title, owner_id=owner_id, switch_active=False)
            _ensure_worker(target_inst)
            disp_new = instances.display_num(target_inst.id, owner_id)
            await send_message(chat_id, f"✨ Created new instance <b>#{disp_new}: {target_inst.title}</b> (your active instance unchanged)", parse_mode="HTML")

        health.record_message()
        disp = instances.display_num(target_inst.id, owner_id)
        await send_message(chat_id, f"📨 Sending to <b>#{disp}: {target_inst.title}</b> (your active instance unchanged)", parse_mode="HTML")

        async def _oneshot_enqueue():
            try:
                await _enqueue_message(QueuedMessage(
                    chat_id=chat_id,
                    msg_type=MessageType.TEXT,
                    text=oneshot_text,
                    voice_reply=_voice_reply_mode,
                    instance_id=target_inst.id,
                    user_id=user_id,
                ))
            except Exception as e:
                logger.error("One-shot enqueue failed: %s", e)
                await send_message(chat_id, f"Error sending to @{target_ref}: {e}")

        asyncio.create_task(_oneshot_enqueue())
        return

    # Regular text message -- route to instance and process
    health.record_message()

    async def _route_and_enqueue():
        try:
            target_instance = await _resolve_target_instance_async(text, user_id)
            await _enqueue_message(QueuedMessage(
                chat_id=chat_id,
                msg_type=MessageType.TEXT,
                text=text,
                voice_reply=_voice_reply_mode,
                instance_id=target_instance.id,
                user_id=user_id,
            ))
        except Exception as e:
            logger.error("Failed to route/enqueue message: %s", e)
            await send_message(chat_id, f"Error queuing message: {e}")

    asyncio.create_task(_route_and_enqueue())


async def _run_polling() -> None:
    """Long-poll Telegram for updates when running without a webhook."""
    offset = 0
    logger.info("Polling loop started")
    while True:
        try:
            updates = await get_updates(offset=offset, timeout=30)
            for update in updates:
                offset = update["update_id"] + 1
                asyncio.create_task(process_update(update))
        except asyncio.CancelledError:
            logger.info("Polling loop cancelled")
            break
        except Exception as exc:
            logger.error("Polling error: %s", exc)
            await asyncio.sleep(5)


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    await process_update(body)
    return JSONResponse({"ok": True})


@app.post("/triggers/webhook/{trigger_id}")
async def trigger_webhook(trigger_id: str, request: Request):
    """HTTP endpoint for external event triggers (GitHub, custom webhooks, etc.)."""
    import hashlib
    import hmac

    if not _triggers_available:
        return JSONResponse({"ok": False, "error": "triggers not available"}, status_code=503)

    trigger = trigger_registry.get_trigger(trigger_id)
    if not trigger:
        return JSONResponse({"ok": False, "error": "trigger not found"}, status_code=404)
    if not trigger.enabled:
        return JSONResponse({"ok": False, "error": "trigger disabled"}, status_code=200)

    raw_body = await request.body()

    # Validate HMAC secret if configured (GitHub-compatible)
    secret = trigger.config.get("secret", "")
    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Trigger '%s': invalid signature", trigger_id)
            return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)

    # Filter by GitHub event type if configured
    event_filter = trigger.config.get("event", "")
    if event_filter:
        gh_event = request.headers.get("X-GitHub-Event", "")
        if gh_event and gh_event != event_filter:
            return JSONResponse({"ok": True, "skipped": f"event {gh_event!r} != {event_filter!r}"})

    # Filter by branch if configured (GitHub push payload: ref = "refs/heads/main")
    branch_filter = trigger.config.get("branch", "")
    if branch_filter:
        try:
            payload = json.loads(raw_body)
            ref = payload.get("ref", "")
            pushed_branch = ref.replace("refs/heads/", "")
            if pushed_branch and pushed_branch != branch_filter:
                return JSONResponse({"ok": True, "skipped": f"branch {pushed_branch!r} != {branch_filter!r}"})
        except Exception:
            pass

    fired = await trigger_worker.fire(trigger_id)
    return JSONResponse({"ok": fired})


def _resolve_target_instance(text: str, user_id: int = 0):
    """Synchronous instance resolution (for photos etc)."""
    resolve_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    return instances.get_active_for(resolve_owner_id)


async def _resolve_target_instance_async(text: str, user_id: int = 0):
    """Route: secondary users go to their active instance; primary user uses Ollama router."""
    resolve_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    if resolve_owner_id != 0:
        return instances.get_active_for(resolve_owner_id)
    # Primary user: use router to pick among their own instances
    target_instance = instances.get_active_for(0)
    user_insts = instances.list_all(for_owner_id=0)
    if len(user_insts) >= 2:
        try:
            inst_list = [{"id": i.id, "title": i.title} for i in user_insts]
            routed_id = await router.route_message(text, inst_list)
            if routed_id is not None:
                routed = instances.get(routed_id)
                if routed:
                    target_instance = routed
        except Exception as e:
            logger.warning("Router failed, using active instance: %s", e)
    return target_instance


# -- Processing functions ----------------------------------------------------


def _label(instance, response: str, owner_id: int = 0, show_emoji: bool = True) -> str:
    """Prefix response with instance label when the user has multiple instances."""
    # Stop signals already carry their own 🛑 — don't prepend the bot emoji
    if response.startswith("\U0001f6d1"):
        show_emoji = False
    prefix = f"{BOT_EMOJI} " if BOT_EMOJI and show_emoji else ""
    owner_insts = instances.list_all(for_owner_id=owner_id)
    if len(owner_insts) >= 2 and instance:
        disp = instances.display_num(instance.id, owner_id)
        return f"{prefix}**[#{disp}: {instance.title}]**\n{response}"
    # If response starts with a markdown header, put the emoji on its own line
    # so the header regex (^## ...) can match at the start of the next line.
    if prefix and response.lstrip().startswith("#"):
        return f"{prefix}\n{response}"
    return f"{prefix}{response}" if prefix else response


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _context_footer(inst) -> str:
    """Build a context window usage footer for the response."""
    if not inst or not inst.context_window:
        return ""
    used = (inst.last_input_tokens + inst.last_cache_read_tokens
            + inst.last_cache_creation_tokens + inst.last_output_tokens)
    if not used:
        return ""
    pct = (used / inst.context_window) * 100
    cost_str = f" \u00b7 ${inst.session_cost:.3f}" if inst.session_cost else ""
    return f"\n\n\u2014\n\U0001f4ca {_fmt_tokens(used)} / {_fmt_tokens(inst.context_window)} ({pct:.1f}%){cost_str}"


# ── Auto-detect media files in responses ──────────────────────────
_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tiff'}
_VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.webm', '.avi'}
_MEDIA_PATH_RE = re.compile(
    r'((?:[A-Za-z]:[/\\]|[/\\]{2}|/|~/)[^\s"\'`\)\]>]+\.(?:png|jpg|jpeg|gif|webp|bmp|tiff|mp4|mov|mkv|webm|avi))',
    re.IGNORECASE,
)


async def _extract_and_send_media(chat_id: int, text: str) -> list[str]:
    """Find image/video file paths in response text and send them via Telegram."""
    sent = []
    seen = set()
    for raw_path in _MEDIA_PATH_RE.findall(text):
        path = os.path.expanduser(raw_path)
        if path in seen:
            continue
        seen.add(path)
        if not os.path.isfile(path) or os.path.getsize(path) < 1024:
            continue
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in _VIDEO_EXTS:
                success = await send_video(chat_id, path)
            else:
                success = await send_photo(chat_id, path)
            if success:
                sent.append(path)
                logger.info("Auto-sent media from response: %s", path)
        except Exception as e:
            logger.error("Failed to auto-send media %s: %s", path, e)
    return sent


async def _process_message(chat_id: int, text: str, voice_reply: bool = False, instance=None, user_id: int = 0) -> None:
    # Check if the user is currently in a borrow session — proxy their message to the peer
    if COLLAB_ENABLED and collab_borrow is not None:
        borrow_info = collab_borrow.is_borrowing(chat_id)
        if borrow_info:
            try:
                peers = load_peers() if load_peers else {}
                peer = peers.get(borrow_info.peer_name)
                if peer and collab_borrow_message is not None:
                    response = await collab_borrow_message(peer, borrow_info.session_id, text)
                    labeled = f"[{borrow_info.label}]\n{response}"
                    await send_message(chat_id, labeled, format_markdown=True)
                    return
                else:
                    await send_message(chat_id, f"Borrow session error: peer '{borrow_info.peer_name}' not found. Use /return to disconnect.")
                    return
            except Exception as e:
                logger.error(f"Borrow proxy error: {e}")
                await send_message(chat_id, f"Borrow session error: {e}. Use /return to disconnect.")
                return

    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    thinking_msg_id = await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id, show_emoji=False), format_markdown=True)

    start = time.time()

    # Agent-aware memory: agents only get their own domain memory, not personal files or general ChromaDB
    if inst.agent_id:
        from agent_memory import get_agent_context
        memory_context = await asyncio.get_event_loop().run_in_executor(
            None, get_agent_context, inst.agent_id, text
        )
    else:
        memory_context = await memory_handler.search_memory(text, user_id=user_id)

    _prefs = display_prefs.get_display_prefs(user_id)

    async def on_progress(progress_text: str):
        if progress_text.startswith("<blockquote"):
            if not _prefs["show_thoughts"]:
                return  # user doesn't want to see thoughts
            # HTML thinking block — send with HTML parse mode, minimal instance label
            inst_label = f"[#{instances.display_num(inst.id, proc_owner_id)}: {inst.title}] " if len(instances.list_all(for_owner_id=proc_owner_id)) >= 2 else ""
            await send_message(chat_id, f"{inst_label}{progress_text}", parse_mode="HTML")
        else:
            if not _prefs["show_tools"]:
                return  # user doesn't want to see tool indicators
            await send_message(chat_id, _label(inst, progress_text, proc_owner_id, show_emoji=False), format_markdown=True)

    # --- Session store: mark this instance as actively processing ---
    if inst.needs_recovery:
        # Recovery message: keep original_prompt intact, just update session_id
        inst.needs_recovery = False
        _session_store.upsert_session(
            chat_id, CLI_RUNNER, inst.id,
            session_id=inst.session_id,
            status="unresolved",
        )
    else:
        _session_store.mark_unresolved(
            chat_id, CLI_RUNNER, inst.id,
            original_prompt=text,
            session_id=inst.session_id,
            title=inst.title,
        )
    _session_store.log_message(chat_id, CLI_RUNNER, inst.id, "user", text)

    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prefixed_text = f"[{sender_name}]: {text}" if sender_name else text

    def _on_subprocess_started(pid: int, log_file: str, start_time: str) -> None:
        _session_store.set_subprocess(chat_id, CLI_RUNNER, inst.id, pid, log_file, start_time)

    response = await runner.run(
        prefixed_text,
        on_progress=on_progress,
        memory_context=memory_context,
        instance=inst,
        on_subprocess_started=_on_subprocess_started,
        chat_id=chat_id,
    )
    elapsed = time.time() - start

    # --- Session store: clear subprocess tracking and log response ---
    _session_store.clear_subprocess(chat_id, CLI_RUNNER, inst.id)
    _session_store.log_message(chat_id, CLI_RUNNER, inst.id, "assistant", response)
    _session_store.update_session_id(chat_id, CLI_RUNNER, inst.id, inst.session_id)

    if thinking_msg_id:
        await delete_message(chat_id, thinking_msg_id)

    if not response or not response.strip():
        response = f"(no text response from {BOT_NAME} — check tool output)"

    logger.info("%s #%d responded in %.1fs (%d chars)", BOT_NAME, inst.id, elapsed, len(response))

    # Store in agent memory if this is a specialist agent, then run background self-critique
    if inst.agent_id:
        from agent_memory import store_agent_work
        asyncio.ensure_future(
            asyncio.get_event_loop().run_in_executor(None, store_agent_work, inst.agent_id, text, response)
        )
        asyncio.ensure_future(
            agent_manager._run_post_task_critique(
                inst.agent_id, text, response, chat_id, send_message, instances=instances
            )
        )

    # Store memory before appending footer
    asyncio.ensure_future(memory_handler.store_conversation(text, response, user_id=user_id))
    asyncio.ensure_future(memory_handler.extract_and_save(text, response, user_id=user_id))

    response += _context_footer(inst)
    labeled = _label(inst, response, proc_owner_id)

    if voice_reply:
        await _send_with_voice(chat_id, labeled)
    else:
        await send_message(chat_id, labeled, format_markdown=True)

    # Mark resolved AFTER delivery — if crash happens before this line,
    # session stays unresolved and recovery will re-run the task on restart.
    _session_store.mark_resolved(chat_id, CLI_RUNNER, inst.id)

    # Auto-detect and send any media files referenced in the response
    await _extract_and_send_media(chat_id, response)

    # Log to daily task report
    daily_report.log_task("Claude", text, response)


async def _handle_document_upload(chat_id: int, file_id: str, dest_path: str, file_name: str) -> None:
    """Download a document from Telegram and save it to the user's folder."""
    try:
        await send_message(chat_id, f"📥 Downloading {file_name}...")
        await download_document(file_id, dest_path)
        await send_message(chat_id, f"✅ Saved to: {dest_path}")
    except Exception as e:
        logger.error("Document download failed: %s", e)
        await send_message(chat_id, f"❌ Failed to save {file_name}: {e}")


async def _process_photo_message(chat_id: int, file_id: str, caption: str = "", instance=None, user_id: int = 0) -> None:
    """Handle an incoming photo: download, send to Claude for vision analysis."""
    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    await send_message(chat_id, _label(inst, "Downloading image...", proc_owner_id), format_markdown=True)

    image_path = None
    try:
        image_path = await download_photo(file_id)
    except Exception as e:
        logger.error("Photo download failed: %s", e)
        await send_message(chat_id, _label(inst, f"\u274c Failed to download photo: {e}", proc_owner_id), format_markdown=True)
        return

    thinking_msg_id = await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id, show_emoji=False), format_markdown=True)

    start = time.time()

    _prefs = display_prefs.get_display_prefs(user_id)

    async def on_progress(progress_text: str):
        if progress_text.startswith("<blockquote"):
            if not _prefs["show_thoughts"]:
                return  # user doesn't want to see thoughts
            inst_label = f"[#{instances.display_num(inst.id, proc_owner_id)}: {inst.title}] " if len(instances.list_all(for_owner_id=proc_owner_id)) >= 2 else ""
            await send_message(chat_id, f"{inst_label}{progress_text}", parse_mode="HTML")
        else:
            if not _prefs["show_tools"]:
                return  # user doesn't want to see tool indicators
            await send_message(chat_id, _label(inst, progress_text, proc_owner_id, show_emoji=False), format_markdown=True)

    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prefixed_caption = f"[{sender_name}]: {caption}" if sender_name else caption
    response = await runner.run(prefixed_caption, on_progress=on_progress, image_path=image_path, instance=inst)
    elapsed = time.time() - start

    logger.info("%s #%d responded to photo in %.1fs (%d chars)", BOT_NAME, inst.id, elapsed, len(response))
    response += _context_footer(inst)
    if thinking_msg_id:
        await delete_message(chat_id, thinking_msg_id)
    await send_message(chat_id, _label(inst, response, proc_owner_id), format_markdown=True)

    # Clean up temp image
    if image_path:
        try:
            os.remove(image_path)
        except OSError:
            pass


async def _process_voice_message(chat_id: int, file_id: str, caption: str = "", instance=None, user_id: int = 0) -> None:
    """Handle an incoming voice/audio message: download, transcribe, process, reply with voice."""
    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    # Step 1: Download and transcribe
    await send_chat_action(chat_id, "typing")
    await send_message(chat_id, _label(inst, "\U0001f3a4 Transcribing voice...", proc_owner_id), format_markdown=True)

    voice_path = None
    try:
        voice_path = await download_voice(file_id)
        transcribed = await transcribe_audio(voice_path)
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await send_message(chat_id, _label(inst, f"\u274c Failed to transcribe voice: {e}", proc_owner_id), format_markdown=True)
        return
    finally:
        if voice_path:
            cleanup_file(voice_path)

    if not transcribed.strip():
        await send_message(chat_id, _label(inst, "\U0001f937 Couldn't understand the voice message.", proc_owner_id), format_markdown=True)
        return

    # Combine caption with transcribed text if present
    raw_prompt = f"{caption}\n\n{transcribed}" if caption else transcribed
    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prompt = f"[{sender_name}]: {raw_prompt}" if sender_name else raw_prompt

    # Show what was transcribed
    await send_message(chat_id, _label(inst, f"\U0001f4dd \"{transcribed}\"", proc_owner_id), format_markdown=True)
    thinking_msg_id = await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id, show_emoji=False), format_markdown=True)

    start = time.time()

    memory_context = await memory_handler.search_memory(raw_prompt, user_id=user_id)

    _prefs = display_prefs.get_display_prefs(user_id)

    async def on_progress(progress_text: str):
        if progress_text.startswith("<blockquote"):
            if not _prefs["show_thoughts"]:
                return  # user doesn't want to see thoughts
            inst_label = f"[#{instances.display_num(inst.id, proc_owner_id)}: {inst.title}] " if len(instances.list_all(for_owner_id=proc_owner_id)) >= 2 else ""
            await send_message(chat_id, f"{inst_label}{progress_text}", parse_mode="HTML")
        else:
            if not _prefs["show_tools"]:
                return  # user doesn't want to see tool indicators
            await send_message(chat_id, _label(inst, progress_text, proc_owner_id, show_emoji=False), format_markdown=True)

    response = await runner.run(prompt, on_progress=on_progress, memory_context=memory_context, instance=inst)
    elapsed = time.time() - start

    if thinking_msg_id:
        await delete_message(chat_id, thinking_msg_id)

    if not response or not response.strip():
        response = f"(no text response from {BOT_NAME} — check tool output)"

    logger.info("%s #%d responded in %.1fs (%d chars)", BOT_NAME, inst.id, elapsed, len(response))

    # Store memory before appending footer
    asyncio.ensure_future(memory_handler.store_conversation(raw_prompt, response, user_id=user_id))
    asyncio.ensure_future(memory_handler.extract_and_save(raw_prompt, response, user_id=user_id))

    # Voice in -> voice + text out
    response += _context_footer(inst)
    await _send_with_voice(chat_id, _label(inst, response, proc_owner_id))

    # Auto-detect and send any media files referenced in the response
    await _extract_and_send_media(chat_id, response)



async def _process_image_generation(chat_id: int, prompt: str) -> None:
    """Generate an image using Gemini and send it to the user."""
    await send_message(chat_id, "\U0001f3a8 Generating image...")

    image_path = None
    try:
        image_path, description = await generate_image(prompt)
        caption = description[:1024] if description else None
        sent = await send_photo(chat_id, image_path, caption=caption)
        if not sent:
            await send_message(chat_id, "\u274c Failed to send the generated image.")
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        await send_message(chat_id, f"\u274c Image generation failed: {e}")
    finally:
        if image_path:
            try:
                os.remove(image_path)
            except OSError:
                pass


async def _process_screenshot(chat_id: int, url: str) -> None:
    """Take a Playwright screenshot of url and send it as a photo."""
    await send_message(chat_id, f"\U0001f4f8 Taking screenshot of {url}...")
    png_path = None
    try:
        png_path = await playwright_handler.screenshot(url)
        sent = await send_photo(chat_id, png_path, caption=url[:200])
        if not sent:
            await send_message(chat_id, "\u274c Failed to send screenshot.")
    except Exception as e:
        logger.error("Screenshot failed for %s: %s", url, e)
        await send_message(chat_id, f"\u274c Screenshot failed: {e}")
    finally:
        if png_path:
            try:
                os.remove(png_path)
            except OSError:
                pass


async def _process_browse(chat_id: int, url: str) -> None:
    """Fetch readable text from url using Playwright and send as message."""
    await send_message(chat_id, f"\U0001f310 Fetching {url}...")
    try:
        text = await playwright_handler.get_page_text(url)
        if not text.strip():
            await send_message(chat_id, "\u274c Page returned no readable text.")
        else:
            await send_message(chat_id, text, format_markdown=True)
    except Exception as e:
        logger.error("Browse failed for %s: %s", url, e)
        await send_message(chat_id, f"\u274c Browse failed: {e}")


async def _send_with_voice(chat_id: int, response: str) -> None:
    """Send a response as both voice and text. Falls back to text-only if TTS fails or text is too long."""
    # Always send text version
    await send_message(chat_id, response, format_markdown=True)

    # Generate and send voice if response isn't too long
    if len(response) > VOICE_MAX_LENGTH:
        logger.info("Response too long for TTS (%d chars > %d), text only", len(response), VOICE_MAX_LENGTH)
        return

    ogg_path = None
    try:
        await send_chat_action(chat_id, "record_voice")
        ogg_path = await text_to_speech(response)
        await send_voice(chat_id, ogg_path)
    except Exception as e:
        logger.error("TTS failed, text-only fallback: %s", e)
    finally:
        if ogg_path:
            cleanup_file(ogg_path)


async def _delayed_restart() -> None:
    """Wait briefly so the webhook response reaches Telegram, then restart.

    We deliberately skip runner.kill_all() and close_client() here — os.execv
    replaces the process instantly, so all tasks and connections die with it.
    Calling kill_all first would let worker tasks race to mark_resolved(),
    preventing crash recovery from resuming in-flight sessions.
    """
    await asyncio.sleep(1)
    # Remove shutdown_clean flag so the new boot detects a "crash" and runs
    # _restore_sessions_after_crash() to resume in-flight work.
    try:
        os.remove(_SHUTDOWN_FLAG)
    except OSError:
        pass
    logger.info("Server restart requested via /server")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", HOST, "--port", str(PORT)],
    )


async def _handle_command(chat_id: int, text: str, user_id: int = 0) -> None:
    cmd = text.split()[0].lower()
    # owner_id=0 means primary user pool; non-zero means that user's own pool
    owner_id = 0 if user_id == ALLOWED_USER_ID else user_id

    if cmd == "/stop":
        inst = instances.get_active_for(owner_id)
        # Clear this instance's queue
        cleared = inst.clear_queue()
        # Stop this instance's Claude process (the worker will see was_stopped
        # and gracefully move to the next queued item)
        stopped = await runner.stop(inst)
        # Only cancel task if there's no process to kill (e.g. stuck on send_message)
        task_cancelled = False
        if not stopped and inst.current_task and not inst.current_task.done():
            inst.current_task.cancel()
            task_cancelled = True
        # Mark resolved so a future restart doesn't try to resume this task
        _session_store.mark_resolved(chat_id, CLI_RUNNER, inst.id)

        label = f" [#{instances.display_num(inst.id, owner_id)}: {inst.title}]" if len(instances.list_all(for_owner_id=owner_id)) >= 2 else ""
        parts = []
        if stopped or task_cancelled:
            parts.append("Stopped current task.")
        if cleared:
            parts.append(f"Cleared {cleared} queued message{'s' if cleared != 1 else ''}.")
        if parts:
            await send_message(chat_id, f"\U0001f6d1 " + " ".join(parts) + label)
        else:
            await send_message(chat_id, f"Nothing running and queue is empty.{label}")

    elif cmd == "/kill":
        # Nuclear option: kill everything across all instances
        for inst in instances.list_all():
            inst.clear_queue()
            if inst.current_task and not inst.current_task.done():
                inst.current_task.cancel()
        await runner.stop_all(instances.list_all())
        await runner.kill_all()
        await send_message(chat_id, "\U0001f480 Killed all Claude processes. All queues cleared.")

    elif cmd in ("/show", "/hide"):
        sub = text.split(maxsplit=1)[1].lower().strip() if len(text.split()) > 1 else ""
        if sub == "code":
            prefs = display_prefs.set_display_prefs(user_id, show_tools=(cmd == "/show"))
            await send_message(chat_id, "Tool indicators on \u26a1" if prefs["show_tools"] else "Tool indicators off")
        elif sub == "thoughts":
            prefs = display_prefs.set_display_prefs(user_id, show_thoughts=(cmd == "/show"))
            await send_message(chat_id, "Thinking blocks on \U0001f4ad" if prefs["show_thoughts"] else "Thinking blocks off")
        elif sub == "both":
            val = (cmd == "/show")
            display_prefs.set_display_prefs(user_id, show_tools=val, show_thoughts=val)
            await send_message(chat_id, "Showing everything \u26a1\U0001f4ad" if val else "Clean output \u2014 just final answers")
        else:
            await send_message(chat_id, f"Usage: {cmd} code | thoughts | both")

    elif cmd == "/new":
        inst = instances.get_active_for(owner_id)
        inst.clear_queue()
        await runner.stop(inst)
        if inst.current_task and not inst.current_task.done():
            inst.current_task.cancel()
        runner.new_session(inst)
        # Mark resolved — starting fresh, no recovery needed on next restart
        _session_store.mark_resolved(chat_id, CLI_RUNNER, inst.id)
        label = f" [#{instances.display_num(inst.id, owner_id)}: {inst.title}]" if len(instances.list_all(for_owner_id=owner_id)) >= 2 else ""
        await send_message(chat_id, f"\U0001f195 New conversation started. Queue cleared.{label}")

    elif cmd == "/server":
        await send_message(chat_id, "\U0001f504 Restarting server...")
        # Delay restart so the webhook can return 200 to Telegram first,
        # otherwise Telegram retries the update and causes a restart loop.
        asyncio.create_task(_delayed_restart())

    elif cmd == "/help":
        active = instances.get_active_for(owner_id)
        user_inst_count = len(instances.list_all(for_owner_id=owner_id))
        inst_info = f"Active: #{instances.display_num(active.id, owner_id)} ({active.title})" if user_inst_count >= 2 else "1 instance running"
        help_text = (
            "**Commands:**\n\n"
            "**Control**\n"
            "/stop \u2014 Stop current task & clear queue\n"
            "/kill \u2014 Force-kill all processes across all instances\n"
            "/new \u2014 Reset conversation for the active instance\n"
            "/server \u2014 Restart bridge server\n"
            f"/model sonnet|opus \u2014 Switch model [{(active.model.split('-')[1] if '-' in active.model else active.model).capitalize()}]\n\n"
            "**Display**\n"
            "/show code | thoughts | both\n"
            "/hide code | thoughts | both\n\n"
            "**Instances**\n"
            f"_{inst_info}_\n"
            "/inst new <title> \u2014 New independent session\n"
            "/inst list \u2014 Show all instances\n"
            "/inst switch <id/title> [new_title] \u2014 Switch/create/rename\n"
            "/inst rename <id> <title>\n"
            "/inst end <id>\n"
            "/inst clear \u2014 Kill all, reset to one Default\n"
            "_`@<id or name> <msg>` \u2014 One-shot message to any instance_\n\n"
            "**Agents**\n"
            "/agent talk <name> \u2014 Switch to an agent\n"
            "/agent back \u2014 Return to default\n"
            "/agent pipeline <a> → <b> \"task\" \u2014 Sequential pipeline\n"
            "/agent proactive start|stop|list\n"
            "/agent proactive <name> set <schedule> <task>\n"
            "/agent proactive <name> on|off|clear\n\n"
            "**Triggers**\n"
            "/trigger run <id> \u2014 Fire a trigger manually\n\n"
            "**Orchestration**\n"
            "/orch <task> \u2014 Parallel agents, synthesized result\n\n"
            "**Recording**\n"
            "/record \u2014 Start screen recording\n"
            "/stoprecord \u2014 Stop and send video\n\n"
            "**Collab**\n"
            "/collab ask <peer> <task>\n"
            "/collab broadcast <msg>\n"
            "/borrow <peer> [bot] \u2014 Route messages to peer's bot\n"
            "/return \u2014 Disconnect from borrowed bot\n\n"
            "/help \u2014 Show this\n\n"
            "_Any unrecognized /command is forwarded to the active runner (Claude skills, etc.)_"
        )
        await send_message(chat_id, help_text, format_markdown=True)

    elif cmd == "/record":
        if screen_recorder.is_recording():
            await send_message(chat_id, f"Already recording. {screen_recorder.status()}\nUse /stoprecord to stop.")
        else:
            path = screen_recorder.start()
            if path:
                await send_message(chat_id, f"\U0001f534 Screen recording started (max {screen_recorder.MAX_DURATION}s).\nUse /stoprecord to stop and receive the video.")
            else:
                await send_message(chat_id, "\u274c Failed to start screen recording. Is ffmpeg installed?")

    elif cmd == "/stoprecord":
        if not screen_recorder.is_recording():
            await send_message(chat_id, "No recording in progress.")
        else:
            await send_message(chat_id, "\u23f9 Stopping recording...")
            video_path = screen_recorder.stop()
            if video_path:
                size_mb = os.path.getsize(video_path) / (1024 * 1024)
                if size_mb > 50:
                    await send_message(chat_id, f"\u26a0\ufe0f Recording is {size_mb:.1f}MB (Telegram limit is 50MB). File saved at: {video_path}")
                else:
                    sent = await send_video(chat_id, video_path, caption="Screen recording")
                    if sent:
                        try:
                            os.remove(video_path)
                        except OSError:
                            pass
                    else:
                        await send_message(chat_id, f"\u274c Failed to send video. File saved at: {video_path}")
            else:
                await send_message(chat_id, "\u274c Recording file was empty or missing.")


    elif cmd == "/model":
        parts = text.split()
        if len(parts) < 2:
            inst = instances.get_active_for(owner_id)
            await send_message(chat_id, f"Current model for <b>#{instances.display_num(inst.id, owner_id)}</b>: <code>{inst.model}</code>\n\nUsage: /model [sonnet|opus]", parse_mode="HTML")
        else:
            m = parts[1].lower()
            new_model = None
            if "sonnet" in m:
                new_model = "claude-sonnet-4-6"
            elif "opus" in m:
                new_model = "claude-opus-4-6"

            if new_model:
                inst = instances.get_active_for(owner_id)
                inst.model = new_model
                await send_message(chat_id, f"\u2705 Model for <b>#{instances.display_num(inst.id, owner_id)}</b> set to <code>{new_model}</code>", parse_mode="HTML")
            else:
                await send_message(chat_id, "\u274c Invalid model. Choose 'sonnet' or 'opus'.")

    elif cmd == "/inst":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "new":
            title = arg or "Untitled"
            inst = instances.create(title, owner_id=owner_id)
            _ensure_worker(inst)
            await send_message(
                chat_id,
                f"\u2728 Created instance <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b> (now active)",
                parse_mode="HTML",
            )

        elif sub == "list":
            await send_message(chat_id, instances.format_list(for_owner_id=owner_id), parse_mode="HTML")

        elif sub == "switch":
            if not arg:
                await send_message(chat_id, "Usage: /inst switch <id/title> [new_title]")
            else:
                # Handle potential rename: /inst switch <id/title> <new_title>
                switch_parts = arg.split(maxsplit=1)
                target = switch_parts[0]
                new_title = switch_parts[1] if len(switch_parts) > 1 else None

                inst = instances.switch(target, owner_id=owner_id)
                if inst:
                    if new_title:
                        instances.rename(inst.id, new_title, owner_id=owner_id)
                        await send_message(chat_id, f"\u25b6 Switched to and renamed <b>#{instances.display_num(inst.id, owner_id)}: {new_title}</b>", parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"\u25b6 Switched to <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b>", parse_mode="HTML")
                    _ensure_worker(inst)
                else:
                    # Not found — create new with the whole 'arg' as title
                    new_inst = instances.create(arg, owner_id=owner_id)
                    _ensure_worker(new_inst)
                    await send_message(
                        chat_id,
                        f"\u2728 Created and switched to <b>#{instances.display_num(new_inst.id, owner_id)}: {new_inst.title}</b>",
                        parse_mode="HTML",
                    )

        elif sub == "rename":
            rename_parts = arg.split(maxsplit=1)
            if len(rename_parts) < 2 or not rename_parts[0].isdigit():
                await send_message(chat_id, "Usage: /inst rename <id> <new title>")
            else:
                disp_num = int(rename_parts[0])
                new_title = rename_parts[1]
                target_inst = instances.get_by_display_num(disp_num, owner_id)
                if target_inst and instances.rename(target_inst.id, new_title, owner_id=owner_id):
                    await send_message(chat_id, f"\u270f\ufe0f Renamed #{disp_num} to <b>{new_title}</b>", parse_mode="HTML")
                else:
                    await send_message(chat_id, f"No instance #{disp_num}. Try /inst list")

        elif sub == "end":
            if not arg or not arg.isdigit():
                await send_message(chat_id, "Usage: /inst end <id>")
            else:
                disp_num = int(arg)
                inst_to_end = instances.get_by_display_num(disp_num, owner_id)
                if inst_to_end:
                    await runner.stop(inst_to_end)
                    inst_to_end.clear_queue()

                removed = instances.remove(inst_to_end.id if inst_to_end else -1, owner_id=owner_id)
                if removed:
                    new_active = instances.get_active_for(owner_id)
                    await send_message(
                        chat_id,
                        f"\U0001f5d1 Ended <b>#{disp_num}: {removed.title}</b>\n"
                        f"Active: #{instances.display_num(new_active.id, owner_id)}: {new_active.title}",
                        parse_mode="HTML",
                    )
                else:
                    owner_inst_count = len(instances.list_all(for_owner_id=owner_id))
                    if inst_to_end and owner_inst_count <= 1:
                        await send_message(chat_id, "Can't end the last instance.")
                    else:
                        await send_message(chat_id, f"No instance #{disp_num}. Try /inst list")

        elif sub == "clear":
            # Kill all processes, remove all instances, start fresh with one Default
            all_insts = instances.list_all(for_owner_id=owner_id)
            count = len(all_insts)
            for inst in all_insts:
                await runner.stop(inst)
                inst.clear_queue()
                if inst.current_task and not inst.current_task.done():
                    inst.current_task.cancel()
            # Remove all except keep one to satisfy the "can't remove last" guard,
            # then rename it to Default and reset its session
            for inst in all_insts[1:]:
                instances.remove(inst.id, owner_id=owner_id)
            surviving = instances.list_all(for_owner_id=owner_id)[0]
            instances.rename(surviving.id, "Default", owner_id=owner_id)
            instances.set_active_for(owner_id, surviving.id)
            runner.new_session(surviving)
            _session_store.mark_resolved(chat_id, CLI_RUNNER, surviving.id)
            await send_message(
                chat_id,
                f"\U0001f9f9 Cleared {count} instance{'s' if count != 1 else ''}. Back to a single Default.",
            )

        else:
            inst = instances.get_active_for(owner_id)
            await send_message(
                chat_id,
                f"Active: <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b>\n\n"
                f"Commands:\n"
                f"/inst new &lt;title&gt; \u2014 New instance\n"
                f"/inst list \u2014 Show all instances\n"
                f"/inst switch &lt;id/title&gt; [new_title] \u2014 Switch/Create/Rename\n"
                f"/inst rename &lt;id&gt; &lt;title&gt; \u2014 Rename\n"
                f"/inst end &lt;id&gt; \u2014 End instance\n"
                f"/inst clear \u2014 Kill all, reset to one Default",
                parse_mode="HTML",
            )

    elif cmd == "/agent":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub in ("talk", "switch"):
            if not arg:
                await send_message(chat_id, "Usage: /agent talk &lt;agent name or id&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(arg)
                if target is None:
                    await send_message(chat_id, f"Agent '{arg}' not found. Try /agent list")
                else:
                    inst = agent_manager.talk_to_agent(target.id, instances, owner_id)
                    if inst:
                        await send_message(chat_id,
                            f"Switched to <b>{target.name}</b>\n"
                            f"You're now talking directly to this agent. "
                            f"Use /agent talk Default or /new to go back.",
                            parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"Failed to spawn {target.name}.")

        elif sub == "back":
            # Switch back to the first non-agent instance (Default)
            default_inst = None
            for inst in instances.list_all(for_owner_id=owner_id):
                if not inst.agent_id:
                    default_inst = inst
                    break
            if default_inst:
                instances.set_active_for(owner_id, default_inst.id)
                await send_message(chat_id, f"Switched back to <b>{default_inst.title}</b>", parse_mode="HTML")
            else:
                await send_message(chat_id, "No default instance found.")

        elif sub == "pipeline":
            # /agent pipeline Research → Analytics "task"
            if not arg:
                await send_message(chat_id,
                    "Usage: /agent pipeline &lt;agent1&gt; → &lt;agent2&gt; \"task\"\n"
                    "Example: /agent pipeline research → analytics \"AI funding trends\"",
                    parse_mode="HTML")
            else:
                agent_ids, task_desc = agent_manager.parse_pipeline_command(arg)
                if len(agent_ids) < 2 or not task_desc:
                    await send_message(chat_id,
                        "Need at least 2 agents and a quoted task.\n"
                        "Example: /agent pipeline research → analytics \"AI funding trends\"")
                else:
                    async def _run_pipeline():
                        result = await agent_manager.run_pipeline(
                            agent_ids, task_desc, chat_id, instances, send_message, owner_id
                        )
                        await send_message(chat_id, result, format_markdown=True)
                    asyncio.create_task(_run_pipeline())

        elif sub == "proactive":
            # /agent proactive list
            # /agent proactive status
            # /agent proactive <name> on
            # /agent proactive <name> off
            # /agent proactive <name> set <HH:MM> <task>
            # /agent proactive <name> clear
            if not arg or arg.strip() in ("list", "status"):
                running = proactive_worker.is_running()
                worker_status = "🟢 Worker running" if running else "🔴 Worker stopped — use /agent proactive start"
                await send_message(chat_id, f"{worker_status}\n\n{proactive_worker.status()}", parse_mode="HTML")
            elif arg.strip() == "start":
                if proactive_worker.is_running():
                    await send_message(chat_id, "Proactive worker is already running.")
                else:
                    await proactive_worker.start(instances, send_message, chat_id)
                    await send_message(chat_id, "🟢 Proactive worker started. Agents with a schedule will fire automatically.")
            elif arg.strip() == "stop":
                if not proactive_worker.is_running():
                    await send_message(chat_id, "Proactive worker is not running.")
                else:
                    await proactive_worker.stop()
                    await send_message(chat_id, "🔴 Proactive worker stopped. No agents will fire until you restart it.")
            else:
                parts = arg.split(maxsplit=2)
                if len(parts) < 2:
                    await send_message(chat_id,
                        "Usage:\n"
                        "/agent proactive start — start the worker\n"
                        "/agent proactive stop — stop the worker\n"
                        "/agent proactive list — show configured agents\n"
                        "/agent proactive &lt;name&gt; set &lt;HH:MM&gt; &lt;task&gt; — configure\n"
                        "/agent proactive &lt;name&gt; on/off — toggle\n"
                        "/agent proactive &lt;name&gt; clear — wipe config",
                        parse_mode="HTML")
                else:
                    target = resolve_agent(parts[0])
                    if target is None:
                        await send_message(chat_id, f"Agent '{parts[0]}' not found. Try /agent list")
                    else:
                        action = parts[1].lower()
                        if action == "on":
                            msg = agent_manager.configure_proactive(target.id, enabled=True,
                                schedule=target.proactive_schedule, task=target.proactive_task)
                            await send_message(chat_id, msg, parse_mode="HTML")
                        elif action == "off":
                            msg = agent_manager.configure_proactive(target.id, enabled=False)
                            await send_message(chat_id, msg, parse_mode="HTML")
                        elif action == "clear":
                            msg = agent_manager.clear_proactive(target.id)
                            await send_message(chat_id, msg, parse_mode="HTML")
                        elif action == "set":
                            if len(parts) < 3:
                                await send_message(chat_id,
                                    "Usage: /agent proactive &lt;name&gt; set &lt;schedule&gt; &lt;task&gt;\n\n"
                                    "Schedule formats:\n"
                                    "  <code>09:00</code> — daily at 9am NYC\n"
                                    "  <code>every 2h</code> — every 2 hours\n"
                                    "  <code>every 30m</code> — every 30 minutes\n"
                                    "  <code>every 1h30m</code> — every 1.5 hours\n\n"
                                    "Example:\n"
                                    "<code>/agent proactive research set 09:00 summarize top AI news</code>\n"
                                    "<code>/agent proactive research set every 2h check for trending topics</code>",
                                    parse_mode="HTML")
                            else:
                                # Schedule is first token (may be "every 2h" = 2 tokens)
                                remainder = parts[2]
                                # Try "every Xh/Xm" (2-word schedule) first
                                every_match = re.match(r"^(every\s+\S+)\s+(.+)$", remainder, re.IGNORECASE)
                                if every_match:
                                    sched, task_desc = every_match.group(1), every_match.group(2)
                                else:
                                    set_parts = remainder.split(maxsplit=1)
                                    if len(set_parts) < 2:
                                        await send_message(chat_id,
                                            "Need both a schedule and a task description.",
                                            parse_mode="HTML")
                                    else:
                                        sched, task_desc = set_parts[0], set_parts[1]
                                        msg = agent_manager.configure_proactive(
                                            target.id, enabled=True, schedule=sched, task=task_desc)
                                        await send_message(chat_id, msg, parse_mode="HTML")
                                if every_match:
                                    msg = agent_manager.configure_proactive(
                                        target.id, enabled=True, schedule=sched, task=task_desc)
                                    await send_message(chat_id, msg, parse_mode="HTML")
                        else:
                            await send_message(chat_id,
                                f"Unknown action '{action}'. Use: on, off, set, clear",
                                parse_mode="HTML")

        else:
            # Default: show agent help
            active_inst = instances.get_active_for(owner_id)
            agent_label = ""
            if active_inst.agent_id:
                active_agent = get_agent(active_inst.agent_id)
                if active_agent:
                    agent_label = f"\nTalking to: <b>{active_agent.name}</b>"

            await send_message(chat_id,
                f"<b>Agent Commands</b>{agent_label}\n\n"
                "<b>/agent talk &lt;name&gt;</b> — Switch to an agent\n"
                "<b>/agent back</b> — Return to default instance\n"
                "<b>/agent pipeline &lt;a&gt; → &lt;b&gt; \"task\"</b> — Sequential pipeline\n"
                "<b>/agent proactive start/stop/list</b> — Manage proactive worker\n"
                "<b>/agent proactive &lt;name&gt; set &lt;schedule&gt; &lt;task&gt;</b> — Configure\n"
                "<b>/agent proactive &lt;name&gt; on/off/clear</b> — Toggle or wipe\n\n"
                "To create/edit/delete agents, just tell me what you need.",
                parse_mode="HTML")

    elif cmd == "/orch":
        task = text[len("/orch"):].strip()
        if not task:
            await send_message(
                chat_id,
                "Usage: /orch <complex task description>\n\n"
                "Breaks the task into 2-4 parallel sub-tasks, spins up a Claude agent for each, "
                "runs them concurrently, then synthesizes all results into one response."
            )
        else:
            async def _run_orch():
                result = await task_orchestrator.orchestrate(
                    task, chat_id, instances, send_message
                )
                await send_message(chat_id, result, format_markdown=True)
            asyncio.create_task(_run_orch())

    elif cmd == "/collab":
        if not COLLAB_ENABLED:
            await send_message(chat_id, "Collab is disabled. Set COLLAB_ENABLED=true to enable.")
            return

        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        try:
            from collab.config import load_peers, add_peer, remove_peer, COLLAB_INSTANCE_NAME
            from collab import client as collab_client
            from collab.feed import get_feed
            import secrets as _secrets
        except Exception as _e:
            await send_message(chat_id, f"Collab module error: {_e}")
            return

        if sub == "ask":
            # /collab ask <peer> <task>
            ask_parts = arg.split(maxsplit=1)
            if len(ask_parts) < 2:
                await send_message(chat_id, "Usage: /collab ask &lt;peer&gt; &lt;task&gt;", parse_mode="HTML")
                return
            target_peer_name, task = ask_parts[0], ask_parts[1]
            peers = load_peers()
            if target_peer_name not in peers:
                await send_message(chat_id, f"Peer '{target_peer_name}' not found. Use /collab peers to see available peers.")
                return
            await send_message(chat_id, f"Delegating task to {target_peer_name}...")
            result = await collab_client.delegate_task(peers[target_peer_name], task)
            await send_message(
                chat_id,
                f"<b>Response from {target_peer_name}:</b>\n\n{result}",
                parse_mode="HTML",
                format_markdown=True,
            )

        elif sub == "broadcast":
            if not arg:
                await send_message(chat_id, "Usage: /collab broadcast &lt;message&gt;", parse_mode="HTML")
                return
            peers = load_peers()
            if not peers:
                await send_message(chat_id, "No peers configured.")
                return
            sent = 0
            failed = 0
            for peer_name, peer in peers.items():
                ok = await collab_client.broadcast_to_peer(peer, arg, from_name=COLLAB_INSTANCE_NAME)
                if ok:
                    sent += 1
                else:
                    failed += 1
            await send_message(
                chat_id,
                f"Broadcast sent to {sent} peer(s)." + (f" {failed} failed." if failed else ""),
            )

        else:
            await send_message(chat_id,
                "<b>/collab ask &lt;peer&gt; &lt;task&gt;</b> — Delegate to a peer\n"
                "<b>/collab broadcast &lt;msg&gt;</b> — Send to all peers",
                parse_mode="HTML")

    elif cmd == "/borrow":
        if not COLLAB_ENABLED or collab_borrow is None:
            await send_message(chat_id, "Collab is disabled. Set COLLAB_ENABLED=true to enable.")
            return

        _borrow_args = text[len("/borrow"):].strip()
        parts = _borrow_args.split() if _borrow_args else []
        if not parts:
            await send_message(
                chat_id,
                "<b>Usage:</b> /borrow &lt;peer&gt; [bot]\n"
                "Example: /borrow diony\n"
                "Example: /borrow diony gemini\n\n"
                "Use /return to disconnect.",
                parse_mode="HTML",
            )
            return

        peer_name = parts[0]
        bot = parts[1] if len(parts) > 1 else None

        # Check if already borrowing
        if collab_borrow.is_borrowing(chat_id):
            await send_message(chat_id, "You're already borrowing a bot. Use /return first.")
            return

        # Look up peer
        peers = load_peers() if load_peers else {}
        if peer_name not in peers:
            await send_message(chat_id, f"Peer '{peer_name}' not found. Use /collab peers to see available peers.")
            return

        peer = peers[peer_name]
        await send_message(chat_id, f"Connecting to {peer_name}'s bot...")

        try:
            result = await collab_borrow_start(peer, bot)
        except Exception as _e:
            await send_message(chat_id, f"Failed to connect to {peer_name}: {_e}")
            return

        if not result:
            await send_message(chat_id, f"Could not start borrow session with {peer_name}. Peer may be offline or the bot is not available.")
            return

        collab_borrow.start_borrow(
            chat_id,
            peer_name,
            result["session_id"],
            result["bot"],
            result["label"],
        )

        await send_message(
            chat_id,
            f"Connected to <b>{result['label']}</b>\n"
            f"Every message you send will go to <b>{peer_name}</b>'s bot.\n"
            f"Say /return to disconnect.",
            parse_mode="HTML",
        )

    elif cmd == "/return":
        if not COLLAB_ENABLED or collab_borrow is None:
            await send_message(chat_id, "Collab is disabled. Set COLLAB_ENABLED=true to enable.")
            return

        borrow_info = collab_borrow.is_borrowing(chat_id)
        if not borrow_info:
            await send_message(chat_id, "You're not borrowing any bot.")
            return

        # Look up peer
        peers = load_peers() if load_peers else {}
        peer = peers.get(borrow_info.peer_name)

        if peer and collab_borrow_end is not None:
            try:
                await collab_borrow_end(peer, borrow_info.session_id)
            except Exception as _e:
                logger.error("borrow_end call failed for peer %s: %s", borrow_info.peer_name, _e)
                # Still disconnect locally even if remote call fails

        collab_borrow.end_borrow(chat_id)
        duration = int((time.time() - borrow_info.started_at) / 60)

        await send_message(
            chat_id,
            f"Disconnected from <b>{borrow_info.label}</b>. "
            f"Session lasted {duration} min. Back to your Claude.",
            parse_mode="HTML",
        )

    elif cmd == "/trigger":
        if not _triggers_available:
            await send_message(chat_id, "Trigger system is not available.")
            return

        parts = text.split(maxsplit=3)
        sub = parts[1].lower() if len(parts) > 1 else ""

        # /trigger run <id>
        if sub == "run":
            if len(parts) < 3:
                await send_message(chat_id, "Usage: `/trigger run <id>`", format_markdown=True)
                return
            trigger_id = parts[2]
            trigger = trigger_registry.get_trigger(trigger_id)
            if not trigger:
                await send_message(chat_id, f"Trigger `{trigger_id}` not found.", format_markdown=True)
                return
            # Temporarily set chat_id to current chat so reply comes here
            original_chat = trigger.chat_id
            if original_chat == 0:
                trigger_registry.set_enabled(trigger_id, trigger.enabled)  # no-op to ensure row exists
            fired = await trigger_worker.fire(trigger_id)
            if not fired:
                await send_message(chat_id, f"Failed to fire `{trigger_id}`. Is the agent configured?", format_markdown=True)

        else:
            await send_message(chat_id, "Usage: `/trigger run <id>`", format_markdown=True)

    else:
        # Unknown command — forward to active runner as a regular message.
        # This lets Claude skills (/security-review, /commit, etc.) and
        # any other runner-native slash commands work from Telegram.
        async def _passthrough():
            try:
                target_instance = await _resolve_target_instance_async(text, user_id)
                await _enqueue_message(QueuedMessage(
                    chat_id=chat_id,
                    msg_type=MessageType.TEXT,
                    text=text,
                    voice_reply=_voice_reply_mode,
                    instance_id=target_instance.id,
                    user_id=user_id,
                ))
            except Exception as _e:
                logger.error("Unknown command passthrough failed: %s", _e)
                await send_message(chat_id, f"Unknown command: {cmd}\nTry /help")
        asyncio.create_task(_passthrough())
