"""The "Configuring syncade" skill section: the in-harness config surface (pr-v2-32 / pr-v2-33).

This section is OUTSIDE the ``SYNCADE-SHARED`` span (harness-specific), so the drift test does not
cover it — but it must stay consistent across the 4 skill copies and keep referencing the right
commands, else the harness would drive a stale/wrong config surface.

pr-v2-33 settled the division of labour: **browsing belongs in a terminal** (the curses TUI is the
real menu and cannot run in a chat pane — a skill emits text, and the harness's own menu chrome
is not available to it), while **a named change belongs in-pane** (natural language →
``--config set``). The section must say both, so a future assistant neither fakes a menu with a
wide table nor sends the operator to a terminal for a one-line edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_COPIES = [
    _REPO / ".claude" / "skills" / "syncade" / "SKILL.md",
    _REPO / ".codex" / "skills" / "syncade" / "SKILL.md",
    _REPO / "src" / "syncade" / "skills" / "claude" / "SKILL.md",
    _REPO / "src" / "syncade" / "skills" / "codex" / "SKILL.md",
]


def _config_section(path: Path) -> str:
    text = path.read_text()
    start = text.index("## Configuring syncade")
    end = text.index("\n## ", start + 1)  # the next top-level header (## Workflow)
    return text[start:end]


def test_config_sections_identical_across_copies():
    """Both harnesses drive the same config surface, and each `src/` bundle must match its canonical
    copy (else `--install-skill` ships a different flow than the repo runs)."""
    sections = [_config_section(p) for p in _COPIES]
    assert all(s == sections[0] for s in sections[1:]), "the config section drifted across copies"


@pytest.mark.parametrize("path", _COPIES)
def test_browsing_points_at_the_terminal_tui(path):
    """pr-v2-33: the real menu is the terminal TUI. The section must point there for browsing and
    say why it can't run in the pane — NOT imitate a menu with a table."""
    section = _config_section(path)
    assert "arrow keys" in section, "must describe the terminal TUI as the real menu"
    assert "real terminal" in section, "must say why the TUI can't run in the pane"
    assert "no wide tables" in section, "must forbid faking a menu with a table"


@pytest.mark.parametrize("path", _COPIES)
def test_in_pane_editing_is_the_natural_language_path(path):
    section = _config_section(path)
    # The full-surface read backs the shadow check; `set` applies the change.
    assert "--config list --all" in section
    assert "--config set" in section
    # thinking/effort must be mappable — the operator asks for it by name ("medium effort").
    assert "producer.thinking" in section
    # Target semantics + shadow legibility + the never-write-broken guarantee.
    assert "--repo" in section
    assert "overrides global" in section
    assert "exit 50" in section
