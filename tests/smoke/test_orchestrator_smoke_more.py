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
from syncade.exit_codes import SUCCESS
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
def test_run_review_fires_test_leg_end_to_end_with_real_pytest(tmp_path):
    """PR-7.5 end-to-end: ``[loop] test_command`` set + every reviewer
    succeeded + synth clean → test leg fires + folds correctly into
    the verdict.

    Uses a SELF-CONTAINED tmp_path repo with a tiny one-line test
    file. Earlier versions of this smoke pointed the reviewers at
    the syncade repo itself (re-using ``tests/test_findings.py`` as
    the test_command target), but that produced a recursive
    context: the reviewers could see syncade's in-progress
    ``.syncade/runs/`` history, the production commit log, and the
    in-flight reviewer template — and started roleplaying as
    engineers reporting on PR-7.5 itself rather than reviewing the
    synthetic PR doc. Two real-CLI runs in a row (claude on
    haiku+low+yolo, codex on gpt-5.5+low+yolo) produced output
    that didn't validate as ReviewerOutput JSON: claude embedded
    valid JSON inside meta-prose about the failing smoke; codex
    emitted pure prose about "the corrected targeted tests
    passed" with no JSON fence at all. The recursive-context
    confusion is a real smoke-design issue (and a PR-8 trap when
    syncade reviews itself) — fixing the smoke design avoids both.

    The self-contained tmp_path approach: init a fresh git repo
    with one trivial source file + one trivial pytest file, commit
    it, write a synthetic PR doc that describes the trivial code
    change. Reviewers see ONLY this minimal repo (no syncade
    history, no in-progress artifacts) and treat the PR doc as
    the discrete review target it's meant to be.

    Acceptance per the PR-7.5 brief:

    1. The test leg fires (``test_result is not None``).
    2. test_result.outcome == "passed" (the trivial test always
       passes).
    3. test-run.{stdout,stderr,exit-code.txt} land at the
       documented paths.
    4. manifest.json's ``test_run`` section reflects the success.
    5. summary.md's Test Suite subsection renders the passed
       state.
    6. findings.md's Test Suite section is at the TOP of the
       document (above Synthesis summary).
    7. The verdict is exit 0 (synth-clean + test-passed → SUCCESS).
    """
    _skip_if_clis_missing()
    if shutil.which("pytest") is None:
        pytest.skip("pytest not on PATH")

    # Self-contained tmp_path repo — see docstring above for the
    # recursive-context rationale.
    repo = (tmp_path / "repo").resolve()
    _init_git_repo(repo)
    # Add a one-line module + a one-line passing test so the test
    # leg has something real to run. Both committed so the
    # snapshot commit_sha resolves cleanly.
    (repo / "widget.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")
    (repo / "test_widget.py").write_text(
        "from widget import greet\n\n"
        "def test_greet():\n"
        "    assert greet('PR-7.5') == 'Hello, PR-7.5!'\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add greet widget + passing test"],
        cwd=repo,
        check=True,
    )
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text(
        "# Synthetic PR (PR-7.5 smoke)\n\n"
        "**Goal:** Add a tiny `greet(name)` helper in `widget.py` "
        "that returns `f'Hello, {name}!'`, plus a passing pytest "
        "in `test_widget.py`.\n\n"
        "**Acceptance:** the test command passes against the new "
        "widget; the reviewers verify the helper matches the goal.\n"
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
        loop={
            "test_command": "pytest test_widget.py -q",
            # 1800s matches the production default. Earlier 600s
            # was too aggressive for a real reviewer doing tool-
            # use against any repo (even this tiny one).
            "timeout_seconds": 1800,
        },
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
    )

    # The test leg only fires if every reviewer succeeded AND synth
    # was clean. Surface skipped paths as clear failures of the
    # smoke (the smoke target is the test-leg path, not the
    # skipped paths).
    assert result.dispatch_result.all_succeeded, (
        f"reviewer dispatch did not all succeed: failures="
        f"{[type(r.error).__name__ for r in result.dispatch_result.failures]}"
    )
    assert result.synth_result is not None, "synthesizer phase did not fire"
    if result.synth_result.output is None:
        pytest.fail(
            f"synthesizer phase failed: error="
            f"{type(result.synth_result.error).__name__}: "
            f"{result.synth_result.error}"
        )
    # Synthesizer may surface findings against the synthetic PR (the
    # reviewers are interpreting a deliberately-bare brief). In that
    # case the test leg is skipped (synth-blocker bypass). The smoke
    # only validates the test-leg path, so skip if we hit a
    # synth-blocker run.
    from syncade.synthesis import has_active_blocker

    if has_active_blocker(result.synth_result.output):
        pytest.skip(
            "synthesizer surfaced an active blocker; the test leg is "
            "skipped on synth-blocker paths. Re-run the smoke against "
            "a clearer PR doc to validate the test-leg path."
        )

    # The test leg fired.
    assert result.test_result is not None, "test leg did not fire"
    assert result.test_result.outcome == "passed", (
        f"unexpected test outcome={result.test_result.outcome!r}; "
        f"exit_code={result.test_result.exit_code}; "
        f"stderr={result.test_result.stderr[:500]!r}"
    )
    assert result.test_result.exit_code == 0

    # Artifacts on disk
    round_dir = result.artifacts.round_dir
    assert (round_dir / "test-run.stdout").is_file()
    assert (round_dir / "test-run.stderr").is_file()
    assert (round_dir / "test-run.exit-code.txt").is_file()
    assert (round_dir / "test-run.exit-code.txt").read_text() == "0\n"

    # manifest.json reflects the test_run section
    manifest = json.loads((round_dir / "manifest.json").read_text())
    test_run = manifest["test_run"]
    assert test_run is not None
    assert test_run["outcome"] == "passed"
    assert test_run["exit_code"] == 0
    assert test_run["command"] == "pytest test_widget.py -q"
    assert test_run["error_type"] is None

    # summary.md has the Test Suite subsection
    summary_text = (round_dir / "summary.md").read_text()
    assert "## Test Suite" in summary_text
    assert "**Outcome:** passed" in summary_text
    assert "test-run.stdout" in summary_text

    # findings.md has the Test Suite section at the TOP (before
    # Synthesis summary)
    findings_text = (round_dir / "findings.md").read_text()
    assert "## Test Suite" in findings_text
    ts_idx = findings_text.find("## Test Suite")
    synth_idx = findings_text.find("## Synthesis summary")
    assert ts_idx < synth_idx, "Test Suite section should precede Synthesis summary in findings.md"

    # Verdict: synth-clean + test-passed → exit 0
    assert result.exit_code == SUCCESS, (
        f"unexpected exit_code={result.exit_code}; expected SUCCESS (0) "
        f"on synth-clean + test-passed"
    )


