"""Multi-instance CLI session manager.

Tracks multiple CLI sessions with titles, IDs, and per-instance
session state. Each instance has its own message queue, process tracking,
and worker task so multiple instances can run concurrently.

Ownership model:
  owner_id = 0  →  global pool (primary user's instances)
  owner_id = N  →  instances belonging to user N

In server.py, translate with:
  owner_id = 0 if user_id == ALLOWED_USER_ID else user_id
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("bridge.instances")

MAX_INSTANCE_QUEUE = 10


@dataclass
class Instance:
    id: int
    title: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_started: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    # Model selection (adapter-specific, e.g. "claude-sonnet-4-6")
    model: str = ""
    # Runtime state — per-instance process and queue tracking
    process: Any = field(default=None, repr=False, compare=False)
    current_task: Any = field(default=None, repr=False, compare=False)
    processing: bool = field(default=False, repr=False, compare=False)
    was_stopped: bool = field(default=False, repr=False, compare=False)
    queue: Any = field(default=None, init=False, repr=False, compare=False)
    worker_task: Any = field(default=None, repr=False, compare=False)
    # Token tracking for context window display
    context_window: int = field(default=0, repr=False, compare=False)
    last_input_tokens: int = field(default=0, repr=False, compare=False)
    last_cache_read_tokens: int = field(default=0, repr=False, compare=False)
    last_cache_creation_tokens: int = field(default=0, repr=False, compare=False)
    last_output_tokens: int = field(default=0, repr=False, compare=False)
    last_total_tokens: int = field(default=0, repr=False, compare=False)
    session_cost: float = field(default=0.0, repr=False, compare=False)
    # Agent identity — set when this instance represents a named specialist agent
    agent_id: str | None = field(default=None, repr=False, compare=False)
    agent_system_prompt: str = field(default="", repr=False, compare=False)
    # Extensible: adapter-specific state (e.g. thread_id for Codex)
    adapter_data: dict = field(default_factory=dict, repr=False, compare=False)
    # Crash recovery: True when this instance was restored after a crash and
    # the next message is the auto-recovery prompt (not a fresh user message).
    needs_recovery: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self):
        self.queue = asyncio.Queue(maxsize=MAX_INSTANCE_QUEUE)

    def clear_queue(self) -> int:
        """Clear all pending messages from this instance's queue."""
        cleared = 0
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        return cleared


