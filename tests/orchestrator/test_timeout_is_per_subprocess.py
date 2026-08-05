"""``[loop] timeout_seconds`` caps each SUBPROCESS, not a round — PR-h-03 item 3.

The README called it "per round, recommended ~30 min". It is the fallback wall clock for
every leg — each reviewer, the judge, the test run, each mechanical check, the producer —
so a round's worst case is a MULTIPLE of it. Measured on this repo's own config
(2 reviewers, a test command, 3 checks): 7x the configured value.

``round.py:319`` already carried a comment admitting a per-reviewer override "makes
'1800s each' a lie". The docs never caught up. These tests hold both halves in place: the
behaviour, and the words used to describe it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.checks_config import CheckConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _ship,
    _synth_clean,
    _two_reviewer_config,
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)

_T = 137.0  # distinctive, so a captured timeout cannot be coincidence


def test_one_round_hands_the_same_cap_to_several_subprocesses(repo_with_pr_doc, monkeypatch):
    """The claim, executed: ONE round, and `timeout_seconds` is spent more than once.

    A per-ROUND budget would have to divide the value across legs. It does not — every leg
    gets the whole thing, which is why the round ceiling is a multiple.
    """
    repo, pr_doc = repo_with_pr_doc
    config = _two_reviewer_config()
    config.loop.timeout_seconds = _T
    config.loop.test_command = "echo test-leg"
    config.checks = [CheckConfig(name="c1", command="echo check-leg", severity="advisory")]

    seen: list[tuple[float, str]] = []
    import syncade.test_runner as tr

    real = tr.run_subprocess

    def spy(argv, **kw):
        if kw.get("timeout") is not None:
            seen.append((kw["timeout"], " ".join(map(str, argv))))

        return real(argv, **kw)

    monkeypatch.setattr(tr, "run_subprocess", spy)

    run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_ship()), FakeAdapter(canned_output=_ship())
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_synth_clean()),
        logger=Logger(level="quiet"),
    )

    # DISTINCT legs, identified by their own command text — not one leg counted twice.
    legs = {argv for timeout, argv in seen if timeout == _T}
    assert any("test-leg" in a for a in legs) and any("check-leg" in a for a in legs), (
        f"expected the FULL loop timeout at two different legs in one round; captured {seen}. "
        "If it is now a per-round budget divided across legs, the README and the --config "
        "labels must stop calling it per-subprocess."
    )


_OPERATOR_FACING = [
    "README.md",
    "src/syncade/cli/config_mode.py",
    "src/syncade/cli/config_menu_rows.py",
    "src/syncade/cli/parser.py",
    "src/syncade/skills/claude/SKILL.md",
    "src/syncade/skills/codex/SKILL.md",
    ".claude/skills/syncade/SKILL.md",
    ".codex/skills/syncade/SKILL.md",
]


def test_no_operator_facing_surface_calls_the_timeout_per_round():
    """The words, not just the behaviour.

    The bug was never in the code — it was ten operator-facing sites, including the label
    shown while EDITING the value (`--config` / the TUI). A grep test is the only thing
    that notices when a new surface reintroduces the phrasing.
    """
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for rel in _OPERATOR_FACING:
        for i, line in enumerate((root / rel).read_text().splitlines(), 1):
            low = line.lower()
            if "per round" not in low and "per-round" not in low:
                continue
            if "timeout" in low or "time per" in low:
                # the corrective note in README is allowed to name what it is NOT
                if "not per round" in low:
                    continue
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "timeout described as per-round:\n" + "\n".join(offenders)