@pytest.mark.smoke
def test_run_review_folds_real_pytest_into_verdict_env_independent(tmp_path):
    """PR-7.5 acceptance criterion #11, env-independent variant:
    "Smoke test exercises real ``pytest`` and folds the result into
    the orchestrator's verdict end-to-end."

    The sibling smoke ``test_run_review_fires_test_leg_end_to_end_with_real_pytest``
    exercises the SAME orchestrator path but requires real Anthropic
    + OpenAI credit/auth — when either CLI is auth-broken (rate-
    limited, credit-exhausted, network-down) that smoke fails for
    env reasons and the criterion goes unverified. This variant
    uses ``FakeAdapter`` / ``FakeSynthesizerAdapter`` for the
    reviewer + synth phases so the only real subprocess is the
    operator-configured ``test_command`` — pytest itself.

    What this validates that the unit-level
    ``tests/smoke/test_test_runner_smoke.py`` does NOT:
    - The orchestrator actually calls ``run_tests`` on the
      synth-clean path (not just that ``run_tests`` works in
      isolation).
    - The orchestrator's exit code derives from the real pytest's
      outcome via ``_compute_exit_code``'s boolean fold (not just
      the unit-level decision-table cases against synthetic
      ``TestRunResult`` instances).
    - The artifacts on disk reflect the real pytest's actual stdout
      / exit code (not a synthesized one).

    Runs against the syncade repo itself — no external project
    needed. Three sub-cases: test_command passes, test_command
    fails, test_command unset. Each asserts the verdict folded
    correctly.
    """
    from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
    from syncade.exit_codes import FINDINGS_PRESENT
    from syncade.findings import ReviewerOutput
    from syncade.synthesis import SynthesizerOutput

    if shutil.which("pytest") is None:
        pytest.skip("pytest not on PATH")

    repo = Path(__file__).resolve().parents[2]

    def _ship() -> ReviewerOutput:
        return ReviewerOutput(
            verdict="SHIP",
            findings=[],
            summary="env-independent smoke SHIP",
            priority_order=[],
            coverage_gaps=[],
            dismissed_concerns=[],
        )

    def _clean_synth() -> SynthesizerOutput:
        return SynthesizerOutput(
            consolidated_findings=[],
            synthesis_summary="env-independent smoke: both reviewers clean",
        )

    def _factory(*adapters):
        import threading

        it = iter(adapters)
        lock = threading.Lock()

        def f(_provider):
            with lock:
                return next(it)

        return f

    def _config(test_command):
        # ``max_rounds=1`` is load-bearing: this test asserts on a
        # SINGLE-ROUND verdict fold (synth+test → SUCCESS or
        # FINDINGS_PRESENT). The PR-7.5 brief this test was written
        # against had ``max_rounds`` hardcoded to 1; PR-8 introduced
        # the loop with default 3. Without this override, sub-case 2
        # (test-failed → exit 30) advances to the producer phase in
        # round 1, the producer subprocess errors, and the loop
        # terminator returns exit 40 — masking the single-round
        # verdict assertion this smoke is meant to pin.
        loop = {"timeout_seconds": 1800, "max_rounds": 1}
        if test_command is not None:
            loop["test_command"] = test_command
        return SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="rv1", provider="fake1", model="x"),
                ReviewerConfig(name="rv2", provider="fake2", model="y"),
            ],
            loop=loop,
        )

    # Sub-case 1: real pytest PASSES → orchestrator verdict = exit 0
    pr_doc = tmp_path / "pr-pass.md"
    pr_doc.write_text("# Smoke: real pytest passes\n")
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config("pytest tests/findings/test_parser_and_fields.py -q"),
        adapter_factory=_factory(
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_clean_synth()),
    )
    assert result.test_result is not None
    assert result.test_result.outcome == "passed", (
        f"unexpected pytest outcome: {result.test_result.outcome!r}; "
        f"exit_code={result.test_result.exit_code}; "
        f"stderr={result.test_result.stderr[:500]!r}"
    )
    assert result.test_result.exit_code == 0
    assert result.exit_code == SUCCESS
    # The real pytest's stdout actually reached the artifact
    rd = result.artifacts.round_dir
    assert " passed" in (rd / "test-run.stdout").read_text(), (
        "test-run.stdout doesn't show pytest's own 'passed' summary — "
        "the real subprocess output didn't reach the artifact"
    )
    assert (rd / "test-run.exit-code.txt").read_text() == "0\n"

    # Sub-case 2: real pytest FAILS (nonexistent test file) →
    # orchestrator verdict = exit 30
    pr_doc = tmp_path / "pr-fail.md"
    pr_doc.write_text("# Smoke: real pytest fails\n")
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config("pytest tests/this_file_definitely_does_not_exist.py -q"),
        adapter_factory=_factory(
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_clean_synth()),
    )
    assert result.test_result is not None
    assert result.test_result.outcome == "failed", (
        f"unexpected pytest outcome: {result.test_result.outcome!r}; "
        f"exit_code={result.test_result.exit_code}"
    )
    assert result.test_result.exit_code > 0
    assert result.exit_code == FINDINGS_PRESENT, (
        f"expected exit 30 from synth-clean + test-failed; got {result.exit_code}"
    )

    # Sub-case 3: test_command unset → back-compat exit 0 (synth-only)
    pr_doc = tmp_path / "pr-skipped.md"
    pr_doc.write_text("# Smoke: test_command unset\n")
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_config(None),
        adapter_factory=_factory(
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ),
        synthesizer_adapter=FakeSynthesizerAdapter(canned_output=_clean_synth()),
    )
    assert result.test_result is None
    assert result.exit_code == SUCCESS
