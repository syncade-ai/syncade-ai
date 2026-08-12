"""Shared helpers for the split ``test_adapters_openai.py`` files."""

from __future__ import annotations

import json

from syncade.config import ReviewerConfig


def _make_config(
    *,
    name: str = "codex-reviewer",
    provider: str = "openai",
    model: str = "gpt-5-codex",
    thinking: str = "high",
    permissions: str = "yolo",
) -> ReviewerConfig:
    return ReviewerConfig(
        name=name,
        provider=provider,
        model=model,
        thinking=thinking,  # type: ignore[arg-type]
        permissions=permissions,  # type: ignore[arg-type]
    )


def _jsonl_envelope(
    agent_messages: list[str] | None = None,
    *,
    error_messages: list[str] | None = None,
    turn_failed_message: str | None = None,
    turn_failed_type: str | None = None,
) -> str:
    """Construct a JSONL stream matching codex exec --json output
    (per the CLI output format). Only the fields the adapter
    consumes are populated."""
    lines: list[str] = [
        json.dumps({"type": "thread.started", "thread_id": "test-thread"}),
        json.dumps({"type": "turn.started"}),
    ]
    for msg in error_messages or []:
        lines.append(json.dumps({"type": "error", "message": msg}))
    for i, text in enumerate(agent_messages or []):
        lines.append(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"item_{i}",
                        "type": "agent_message",
                        "text": text,
                    },
                }
            )
        )
    if turn_failed_message is not None:
        error: dict[str, str] = {"message": turn_failed_message}
        if turn_failed_type is not None:
            error["type"] = turn_failed_type
        lines.append(json.dumps({"type": "turn.failed", "error": error}))
    else:
        lines.append(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )
        )
    return "\n".join(lines) + "\n"
