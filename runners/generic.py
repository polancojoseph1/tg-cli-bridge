"""Generic CLI runner adapter — fallback for any CLI that accepts a prompt.

Subprocess: <CLI_COMMAND> <prompt>
No session management, no streaming, plain text output.
"""

import asyncio
import logging
import os
import re
from typing import Callable, Awaitable

from runners.base import RunnerBase

logger = logging.getLogger("bridge.generic")


class GenericRunner(RunnerBase):
    name = "generic"

    def __init__(self):
        from config import CLI_COMMAND, CLI_TIMEOUT
        self.cli_command = CLI_COMMAND
        self.timeout = CLI_TIMEOUT

    def new_session(self, instance) -> None:
        """Generic runner has no session state — nothing to reset."""
        instance.session_started = False

    async def stop(self, instance) -> bool:
        proc = instance.process
        if proc is not None and proc.returncode is None:
            instance.was_stopped = True
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            instance.process = None
            return True
        return False

    async def kill_all(self) -> int:
        return self._kill_processes(self.cli_command)

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        """Stateless one-shot query — same as run() for generic CLIs."""
        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return f'{{"error": "{self.cli_command} CLI not found"}}'

        cmd = [binary, prompt]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.expanduser("~"),
            )
        except OSError as exc:
            return f'{{"error": "Failed to start {self.cli_command}: {exc}"}}'

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout)
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return '{"error": "timed out"}'

        return self.decode_cli_output(
            stdout_data,
            stderr_data,
            err_prefix="[error] ",
            strip_ansi=True,
            max_err_len=500,
        )

    async def run(
        self,
        message: str,
        instance,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        image_path: str | list | None = None,
        memory_context: str = "",
        on_subprocess_started: Callable[[int, str, str], None] | None = None,
        chat_id: int = 0,
    ) -> str:
        """Run the generic CLI with the message as a single argument."""
        instance.was_stopped = False

        try:
            binary = self.discover_binary()
        except FileNotFoundError:
            return f"\u274c Error: {self.cli_command} CLI not found in PATH."

        prompt = message
        if image_path:
            prompt = f"Image: {image_path}\n\n{message}" if message else f"Describe the image at: {image_path}"

        cmd = [binary, prompt]

        if on_progress:
            await on_progress(f"\u26a1 Running {self.cli_command}...")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.expanduser("~"),
            )
            instance.process = proc
        except OSError as exc:
            return f"\u274c Error starting {self.cli_command}: {exc}"

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            instance.process = None
            return f"\u23f0 {self.cli_command} took too long to respond (timed out)."

        instance.process = None

        if instance.was_stopped:
            instance.was_stopped = False
            return "\U0001f6d1 Stopped."

        if proc.returncode == 0:
            instance.session_started = True

        if proc.returncode != 0:
            err = stderr_data.decode(errors="replace").strip()
            return f"\u274c {self.cli_command} error:\n{err}" if err else f"\u274c {self.cli_command} exited with an error."

        result = stdout_data.decode(errors="replace").strip()
        return result if result else f"(empty response from {self.cli_command})"