class InstanceManager:
    def __init__(self):
        self._instances: dict[int, Instance] = {}
        self._active_id: int = 1          # global active (owner_id=0)
        self._next_id: int = 1
        self._user_instance_map: dict[int, int] = {}   # user_id -> primary pinned instance_id
        self._instance_owner: dict[int, int] = {}      # instance_id -> owner_id (0 = global)
        self._user_active: dict[int, int] = {}         # owner_id -> their current active instance_id
        # Create default instance on startup
        self.create("Default", owner_id=0)

    # ------------------------------------------------------------------
    # Active instance helpers
    # ------------------------------------------------------------------

    @property
    def active(self) -> Instance:
        """Global active instance (owner_id=0)."""
        return self._instances[self._active_id]

    @property
    def active_id(self) -> int:
        return self._active_id

    @property
    def count(self) -> int:
        return len(self._instances)

    def get_active_for(self, owner_id: int) -> Instance:
        """Return the active instance for the given owner.

        owner_id=0 returns the global active.
        """
        if owner_id == 0:
            return self._instances[self._active_id]
        aid = self._user_active.get(owner_id)
        if aid and aid in self._instances:
            return self._instances[aid]
        # Fall back to pinned instance
        pinned = self.get_pinned_by_owner(owner_id)
        if pinned:
            self._user_active[owner_id] = pinned.id
            return pinned
        # Should not happen — return global active as last resort
        return self._instances[self._active_id]

    def set_active_for(self, owner_id: int, instance_id: int) -> None:
        if owner_id == 0:
            self._active_id = instance_id
        else:
            self._user_active[owner_id] = instance_id

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(self, title: str, owner_id: int = 0, switch_active: bool = True) -> Instance:
        """Create a new instance for the given owner.

        If switch_active=True (default), makes this the owner's active instance.
        Pass switch_active=False to create without changing the current active.
        """
        inst = Instance(id=self._next_id, title=title)
        self._instances[self._next_id] = inst
        self._instance_owner[self._next_id] = owner_id
        if switch_active:
            self.set_active_for(owner_id, self._next_id)
        self._next_id += 1
        logger.info("Created instance #%d: %s owner=%d (session %s)", inst.id, inst.title, owner_id, inst.session_id)
        return inst

    def switch(self, id_or_title: str, owner_id: int = 0) -> Instance | None:
        """Switch active instance by ID or title within the owner's pool.

        Returns instance or None if not found. If not found, does NOT create.
        """
        candidates = self.list_all(for_owner_id=owner_id)

        # Try by display number (1-based position in owner's list)
        if id_or_title.isdigit():
            disp_num = int(id_or_title)
            inst = self.get_by_display_num(disp_num, owner_id)
            if inst:
                self.set_active_for(owner_id, inst.id)
                logger.info("Switched owner=%d to instance #%d (display #%d): %s", owner_id, inst.id, disp_num, inst.title)
                return inst
            return None

        # Try by title (case-insensitive partial match)
        for inst in candidates:
            if id_or_title.lower() in inst.title.lower():
                self.set_active_for(owner_id, inst.id)
                logger.info("Switched owner=%d to instance #%d: %s", owner_id, inst.id, inst.title)
                return inst
        return None

    def get(self, instance_id: int) -> Instance | None:
        return self._instances.get(instance_id)

    def remove(self, instance_id: int, owner_id: int = 0) -> Instance | None:
        """Remove an instance owned by owner_id. Can't remove the last one."""
        if instance_id not in self._instances:
            return None
        # Check ownership
        if self._instance_owner.get(instance_id, 0) != owner_id:
            return None
        owner_instances = self.list_all(for_owner_id=owner_id)
        if len(owner_instances) <= 1:
            return None  # Can't remove the last instance for this owner
        removed = self._instances.pop(instance_id)
        self._instance_owner.pop(instance_id, None)
        # Update active if we just removed it
        if owner_id == 0 and instance_id == self._active_id:
            remaining = self.list_all(for_owner_id=0)
            self._active_id = remaining[0].id if remaining else next(iter(self._instances))
        elif owner_id != 0 and self._user_active.get(owner_id) == instance_id:
            remaining = self.list_all(for_owner_id=owner_id)
            if remaining:
                self._user_active[owner_id] = remaining[0].id
            else:
                self._user_active.pop(owner_id, None)
        # Cancel the worker task
        if removed.worker_task and not removed.worker_task.done():
            removed.worker_task.cancel()
        if removed.current_task and not removed.current_task.done():
            removed.current_task.cancel()
        logger.info("Removed instance #%d: %s (owner=%d)", removed.id, removed.title, owner_id)
        return removed

    def create_with_number(self, number: int, title: str, owner_id: int = 0) -> Instance:
        """Create an instance with a specific ID, or update the existing one.

        Used during crash recovery to recreate instances in their original order.
        If the instance already exists (e.g. the auto-created Default #1),
        its title and ownership are updated in-place.
        """
        if number in self._instances:
            inst = self._instances[number]
            inst.title = title
            self._instance_owner[number] = owner_id
            return inst
        inst = Instance(id=number, title=title)
        self._instances[number] = inst
        self._instance_owner[number] = owner_id
        if number >= self._next_id:
            self._next_id = number + 1
        logger.info("Restored instance #%d: %s owner=%d", number, title, owner_id)
        return inst

    # ------------------------------------------------------------------
    # User pinning (primary instance per non-primary user)
    # ------------------------------------------------------------------

    def pin_user(self, user_id: int, instance_id: int) -> None:
        """Pin a user_id to a specific instance as their primary."""
        self._user_instance_map[user_id] = instance_id
        logger.info("Pinned user %d to instance #%d", user_id, instance_id)

    def get_pinned(self, user_id: int) -> Instance | None:
        """Return the primary pinned instance for a user_id, or None."""
        inst_id = self._user_instance_map.get(user_id)
        if inst_id is not None:
            return self._instances.get(inst_id)
        return None

    def get_pinned_by_owner(self, owner_id: int) -> Instance | None:
        """Return the first instance owned by owner_id, or None."""
        for inst_id, oid in self._instance_owner.items():
            if oid == owner_id and inst_id in self._instances:
                return self._instances[inst_id]
        return None

    def ensure_pinned(self, user_id: int, title: str) -> Instance:
        """Get the primary instance for user_id, creating one if needed.

        Does NOT change the global active instance.
        """
        existing = self.get_pinned(user_id)
        if existing:
            return existing
        owner_id = user_id  # non-primary users own their own instances
        inst = Instance(id=self._next_id, title=title)
        self._instances[self._next_id] = inst
        self._instance_owner[self._next_id] = owner_id
        self._user_instance_map[user_id] = self._next_id
        self._user_active[owner_id] = self._next_id
        self._next_id += 1
        logger.info("Created pinned instance #%d: %s for user %d", inst.id, title, user_id)
        return inst

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    def rename(self, instance_id: int, new_title: str, owner_id: int | None = None) -> bool:
        """Rename an instance. If owner_id given, verifies ownership first."""
        if instance_id not in self._instances:
            return False
        if owner_id is not None and self._instance_owner.get(instance_id, 0) != owner_id:
            return False
        old = self._instances[instance_id].title
        self._instances[instance_id].title = new_title
        logger.info("Renamed instance #%d: %s -> %s", instance_id, old, new_title)
        return True

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def display_num(self, instance_id: int, owner_id: int) -> int:
        """Return the 1-based display number of an instance within the owner's list."""
        owner_instances = self.list_all(for_owner_id=owner_id)
        for i, inst in enumerate(owner_instances, start=1):
            if inst.id == instance_id:
                return i
        return instance_id  # fallback

    def get_by_display_num(self, num: int, owner_id: int) -> Instance | None:
        """Return the instance at 1-based display position num within the owner's list."""
        owner_instances = self.list_all(for_owner_id=owner_id)
        if 1 <= num <= len(owner_instances):
            return owner_instances[num - 1]
        return None

    def list_all(self, for_owner_id: int | None = None, exclude_user_ids: set[int] | None = None) -> list[Instance]:
        """Return instances filtered by owner.

        for_owner_id=None  -> all instances (no filter)
        for_owner_id=0     -> only global instances
        for_owner_id=N     -> only instances owned by user N
        exclude_user_ids   -> additionally exclude instances owned by these user_ids
        """
        excluded_inst_ids: set[int] = set()
        if exclude_user_ids:
            for inst_id, oid in self._instance_owner.items():
                if oid in exclude_user_ids:
                    excluded_inst_ids.add(inst_id)

        def _keep(inst: Instance) -> bool:
            if inst.id in excluded_inst_ids:
                return False
            if for_owner_id is None:
                return True
            return self._instance_owner.get(inst.id, 0) == for_owner_id

        return sorted(
            (i for i in self._instances.values() if _keep(i)),
            key=lambda i: i.id,
        )

    def format_list(self, for_owner_id: int | None = None, exclude_user_ids: set[int] | None = None, bot_name: str = "CLI") -> str:
        """Return a formatted HTML string of instances for display."""
        visible = self.list_all(for_owner_id=for_owner_id, exclude_user_ids=exclude_user_ids)
        if not visible:
            return "No instances."
        active_id = self._user_active.get(for_owner_id, self._active_id) if for_owner_id else self._active_id
        lines = [f"<b>{bot_name} Instances ({len(visible)}):</b>"]
        for disp_num, inst in enumerate(visible, start=1):
            marker = "\u25b6" if inst.id == active_id else " "
            status = "busy" if inst.processing else ("active" if inst.session_started else "new")
            queue_size = inst.queue.qsize() if inst.queue else 0
            queue_info = f" +{queue_size} queued" if queue_size > 0 else ""
            model_label = ""
            if inst.model:
                parts = inst.model.split("-")
                model_label = f" ({parts[1].capitalize()})" if len(parts) > 1 else f" ({inst.model})"
            agent_tag = f" \U0001f916{inst.agent_id}" if inst.agent_id else ""
            lines.append(
                f"{marker} <b>#{disp_num}</b> {inst.title}{agent_tag} [{status}{queue_info}]"
                f"{model_label} "
                f"({inst.created_at.strftime('%H:%M')})"
            )
        return "\n".join(lines)
