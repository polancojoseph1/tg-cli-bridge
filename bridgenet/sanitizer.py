"""Prompt sanitization for BridgeNet — strip injection attempts before any
peer-originated or relay-forwarded task reaches the AI runner.

This is a security layer, not a content filter. It targets patterns that
attempt to override system instructions, impersonate system roles, or inject
persona changes into the model context.
"""

import logging
import re

logger = logging.getLogger("bridge.bridgenet.sanitizer")

# Maximum characters allowed in a sanitized task
_MAX_LENGTH = 8000

# ── Injection patterns ───────────────────────────────────────────────────────
# Each tuple is (human_readable_name, compiled_regex).
# All patterns are case-insensitive and applied to the full text.

_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "ignore_instructions",
        re.compile(
            r"ignore\s+(previous|all|your)\s+instructions",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_instructions",
        re.compile(
            r"disregard\s+(previous|all|your)\s+instructions",
            re.IGNORECASE,
        ),
    ),
    (
        "you_are_now",
        re.compile(
            r"you\s+are\s+now\s+(a|an)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pretend_you_are",
        re.compile(
            r"pretend\s+(you\s+are|to\s+be)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "act_as",
        re.compile(
            r"act\s+as\s+(a|an|if)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forget_everything",
        re.compile(
            r"forget\s+(everything|all\s+instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "new_persona",
        re.compile(
            r"\bnew\s+persona\b",
            re.IGNORECASE,
        ),
    ),
    (
        "line_starting_system",
        re.compile(
            r"(?m)^system\s*:\s*",
            re.IGNORECASE,
        ),
    ),
    (
        "markdown_role_header",
        re.compile(
            r"###\s*(SYSTEM|HUMAN|ASSISTANT)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_code_block",
        re.compile(
            r"```\s*system\b",
            re.IGNORECASE,
        ),
    ),
]


def sanitize_task(text: str, task_type: str = "chat") -> tuple[str, list[str]]:
    """Strip prompt-injection patterns from a peer-supplied task string.

    Args:
        text:      The raw task content supplied by the peer or relay.
        task_type: Informational label for logging (e.g. "chat", "delegate").

    Returns:
        A tuple of (sanitized_text, violations_found) where:
          - sanitized_text is the cleaned string, truncated to _MAX_LENGTH chars.
          - violations_found is a list of human-readable violation names (may be empty).

    Security note: violation *content* is never logged — only the violation
    name and the peer/task_type label. This prevents injection content from
    leaking into log aggregators.
    """
    violations: list[str] = []
    sanitized = text

    for name, pattern in _PATTERNS:
        if pattern.search(sanitized):
            violations.append(name)
            # Replace the matched span(s) with a neutral placeholder
            sanitized = pattern.sub("[redacted]", sanitized)

    # Enforce length cap
    if len(sanitized) > _MAX_LENGTH:
        sanitized = sanitized[:_MAX_LENGTH]

    if violations:
        logger.warning(
            "sanitize_task: %d injection pattern(s) detected in task_type=%s — violations=%s",
            len(violations),
            task_type,
            violations,
        )

    return sanitized, violations


def is_safe_task(text: str, task_type: str = "chat") -> bool:
    """Return True if no injection patterns are found in text.

    This is a quick check that does not modify the text. If you need both
    the sanitized text and the violation list, call sanitize_task() instead.
    """
    for _name, pattern in _PATTERNS:
        if pattern.search(text):
            return False
    return True
