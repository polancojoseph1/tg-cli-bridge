"""Abstract base class for CLI runner adapters.

Each adapter wraps a specific AI CLI tool (Claude, Gemini, Codex, etc.)
and provides a uniform interface for:
  - Running prompts with streaming progress
  - Stateless one-shot queries
  - Session management (start, resume, reset)
  - Process lifecycle (start, stop, kill)
"""

from abc import ABC, abstractmethod
import platform
import shutil
import subprocess
from typing import Callable, Awaitable, Any


class RunnerBase(ABC):
    """Base class all CLI runner adapters must implement."""

    # Subclasses set these
    name: str = ""              # e.g. "claude", "gemini", "codex"
    cli_command: str = ""       # binary name to find in PATH (e.g. "claude")

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
