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
    CLI_RUNNER, BOT_NAME, MEMORY_DIR,
    is_cli_available, validate_config, logger,
)
from runners import create_runner
from telegram_handler import send_message, send_voice, send_photo, send_video, send_chat_action, download_photo, download_document, register_webhook, close_client
from image_handler import generate_image
from voice_handler import download_voice, transcribe_audio, text_to_speech, cleanup_file
import memory_handler
import task_handler
import daily_report
from instance_manager import InstanceManager, Instance
import router
import agent_manager
from agent_registry import create_agent, resolve_agent, list_agents, update_agent, delete_agent, get_agent
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
    import research_handler
except ImportError:
    research_handler = None  # type: ignore

# Initialize the CLI runner
runner = create_runner()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)




# -- Prompt dictionary -------------------------------------------------------
# All AI prompts live here. Use {placeholders} for dynamic values.
# View all prompts at GET /prompts

PROMPTS = {}



# -- Instance manager --------------------------------------------------------
instances = InstanceManager()


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
        jefe_count = await memory_handler.index_files(0)
        secondary_count = await memory_handler.index_files(memory_handler.SECONDARY_USER_ID)
        logger.info("Memory initialized: %d chunks (primary) + %d chunks (secondary) indexed", jefe_count, secondary_count)
    except Exception as e:
        logger.warning("Memory initialization failed (non-fatal): %s", e)


async def _start_scheduler_background() -> None:
    """Start scheduler after startup has fully completed."""
    await asyncio.sleep(0.2)
    await scheduler.scheduler_loop()


async def _notify_startup_background() -> None:
    """Send startup ping without blocking server readiness."""
    await asyncio.sleep(0.2)
    await send_message(ALLOWED_USER_ID, "\u2705 Server restarted and ready.")


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
    if research_handler:
        research_handler.init(runner)
    if task_orchestrator:
        task_orchestrator.init(runner)
    if WEBHOOK_URL:
        await register_webhook(WEBHOOK_URL)
        logger.info("Webhook registered from WEBHOOK_URL env")
    else:
        logger.warning("WEBHOOK_URL not set -- webhook won't be auto-registered")

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
    asyncio.create_task(_notify_startup_background())
    # Proactive worker does NOT auto-start — use /agent proactive start to enable
    yield
    # Clean up voice call if active
    from call_handler import end_call, get_manager
    if get_manager() and get_manager().is_active:
        await end_call()
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
    logger.info("Bridge shut down")


app = FastAPI(title="Telegram-Claude Bridge", lifespan=lifespan)


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






@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()

    # Deduplicate webhook retries from Telegram
    update_id = body.get("update_id")
    if update_id:
        if update_id in _processed_updates:
            return JSONResponse({"ok": True})
        _processed_updates.add(update_id)
        if len(_processed_updates) > 1000:
            oldest = sorted(_processed_updates)[:500]
            _processed_updates.difference_update(oldest)

    message = body.get("message")
    if not message:
        return JSONResponse({"ok": True})

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
        return JSONResponse({"ok": True})

    # Normalize command to lowercase (preserve args) so all commands are case-insensitive
    if text.startswith("/"):
        _space = text.find(" ")
        text = (text[:_space].lower() + text[_space:]) if _space != -1 else text.lower()

    # Bot commands -- handled directly (fast, no background needed)
    # /call and /endcall are handled before queue-based commands
    if text.startswith("/call") and not text.startswith("/chrome"):
        await _handle_command(chat_id, text)
        return JSONResponse({"ok": True})

    if text == "/endcall":
        await _handle_command(chat_id, text)
        return JSONResponse({"ok": True})

    # Gate text messages during an active voice call
    from call_handler import get_manager
    call_mgr = get_manager()
    if call_mgr and call_mgr.is_active and not text.startswith("/"):
        await send_message(
            chat_id,
            "\U0001f3a4 Voice call is active \u2014 speak in the group voice chat!\n"
            "Use /endcall to leave the call first.",
        )
        return JSONResponse({"ok": True})

    # /research runs independently (fetches public data + Ollama analysis)
    if text.startswith("/research"):
        company = text[len("/research"):].strip()
        if not company:
            await send_message(chat_id, "Usage: /research <company name>\nExample: /research Apple Inc")
            return JSONResponse({"ok": True})
        health.record_message()
        asyncio.create_task(_process_research(chat_id, company))
        return JSONResponse({"ok": True})

    # /objective — find companies pursuing a specific goal + what they're each doing
    if text.startswith("/objective"):
        objective = text[len("/objective"):].strip()
        if not objective:
            await send_message(
                chat_id,
                "Usage: /objective <goal or theme>\nExample: /objective improve voice-based AI",
            )
            return JSONResponse({"ok": True})
        health.record_message()
        asyncio.create_task(_process_objective(chat_id, objective))
        return JSONResponse({"ok": True})

    # /imagine is special -- it runs independently (uses Gemini, not Claude)
    if text.startswith("/imagine"):
        prompt = text[len("/imagine"):].strip()
        if not prompt:
            await send_message(chat_id, "Usage: /imagine <description of the image>")
            return JSONResponse({"ok": True})
        health.record_message()
        asyncio.create_task(_process_image_generation(chat_id, prompt))
        return JSONResponse({"ok": True})

    if text.startswith("/"):
        await _handle_command(chat_id, text, user_id=user_id)
        return JSONResponse({"ok": True})

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
        return JSONResponse({"ok": True})

    # Document upload -- save to uploads folder inside memory dir
    if document:
        file_id = document["file_id"]
        file_name = document.get("file_name", f"file_{file_id[:8]}")
        save_dir = os.path.join(MEMORY_DIR, "uploads")
        dest_path = os.path.join(save_dir, file_name)
        health.record_message()
        asyncio.create_task(_handle_document_upload(chat_id, file_id, dest_path, file_name))
        return JSONResponse({"ok": True})

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
        return JSONResponse({"ok": True})

    # Skip empty messages
    if not text.strip() and not photo and not voice and not audio and not document:
        return JSONResponse({"ok": True})

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
        return JSONResponse({"ok": True})

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
    return JSONResponse({"ok": True})


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


