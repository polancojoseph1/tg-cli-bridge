"""Per-agent memory — ChromaDB collections and KuzuDB knowledge graph.

Each agent gets its own ChromaDB collection: "agent_<agent_id>"
KuzuDB tracks entities and relationships discovered across agent tasks.

Usage:
    from agent_memory import store_agent_work, search_agent_memory, get_agent_context
"""

import hashlib
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("bridge.agent_memory")

from config import MEMORY_DIR  # noqa: E402
MEMORY_BASE = Path(MEMORY_DIR)
CHROMA_PATH = str(MEMORY_BASE / ".chroma_db")
GRAPH_DB_PATH = str(MEMORY_BASE / ".graph_db")

# Lazy-loaded ChromaDB collections per agent
_agent_collections: dict[str, object] = {}

# KuzuDB connection (lazy)
_kuzu_conn = None
_kuzu_available = False


# ── ChromaDB per-agent collections ─────────────────────────────────────────


def _collection_name(agent_id: str) -> str:
    return f"agent_{agent_id}"


def _get_agent_collection(agent_id: str):
    """Get or create a ChromaDB collection for this agent."""
    col_name = _collection_name(agent_id)
    if col_name in _agent_collections:
        return _agent_collections[col_name]

    try:
        import chromadb
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_or_create_collection(
            name=col_name,
            metadata={"hnsw:space": "cosine"},
        )
        _agent_collections[col_name] = collection
        logger.info("Agent collection '%s' ready (%d entries)", col_name, collection.count())
        return collection
    except Exception as e:
        logger.error("Failed to get agent collection '%s': %s", col_name, e)
        return None


def store_agent_work(agent_id: str, task: str, result: str) -> str:
    """Store a completed agent task+result in the agent's ChromaDB collection.
    Also updates the KuzuDB knowledge graph with extracted entities.
    Returns the doc_id so callers can link outcomes to this task.
    """
    collection = _get_agent_collection(agent_id)
    ts = time.time()

    combined = f"Task: {task}\n\nResult: {result}"
    if len(combined) > 3000:
        combined = combined[:3000] + "..."

    doc_id = f"agent:{agent_id}:task:{hashlib.sha256(combined.encode()).hexdigest()[:16]}:{ts}"

    if collection is not None:
        try:
            collection.upsert(
                ids=[doc_id],
                documents=[combined],
                metadatas=[{
                    "source": "agent_task",
                    "agent_id": agent_id,
                    "task_preview": task[:100],
                    "timestamp": ts,
                }],
            )
            logger.info("Stored agent work for '%s': %s...", agent_id, task[:60])
        except Exception as e:
            logger.error("Failed to store agent work for '%s': %s", agent_id, e)

    # Always create task node in graph (entity extraction is optional bonus)
    entities = _extract_entities(result)
    _update_graph(agent_id, doc_id, task, entities)

    return doc_id


