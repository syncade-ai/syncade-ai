"""End-to-end orchestrator smoke against real ``claude`` + ``codex``.

This is the integration check PR-5's brief calls out: catching the
"individually-tested modules don't actually compose into a working
tool" failure mode. Every component has its own unit tests
(``tests/snapshot/``, ``tests/dispatcher/``,
``tests/orchestrator/``, etc.); this file is the only place the
real :class:`~syncade.orchestrator.run_review` runs against real
reviewer CLIs.

Gated behind ``@pytest.mark.smoke``. The default ``pytest`` run
deselects it via ``addopts = "-m 'not smoke'"``. The test skips
cleanly if either ``claude`` or ``codex`` is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from syncade.config import ReviewerConfig, SyncadeConfig
from syncade.exit_codes import FINDINGS_PRESENT, SUCCESS
from syncade.findings import ReviewerOutput
from syncade.orchestrator import run_review
from syncade.synthesis import SynthesizerOutput


def _skip_if_clis_missing() -> None:
    """Skip the smoke unless BOTH reviewer CLIs are on PATH. PR-5's
    smoke specifically exercises the multi-reviewer dispatch surface;
    running with one CLI doesn't validate the integration that
    matters."""
    missing = [c for c in ("claude", "codex") if shutil.which(c) is None]
    if missing:
        pytest.skip(f"required CLIs not on PATH: {', '.join(missing)}")


def _init_git_repo(repo: Path) -> None:
    """Initialize a tiny git working tree with a single commit. The
    orchestrator's :func:`syncade.snapshot.take_snapshot` and
    :class:`~syncade.worktree.WorktreeManager` need a real repo;
    ``tmp_path`` alone isn't enough."""
    repo.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.com"],
        ["config", "user.name", "Smoke"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "README.md").write_text("syncade smoke repo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


@pytest.mark.smoke
def test_run_review_round_trips_against_real_claude_and_codex(tmp_path):
    """Full pipeline against real CLIs.

    Sets up an ephemeral git repo + synthetic PR doc, constructs a
    :class:`SyncadeConfig` with both default reviewers at
    ``thinking="low"`` and the discovery-doc-confirmed cheap models,
    and calls :func:`run_review` with no ``base_ref`` (the most
    common production shape).

    Acceptance per the brief:

    1. Returned :class:`RunResult` has ``exit_code`` in ``(0, 30)``.
       Both are legitimate outcomes for a synthetic PR — the model
       may decide the trivial fixture qualifies for SHIP, or it may
       insist on a NO-SHIP because the synthetic PR doc has no real
       spec to verify against. We don't assert on the verdict, only
       that the round-trip completed.
    2. On-disk artifacts exist at the documented paths:
       ``<run_dir>/round-0/manifest.json``, plus per-reviewer
       ``.stdout``, ``.stderr``, ``.parsed.json``.
    3. Each ``.parsed.json`` parses back to a valid
       :class:`ReviewerOutput`.

    The whole test should complete in under 2 minutes against the
    cheapest models (haiku + gpt-5.5). The per-reviewer timeout
    (LoopConfig default, 1800s) is the upper bound, but real runs
    land around 10-30s per reviewer.
    """
    _skip_if_clis_missing()

    repo = (tmp_path / "repo").resolve()
    _init_git_repo(repo)

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text(
        "# Synthetic PR\n\n"
        "**Goal:** Add a comment to README.md noting the project is in "
        "active development.\n\n"
        "**Acceptance:** README.md ends with a `# WIP` marker line.\n"
    )

    # Two reviewers at cheap models / low thinking — matches the
    # discipline of test_anthropic_smoke and test_codex_smoke.
    #
    # ``max_rounds=1`` keeps this smoke as a single-pass round-trip
    # check (the PR-7.5 shape this test was originally written for):
    # the assertions below only inspect round-0 artifacts and accept
    # ``exit_code in (SUCCESS, FINDINGS_PRESENT)``. With the
    # post-PR-8 default ``max_rounds=3``, a NO-SHIP from either real
    # reviewer would advance the loop into the producer phase, which
    # invokes a real codex subprocess in a worktree without a
    # configured ``[producer]`` block — that subprocess returns a
    # subprocess_error (codex --skip-git-repo-check refuses, or
    # producer permission constraints fire) and the loop terminator
    # turns the run into exit 40 instead of the expected 0/30.
    # The smoke's intent is the reviewer round-trip, not the loop
    # convergence; pin to single-pass.
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
        loop={"max_rounds": 1},
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
    )

    # Exit code: SHIP or FINDINGS — both are acceptable smoke outcomes.
    assert result.exit_code in (SUCCESS, FINDINGS_PRESENT), (
        f"unexpected exit_code {result.exit_code}; "
        f"failures: {[type(r.error).__name__ for r in result.dispatch_result.failures]}"
    )
    assert result.dispatch_result.all_succeeded

    round_dir = result.artifacts.round_dir
    assert (round_dir / "manifest.json").is_file()

    for name in ("claude-reviewer", "codex-reviewer"):
        assert (round_dir / f"{name}.stdout").is_file(), f"{name}.stdout missing"
        assert (round_dir / f"{name}.stderr").is_file(), f"{name}.stderr missing"
        parsed_path = round_dir / f"{name}.parsed.json"
        assert parsed_path.is_file(), f"{name}.parsed.json missing"
        # parsed.json must round-trip through pydantic — that's what
        # the orchestrator wrote and what downstream PR-6 synthesis
        # will read.
        parsed = ReviewerOutput.model_validate_json(parsed_path.read_text())
        assert parsed.verdict in ("SHIP", "NO-SHIP")
        # No error.txt on the success path (the test asserts
        # all_succeeded above, so this is a tighter cross-check that
        # persist_reviewer_result didn't surprise-write one).
        assert not (round_dir / f"{name}.error.txt").exists()

    # Manifest mirrors the on-disk state.
    # PR-8 polish R1.T1: per-round key renamed exit_code →
    # round_exit_code for consistency with loop-manifest.json.
    manifest = json.loads((round_dir / "manifest.json").read_text())
    assert manifest["round_exit_code"] == result.exit_code
    assert len(manifest["reviewers"]) == 2
    for entry in manifest["reviewers"]:
        assert entry["outcome"] == "success"
        assert entry["verdict"] in ("SHIP", "NO-SHIP")

    # PR-7: with both reviewers succeeded, the synthesizer phase
    # fired. The orchestrator's RunResult carries the synth result,
    # and persistence produced synthesizer.{stdout,stderr,parsed.json}
    # plus findings.md.
    assert result.synth_result is not None, (
        "synthesizer phase should have fired since both reviewers "
        "succeeded — see syncade.synthesizer.run_synthesizer"
    )

    # Synthesizer artifacts on disk.
    assert (round_dir / "synthesizer.stdout").is_file(), "synthesizer.stdout missing"
    assert (round_dir / "synthesizer.stderr").is_file(), "synthesizer.stderr missing"

    # If the synth succeeded, parsed.json + findings.md exist; if it
    # failed (e.g. the codex synthesizer emitted invalid JSON
    # against this template — possible on a model-quality day),
    # surface the failure clearly rather than silently passing.
    if result.synth_result.output is not None:
        parsed_path = round_dir / "synthesizer.parsed.json"
        assert parsed_path.is_file(), "synthesizer.parsed.json missing on success path"
        # parsed.json must round-trip through pydantic — same
        # round-trip discipline as the per-reviewer .parsed.json
        # checks above.
        parsed_synth = SynthesizerOutput.model_validate_json(parsed_path.read_text())
        assert len(parsed_synth.synthesis_summary) >= 1
        # findings.md must exist on synth success
        findings_md = round_dir / "findings.md"
        assert findings_md.is_file(), "findings.md missing on synth success path"
        findings_text = findings_md.read_text()
        # findings.md carries the mechanical verdict label
        assert "**Verdict:**" in findings_text
        assert "## Synthesis summary" in findings_text
        # Manifest's synthesizer section reflects success
        assert manifest["synthesizer"] is not None
        assert manifest["synthesizer"]["outcome"] == "success"
        # And the run-result paths are populated for downstream
        # tooling (the CLI / future loop).
        assert result.artifacts.findings_md_path == findings_md
        assert result.artifacts.synthesizer_paths is not None
        assert result.artifacts.synthesizer_paths.parsed == parsed_path
    else:
        # Synth failed — surface the failure shape so the smoke run
        # log shows what went wrong. The test still asserts that
        # persistence wrote the artifacts it was supposed to in the
        # failure case (synthesizer.error.txt).
        assert (round_dir / "synthesizer.error.txt").is_file(), (
            f"synth failed without error.txt: error="
            f"{type(result.synth_result.error).__name__}: "
            f"{result.synth_result.error}"
        )
        pytest.fail(
            f"synthesizer phase failed in smoke run: error_class="
            f"{type(result.synth_result.error).__name__}, "
            f"error_message={result.synth_result.error}. Synthesizer "
            f"output: see {round_dir / 'synthesizer.stdout'}"
        )
