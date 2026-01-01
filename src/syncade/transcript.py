"""Claude Code session-transcript parsing.

Turns a Claude Code session JSONL into a clean, role-labeled **dialogue text** the
cold drafter (:mod:`syncade.spec_draft`) reads to manufacture a spec. This is the
**only** Claude-Code-format-aware component in syncade — the firewall's "front
door" coupling is quarantined here; the agnostic core never imports it and only
ever sees the plain text this produces.

What is kept vs dropped (the firewall starts here — intent in, "what was built"
out):

- **Kept:** the text of ``type: "user"`` and ``type: "assistant"`` turns, in
  order, each labeled ``User:`` / ``Assistant:``.
- **Dropped:** ``tool_use`` / ``tool_result`` blocks (those are the actions/
  execution — i.e. *what was built*, which is the diff's job, not intent),
  assistant ``thinking`` blocks (private reasoning, not what the user affirmed),
  ``isSidechain`` turns (embedded subagent transcripts), non-dialogue entries
  (``queue-operation`` etc.), and harness-injected wrappers (``system-reminder`` /
  ``local-command-caveat`` / ``local-command-stdout`` / the slash-command
  ``command-name`` / ``command-message`` / ``command-args`` markers) that are
  never build intent.

Pure stdlib (``json`` / ``re`` / ``pathlib``); no syncade imports. The drafter's
prompt does the *semantic* firewall (forward-looking intent vs backward-looking
justification); this module only does the structural strip.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Harness-injected wrapper blocks that appear inside user turns but are never the
# user's build intent: the auto-injected context/reminder blocks, local-command
# boilerplate/output, AND slash-command invocation markers. Stripped (content
# included). (QA finding 2026-06-03: real sessions are full of `/model`,
# `/compact`, etc. whose `<command-name>/<command-message>/<command-args>` wrappers
# are meta-noise, not intent about what to build — they were leaking into the
# drafter's dialogue. A turn that is ONLY a slash command drops out as empty.)
_HARNESS_TAGS = (
    "system-reminder",
    "local-command-caveat",
    "local-command-stdout",
    "command-name",
    "command-message",
    "command-args",
)
_HARNESS_BLOCK_RE = re.compile(
    r"<(" + "|".join(_HARNESS_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)


class TranscriptError(Exception):
    """A transcript could not be read or yielded no dialogue (missing/unreadable
    file, or only noise). The CLI surfaces it as a stop-before-the-drafter per
    CLAUDE.md's "Exit-code convention for CLI mode handlers"."""


def _text_from_content(content: object) -> str:
    """Extract the human-readable text from a turn's ``message.content``: a plain
    string verbatim, or the ``text`` blocks of a content list (``thinking`` /
    ``tool_use`` / ``tool_result`` blocks dropped). Anything else → ``""``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _strip_harness(text: str) -> str:
    """Remove harness-injected wrapper blocks (see :data:`_HARNESS_TAGS`) and
    trim. What remains is the turn's actual dialogue text."""
    return _HARNESS_BLOCK_RE.sub("", text).strip()


def parse_transcript(jsonl_path: Path) -> str:
    """Parse a Claude Code session JSONL into a role-labeled dialogue text.

    Keeps user + assistant turn text in order (``User:`` / ``Assistant:``),
    dropping tool/thinking blocks, sidechains, non-dialogue entries, and
    harness-injected wrappers. Individual malformed (non-parseable) JSONL lines
    are **skipped, not fatal** — a live session transcript can legitimately have a
    trailing mid-write partial line — BUT if any are skipped a **loud stderr
    warning** is emitted (it survives ``--quiet``): silently dropping intent is the
    failure mode this guards against, so the skip is announced, never silent.
    Raises :class:`TranscriptError` only if the file is missing/unreadable or
    yields no dialogue at all.
    """
    if not jsonl_path.is_file():
        raise TranscriptError(f"transcript not found: {jsonl_path}")
    try:
        raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TranscriptError(f"could not read transcript {jsonl_path}: {exc}") from exc

    turns: list[str] = []
    skipped_malformed = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            # Skip a malformed line (robust to a few bad/partial lines, e.g. a
            # live session's trailing mid-write line) — but count it; the skip is
            # announced loudly below, never silent.
            skipped_malformed += 1
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind not in ("user", "assistant"):
            continue
        if entry.get("isSidechain") is True:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        text = _strip_harness(_text_from_content(message.get("content")))
        if not text:
            continue
        label = "User" if kind == "user" else "Assistant"
        turns.append(f"{label}: {text}")

    if not turns:
        raise TranscriptError(
            f"no user/assistant dialogue found in transcript {jsonl_path} "
            "(only tool calls, sidechains, or harness noise)"
        )
    if skipped_malformed:
        # Loud + unconditional (bypasses any logger so it survives --quiet, like
        # syncade's deprecation / scope-fallback warnings): a manufactured draft
        # that silently dropped intent is exactly the risk we refuse to ship.
        print(
            f"[syncade] transcript: skipped {skipped_malformed} malformed/unparseable "
            f"line(s) in {jsonl_path} — the manufactured draft may be missing some "
            "dialogue; check the source if the result looks incomplete.",
            file=sys.stderr,
        )
    return "\n\n".join(turns) + "\n"
