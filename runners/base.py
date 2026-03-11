"""Abstract base class for CLI runner adapters.

Each adapter wraps a specific AI CLI tool (Claude, Gemini, Codex, etc.)
and provides a uniform interface for:
  - Running prompts with streaming progress
  - Stateless one-shot queries
  - Session management (start, resume, reset)
  - Process lifecycle (start, stop, kill)
"""

from abc import ABC, abstractmethod
import asyncio
import os
import platform
import shutil
import subprocess
import sys
from typing import AsyncGenerator, Callable, Awaitable, Any

# Subprocess logger wrapper script path
_SUBPROCESS_LOGGER = os.path.join(os.path.dirname(__file__), "subprocess_logger.py")
_LOG_DIR = os.path.expanduser("~/.jefe/subprocess_logs")


class RunnerBase(ABC):
    """Base class all CLI runner adapters must implement."""

    # Subclasses set these
    name: str = ""              # e.g. "claude", "gemini", "codex"
    cli_command: str = ""       # binary name to find in PATH (e.g. "claude")

    @staticmethod
    def _brief_thought(text: str, limit: int = 80) -> str:
        """Condense a thinking/planning string for display as a status bubble.

        - Takes only the first line
        - Strips common filler openers ("The user wants", "I need to", etc.)
        - Truncates at the last word boundary before `limit` chars
        - Appends … if truncated
        """
        line = text.strip().splitlines()[0] if text.strip() else ""
        if not line:
            return ""
        # Strip redundant filler openers
        _FILLERS = (
            "the user wants to ", "the user wants ", "the user asked ",
            "the user is asking ", "the user needs ",
            "i need to ", "i will ", "i am going to ", "i should ", "i'm going to ",
        )
        lower = line.lower()
        for filler in _FILLERS:
            if lower.startswith(filler):
                line = line[len(filler):]
                line = line[0].upper() + line[1:] if line else line
                break
        # Truncate at word boundary
        if len(line) <= limit:
            return line
        truncated = line[:limit].rsplit(" ", 1)[0].rstrip(",;:")
        return truncated + "…"

    def discover_binary(self) -> str:
        """Find the CLI binary in PATH. Raises FileNotFoundError if missing."""
        path = shutil.which(self.cli_command)
        if path is None:
            raise FileNotFoundError(
                f"{self.cli_command} CLI not found in PATH. "
                f"Is {self.name} installed?"
            )
        return path

    def is_available(self) -> bool:
        """Check if the CLI binary exists in PATH."""
        return shutil.which(self.cli_command) is not None

    @abstractmethod
    async def run(
        self,
        message: str,
        instance: Any,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        image_path: str | None = None,
        memory_context: str = "",
    ) -> str:
        """Run a prompt with full session tracking and streaming progress.

        Args:
            message: The user's message text.
            instance: Instance object with session state (from instance_manager).
            on_progress: Async callback for tool-use status updates.
            image_path: Optional path to an image file to include.
            memory_context: Optional ChromaDB memory context to inject.

        Returns:
            The assistant's response text.
        """

    @abstractmethod
    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query for automation (no session, no memory).

        Args:
            prompt: The prompt text.
            timeout: Timeout in seconds.

        Returns:
            The response text.
        """

    @abstractmethod
    async def stop(self, instance: Any) -> bool:
        """Stop the running process for a specific instance.

        Returns True if a process was actually stopped.
        """

    @abstractmethod
    def new_session(self, instance: Any) -> None:
        """Reset session state so the next message starts a fresh conversation."""

    async def stop_all(self, instances: list) -> int:
        """Stop processes for all given instances. Returns count stopped."""
        count = 0
        for inst in instances:
            if await self.stop(inst):
                count += 1
        return count

    async def kill_all(self) -> int:
        """Kill ALL processes of this CLI type on the system.

        Default implementation does nothing. Override for pkill-based cleanup.
        Returns count killed (0 or 1).
        """
        return 0

    @staticmethod
    def _kill_processes(pattern: str) -> int:
        """Kill processes matching pattern. Cross-platform (Windows + Unix).

        On Windows: uses taskkill /F /IM <pattern.exe>
        On Unix: uses pkill -9 -f <pattern>
        Returns 1 if any were killed, 0 otherwise.
        """
        try:
            if platform.system() == "Windows":
                # Ensure .exe suffix for taskkill
                exe = pattern if pattern.endswith(".exe") else f"{pattern.split()[0]}.exe"
                result = subprocess.run(
                    ["taskkill", "/F", "/IM", exe],
                    capture_output=True, timeout=5,
                )
            else:
                result = subprocess.run(
                    ["pkill", "-9", "-f", pattern],
                    capture_output=True, timeout=5,
                )
            return 1 if result.returncode == 0 else 0
        except Exception:
            return 0

    @staticmethod
    def make_log_path(bot_name: str, chat_id: int, instance_id: int) -> str:
        """Return the log file path for a specific instance."""
        os.makedirs(_LOG_DIR, exist_ok=True)
        return os.path.join(_LOG_DIR, f"{bot_name}_{chat_id}_{instance_id}.log")

    @staticmethod
    def get_pid_start_time(pid: int) -> str:
        """Return a string identifier for the process start time, for recycling detection."""
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def is_pid_alive(pid: int, start_time: str) -> bool:
        """Return True if pid is still running AND has the same start time (not recycled)."""
        if not pid:
            return False
        current = RunnerBase.get_pid_start_time(pid)
        if not current:
            return False
        return current == start_time

    @staticmethod
    async def tail_log_file(
        log_path: str,
        start_offset: int = 0,
        proc=None,
    ) -> AsyncGenerator[tuple, None]:
        """Async generator: tail a log file, yielding (line, offset) as they appear.

        Polls every 50ms. Stops when proc exits (if provided) and all lines are read.
        """
        try:
            f = open(log_path, "r", errors="replace")
        except OSError:
            return

        try:
            f.seek(start_offset)
            while True:
                line = f.readline()
                if line:
                    yield line.rstrip("\n"), f.tell()
                else:
                    if proc is not None and proc.returncode is not None:
                        # Process done — drain any remaining lines
                        for line in f:
                            yield line.rstrip("\n"), f.tell()
                        break
                    await asyncio.sleep(0.05)
        finally:
            f.close()

    def format_tool_progress(self, name: str, params: dict) -> str:
        """Format a tool call into a human-readable progress string.

        Override in subclasses for CLI-specific tool names.
        """
        if name in ("Bash", "shell", "bash", "run_shell_command", "exec_command"):
            cmd = params.get("command", params.get("cmd", ""))
            return f"\u26a1 Shell: {cmd[:200]}" if cmd else "\u26a1 Shell"
        elif name in ("Edit", "edit_file"):
            return f"\u270f\ufe0f Edit: {params.get('file_path', '')}"
        elif name in ("Write", "write_file", "write_new_file", "apply_patch"):
            return f"\U0001f4dd Write: {params.get('file_path', params.get('path', ''))}"
        elif name == "read_file":
            return f"\U0001f4c4 Read: {params.get('file_path', params.get('path', ''))}"
        elif name in ("Read", "Grep", "Glob", "list_directory"):
            # Silent — noisy filesystem lookups not worth surfacing to user
            return ""
        elif name in ("WebFetch", "web_fetch"):
            return f"\U0001f310 Fetch: {params.get('url', '')}"
        elif name in ("WebSearch", "google_web_search"):
            query = params.get("query", params.get("search_query", ""))
            return f"\U0001f50e Search: {query}"
        elif name in ("Agent", "Task"):
            desc = params.get("description", params.get("prompt", ""))[:100]
            return f"\U0001f916 Sub-agent: {desc}"
        elif name:
            return f"\U0001f527 {name}"
        return ""
