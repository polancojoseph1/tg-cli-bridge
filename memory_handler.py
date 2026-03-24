"""Vector memory system using ChromaDB.

Provides:
  - Persistent vector storage at MEMORY_DIR/.chroma_db/
  - Auto-indexing of .md/.txt files from the user's memory dir on startup
  - Conversation memory: stores user+response pairs after each turn
  - Explicit /remember command: saves to both ChromaDB and text file
  - Semantic search: retrieves top-K relevant memories for context injection
"""

import asyncio
import hashlib
import logging
import time
from datetime import datetime
from pathlib import Path

from config import MEMORY_ENABLED, MEMORY_DIR, MEMORY_COLLECTION, MEMORY_TOP_K

logger = logging.getLogger("bridge.memory")

# Per-user lazy-loaded ChromaDB clients and collections
_clients: dict = {}
_collections: dict = {}


# ── User config routing ─────────────────────────────────────────


def _user_config(user_id: int) -> tuple[str, str]:
    """Return (memory_dir, collection_name) for the given user_id."""
    return str(MEMORY_DIR), MEMORY_COLLECTION


# ── Initialization ──────────────────────────────────────────────


def _ensure_dirs(user_id: int = 0) -> None:
    mem_dir = Path(_user_config(user_id)[0])
    chroma_dir = mem_dir / ".chroma_db"
    mem_dir.mkdir(parents=True, exist_ok=True)
    chroma_dir.mkdir(parents=True, exist_ok=True)


def _get_collection(user_id: int = 0):
    """Lazy-initialize ChromaDB PersistentClient and return the collection for this user."""
    if user_id in _collections:
        return _collections[user_id]

    import chromadb

    _ensure_dirs(user_id)
    mem_dir, col_name = _user_config(user_id)
    chroma_path = str(Path(mem_dir) / ".chroma_db")
    logger.info("Initializing ChromaDB at %s (user_id=%s)", chroma_path, user_id)

    client = chromadb.PersistentClient(path=chroma_path)
    collection = client.get_or_create_collection(
        name=col_name,
        metadata={"hnsw:space": "cosine"},
    )
    _clients[user_id] = client
    _collections[user_id] = collection
    logger.info("ChromaDB collection '%s' ready (%d entries)", col_name, collection.count())
    return collection


# ── Text file indexing ──────────────────────────────────────────


def _chunk_text(text: str) -> list[str]:
    """Split text by double newlines (paragraphs). Skip chunks under 20 chars."""
    chunks = []
    for para in text.split("\n\n"):
        cleaned = para.strip()
        if len(cleaned) >= 20:
            chunks.append(cleaned)
    return chunks


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _index_files_sync(user_id: int = 0) -> int:
    """Index all .md and .txt files from the user's memory dir into their ChromaDB collection.

    Uses file path + chunk hash as doc ID so re-indexing is idempotent.
    Returns count of chunks indexed.
    """
    collection = _get_collection(user_id)
    mem_dir, _ = _user_config(user_id)
    memory_dir = Path(mem_dir)
    total_chunks = 0

    for ext in ("**/*.md", "**/*.txt"):
        for file_path in memory_dir.glob(ext):
            if file_path.name.startswith(".") or ".chroma_db" in file_path.parts:
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("Failed to read %s: %s", file_path, e)
                continue

            chunks = _chunk_text(content)
            if not chunks:
                continue

            ids = []
            documents = []
            metadatas = []

            rel_path = str(file_path.relative_to(memory_dir))
            for i, chunk in enumerate(chunks):
                doc_id = f"file:{rel_path}:chunk:{i}:{_file_hash(chunk)}"
                ids.append(doc_id)
                documents.append(chunk)
                metadatas.append({
                    "source": "file",
                    "file": rel_path,
                    "chunk_index": i,
                    "indexed_at": time.time(),
                })

            collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
            total_chunks += len(chunks)
            logger.info("Indexed %s: %d chunks", file_path.name, len(chunks))

    logger.info("File indexing complete: %d total chunks from %s", total_chunks, mem_dir)
    return total_chunks


async def index_files(user_id: int = 0) -> int:
    """Index text files from the user's memory dir (async wrapper). Returns chunk count."""
    if not MEMORY_ENABLED:
        return 0
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _index_files_sync, user_id)


# ── Memory search ──────────────────────────────────────────────


def _search_sync(query: str, n_results: int, user_id: int) -> list[dict]:
    """Synchronous ChromaDB query. Returns list of {text, source, file, distance}."""
    collection = _get_collection(user_id)
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, collection.count() or 1),
        include=["documents", "metadatas", "distances"],
    )

    memories = []
    if results and results["documents"] and results["documents"][0]:
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            memories.append({
                "text": doc,
                "source": meta.get("source", "unknown"),
                "file": meta.get("file", ""),
                "distance": dist,
            })
    return memories


async def search_memory(query: str, n_results: int = 0, user_id: int = 0) -> str:
    """Search ChromaDB for relevant memories. Returns formatted context string.

    Returns empty string if memory is disabled or no results found.
    """
    if not MEMORY_ENABLED:
        return ""

    if n_results <= 0:
        n_results = MEMORY_TOP_K

    loop = asyncio.get_event_loop()
    try:
        memories = await loop.run_in_executor(None, _search_sync, query, n_results, user_id)
    except Exception as e:
        logger.error("Memory search failed: %s", e)
        return ""

    if not memories:
        return ""

    lines = ["[Relevant memories from previous conversations and notes:]"]
    for i, mem in enumerate(memories, 1):
        source_tag = f" (from {mem['file']})" if mem["file"] else ""
        lines.append(f"{i}. {mem['text']}{source_tag}")

    return "\n".join(lines)


# ── Store conversation ─────────────────────────────────────────


