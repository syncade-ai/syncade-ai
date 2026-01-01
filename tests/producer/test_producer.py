"""Tests for :mod:`syncade.producer` — the PR-8 producer subprocess
phase + stall detection (part 1: ProducerResult contract + the
committed / stalled outcomes).

Uses :class:`~syncade.adapters.fake.FakeProducerAdapter` to avoid
spawning real ``claude`` / ``codex`` subprocesses. The fake's
optional fixture-commit lets these tests exercise the
``committed`` / ``stalled`` outcome split end-to-end without ever
shelling out to a real LLM.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.adapters.fake import FakeProducerAdapter
from syncade.adapters.producer import ProducerOutput
from syncade.config import ProducerConfig
from syncade.process import (
    SubprocessTimeoutError,
)
from syncade.producer import ProducerResult, run_producer
from tests.producer._helpers import (
    _git_required,
    _make_findings_md,
    _make_pr_doc,
    _read_head,
    _seed_repo,
)


def _seed_sha256_repo(tmp_path):
    try:
        subprocess.run(["git", "init", "-q", "--object-format=sha256", str(tmp_path)], check=True)
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"git does not support sha256 object-format: {exc.stderr}")
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    return _read_head(tmp_path)


# ---------------------------------------------------------------------------
# ProducerResult __post_init__ — exactly-one contract
# ---------------------------------------------------------------------------


class TestProducerResultPostInit:
    """The dataclass enforces the (outcome, output, error, SHA-move)
    consistency rule so persistence never sees a result claiming
    'committed' without an actual HEAD move (or 'stalled' with an
    error attached, etc.)."""

    def test_committed_with_sha_move_and_output_ok(self):
        result = ProducerResult(
            outcome="committed",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=1.0,
            output=ProducerOutput(narrative_text="ok"),
            error=None,
        )
        assert result.outcome == "committed"
        assert result.ending_sha == "b" * 40

    def test_committed_without_sha_move_rejected(self):
        with pytest.raises(ValueError, match="ending_sha != starting_sha"):
            ProducerResult(
                outcome="committed",
                starting_sha="a" * 40,
                ending_sha="a" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text="ok"),
                error=None,
            )

    def test_committed_with_output_none_rejected(self):
        with pytest.raises(ValueError, match="output is not None"):
            ProducerResult(
                outcome="committed",
                starting_sha="a" * 40,
                ending_sha="b" * 40,
                duration_seconds=1.0,
                output=None,
                error=None,
            )

    def test_committed_with_error_rejected(self):
        with pytest.raises(ValueError, match="error is None"):
            ProducerResult(
                outcome="committed",
                starting_sha="a" * 40,
                ending_sha="b" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text="ok"),
                error=ValueError("nope"),
            )

    def test_stalled_with_sha_unchanged_ok(self):
        result = ProducerResult(
            outcome="stalled",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=1.0,
            output=ProducerOutput(narrative_text="I cannot fix this"),
            error=None,
        )
        assert result.outcome == "stalled"

    def test_stalled_with_sha_move_rejected(self):
        with pytest.raises(ValueError, match="ending_sha == starting_sha"):
            ProducerResult(
                outcome="stalled",
                starting_sha="a" * 40,
                ending_sha="b" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text=""),
                error=None,
            )

    def test_stalled_with_output_none_rejected(self):
        with pytest.raises(ValueError, match="output is not None"):
            ProducerResult(
                outcome="stalled",
                starting_sha="a" * 40,
                ending_sha="a" * 40,
                duration_seconds=1.0,
                output=None,
                error=None,
            )

    def test_subprocess_error_with_error_and_no_output_ok(self):
        result = ProducerResult(
            outcome="subprocess_error",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=0.5,
            output=None,
            error=SubprocessTimeoutError("timeout", stdout="", stderr="", timeout=10.0),
        )
        assert result.outcome == "subprocess_error"
        assert isinstance(result.error, SubprocessTimeoutError)

    def test_subprocess_error_with_output_rejected(self):
        with pytest.raises(ValueError, match="output is None"):
            ProducerResult(
                outcome="subprocess_error",
                starting_sha="a" * 40,
                ending_sha="a" * 40,
                duration_seconds=0.5,
                output=ProducerOutput(narrative_text="bug"),
                error=ValueError("x"),
            )

    def test_subprocess_error_without_error_rejected(self):
        with pytest.raises(ValueError, match="error is not None"):
            ProducerResult(
                outcome="subprocess_error",
                starting_sha="a" * 40,
                ending_sha="a" * 40,
                duration_seconds=0.5,
                output=None,
                error=None,
            )

    def test_subprocess_error_with_sha_move_ok(self):
        result = ProducerResult(
            outcome="subprocess_error",
            starting_sha="a" * 40,
            ending_sha="b" * 40,
            duration_seconds=0.5,
            output=None,
            error=ValueError("x"),
        )
        assert result.outcome == "subprocess_error"
        assert result.ending_sha == "b" * 40

    def test_escalated_with_escalation_and_no_sha_move_ok(self):
        """PR-22: escalation is a stall-variant — HEAD unchanged, output
        present, error None, and the structured escalation populated."""
        from syncade.producer_escalation import ProducerEscalation

        result = ProducerResult(
            outcome="escalated",
            starting_sha="a" * 40,
            ending_sha="a" * 40,
            duration_seconds=1.0,
            output=ProducerOutput(narrative_text="this needs an operator decision"),
            error=None,
            escalation=ProducerEscalation(
                finding_indices=[0],
                finding="spec vs code conflict",
                decision="X or Y?",
                options=["X", "Y"],
                rationale="reproduced both constraints",
            ),
        )
        assert result.outcome == "escalated"
        assert result.escalation is not None

    def test_escalated_without_escalation_rejected(self):
        with pytest.raises(ValueError, match="escalation"):
            ProducerResult(
                outcome="escalated",
                starting_sha="a" * 40,
                ending_sha="a" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text="x"),
                error=None,
                escalation=None,
            )

    def test_escalated_with_sha_move_rejected(self):
        from syncade.producer_escalation import ProducerEscalation

        with pytest.raises(ValueError, match="ending_sha == starting_sha"):
            ProducerResult(
                outcome="escalated",
                starting_sha="a" * 40,
                ending_sha="b" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text="x"),
                error=None,
                escalation=ProducerEscalation(
                    finding_indices=[0], finding="f", decision="d", options=["o"], rationale="r"
                ),
            )

    def test_committed_with_escalation_rejected(self):
        """escalation must be set IFF outcome=='escalated' — a committed
        result carrying an escalation is a caller bug."""
        from syncade.producer_escalation import ProducerEscalation

        with pytest.raises(ValueError, match="escalation"):
            ProducerResult(
                outcome="committed",
                starting_sha="a" * 40,
                ending_sha="b" * 40,
                duration_seconds=1.0,
                output=ProducerOutput(narrative_text="x"),
                error=None,
                escalation=ProducerEscalation(
                    finding_indices=[0], finding="f", decision="d", options=["o"], rationale="r"
                ),
            )

    def test_unknown_outcome_rejected(self):
        """``outcome`` is a ``Literal`` at type-check time; a dict-
        fed dataclass construction can still pass an unknown
        value. The runtime check catches it."""
        with pytest.raises(ValueError, match="unknown outcome"):
            ProducerResult(
                outcome="weird-state",  # type: ignore[arg-type]
                starting_sha="a" * 40,
                ending_sha="a" * 40,
                duration_seconds=0.5,
                output=ProducerOutput(narrative_text=""),
                error=None,
            )


# ---------------------------------------------------------------------------
# run_producer — happy path (committed)
# ---------------------------------------------------------------------------


class TestRunProducerCommitted:
    def test_producer_committed_outcome(self, tmp_path):
        """Fixture commit moves HEAD → ``outcome="committed"``,
        ``ending_sha != starting_sha``, ``output`` populated."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(commit_message="fix: handle the null case")
        result = run_producer(
            worktree_path=tmp_path,
            starting_sha=starting_sha,
            pr_doc_path=pr_doc,
            findings_md_path=findings,
            test_run_stdout_path=None,
            producer_config=ProducerConfig(),
            timeout_seconds=30.0,
            round_number=0,
            max_rounds=3,
            repo_root=tmp_path,
            adapter=fake,
        )

        assert result.outcome == "committed"
        assert result.starting_sha == starting_sha
        assert result.ending_sha != starting_sha
        assert result.ending_sha == _read_head(tmp_path)
        assert result.output is not None
        assert result.error is None
        assert result.raw_subprocess_result is not None

    def test_producer_committed_outcome_supports_sha256_repos(self, tmp_path):
        _git_required()
        starting_sha = _seed_sha256_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(commit_message="fix: handle sha256")
        result = run_producer(
            worktree_path=tmp_path,
            starting_sha=starting_sha,
            pr_doc_path=pr_doc,
            findings_md_path=findings,
            test_run_stdout_path=None,
            producer_config=ProducerConfig(),
            timeout_seconds=30.0,
            round_number=0,
            max_rounds=3,
            repo_root=tmp_path,
            adapter=fake,
        )

        assert len(starting_sha) == 64
        assert result.outcome == "committed"
        assert result.ending_sha == _read_head(tmp_path)
        assert len(result.ending_sha) == 64

    def test_committed_narrative_text_preserved(self, tmp_path):
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        canned = ProducerOutput(narrative_text="I fixed it by doing X.")
        fake = FakeProducerAdapter(canned_output=canned, commit_message="fix: do X")
        result = run_producer(
            worktree_path=tmp_path,
            starting_sha=starting_sha,
            pr_doc_path=pr_doc,
            findings_md_path=findings,
            test_run_stdout_path=None,
            producer_config=ProducerConfig(),
            timeout_seconds=30.0,
            round_number=0,
            max_rounds=3,
            repo_root=tmp_path,
            adapter=fake,
        )
        assert result.output is not None
        assert result.output.narrative_text == "I fixed it by doing X."


# ---------------------------------------------------------------------------
# run_producer — stall path
# ---------------------------------------------------------------------------


class TestRunProducerStalled:
    def test_no_commit_yields_stalled_outcome(self, tmp_path):
        """commit_message=None on the fake → no commit → HEAD
        unchanged → ``outcome="stalled"``."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter()  # no fixture commit
        result = run_producer(
            worktree_path=tmp_path,
            starting_sha=starting_sha,
            pr_doc_path=pr_doc,
            findings_md_path=findings,
            test_run_stdout_path=None,
            producer_config=ProducerConfig(),
            timeout_seconds=30.0,
            round_number=0,
            max_rounds=3,
            repo_root=tmp_path,
            adapter=fake,
        )

        assert result.outcome == "stalled"
        assert result.starting_sha == starting_sha
        assert result.ending_sha == starting_sha
        assert result.output is not None
        assert result.error is None
        # On stall the orchestrator still wants the narrative the
        # producer emitted (the operator inspects it to understand
        # why the producer couldn't make progress).
        assert result.raw_subprocess_result is not None
