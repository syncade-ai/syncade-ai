"""Shared helpers for the split ``test_adapters_anthropic.py`` files."""

from __future__ import annotations

import json

from syncade.config import ReviewerConfig


def _make_config(
    *,
    name: str = "claude-reviewer",
    provider: str = "anthropic",
    model: str = "claude-opus-4-6",
    thinking: str = "high",
    permissions: str = "yolo",
) -> ReviewerConfig:
    """Construct a ReviewerConfig with sensible defaults for adapter tests."""
    return ReviewerConfig(
        name=name,
        provider=provider,
        model=model,
        thinking=thinking,  # type: ignore[arg-type]
        permissions=permissions,  # type: ignore[arg-type]
    )


def _make_envelope_stdout(
    result_text: str,
    *,
    is_error: bool = False,
    api_error_status: int | None = None,
) -> str:
    """Construct the kind of JSON envelope `claude -p --output-format
    json` emits (per the CLI output format). Only the fields the
    adapter actually consumes are populated.

    Note: ``subtype`` is observed to stay ``"success"`` even when
    ``is_error`` is True (verified live), so we mirror that quirk here.
    """
    envelope: dict = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "api_error_status": api_error_status,
        "result": result_text,
    }
    return json.dumps(envelope)


def _make_stream_stdout(
    result_text: str,
    *,
    is_error: bool = False,
    api_error_status: int | None = None,
    with_tool_use: bool = True,
) -> str:
    """Construct the JSONL stream `claude -p --output-format stream-json
    --verbose` emits (the format the reviewer adapter now uses so the full
    tool-call transcript is captured in the persisted `.stdout`).

    Shape verified live: a sequence of `system` / `assistant` / `user`
    events (with `tool_use` / `tool_result` content blocks) terminated by a
    single `{"type":"result",...}` line carrying the SAME keys the legacy
    `json` envelope had. The adapter extracts the LAST `type:"result"` line.
    """
    lines: list[str] = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s"}),
    ]
    if with_tool_use:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}
                        ]
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "1300 passed"}]},
                }
            )
        )
    lines.append(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": is_error,
                "api_error_status": api_error_status,
                "result": result_text,
            }
        )
    )
    return "\n".join(lines) + "\n"