def _label(instance, response: str, owner_id: int = 0) -> str:
    """Prefix response with instance label when the user has multiple instances."""
    owner_insts = instances.list_all(for_owner_id=owner_id)
    if len(owner_insts) >= 2 and instance:
        disp = instances.display_num(instance.id, owner_id)
        return f"**[#{disp}: {instance.title}]**\n{response}"
    return response


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
    inst = instance or instances.active
    proc_owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
    await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id), format_markdown=True)

    start = time.time()

    # Agent-aware memory: agents only get their own domain memory, not personal files or general ChromaDB
    if inst.agent_id:
        from agent_memory import get_agent_context
        memory_context = await asyncio.get_event_loop().run_in_executor(
            None, get_agent_context, inst.agent_id, text
        )
    else:
        memory_context = await memory_handler.search_memory(text, user_id=user_id)

    async def on_progress(progress_text: str):
        await send_message(chat_id, _label(inst, progress_text, proc_owner_id), format_markdown=True)

    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prefixed_text = f"[{sender_name}]: {text}" if sender_name else text
    response = await runner.run(prefixed_text, on_progress=on_progress, memory_context=memory_context, instance=inst)
    elapsed = time.time() - start

    if not response or not response.strip():
        response = "(no text response from Claude — check tool output)"

    logger.info("Claude #%d responded in %.1fs (%d chars)", inst.id, elapsed, len(response))

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
    await send_message(chat_id, _label(inst, "\U0001f4f7 Downloading image...", proc_owner_id), format_markdown=True)

    image_path = None
    try:
        image_path = await download_photo(file_id)
    except Exception as e:
        logger.error("Photo download failed: %s", e)
        await send_message(chat_id, _label(inst, f"\u274c Failed to download photo: {e}", proc_owner_id), format_markdown=True)
        return

    await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id), format_markdown=True)

    start = time.time()

    async def on_progress(progress_text: str):
        await send_message(chat_id, _label(inst, progress_text, proc_owner_id), format_markdown=True)

    sender_name = USER_NAMES.get(user_id, "") if user_id else ""
    prefixed_caption = f"[{sender_name}]: {caption}" if sender_name else caption
    response = await runner.run(prefixed_caption, on_progress=on_progress, image_path=image_path, instance=inst)
    elapsed = time.time() - start

    logger.info("Claude #%d responded to photo in %.1fs (%d chars)", inst.id, elapsed, len(response))
    response += _context_footer(inst)
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
    await send_message(chat_id, _label(inst, "\U0001f9e0 Thinking...", proc_owner_id), format_markdown=True)

    start = time.time()

    memory_context = await memory_handler.search_memory(raw_prompt, user_id=user_id)

    async def on_progress(progress_text: str):
        await send_message(chat_id, _label(inst, progress_text, proc_owner_id), format_markdown=True)

    response = await runner.run(prompt, on_progress=on_progress, memory_context=memory_context, instance=inst)
    elapsed = time.time() - start

    if not response or not response.strip():
        response = "(no text response from Claude — check tool output)"

    logger.info("Claude #%d responded in %.1fs (%d chars)", inst.id, elapsed, len(response))

    # Store memory before appending footer
    asyncio.ensure_future(memory_handler.store_conversation(raw_prompt, response, user_id=user_id))
    asyncio.ensure_future(memory_handler.extract_and_save(raw_prompt, response, user_id=user_id))

    # Voice in -> voice + text out
    response += _context_footer(inst)
    await _send_with_voice(chat_id, _label(inst, response, proc_owner_id))

    # Auto-detect and send any media files referenced in the response
    await _extract_and_send_media(chat_id, response)


