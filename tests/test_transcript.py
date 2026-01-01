"""Tests for :mod:`syncade.transcript` (PR-D — cold-drafter front door).

Hand-built Claude Code session JSONL fixtures, no mocks. The parser is the ONLY
Claude-Code-format-aware component; it turns the raw JSONL into a clean
role-labeled dialogue text for the cold drafter, dropping the noise that is "what
was built" (tool calls/results) or pure harness injection (system reminders,
sidechains, queue operations).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncade.transcript import TranscriptError, parse_transcript


def _jsonl(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return p


def _user(content, *, sidechain: bool = False) -> dict:
    return {
        "type": "user",
        "isSidechain": sidechain,
        "message": {"role": "user", "content": content},
    }


def _assistant(content, *, sidechain: bool = False) -> dict:
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"role": "assistant", "content": content},
    }


class TestParseTranscript:
    def test_extracts_user_and_assistant_text_in_order(self, tmp_path: Path):
        path = _jsonl(
            tmp_path,
            [
                _user("Please add a login button."),
                _assistant(
                    [
                        {"type": "thinking", "thinking": "internal reasoning"},
                        {"type": "text", "text": "I'll add a blue login button."},
                        {"type": "tool_use", "name": "Edit", "input": {}},
                    ]
                ),
                _user("Actually, make it green."),
            ],
        )
        out = parse_transcript(path)
        assert "Please add a login button." in out
        assert "I'll add a blue login button." in out
        assert "Actually, make it green." in out
        # order preserved
        assert (
            out.index("login button") < out.index("blue login button") < out.index("make it green")
        )

    def test_role_labels_present(self, tmp_path: Path):
        path = _jsonl(tmp_path, [_user("hi"), _assistant([{"type": "text", "text": "hello"}])])
        out = parse_transcript(path)
        assert "User:" in out and "Assistant:" in out

    def test_strips_system_reminder(self, tmp_path: Path):
        path = _jsonl(
            tmp_path,
            [_user("<system-reminder>HARNESS NOISE do not include</system-reminder>Make it blue.")],
        )
        out = parse_transcript(path)
        assert "Make it blue." in out
        assert "HARNESS NOISE" not in out

    def test_strips_slash_command_markers(self, tmp_path: Path):
        # QA finding (real-data, 2026-06-03): slash-command invocations
        # (`/model`, `/compact`, …) inject <command-name>/<command-message>/
        # <command-args> wrappers that are meta-noise, not build intent. Strip
        # them; a turn that is ONLY a slash command drops out (no real text).
        path = _jsonl(
            tmp_path,
            [
                _user(
                    "<command-name>/model</command-name>\n"
                    "<command-message>model</command-message>\n"
                    "<command-args></command-args>"
                ),
                _user("add a login button"),
            ],
        )
        out = parse_transcript(path)
        assert "add a login button" in out
        assert "command-name" not in out and "/model" not in out

    def test_drops_thinking_tooluse_toolresult(self, tmp_path: Path):
        path = _jsonl(
            tmp_path,
            [
                _assistant(
                    [
                        {"type": "thinking", "thinking": "SECRET THINKING"},
                        {"type": "text", "text": "kept assistant text"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "TOOLUSE"}},
                    ]
                ),
                _user([{"type": "tool_result", "content": "TOOLRESULT output"}]),
            ],
        )
        out = parse_transcript(path)
        assert "kept assistant text" in out
        assert "SECRET THINKING" not in out
        assert "TOOLUSE" not in out
        assert "TOOLRESULT" not in out

    def test_drops_queue_operation_and_sidechain(self, tmp_path: Path):
        path = _jsonl(
            tmp_path,
            [
                {"type": "queue-operation", "operation": "QUEUEOP"},
                _user("real user intent here"),
                _user("SIDECHAIN subagent noise", sidechain=True),
                _assistant([{"type": "text", "text": "SIDECHAIN assistant"}], sidechain=True),
            ],
        )
        out = parse_transcript(path)
        assert "real user intent here" in out
        assert "QUEUEOP" not in out
        assert "SIDECHAIN" not in out

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(TranscriptError):
            parse_transcript(tmp_path / "nope.jsonl")

    def test_empty_file_raises(self, tmp_path: Path):
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        with pytest.raises(TranscriptError):
            parse_transcript(p)

    def test_no_extractable_turns_raises(self, tmp_path: Path):
        # only noise → nothing to draft from → error, not an empty string
        path = _jsonl(
            tmp_path,
            [
                {"type": "queue-operation", "operation": "x"},
                _user([{"type": "tool_result", "content": "only a tool result"}]),
            ],
        )
        with pytest.raises(TranscriptError):
            parse_transcript(path)

    def test_malformed_lines_skipped_but_valid_kept(self, tmp_path: Path):
        # Amended contract (dogfood 2026-06-02T13-45-00): individual malformed
        # lines are SKIPPED, not fatal — robust to a live session's trailing
        # mid-write partial line. (See test_skipped_malformed_lines_warn_loudly
        # for the non-silence guarantee.)
        p = tmp_path / "mixed.jsonl"
        good = json.dumps(_user("valid intent survives"))
        p.write_text("this is not json\n" + good + "\n{also bad\n", encoding="utf-8")
        out = parse_transcript(p)
        assert "valid intent survives" in out

    def test_skipped_malformed_lines_warn_loudly(self, tmp_path: Path, capsys):
        # The skip must NOT be silent — that was codex's data-loss objection.
        # A loud stderr warning naming the count is emitted (survives --quiet).
        p = tmp_path / "noisy.jsonl"
        good = json.dumps(_user("valid intent survives"))
        p.write_text("BAD LINE 1\n" + good + "\nBAD LINE 2\n", encoding="utf-8")
        parse_transcript(p)
        err = capsys.readouterr().err
        assert "skipped 2 malformed" in err