def _store_sync(user_msg: str, response: str, user_id: int) -> None:
    """Store a conversation turn as a memory entry."""
    collection = _get_collection(user_id)

    combined = f"User: {user_msg}\n\nAssistant: {response}"
    if len(combined) > 2000:
        combined = combined[:2000] + "..."

    doc_id = f"conv:{hashlib.sha256(combined.encode()).hexdigest()}:{time.time()}"

    collection.upsert(
        ids=[doc_id],
        documents=[combined],
        metadatas=[{
            "source": "conversation",
            "user_msg_preview": user_msg[:100],
            "timestamp": time.time(),
        }],
    )
    logger.info("Stored conversation memory: %s...", user_msg[:50])


async def store_conversation(user_msg: str, response: str, user_id: int = 0) -> None:
    """Store a conversation turn in ChromaDB (async, fire-and-forget)."""
    if not MEMORY_ENABLED:
        return

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _store_sync, user_msg, response, user_id)
    except Exception as e:
        logger.error("Failed to store conversation memory: %s", e)


# ── Explicit /remember ─────────────────────────────────────────


def _remember_sync(text: str, user_id: int) -> None:
    """Save text to both ChromaDB and the user's remembered.md."""
    collection = _get_collection(user_id)

    doc_id = f"remember:{hashlib.sha256(text.encode()).hexdigest()}:{time.time()}"
    collection.upsert(
        ids=[doc_id],
        documents=[text],
        metadatas=[{
            "source": "remembered",
            "timestamp": time.time(),
        }],
    )

    _ensure_dirs(user_id)
    mem_dir = Path(_user_config(user_id)[0])
    remembered_path = mem_dir / "remembered.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(remembered_path, "a", encoding="utf-8") as f:
        f.write(f"\n\n## {timestamp}\n\n{text}\n")

    logger.info("Remembered: %s...", text[:50])


async def remember(text: str, user_id: int = 0) -> str:
    """Explicitly save something to memory. Returns confirmation message."""
    if not MEMORY_ENABLED:
        return "Memory is disabled."

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _remember_sync, text, user_id)
        return f"Remembered: \"{text[:100]}{'...' if len(text) > 100 else ''}\""
    except Exception as e:
        logger.error("Remember failed: %s", e)
        return f"Failed to save memory: {e}"


# ── Post-processing extraction (safety net) ────────────────────


# Phrases that signal the user wants something remembered
_REMEMBER_TRIGGERS = (
    "remember that", "remember this", "don't forget",
    "keep in mind", "note that", "save this",
    "my name is", "i go by", "call me",
    "i moved to", "i live in", "i work at",
    "i started", "i quit", "i joined",
)


def _extract_and_save_sync(user_msg: str, response: str, user_id: int) -> None:
    """Check if the user said something worth auto-saving to remembered.md.

    This is a safety net — if Claude's system prompt instruction to update
    Memory files didn't fire, this catches explicit 'remember' requests.
    Only triggers on clear user intent, not every message.
    """
    msg_lower = user_msg.lower()

    # Check if any trigger phrase is in the user's message
    matched = any(trigger in msg_lower for trigger in _REMEMBER_TRIGGERS)
    if not matched:
        return

    # Save the user's message (not the response) as a remembered fact
    _ensure_dirs(user_id)
    mem_dir = Path(_user_config(user_id)[0])
    remembered_path = mem_dir / "remembered.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with open(remembered_path, "a", encoding="utf-8") as f:
        f.write(f"\n\n## {timestamp} (auto-extracted)\n\n{user_msg}\n")

    # Also store in ChromaDB with special source tag
    collection = _get_collection(user_id)
    doc_id = f"auto:{hashlib.sha256(user_msg.encode()).hexdigest()}:{time.time()}"
    collection.upsert(
        ids=[doc_id],
        documents=[user_msg],
        metadatas=[{
            "source": "auto-extracted",
            "timestamp": time.time(),
        }],
    )
    logger.info("Auto-extracted memory: %s...", user_msg[:50])


async def extract_and_save(user_msg: str, response: str, user_id: int = 0, owner_only: bool = False) -> None:
    """Post-processing: auto-extract facts from conversation (async, fire-and-forget).

    Args:
        owner_only: When True (non-owner user message), skip auto-extraction entirely.
                    Prevents memory poisoning via user-crafted trigger phrases.
    """
    if not MEMORY_ENABLED:
        return
    if owner_only:
        return  # Non-owner messages never write to persistent memory

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _extract_and_save_sync, user_msg, response, user_id)
    except Exception as e:
        logger.error("Auto-extraction failed: %s", e)


# ── Stats and re-index ─────────────────────────────────────────


def _get_stats_sync(user_id: int) -> dict:
    collection = _get_collection(user_id)
    mem_dir, col_name = _user_config(user_id)
    memory_dir = Path(mem_dir)
    file_count = sum(1 for _ in memory_dir.glob("*.md")) + sum(1 for _ in memory_dir.glob("*.txt"))
    remembered_path = memory_dir / "remembered.md"

    return {
        "total_entries": collection.count(),
        "collection": col_name,
        "memory_dir": mem_dir,
        "text_files": file_count,
        "remembered_file": remembered_path.exists(),
        "enabled": MEMORY_ENABLED,
    }


async def get_stats(user_id: int = 0) -> dict:
    """Get memory statistics for the given user."""
    if not MEMORY_ENABLED:
        return {"enabled": False}

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _get_stats_sync, user_id)
    except Exception as e:
        logger.error("Memory stats failed: %s", e)
        return {"enabled": True, "error": str(e)}


async def reindex(user_id: int = 0) -> int:
    """Re-index all text files for the given user. Returns chunk count."""
    return await index_files(user_id)
