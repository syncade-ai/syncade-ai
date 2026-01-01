"""Tests for :class:`syncade.adapters.fake.FakeAdapter`."""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.adapters.base import (
    Invocation,
    ReviewerAdapter,
    ReviewerInvocationError,
)
from syncade.adapters.fake import FakeAdapter
from syncade.config import ReviewerConfig
from syncade.findings import Finding, ReviewerOutput, ReviewerOutputError
from syncade.process import SubprocessResult
from tests.test_worktree_env import make_worktree_src, resolve_syncade_in_child


def _config() -> ReviewerConfig:
    return ReviewerConfig(
        name="fake-reviewer",
        provider="fake",
        model="ignored-by-fake",
    )


def _empty_result() -> SubprocessResult:
    return SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)


class TestFakeAdapter:
    def test_implements_reviewer_adapter_protocol(self):
        """The fake is a drop-in for ReviewerAdapter so dispatcher tests
        in PR #4 can swap it in without conditional logic."""
        adapter = FakeAdapter()
        assert isinstance(adapter, ReviewerAdapter)
        assert adapter.name == "fake"

    def test_default_canned_output_is_zero_finding_ship(self, tmp_path: Path):
        adapter = FakeAdapter()
        out = adapter.parse_output(_empty_result())
        assert isinstance(out, ReviewerOutput)
        assert out.verdict == "SHIP"
        assert out.findings == []

    def test_returns_custom_canned_output(self, tmp_path: Path):
        canned = ReviewerOutput(
            verdict="NO-SHIP",
            findings=[
                Finding(
                    severity="blocker",
                    file="src/x.py",
                    spec_clause="G1",
                    finding="problem",
                )
            ],
            summary="custom canned NO-SHIP",
            priority_order=[0],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        adapter = FakeAdapter(canned_output=canned)
        out = adapter.parse_output(_empty_result())
        assert out.verdict == "NO-SHIP"
        assert len(out.findings) == 1
        assert out.findings[0].severity == "blocker"

    def test_raises_canned_exception(self, tmp_path: Path):
        adapter = FakeAdapter(
            canned_exception=ReviewerInvocationError(
                "simulated failure",
                returncode=1,
                stdout="",
                stderr="boom",
            )
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(_empty_result())
        assert exc_info.value.returncode == 1
        assert exc_info.value.stderr == "boom"

    def test_canned_exception_can_be_output_error(self, tmp_path: Path):
        # The dispatcher distinguishes invocation vs output failures;
        # the fake must be usable to simulate either.
        adapter = FakeAdapter(canned_exception=ReviewerOutputError("simulated parse failure"))
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(_empty_result())

    def test_records_invocations_by_default(self, tmp_path: Path):
        adapter = FakeAdapter()
        config = _config()
        adapter.build_invocation(config, tmp_path, "first prompt")
        adapter.build_invocation(config, tmp_path / "another", "second prompt")
        assert len(adapter.invocations) == 2
        assert adapter.invocations[0][2] == "first prompt"
        assert adapter.invocations[1][1] == tmp_path / "another"

    def test_recording_can_be_disabled(self, tmp_path: Path):
        adapter = FakeAdapter(record_invocations=False)
        adapter.build_invocation(_config(), tmp_path, "x")
        assert adapter.invocations == []

    def test_build_invocation_returns_noop_argv_with_caller_env(self, tmp_path: Path):
        adapter = FakeAdapter()
        inv = adapter.build_invocation(_config(), tmp_path, "x")
        assert isinstance(inv, Invocation)
        # cwd is the worktree, env is inherited so a dispatcher that
        # actually executes the no-op finds PATH etc.
        assert inv.cwd == tmp_path
        assert inv.env  # not empty
        assert inv.stdin_text is None
        # argv points at a no-op shell command — the dispatcher CAN
        # actually run it, but doesn't have to.
        assert len(inv.argv) >= 1

    def test_build_invocation_env_resolves_worktree_src(self, tmp_path: Path):
        # PR-23: the fake reviewer mirrors the real adapters' worktree-scoped
        # env so it stays a faithful double (same Invocation env shape).
        worktree = make_worktree_src(tmp_path / "wt")
        adapter = FakeAdapter()
        inv = adapter.build_invocation(_config(), worktree, "x")
        resolved = resolve_syncade_in_child(inv.env, worktree)
        assert resolved.startswith(str((worktree / "src" / "syncade").resolve()))
