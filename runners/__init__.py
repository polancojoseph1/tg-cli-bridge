"""CLI runner adapters for bridgebot.

Factory function creates the appropriate runner based on CLI_RUNNER config.
"""

from runners.base import RunnerBase


def create_runner() -> RunnerBase:
    """Create the runner adapter for the configured CLI_RUNNER."""
    from config import CLI_RUNNER

    if CLI_RUNNER == "claude":
        from runners.claude import ClaudeRunner
        return ClaudeRunner()
    elif CLI_RUNNER == "gemini":
        from runners.gemini import GeminiRunner
        return GeminiRunner()
    elif CLI_RUNNER == "codex":
        from runners.codex import CodexRunner
        return CodexRunner()
    elif CLI_RUNNER == "qwen":
        from runners.qwen import QwenRunner
        return QwenRunner()
    elif CLI_RUNNER == "generic":
        from runners.generic import GenericRunner
        return GenericRunner()
    elif CLI_RUNNER in ("opencode", "freecode"):
        from runners.freecode import FreeCodeBaseRunner
        return FreeCodeBaseRunner()
    elif CLI_RUNNER == "free":
        from runners.free import FreeCodeRunner
        return FreeCodeRunner()
    elif CLI_RUNNER == "router":
        from runners.cli_router import CLIRouterRunner
        return CLIRouterRunner()
    else:
        raise ValueError(f"Unknown CLI_RUNNER: '{CLI_RUNNER}'. Use: claude, gemini, codex, qwen, freecode, generic, free, or router")