def search_agent_memory(agent_id: str, query: str, n: int = 5) -> str:
    """Search this agent's ChromaDB collection for relevant past work.
    Returns a formatted string or empty string if nothing relevant found.
    """
    collection = _get_agent_collection(agent_id)
    if collection is None or collection.count() == 0:
        return ""

    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(n, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error("Agent memory search failed for '%s': %s", agent_id, e)
        return ""

    docs = results.get("documents", [[]])[0]
    distances = results.get("distances", [[]])[0]
    if not docs:
        return ""

    # Filter to reasonably relevant results (cosine distance < 0.8)
    relevant = [(doc, dist) for doc, dist in zip(docs, distances) if dist < 0.8]
    if not relevant:
        return ""

    lines = [f"[{agent_id.title()} Agent — past work on similar topics:]"]
    for i, (doc, _) in enumerate(relevant[:3], 1):
        preview = doc[:300].replace("\n", " ")
        lines.append(f"{i}. {preview}...")
    return "\n".join(lines)


def get_agent_context(agent_id: str, query: str) -> str:
    """Combined context: agent's ChromaDB memory + KuzuDB graph context.
    Returns a formatted string to inject into the agent's system prompt.
    """
    parts = []

    mem = search_agent_memory(agent_id, query)
    if mem:
        parts.append(mem)

    graph_ctx = _query_graph_context(agent_id, query)
    if graph_ctx:
        parts.append(graph_ctx)

    return "\n\n".join(parts)


# ── Entity extraction (best-effort regex) ──────────────────────────────────


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_DOLLAR_RE = re.compile(r"\$[\d.,]+\s*(?:million|billion|thousand|M|B|K)\b", re.IGNORECASE)
_COMPANY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b")  # 2-4 capitalized words


def _extract_entities(text: str) -> list[dict]:
    """Extract URLs, dollar amounts, and potential company/org names from text."""
    entities = []

    for url in _URL_RE.findall(text)[:10]:
        entities.append({"type": "url", "value": url[:200]})

    for amount in _DOLLAR_RE.findall(text)[:5]:
        entities.append({"type": "money", "value": amount})

    # Rough org name extraction — capitalized bigrams/trigrams
    for match in _COMPANY_RE.findall(text)[:10]:
        if len(match) > 4 and match not in ("The", "This", "That", "They", "Their"):
            entities.append({"type": "org", "value": match})

    return entities


# ── KuzuDB knowledge graph ──────────────────────────────────────────────────


def _get_kuzu_conn():
    global _kuzu_conn, _kuzu_available
    if _kuzu_conn is not None:
        return _kuzu_conn
    try:
        import kuzu
        db = kuzu.Database(GRAPH_DB_PATH)
        _kuzu_conn = kuzu.Connection(db)
        _init_graph_schema()
        _kuzu_available = True
        logger.info("KuzuDB connected at %s", GRAPH_DB_PATH)
        return _kuzu_conn
    except ImportError:
        logger.info("kuzu not installed — graph features disabled")
        return None
    except Exception as e:
        logger.warning("KuzuDB init failed: %s", e)
        return None


def _init_graph_schema() -> None:
    conn = _kuzu_conn
    if conn is None:
        return
    schema_stmts = [
        "CREATE NODE TABLE IF NOT EXISTS Agent(id STRING, name STRING, PRIMARY KEY(id))",
        "CREATE NODE TABLE IF NOT EXISTS AgentTask(id STRING, description STRING, timestamp DOUBLE, PRIMARY KEY(id))",
        "CREATE NODE TABLE IF NOT EXISTS Entity(id STRING, value STRING, entity_type STRING, PRIMARY KEY(id))",
        # Outcome node — records feedback/corrections on a task
        "CREATE NODE TABLE IF NOT EXISTS Outcome(id STRING, feedback STRING, outcome_type STRING, timestamp DOUBLE, PRIMARY KEY(id))",
        "CREATE REL TABLE IF NOT EXISTS PERFORMED(FROM Agent TO AgentTask)",
        "CREATE REL TABLE IF NOT EXISTS DISCOVERED(FROM AgentTask TO Entity)",
        "CREATE REL TABLE IF NOT EXISTS RELATES_TO(FROM Entity TO Entity)",
        # PRODUCED links a task to its outcome (feedback signal)
        "CREATE REL TABLE IF NOT EXISTS PRODUCED(FROM AgentTask TO Outcome)",
    ]
    for stmt in schema_stmts:
        try:
            conn.execute(stmt)
        except Exception as e:
            logger.debug("Schema stmt skipped (likely exists): %s — %s", stmt[:60], e)


def _update_graph(agent_id: str, task_id: str, task_desc: str, entities: list[dict]) -> None:
    """Add agent task and discovered entities to the KuzuDB graph."""
    conn = _get_kuzu_conn()
    if conn is None:
        return
    try:
        # Upsert Agent node
        conn.execute(
            "MERGE (a:Agent {id: $id}) ON CREATE SET a.name = $name",
            {"id": agent_id, "name": agent_id.title() + " Expert"},
        )
        # Create AgentTask node ($id and $desc are reserved in KuzuDB — use prefixed names)
        conn.execute(
            "CREATE (t:AgentTask {id: $tid, description: $tdesc, timestamp: $tts})",
            {"tid": task_id, "tdesc": task_desc[:200], "tts": time.time()},
        )
        # PERFORMED edge
        conn.execute(
            "MATCH (a:Agent {id: $aid}), (t:AgentTask {id: $tid}) CREATE (a)-[:PERFORMED]->(t)",
            {"aid": agent_id, "tid": task_id},
        )
        # Entity nodes and DISCOVERED edges (optional — may be empty)
        for ent in entities[:5]:  # cap at 5 entities per task
            ent_id = hashlib.sha256(ent["value"].encode()).hexdigest()[:16]
            conn.execute(
                "MERGE (e:Entity {id: $id}) ON CREATE SET e.value = $val, e.entity_type = $etype",
                {"id": ent_id, "val": ent["value"][:200], "etype": ent["type"]},
            )
            conn.execute(
                "MATCH (t:AgentTask {id: $tid}), (e:Entity {id: $eid}) CREATE (t)-[:DISCOVERED]->(e)",
                {"tid": task_id, "eid": ent_id},
            )
    except Exception as e:
        logger.warning("Graph update failed for agent '%s': %s", agent_id, e)


def get_last_task_id(agent_id: str) -> str | None:
    """Return the KuzuDB id of the most recently performed task for this agent."""
    conn = _get_kuzu_conn()
    if conn is None:
        return None
    try:
        result = conn.execute(
            "MATCH (a:Agent {id: $aid})-[:PERFORMED]->(t:AgentTask) "
            "RETURN t.id ORDER BY t.timestamp DESC LIMIT 1",
            {"aid": agent_id},
        )
        if result.has_next():
            return str(result.get_next()[0])
    except Exception as e:
        logger.debug("get_last_task_id failed: %s", e)
    return None


def record_outcome(agent_id: str, feedback: str, outcome_type: str = "corrected", task_id: str | None = None) -> bool:
    """Record a feedback outcome linked to the agent's most recent task in KuzuDB.

    outcome_type: "corrected" | "approved" | "rejected"
    task_id: if None, uses the most recent task for this agent.
    Returns True if recorded successfully.
    """
    conn = _get_kuzu_conn()
    if conn is None:
        logger.warning("record_outcome: KuzuDB not available")
        return False

    tid = task_id or get_last_task_id(agent_id)
    if tid is None:
        logger.warning("record_outcome: no task found for agent '%s'", agent_id)
        return False

    outcome_id = f"outcome:{agent_id}:{hashlib.sha256(feedback.encode()).hexdigest()[:12]}:{time.time()}"
    try:
        conn.execute(
            "CREATE (o:Outcome {id: $oid, feedback: $fb, outcome_type: $otype, timestamp: $ots})",
            {"oid": outcome_id, "fb": feedback[:500], "otype": outcome_type, "ots": time.time()},
        )
        conn.execute(
            "MATCH (t:AgentTask {id: $tid}), (o:Outcome {id: $oid}) CREATE (t)-[:PRODUCED]->(o)",
            {"tid": tid, "oid": outcome_id},
        )
        logger.info("Recorded outcome for agent '%s' task '%s': %s", agent_id, tid[:30], feedback[:80])
        return True
    except Exception as e:
        logger.warning("record_outcome failed for '%s': %s", agent_id, e)
        return False


def _query_graph_context(agent_id: str, query: str) -> str:
    """Query KuzuDB for recently discovered entities AND recent outcome signals for this agent."""
    conn = _get_kuzu_conn()
    if conn is None:
        return ""

    parts = []

    # Recent entities discovered
    try:
        result = conn.execute(
            "MATCH (a:Agent {id: $aid})-[:PERFORMED]->(t:AgentTask)-[:DISCOVERED]->(e:Entity) "
            "RETURN e.value, e.entity_type, t.description ORDER BY t.timestamp DESC LIMIT 10",
            {"aid": agent_id},
        )
        entity_lines = []
        while result.has_next():
            row = result.get_next()
            entity_lines.append(f"  {row[1]}: {row[0]} (from: {str(row[2])[:60]})")
        if entity_lines:
            parts.append(f"[{agent_id.title()} Agent — known entities from past work:]\n" + "\n".join(entity_lines))
    except Exception as e:
        logger.debug("Entity graph query failed: %s", e)

    # Recent outcome signals — what went wrong in past tasks
    try:
        result2 = conn.execute(
            "MATCH (a:Agent {id: $aid})-[:PERFORMED]->(t:AgentTask)-[:PRODUCED]->(o:Outcome) "
            "WHERE o.outcome_type IN ['corrected', 'rejected', 'self_corrected'] "
            "RETURN o.feedback, t.description, o.timestamp ORDER BY o.timestamp DESC LIMIT 5",
            {"aid": agent_id},
        )
        correction_lines = []
        while result2.has_next():
            row = result2.get_next()
            correction_lines.append(f"  On '{str(row[1])[:50]}': {str(row[0])[:120]}")
        if correction_lines:
            parts.append(f"[{agent_id.title()} Agent — past corrections to avoid repeating:]\n" + "\n".join(correction_lines))
    except Exception as e:
        logger.debug("Outcome graph query failed: %s", e)

    return "\n\n".join(parts)


def get_last_agent_response(agent_id: str) -> str | None:
    """Fetch the most recent stored task+result text for this agent from ChromaDB.

    Used by the diagnostic step so Claude can compare what the agent said vs the correction.
    Returns the combined 'Task: ...\n\nResult: ...' string, or None if nothing is stored yet.
    """
    collection = _get_agent_collection(agent_id)
    if collection is None or collection.count() == 0:
        return None
    try:
        result = collection.get(
            where={"source": "agent_task"},
            include=["documents", "metadatas"],
        )
        docs = result.get("documents", [])
        metas = result.get("metadatas", [])
        if not docs:
            return None
        # Sort by timestamp descending, return the most recent document
        paired = sorted(zip(metas, docs), key=lambda x: x[0].get("timestamp", 0), reverse=True)
        return paired[0][1]
    except Exception as e:
        logger.error("get_last_agent_response failed for '%s': %s", agent_id, e)
        return None


async def search_shared(query: str, limit: int = 5) -> list[dict]:
    """Search only the Shared/ memory collection for friend-tier collab peers.

    Uses (or creates) a ChromaDB collection named "collab_shared" populated from
    MEMORY_DIR/Shared/ markdown and text files.

    Returns a list of {content, metadata, score} dicts.
    """
    shared_dir = MEMORY_BASE / "Shared"

    # Lazy-build the collection if it doesn't exist yet
    col_name = "collab_shared"
    if col_name not in _agent_collections:
        try:
            import chromadb
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            collection = client.get_or_create_collection(
                name=col_name,
                metadata={"hnsw:space": "cosine"},
            )
            _agent_collections[col_name] = collection

            # Index any .md / .txt files found in Shared/
            if shared_dir.exists():
                import hashlib as _hashlib
                indexed = 0
                for fpath in shared_dir.rglob("*"):
                    if fpath.suffix.lower() not in (".md", ".txt"):
                        continue
                    try:
                        text = fpath.read_text(encoding="utf-8", errors="replace")
                        if not text.strip():
                            continue
                        doc_id = "shared:" + _hashlib.sha256(str(fpath).encode()).hexdigest()[:20]
                        collection.upsert(
                            ids=[doc_id],
                            documents=[text[:4000]],
                            metadatas=[{"source": str(fpath), "collection": "shared"}],
                        )
                        indexed += 1
                    except Exception as _e:
                        logger.debug("Could not index shared file %s: %s", fpath, _e)
                logger.info("collab_shared: indexed %d files from %s", indexed, shared_dir)
        except Exception as e:
            logger.error("Failed to initialise collab_shared collection: %s", e)
            return []

    collection = _agent_collections.get(col_name)
    if collection is None or collection.count() == 0:
        return []

    try:
        results = collection.query(
            query_texts=[query],
            n_results=min(limit, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error("search_shared query failed: %s", e)
        return []

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    output = []
    for doc, meta, dist in zip(docs, metas, distances):
        # Convert cosine distance to a similarity-ish score (lower distance = higher score)
        score = round(max(0.0, 1.0 - dist), 4)
        output.append({"content": doc, "metadata": meta, "score": score})

    return output


def get_agent_graph_summary(agent_id: str) -> str:
    """Return a human-readable summary of this agent's graph knowledge."""
    conn = _get_kuzu_conn()
    if conn is None:
        return "Knowledge graph not available (kuzu not installed)."
    try:
        result = conn.execute(
            "MATCH (a:Agent {id: $aid})-[:PERFORMED]->(t:AgentTask) RETURN count(t) AS task_count",
            {"aid": agent_id},
        )
        task_count = int(result.get_next()[0]) if result.has_next() else 0

        result2 = conn.execute(
            "MATCH (a:Agent {id: $aid})-[:PERFORMED]->(t:AgentTask)-[:DISCOVERED]->(e:Entity) "
            "RETURN count(e) AS entity_count",
            {"aid": agent_id},
        )
        entity_count = int(result2.get_next()[0]) if result2.has_next() else 0

        return f"Graph: {task_count} tasks, {entity_count} entities discovered"
    except Exception as e:
        logger.debug("Graph summary failed: %s", e)
        return "Graph summary unavailable"
