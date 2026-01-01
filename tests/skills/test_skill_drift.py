"""Drift guard: the Claude and Codex syncade skill copies must share their
Step 2 -> Invariants workflow span byte-for-byte.

The two ``SKILL.md`` files diverge in their harness-specific head (frontmatter,
Step 0 invocation mapping + tier handling) and their pointers, but the
load-bearing review workflow -- the safety checks, the confirmation gate, the
invoke/summary steps, the producer-commit surfacing, the failure modes, and the
invariants -- is single-source. Both copies wrap that span in
``<!-- SYNCADE-SHARED:start -->`` / ``<!-- SYNCADE-SHARED:end -->`` markers, and
this test asserts the bytes between the markers are identical.

The comparison is done on raw bytes (``read_bytes`` + split on ``b"\\n"``) rather
than decoded text so that a CRLF-vs-LF or other whitespace divergence inside the
span is caught too -- ``str.splitlines()`` + text-mode reads would silently
normalize those away and the "byte-identical" guarantee would be a lie.

This is the mechanical divergence guard the v2 PRD's PR-1 risk register calls
for (harness skill divergence is the top skill risk). If it fails you changed
the shared workflow in one copy and not the other; reconcile them so the span
matches byte-for-byte. **Edit BOTH copies together.**
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_SKILL = _REPO_ROOT / ".claude" / "skills" / "syncade" / "SKILL.md"
CODEX_SKILL = _REPO_ROOT / ".codex" / "skills" / "syncade" / "SKILL.md"

# Anchored on the ``<!-- `` prefix so the start marker's own prose (which names
# "SYNCADE-SHARED:end" mid-line) is not mistaken for the end marker. Bytes, so
# the comparison never round-trips through newline-normalizing text decoding.
_START = b"<!-- SYNCADE-SHARED:start"
_END = b"<!-- SYNCADE-SHARED:end"


def _shared_span(path: Path) -> bytes:
    """Return the raw bytes strictly between the two SHARED markers.

    Raises AssertionError if either marker is missing, duplicated, or ordered
    wrong -- a malformed marker set must fail loudly, not silently compare an
    empty span against another empty span.
    """
    lines = path.read_bytes().split(b"\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith(_START)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(_END)]
    assert len(starts) == 1, f"{path}: expected exactly one {_START!r} line, found {len(starts)}"
    assert len(ends) == 1, f"{path}: expected exactly one {_END!r} line, found {len(ends)}"
    assert starts[0] < ends[0], f"{path}: start marker must precede end marker"
    return b"\n".join(lines[starts[0] + 1 : ends[0]])


def test_skill_copies_exist() -> None:
    assert CLAUDE_SKILL.is_file(), f"missing Claude skill copy: {CLAUDE_SKILL}"
    assert CODEX_SKILL.is_file(), f"missing Codex skill copy: {CODEX_SKILL}"


def test_shared_span_is_byte_identical() -> None:
    claude_span = _shared_span(CLAUDE_SKILL)
    codex_span = _shared_span(CODEX_SKILL)
    assert claude_span == codex_span, (
        "The syncade skill copies' SYNCADE-SHARED span diverged. Edit BOTH "
        ".claude/skills/syncade/SKILL.md and .codex/skills/syncade/SKILL.md so "
        "the Step 2 -> Invariants span matches byte-for-byte."
    )


def test_shared_span_is_substantial_and_harness_neutral() -> None:
    span = _shared_span(CLAUDE_SKILL).decode("utf-8")
    assert len(span) > 500, "shared span suspiciously small -- markers misplaced?"
    # The shared span must not carry a harness-steering noun; those live only in
    # the per-harness head/pointers outside the markers.
    assert "interactive Claude" not in span
    assert "interactive Codex" not in span


def test_shared_span_surfaces_no_ship_and_decision_docs() -> None:
    """PR-v2-17: on exit 10/20/30 the skill must surface the run's operator-facing documents
    in-pane, or an escalation stalls unread (the 2026-07-11 incident that motivated the PR). This
    legibility was half-built once and the missing half bit us; lock the references so they can't
    silently regress. The span is byte-identical across copies (asserted above), so one copy is
    authoritative for the content check."""
    span = _shared_span(CLAUDE_SKILL).decode("utf-8")
    required = (
        "decision-needed.md",  # exit-10 operator doc
        "handoff.md",  # exit-20/30 operator doc
        "decision.txt",  # exit-10 continuation input
        "syncade --resume",  # exit-10 continuation command
        "clarification or operator decision needed",  # the exit-10 entry in the exit-code list
    )
    missing = [s for s in required if s not in span]
    assert not missing, (
        f"skill shared span no longer surfaces {missing} -- PR-v2-17 legibility regressed"
    )
