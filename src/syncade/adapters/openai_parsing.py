"""JSONL-event parsing + failure classification for the codex adapter.

``openai.py``'s ``extract_final_text`` calls these as module
functions.
"""

from __future__ import annotations

import json
from typing import Final

from syncade.process import SubprocessResult

_AUTH_FAILURE_MARKERS: Final[tuple[str, ...]] = (
    "401 unauthorized",
    "unauthorized",
    "missing bearer",
    "basic authentication",
)


def _parse_jsonl_events(stdout: str) -> list[dict]:
    """Parse JSONL events from codex's stdout.

    Each non-blank line is parsed independently; lines that fail
    to parse as JSON, or parse to something that isn't a dict, are
    silently skipped. This is defensive — real
    ``codex exec --json`` emits well-formed JSONL only, but a
    future CLI update that leaks a non-JSON line shouldn't break
    the whole parse.
    """
    events: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _extract_failure_message(
    failure_events: list[dict],
    result: SubprocessResult,
) -> str:
    """Pull the most informative message out of codex's failure
    events.

    Order of preference:
    1. The LAST ``turn.failed`` event's ``error.message`` (this
       is the terminal failure message).
    2. The LAST ``error`` event's ``message``.
    3. ``stderr`` content (CLI-level failures like unknown flag).
    4. ``stdout`` snippet as a fallback.
    5. A generic "exited with code N" if nothing useful is
       available.
    """
    # Try turn.failed.error.message first, PREFIXED with error.type when present.
    #
    # codex carries its failure kind as a TYPED variant (`UsageLimitReached`, `QuotaExceeded`,
    # …) beside a message that may be generic. Reducing the event to its message alone discards
    # the only unambiguous signal in it, and downstream classification then has to guess from
    # prose. Keeping the type in the text is the smallest change that preserves it: the message
    # stays human-readable and `retry.is_usage_limit_error` gets an exact term to match instead
    # of a substring of English (PR-h-field-02 dogfood, blocker 3).
    for event in reversed(failure_events):
        if event.get("type") == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                msg = error.get("message")
                if isinstance(msg, str) and msg:
                    kind = error.get("type")
                    if isinstance(kind, str) and kind and kind not in msg:
                        return f"{kind}: {msg}"
                    return msg
    # Then bare error event messages
    for event in reversed(failure_events):
        if event.get("type") == "error":
            msg = event.get("message")
            if isinstance(msg, str) and msg:
                return msg
    # Fall back to stderr (CLI-level failures land here)
    if result.stderr.strip():
        return result.stderr.strip()
    if result.stdout.strip():
        return result.stdout.strip()
    return f"codex exited with code {result.returncode}"


def _looks_like_auth_failure(message: str, events: list[dict]) -> bool:
    """Detect codex's auth-failure pattern.

    Codex's missing-auth shape emits multiple error events whose
    messages contain "401 Unauthorized" and/or missing bearer/basic
    authentication text. Classify as auth failure only when one of
    those auth-specific markers is in the consolidated message or any
    event's text.
    """
    if _contains_auth_failure_marker(message):
        return True
    for event in events:
        for value in (
            event.get("message"),
            (event.get("error") or {}).get("message")
            if isinstance(event.get("error"), dict)
            else None,
        ):
            if isinstance(value, str) and _contains_auth_failure_marker(value):
                return True
    return False


def _contains_auth_failure_marker(text: str) -> bool:
    normalized = text.lower()
    return any(marker in normalized for marker in _AUTH_FAILURE_MARKERS)