async def _process_research(chat_id: int, company: str) -> None:
    """Run company intelligence research and send the report."""
    await send_message(
        chat_id,
        f"🔍 Researching <b>{company}</b>...\n"
        "Pulling SEC filings, contracts, and news. This takes ~60s.",
        parse_mode="HTML",
    )
    try:
        report = await research_handler.research_company(company)
        await send_message(chat_id, report, parse_mode="HTML")
    except Exception as e:
        logger.error("Research failed for %s: %s", company, e)
        await send_message(chat_id, f"❌ Research failed: {e}")


async def _process_objective(chat_id: int, objective: str) -> None:
    """Find companies working toward an objective and what each is doing."""
    await send_message(
        chat_id,
        f"🎯 Researching companies pursuing: <b>{objective}</b>\n"
        "Scanning news + running analysis. ~60s.",
        parse_mode="HTML",
    )
    try:
        report = await research_handler.research_objective(objective)
        await send_message(chat_id, report, parse_mode="HTML")
    except Exception as e:
        logger.error("Objective research failed for %s: %s", objective, e)
        await send_message(chat_id, f"❌ Objective research failed: {e}")


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
    """Wait briefly so the webhook response reaches Telegram, then restart."""
    await asyncio.sleep(1)
    await runner.kill_all()
    await close_client()
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

    if cmd == "/start":
        await send_message(
            chat_id,
            "Welcome to the Telegram-Claude Bridge!\n\n"
            "Send me any message and I'll forward it to Claude Code "
            "running on your local machine. Claude remembers your "
            "conversation until you start a new one.\n\n"
            "Messages sent while Claude is busy are queued (up to 10) "
            "and processed in order.\n\n"
            "You can also send voice notes! I'll transcribe them "
            "and reply with both text and voice.\n\n"
            "Commands:\n"
            "/imagine &lt;prompt&gt; \u2014 Generate an image\n"
            "/research &lt;company&gt; \u2014 Company intel: vendors, contracts, forecast\n"
            "/objective &lt;goal&gt; \u2014 Companies pursuing an objective + what each is doing\n"
            "/call \u2014 Join group voice chat for live conversation\n"
            "/endcall \u2014 Leave voice chat\n"
            "/stop \u2014 Stop current task & clear queue\n"
            "/kill \u2014 Force-kill all Claude processes\n"
            "/new \u2014 Start a new conversation\n"
            "/voice \u2014 Toggle voice replies for text messages\n"
            "/chrome \u2014 Toggle Chrome browser integration\n"
            "/remember &lt;text&gt; \u2014 Save to memory\n"
            "/task \u2014 View/manage task list (add, done)\n"
            "/memory \u2014 Memory stats &amp; re-index\n"
            "/server \u2014 Restart the bridge server\n"
            "**\U0001f4bb System**\n"
            "/status \u2014 Server status\n"
            "/help \u2014 Show this help",
        )

    elif cmd == "/call":
        from call_handler import start_call

        async def call_status(text):
            await send_message(chat_id, text)

        await start_call(on_status=call_status)

    elif cmd == "/endcall":
        from call_handler import end_call, get_manager
        mgr = get_manager()
        if mgr and mgr.is_active:
            await end_call()
        else:
            await send_message(chat_id, "No active call.")

    elif cmd == "/getid":
        await send_message(chat_id, f"Chat ID: <code>{chat_id}</code>", parse_mode="HTML")

    elif cmd == "/stop":
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

    elif cmd == "/voice":
        global _voice_reply_mode
        _voice_reply_mode = not _voice_reply_mode
        status = "ON" if _voice_reply_mode else "OFF"
        await send_message(chat_id, f"\U0001f50a Voice replies for text messages: {status}")

    elif cmd == "/chrome":
        if hasattr(runner, 'chrome_enabled'):
            runner.chrome_enabled = not runner.chrome_enabled
        status = "ON" if getattr(runner, 'chrome_enabled', False) else "OFF"
        await send_message(chat_id, f"\U0001f310 Chrome browser integration: {status}")

    elif cmd == "/new":
        inst = instances.get_active_for(owner_id)
        inst.clear_queue()
        await runner.stop(inst)
        if inst.current_task and not inst.current_task.done():
            inst.current_task.cancel()
        runner.new_session(inst)
        label = f" [#{instances.display_num(inst.id, owner_id)}: {inst.title}]" if len(instances.list_all(for_owner_id=owner_id)) >= 2 else ""
        await send_message(chat_id, f"\U0001f195 New conversation started. Queue cleared.{label}")

    elif cmd == "/server":
        await send_message(chat_id, "\U0001f504 Restarting server...")
        # Delay restart so the webhook can return 200 to Telegram first,
        # otherwise Telegram retries the update and causes a restart loop.
        asyncio.create_task(_delayed_restart())

    elif cmd == "/status":
        info = health.get_status()
        uptime_min = info["uptime_seconds"] / 60
        claude_ok = "\u2705" if info["claude_available"] else "\u274c"

        # Per-instance status (scoped to requesting user's pool)
        _active_for_user = instances.get_active_for(owner_id)
        inst_lines = []
        for disp_num, inst in enumerate(instances.list_all(for_owner_id=owner_id), start=1):
            marker = "\u25b6" if inst.id == _active_for_user.id else " "
            status = "busy" if inst.processing else "idle"
            q = inst.queue.qsize() if inst.queue else 0
            inst_lines.append(f"{marker}#{disp_num} {inst.title}: {status} (queue: {q})")
        inst_status = "\n".join(inst_lines)

        from call_handler import get_manager
        call_mgr = get_manager()
        call_state = call_mgr.state if call_mgr else "idle"

        await send_message(
            chat_id,
            f"Server uptime: {uptime_min:.1f} min\n"
            f"Messages processed: {info['message_count']}\n"
            f"Claude CLI available: {claude_ok}\n\n"
            f"Instances:\n{inst_status}\n\n"
            f"Voice call: {call_state}",
        )

    elif cmd == "/help":
        voice_status = "ON" if _voice_reply_mode else "OFF"
        chrome_status = "ON" if getattr(runner, 'chrome_enabled', False) else "OFF"
        from call_handler import get_manager
        call_mgr = get_manager()
        call_status = call_mgr.state if (call_mgr and call_mgr.is_active) else "off"
        active = instances.get_active_for(owner_id)
        user_inst_count = len(instances.list_all(for_owner_id=owner_id))
        inst_info = f"Active: #{instances.display_num(active.id, owner_id)} ({active.title})" if user_inst_count >= 2 else "1 instance running"
        help_text = (
            "**Available Commands:**\n\n"
            "**\U0001f3a8 Image Generation**\n"
            "/imagine <prompt> \u2014 Generate an image\n\n"
            "**\U0001f50d Research & Intel**\n"
            "/research <company> \u2014 Company intel report: vendors, contracts, SEC filings, tactical forecast\n"
            "/objective <goal> \u2014 Who is pursuing an objective + what each company is doing toward it\n\n"
            "**\U0001f916 Orchestration**\n"
            "/orch <task> \u2014 Break task into parallel agents, synthesize results\n\n"
            "**\U0001f916 Agents**\n"
            "/agent list \u2014 Show all agents\n"
            "/agent create <type> <name> \u2014 Create a specialist agent  _→ /agent create research News Hound_\n"
            "/agent talk <name> \u2014 Talk directly to an agent  _→ /agent talk News Hound_\n"
            "/agent back \u2014 Return to default instance\n"
            "/agent task <name> <task> \u2014 Assign a one-off task  _→ /agent task News Hound summarize AI news_\n"
            "/agent fix <name> <rule> \u2014 Patch a rule into agent's prompt  _→ /agent fix News Hound always cite sources_\n"
            "/agent feedback <name> <issue> \u2014 Record feedback & auto-improve  _→ /agent feedback News Hound missed the SEC angle_\n"
            "/agent delete <name> \u2014 Delete an agent\n"
            "_Types: research, analytics, writing, coding, manager_\n\n"
            "**\U0001f4dc Instances (Multi-Chat)**\n"
            f"_{inst_info}_\n"
            "Each instance is a separate Claude Code session with its own conversation history. "
            "They don't share context \u2014 you can have one researching while another codes.\n"
            "Instances run concurrently \u2014 you can send messages to different instances without waiting.\n"
            "/claude new <title> \u2014 Spin up a new independent Claude session\n"
            "/claude list \u2014 Show all running instances with IDs & titles\n"
            "/claude switch <id/title> [new_title] \u2014 Switch instance (creates if missing, renames if new_title given)\n"
            "/claude rename <id> <title> \u2014 Rename an instance\n"
            "/claude end <id> \u2014 Close an instance (can't close the last one)\n"
            "/claude \u2014 Show active instance & subcommands\n"
            "_When 2+ instances exist, responses are labeled. Mention an instance by name or # to auto-route._\n"
            "@<id or name> <message> \u2014 One-shot to a specific instance without switching active (creates if missing)\n\n"
            "**\U0001f3a4 Voice**\n"
            f"/call \u2014 Join group voice chat [{call_status}]\n"
            "/endcall \u2014 Leave voice chat\n"
            f"/voice \u2014 Toggle voice replies [{voice_status}]\n\n"
            "**\u2699\ufe0f Control**\n"
            "/new \u2014 Reset conversation for the active instance\n"
            "/stop \u2014 Stop current task & clear queue (active instance only)\n"
            "/kill \u2014 Force-kill all Claude processes across all instances\n"
            f"/chrome \u2014 Toggle Chrome browser [{chrome_status}]\n"
            f"/model sonnet|opus \u2014 Switch model for active instance [{active.model.split("-")[1].capitalize()}]\n\n"
            "**\U0001f9e0 Memory & Tasks**\n"
            "/remember <text> \u2014 Save to memory\n"
            "/task \u2014 View/manage task list\n"
            "/memory \u2014 Memory stats & re-index\n\n"

            "**\U0001f4bb System**\n"
            "/status \u2014 Server status & queue depth\n"
            "/server \u2014 Restart bridge server\n"
            "/help \u2014 Show this help\n\n"
            "Messages are queued per instance (up to 10 each). "
            "Different instances process concurrently."
        )
        await send_message(chat_id, help_text, format_markdown=True)

    elif cmd == "/task":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""

        if sub == "add" and len(parts) > 2:
            result = task_handler.add_task(parts[2])
            await send_message(chat_id, f"\u2705 {result}")
        elif sub == "done" and len(parts) > 2:
            try:
                num = int(parts[2])
                result = task_handler.done_task(num)
                await send_message(chat_id, result)
            except ValueError:
                await send_message(chat_id, "Usage: /task done <number>")
        else:
            result = task_handler.list_tasks()
            await send_message(chat_id, result)

    elif cmd == "/remember":
        text_to_remember = text[len("/remember"):].strip()
        if not text_to_remember:
            await send_message(chat_id, "Usage: /remember <text to save>")
        else:
            result = await memory_handler.remember(text_to_remember, user_id=user_id)
            await send_message(chat_id, f"\U0001f4be {result}")

    elif cmd == "/memory":
        parts = text.split()
        if len(parts) > 1 and parts[1].lower() == "reindex":
            await send_message(chat_id, "\U0001f504 Re-indexing memory files...")
            count = await memory_handler.reindex(user_id=user_id)
            await send_message(chat_id, f"\u2705 Re-indexed {count} chunks from text files.")
        else:
            stats = await memory_handler.get_stats(user_id=user_id)
            if not stats.get("enabled"):
                await send_message(chat_id, "Memory is disabled.")
            elif "error" in stats:
                await send_message(chat_id, f"Memory error: {stats['error']}")
            else:
                await send_message(
                    chat_id,
                    f"\U0001f9e0 Memory Stats:\n"
                    f"Total entries: {stats['total_entries']}\n"
                    f"Collection: {stats['collection']}\n"
                    f"Text files: {stats['text_files']}\n"
                    f"Memory dir: {stats['memory_dir']}\n"
                    f"Remembered file: {'Yes' if stats['remembered_file'] else 'No'}\n\n"
                    f"Use /memory reindex to re-index text files.",
                )

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

    elif cmd == "/claude":
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
                await send_message(chat_id, "Usage: /claude switch <id/title> [new_title]")
            else:
                # Handle potential rename: /claude switch <id/title> <new_title>
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
                await send_message(chat_id, "Usage: /claude rename <id> <new title>")
            else:
                disp_num = int(rename_parts[0])
                new_title = rename_parts[1]
                target_inst = instances.get_by_display_num(disp_num, owner_id)
                if target_inst and instances.rename(target_inst.id, new_title, owner_id=owner_id):
                    await send_message(chat_id, f"\u270f\ufe0f Renamed #{disp_num} to <b>{new_title}</b>", parse_mode="HTML")
                else:
                    await send_message(chat_id, f"No instance #{disp_num}. Try /claude list")

        elif sub == "end":
            if not arg or not arg.isdigit():
                await send_message(chat_id, "Usage: /claude end <id>")
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
                        await send_message(chat_id, f"No instance #{disp_num}. Try /claude list")

        else:
            inst = instances.get_active_for(owner_id)
            await send_message(
                chat_id,
                f"Active: <b>#{instances.display_num(inst.id, owner_id)}: {inst.title}</b>\n\n"
                f"Commands:\n"
                f"/claude new &lt;title&gt; \u2014 New instance\n"
                f"/claude list \u2014 Show all instances\n"
                f"/claude switch &lt;id/title&gt; [new_title] \u2014 Switch/Create/Rename\n"
                f"/claude rename &lt;id&gt; &lt;title&gt; \u2014 Rename\n"
                f"/claude end &lt;id&gt; \u2014 End instance",
                parse_mode="HTML",
            )

    elif cmd == "/agent":
        parts = text.split(maxsplit=2)
        sub = parts[1].lower() if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "list":
            await send_message(chat_id, agent_manager.format_agent_list(instances), parse_mode="HTML")

        elif sub == "create":
            # /agent create <type> <name>  OR  /agent create <name> (custom type)
            create_parts = arg.split(maxsplit=1)
            if not create_parts:
                await send_message(chat_id,
                    "Usage: /agent create &lt;type&gt; &lt;name&gt;\n"
                    f"Types: {', '.join(SKILL_PACKS.keys())}\n"
                    "Example: /agent create research My Researcher",
                    parse_mode="HTML")
            else:
                type_or_name = create_parts[0].lower()
                is_proactive_type = type_or_name == "proactive"
                if (type_or_name in SKILL_PACKS or is_proactive_type) and len(create_parts) > 1:
                    agent_type = type_or_name
                    agent_name = create_parts[1]
                else:
                    agent_type = "custom"
                    agent_name = arg
                agent_id = re.sub(r"[^a-z0-9_]", "_", agent_name.lower())[:20]
                try:
                    from agent_skills import DEFAULT_AGENT_PROMPTS
                    system_prompt = DEFAULT_AGENT_PROMPTS.get(agent_type, "")
                    new_agent = create_agent(
                        agent_id=agent_id,
                        name=agent_name,
                        agent_type=agent_type,
                        system_prompt=system_prompt,
                        skills=[agent_type] if agent_type in SKILL_PACKS else [],
                    )
                    if is_proactive_type:
                        await send_message(chat_id,
                            f"🤖 Proactive agent created: <b>{new_agent.name}</b>\n"
                            f"ID: <code>{new_agent.id}</code>\n\n"
                            f"Now set its schedule and task:\n"
                            f"<code>/agent proactive {new_agent.id} set 09:00 your task here</code>\n"
                            f"<code>/agent proactive {new_agent.id} set every 2h your task here</code>\n\n"
                            f"Then start the worker:\n"
                            f"<code>/agent proactive start</code>",
                            parse_mode="HTML")
                    else:
                        await send_message(chat_id,
                            f"Agent created: <b>{new_agent.name}</b>\n"
                            f"ID: {new_agent.id} | Type: {new_agent.agent_type}\n"
                            f"Use /agent talk {new_agent.id} to start talking to it.",
                            parse_mode="HTML")
                except ValueError as e:
                    await send_message(chat_id, f"Error: {e}")

        elif sub in ("talk", "switch"):
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

        elif sub == "task":
            # /agent task <name> <task description>
            task_parts = arg.split(maxsplit=1)
            if len(task_parts) < 2:
                await send_message(chat_id, "Usage: /agent task &lt;agent&gt; &lt;task description&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(task_parts[0])
                task_desc = task_parts[1]
                if target is None:
                    await send_message(chat_id, f"Agent '{task_parts[0]}' not found. Try /agent list")
                else:
                    queued = await agent_manager.assign_task(target.id, task_desc, chat_id, instances, send_message, owner_id)
                    if queued:
                        await send_message(chat_id, f"Task queued for <b>{target.name}</b>: {task_desc[:100]}", parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"Failed to queue task for {target.name} (queue full or agent not found).")

        elif sub == "schedule":
            # /agent schedule <name> <HH:MM> <task description>
            sched_parts = arg.split(maxsplit=2)
            if len(sched_parts) < 3:
                await send_message(chat_id,
                    "Usage: /agent schedule &lt;agent&gt; &lt;HH:MM&gt; &lt;task&gt;\n"
                    "Example: /agent schedule research 09:00 daily AI market briefing",
                    parse_mode="HTML")
            else:
                target = resolve_agent(sched_parts[0])
                time_str = sched_parts[1]
                task_desc = sched_parts[2]
                if target is None:
                    await send_message(chat_id, f"Agent '{sched_parts[0]}' not found.")
                else:
                    result = agent_manager.schedule_agent_task(target.id, time_str, task_desc)
                    await send_message(chat_id, result)

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

        elif sub == "skills":
            if arg:
                target = resolve_agent(arg)
                if target is None:
                    await send_message(chat_id, f"Agent '{arg}' not found.")
                else:
                    from agent_memory import get_agent_graph_summary
                    graph_info = get_agent_graph_summary(target.id)
                    skills_text = "\n".join(f"  {s}" for s in target.skills) if target.skills else "  (none)"
                    await send_message(chat_id,
                        f"<b>{target.name}</b>\n"
                        f"Type: {target.agent_type} | Model: {target.model}\n"
                        f"Skills:\n{skills_text}\n"
                        f"Collaborators: {', '.join(target.collaborators) or 'none'}\n"
                        f"{graph_info}",
                        parse_mode="HTML")
            else:
                await send_message(chat_id, list_skills())

        elif sub == "update":
            # /agent update <name> prompt=<text>  OR  name=<new name>
            update_parts = arg.split(maxsplit=1)
            if len(update_parts) < 2:
                await send_message(chat_id, "Usage: /agent update &lt;agent&gt; prompt=&lt;new prompt&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(update_parts[0])
                field_val = update_parts[1]
                if target is None:
                    await send_message(chat_id, f"Agent '{update_parts[0]}' not found.")
                elif "=" not in field_val:
                    await send_message(chat_id, "Format: field=value (e.g. prompt=You are...)")
                else:
                    field, value = field_val.split("=", 1)
                    field = field.strip()
                    value = value.strip().strip('"')
                    updated = update_agent(target.id, **{field: value})
                    if updated:
                        # Update running instance if active
                        running = agent_manager.get_running_instance(target.id, instances)
                        if running and field == "system_prompt":
                            from agent_skills import build_skills_prompt
                            running.agent_system_prompt = value + "\n\n" + build_skills_prompt(updated.skills)
                        await send_message(chat_id, f"Updated <b>{target.name}</b>: {field} changed.", parse_mode="HTML")
                    else:
                        await send_message(chat_id, f"Update failed.")

        elif sub == "delete":
            if not arg:
                await send_message(chat_id, "Usage: /agent delete &lt;agent name or id&gt;", parse_mode="HTML")
            else:
                target = resolve_agent(arg)
                if target is None:
                    await send_message(chat_id, f"Agent '{arg}' not found.")
                else:
                    # If running, end the instance first
                    running = agent_manager.get_running_instance(target.id, instances)
                    if running:
                        instances.remove(running.id, owner_id=owner_id)
                    deleted = delete_agent(target.id)
                    if deleted:
                        await send_message(chat_id, f"Deleted agent: {target.name}")
                    else:
                        await send_message(chat_id, f"Delete failed.")

        elif sub == "fix":
            # /agent fix <name> "rule to add"
            fix_parts = arg.split(maxsplit=1)
            if len(fix_parts) < 2:
                await send_message(chat_id,
                    "Usage: /agent fix &lt;agent&gt; &lt;rule&gt;\n"
                    "Example: /agent fix research Always cite sources with full URLs",
                    parse_mode="HTML")
            else:
                target = resolve_agent(fix_parts[0])
                rule = fix_parts[1].strip().strip('"')
                if target is None:
                    await send_message(chat_id, f"Agent '{fix_parts[0]}' not found. Try /agent list")
                else:
                    await send_message(chat_id, f"Updating {target.name}'s prompt...", parse_mode="HTML")
                    msg = await agent_manager.fix_agent_prompt(target.id, rule, instances=instances)
                    await send_message(chat_id, msg, parse_mode="HTML")

        elif sub == "feedback":
            # /agent feedback <name> "what was wrong"
            fb_parts = arg.split(maxsplit=1)
            if len(fb_parts) < 2:
                await send_message(chat_id,
                    "Usage: /agent feedback &lt;agent&gt; &lt;what was wrong&gt;\n"
                    "Example: /agent feedback research You forgot to cite sources and gave speculation as fact",
                    parse_mode="HTML")
            else:
                target = resolve_agent(fb_parts[0])
                feedback_text = fb_parts[1].strip().strip('"')
                if target is None:
                    await send_message(chat_id, f"Agent '{fb_parts[0]}' not found. Try /agent list")
                else:
                    await send_message(chat_id, f"Processing feedback for {target.name}...", parse_mode="HTML")
                    msg = await agent_manager.record_agent_feedback(target.id, feedback_text, instances=instances)
                    await send_message(chat_id, msg, parse_mode="HTML")

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
                    agent_label = f"\nCurrently talking to: <b>{active_agent.name}</b>"

            await send_message(chat_id,
                f"<b>Agent System</b>{agent_label}\n\n"
                "<b>/agent list</b> — Show all agents\n"
                "  <i>→ /agent list</i>\n\n"
                "<b>/agent create &lt;type&gt; &lt;name&gt;</b> — Create a specialist agent\n"
                "  <i>→ /agent create research News Hound</i>\n\n"
                "<b>/agent talk &lt;name&gt;</b> — Talk directly to an agent\n"
                "  <i>→ /agent talk News Hound</i>\n\n"
                "<b>/agent back</b> — Return to default instance\n"
                "  <i>→ /agent back</i>\n\n"
                "<b>/agent task &lt;name&gt; &lt;task&gt;</b> — Assign a one-off task\n"
                "  <i>→ /agent task News Hound find top AI funding rounds this week</i>\n\n"
                "<b>/agent schedule &lt;name&gt; &lt;HH:MM&gt; &lt;task&gt;</b> — Schedule recurring task\n"
                "  <i>→ /agent schedule News Hound 09:00 daily AI market briefing</i>\n\n"
                "<b>/agent pipeline &lt;a&gt; → &lt;b&gt; \"task\"</b> — Sequential agent pipeline\n"
                "  <i>→ /agent pipeline News Hound → analytics \"AI funding trends\"</i>\n\n"
                "<b>/agent skills [name]</b> — List skill packs or agent's skills\n"
                "  <i>→ /agent skills News Hound</i>\n\n"
                "<b>/agent update &lt;name&gt; field=value</b> — Update agent config\n"
                "  <i>→ /agent update News Hound prompt=Always cite sources with URLs</i>\n\n"
                "<b>/agent fix &lt;name&gt; &lt;rule&gt;</b> — Add/merge a rule into agent's prompt\n"
                "  <i>→ /agent fix News Hound Always output results as numbered lists</i>\n\n"
                "<b>/agent feedback &lt;name&gt; &lt;what was wrong&gt;</b> — Record feedback + auto-improve\n"
                "  <i>→ /agent feedback News Hound You missed the SEC angle and only cited 2 sources</i>\n\n"
                "<b>🤖 Proactive Agents</b>\n"
                "<b>/agent create proactive &lt;name&gt;</b> — Create a proactive agent\n"
                "  <i>→ /agent create proactive Daily Briefing</i>\n"
                "<b>/agent proactive start/stop</b> — Start or stop the worker\n"
                "<b>/agent proactive list</b> — Show all proactive agents + status\n"
                "<b>/agent proactive &lt;name&gt; set &lt;schedule&gt; &lt;task&gt;</b> — Configure\n"
                "  <i>→ /agent proactive research set 09:00 summarize AI news</i>\n"
                "  <i>→ /agent proactive research set every 2h check trending topics</i>\n"
                "  <i>→ /agent proactive research set every 30m monitor prices</i>\n"
                "<b>/agent proactive &lt;name&gt; on/off</b> — Toggle\n"
                "<b>/agent proactive &lt;name&gt; clear</b> — Wipe config\n\n"
                "<b>/agent delete &lt;name&gt;</b> — Delete an agent\n"
                "  <i>→ /agent delete News Hound</i>\n\n"
                f"<b>Types:</b> {', '.join(SKILL_PACKS.keys())}",
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


    else:
        await send_message(chat_id, f"Unknown command: {cmd}\nTry /help")
