"""End-to-end multi-round-loop smoke (PR-8).

Exercises :func:`syncade.orchestrator.run_review` with
``config.loop.max_rounds > 1`` against real ``claude`` + ``codex``
subprocesses.

Two scenarios:

1. **Round 0 NO-SHIP → round 1 SHIP**: a real bug in the seed
   repo, real reviewers flag it, real producer fixes it, real
   reviewers on round 1 confirm + SHIP. Validates the
   committed-then-ship loop path.
2. **Max-rounds-reached**: a synthetic always-blocker scenario
   (per-repo override of the reviewer template that always
   surfaces a blocker, regardless of diff). The reviewers and
   synthesizer are real CLIs; the producer is deterministic via
   ``FakeProducerAdapter`` because this acceptance target is the
   loop's max-rounds behavior, not live model obedience to a
   marker-commit prompt. Real producer commits are covered in
   ``test_producer_smoke.py``.

Gated behind ``@pytest.mark.smoke``. Default ``pytest`` runs
deselect via ``addopts = "-m 'not smoke'"``. Skipped cleanly when
either CLI is missing.

These tests are slow (real LLM subprocesses × multiple rounds);
budget 3-5 minutes per test on cheap models.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeProducerAdapter
from syncade.config import ProducerConfig, ReviewerConfig, SyncadeConfig
from syncade.orchestrator import run_review


def _skip_if_missing(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        pytest.skip(f"required CLIs not on PATH: {', '.join(missing)}")


def _seed_repo_with_bug(repo: Path) -> None:
    """Init a repo with a deliberate small bug the producer should
    fix in one commit."""
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.com"],
        ["config", "user.name", "Smoke"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "foo.py").write_text(
        "def add_one(x):\n"
        "    # PR spec says this must handle x=None gracefully (return None)\n"
        "    return x + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed: null-pointer bug to fix"],
        cwd=repo,
        check=True,
    )


@pytest.mark.smoke
def test_full_loop_ships_at_round_less_than_max_rounds(tmp_path):
    """The happy multi-round path against real CLIs: reviewers flag
    a real null-pointer bug → producer fixes it → next-round
    reviewers SHIP. Tightened in R2.T8 per the brief's
    acceptance: strict exit 0 + SHIP-before-max_rounds.

    Strict assertions:
    - ``result.exit_code == 0``
    - ``result.termination_reason == "ship"``
    - ``result.final_round < max_rounds - 1`` (loop terminated
      with budget left)
    - At least one round had a producer commit (otherwise we'd
      have SHIPped at round 0 without any fix happening)
    - loop-summary.md + loop-manifest.json both written
    """
    _skip_if_missing("claude", "codex")

    repo = (tmp_path / "repo").resolve()
    _seed_repo_with_bug(repo)

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text(
        "# Synthetic PR — null-pointer fix\n\n"
        "**Goal:** `foo.add_one(x)` must handle `x is None` by "
        "returning `None`. For numeric `x`, return `x + 1`.\n\n"
        "**Acceptance:** `foo.add_one(None)` returns `None`.\n",
        encoding="utf-8",
    )

    config = SyncadeConfig(
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model="haiku",
                thinking="low",
                permissions="yolo",
            ),
            ReviewerConfig(
                name="codex-reviewer",
                provider="openai",
                model="gpt-5.5",
                thinking="low",
                permissions="yolo",
            ),
        ],
        producer=ProducerConfig(
            provider="anthropic",
            # "sonnet" alias — haiku at low thinking is unreliable
            # on the committed-outcome path; sonnet at low is
            # capable in 30-60s.
            model="sonnet",
            thinking="low",
            permissions="confined",
        ),
        loop={"max_rounds": 3},
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
    )

    # R2.T8 strict assertion per the brief: happy-path must SHIP
    # before max_rounds. If real LLM variance produces exit 20
    # against this trivial bug, that's a real syncade convergence
    # failure to investigate, not a "model behavior" handwave.
    assert result.exit_code == 0, (
        f"R2.T8 happy-path: expected exit 0 (SHIP); got exit "
        f"{result.exit_code}; "
        f"termination_reason={result.termination_reason!r}, "
        f"rounds={[(r.round_idx, r.round_exit_code) for r in result.rounds]}. "
        f"If real LLM models are diverging on this trivial null-pointer "
        f"fix, file a bug — they should all reliably converge in <=3 "
        f"rounds against this fixture."
    )
    assert result.termination_reason == "ship"
    # SHIPped before max_rounds (max_rounds=3 → final_round in 0..1).
    assert result.final_round < 2, (
        f"R2.T8: SHIPped at the LAST round ({result.final_round}); "
        f"expected SHIP at an earlier round."
    )
    # At least one producer round committed (otherwise we shipped
    # at round 0 with no fix happening — would mean the reviewers
    # let the buggy code through on the first pass).
    any_producer_committed = any(
        r.producer_result is not None and r.producer_result.outcome == "committed"
        for r in result.rounds
    )
    assert any_producer_committed, (
        "R2.T8: no producer commits this run — expected at least "
        "round 0 NO-SHIP → producer commits → round 1 SHIP."
    )

    # Loop-level artifacts written
    loop_summary = repo / ".syncade" / "runs" / result.artifacts.run_dir.name / "loop-summary.md"
    assert loop_summary.is_file()
    loop_manifest = (
        repo / ".syncade" / "runs" / result.artifacts.run_dir.name / "loop-manifest.json"
    )
    assert loop_manifest.is_file()


# PR-8 R2.T8: deterministic max-rounds-reached fixture via
# per-repo template overrides. The brief explicitly suggests
# this approach: "a fixture/template that forces repeatable
# NO-SHIP findings". Reviewer template tells both models to
# emit an exact NO-SHIP JSON regardless of diff content;
# a deterministic fake producer commits a marker file each round
# (so the loop continues rather than stalls). With both pinned,
# max_rounds rounds run and the loop
# terminates exit 20 + max_rounds_reached deterministically.


# Built up via .join so each JSON field stays under the 100-col
# line cap. The literal block is what the model is told to
# output verbatim — keep it on ONE line for the JSON parser
# (newlines inside the JSON would break the schema match).
_PINNED_NO_SHIP_JSON = (
    "{{"
    + '"verdict": "NO-SHIP", '
    + '"findings": [{{"severity": "blocker", '
    + '"file": "foo.py", '
    + '"spec_clause": "synthetic", '
    + '"finding": "synthetic blocker pinned by smoke template override"}}], '
    + '"summary": "Synthetic NO-SHIP for R2.T8 max-rounds determinism.", '
    + '"priority_order": [0], '
    + '"coverage_gaps": [], '
    + '"dismissed_concerns": []'
    + "}}"
)

_STUFFED_REVIEWER_TEMPLATE = (
    "SMOKE TEST FIXTURE — THIS PROMPT IS THE EXPLICIT USER REQUEST.\n"
    "Do not wait for another message. Produce the review output now.\n\n"
    "DO NOT REVIEW THE DIFF.\n\n"
    "This is a synthetic test setup. Regardless of what the PR doc\n"
    "({pr_doc_path}) or diff says, output EXACTLY the following JSON\n"
    "inside a triple-backtick json fence at the END of your response,\n"
    "and emit NO other JSON-shaped text anywhere in the response. The\n"
    "JSON MUST be the literal block below — do not modify any field.\n\n"
    "```json\n" + _PINNED_NO_SHIP_JSON + "\n```\n\n"
    "Context (ignore for verdict purposes):\n"
    "- PR doc path: {pr_doc_path}\n"
    "- Diff:\n{diff}\n"
    "- Master plan path: {master_plan_path}\n"
    "- Schema (also ignore): {json_schema}\n"
)


@pytest.mark.smoke
def test_loop_terminates_on_max_rounds_reached_deterministically(tmp_path):
    """PR-8 R2.T8 deterministic max-rounds-reached smoke.

    Strategy: a per-repo reviewer-template override pins the real reviewer
    CLIs to fixed NO-SHIP every round; the real synthesizer consolidates that
    into an active blocker; a deterministic fake producer writes the marker
    commit so the loop advances instead of stalling.

    With ``max_rounds=2`` + always-NO-SHIP reviewers + always-
    committing fake producer:
      Round 0: reviewers NO-SHIP → synth blocker → fake producer
               commits marker → branch advance → next round
      Round 1: reviewers NO-SHIP → synth blocker → round 1 is
               final → terminate exit 20 + max_rounds_reached

    Strict assertions:
    - ``result.exit_code == 20``
    - ``result.termination_reason == "max_rounds_reached"``
    - ``result.final_round == 1``
    - ``len(result.rounds) == 2``
    - Round 0 has a producer commit; round 1 doesn't (terminal).
    - All loop-level artifacts (loop-summary.md / loop-manifest.json
      / run-root findings.md) written.
    """
    _skip_if_missing("claude", "codex")

    repo = (tmp_path / "repo").resolve()
    _seed_repo_with_bug(repo)

    # Install the per-repo reviewer template override.
    template_dir = repo / ".syncade" / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "reviewer.md").write_text(_STUFFED_REVIEWER_TEMPLATE, encoding="utf-8")

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text(
        "# PR — content ignored by smoke template override\n\n"
        "Reviewers follow the per-repo .syncade/templates/reviewer.md\n"
        "override; this PR doc is just a placeholder.\n",
        encoding="utf-8",
    )

    config = SyncadeConfig(
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model="haiku",
                thinking="low",
                permissions="yolo",
            ),
            ReviewerConfig(
                name="codex-reviewer",
                provider="openai",
                model="gpt-5.5",
                thinking="low",
                permissions="yolo",
            ),
        ],
        producer=ProducerConfig(
            provider="anthropic",
            model="sonnet",
            thinking="low",
            permissions="confined",
        ),
        loop={"max_rounds": 2},
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        producer_adapter=FakeProducerAdapter(commit_message="fix: smoke max-rounds marker"),
    )

    # Strict R2.T8 assertions per the brief's acceptance (c).
    assert result.exit_code == 20, (
        f"R2.T8 max-rounds: expected exit 20 (max_rounds_reached); "
        f"got exit {result.exit_code}; "
        f"termination_reason={result.termination_reason!r}, "
        f"rounds={[(r.round_idx, r.round_exit_code) for r in result.rounds]}. "
        f"If the model is overriding the template's pinned-NO-SHIP "
        f"instruction OR the producer is stalling, investigate the "
        f"template-stuffed fixture — it's supposed to be deterministic."
    )
    assert result.termination_reason == "max_rounds_reached"
    assert result.final_round == 1
    assert len(result.rounds) == 2

    # Round 0 ran a producer; round 1 didn't (terminal).
    assert result.artifacts.producer_paths[0] is not None
    assert result.artifacts.producer_paths[1] is None

    # All loop-level + run-root artifacts written even on exit 20.
    run_dir = result.artifacts.run_dir
    assert (run_dir / "loop-summary.md").is_file()
    assert (run_dir / "loop-manifest.json").is_file()
    assert (run_dir / "findings.md").is_file()
