"""Tests for the PR-8 producer adapter Protocol + concrete implementations.

Mirrors the structure of ``tests/adapters/test_anthropic.py`` and
``tests/adapters/test_openai.py`` for the reviewer adapters: argv
shape tests against a fixture worktree, parse_output behavior
tests against synthesized :class:`SubprocessResult` instances, and
the headless-deadlock guard for ``permissions="safe"``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade.adapters.base import Invocation, ReviewerInvocationError
from syncade.adapters.fake import FakeProducerAdapter, _default_canned_producer_output
from syncade.adapters.producer import ProducerOutput
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.config import ProducerConfig
from syncade.findings import ReviewerOutputError
from syncade.process import SubprocessResult
from tests.test_worktree_env import make_worktree_src, resolve_syncade_in_child

# ---------------------------------------------------------------------------
# OpenAIProducerAdapter — build_invocation argv shape
# ---------------------------------------------------------------------------


class TestOpenAIProducerBuildInvocation:
    """Argv-shape tests against the CLI output format's
    documented flag set."""

    def test_trusted_permissions_rejected_when_schema_bypassed(self, tmp_path):
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="openai",
            model="gpt-5-codex",
            thinking="high",
            permissions="trusted",
        )
        with pytest.raises(ValueError, match="trusted"):
            adapter.build_invocation(config, tmp_path, "the prompt")

    def test_yolo_permissions_use_bypass_flag(self, tmp_path):
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig(
            provider="openai",
            model="gpt-5.5",
            thinking="low",
            permissions="yolo",
        )
        inv = adapter.build_invocation(config, tmp_path, "p")
        assert "--dangerously-bypass-approvals-and-sandbox" in inv.argv
        # yolo does not use the sandboxed -s/-c approval_policy pairing
        assert "workspace-write" not in inv.argv
        assert "approval_policy=never" not in inv.argv

    def test_safe_permissions_rejected_at_build_invocation(self, tmp_path):
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="openai",
            model="x",
            thinking="high",
            permissions="safe",
        )
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "p")
        assert "safe" in str(exc_info.value)

    def test_wrong_provider_rejected_at_build_invocation(self, tmp_path):
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig.model_construct(
            provider="anthropic",
            model="x",
            thinking="high",
            permissions="yolo",
        )
        with pytest.raises(ValueError) as exc_info:
            adapter.build_invocation(config, tmp_path, "p")
        assert "anthropic" in str(exc_info.value)
        assert "openai" in str(exc_info.value)

    def test_thinking_xhigh_passes_through_to_codex_arg(self, tmp_path):
        """PR-8.5 Task 4: codex's ``xhigh`` reasoning-effort tier
        passes through verbatim as ``-c model_reasoning_effort=xhigh``.
        The producer adapter does no value mapping; the schema's
        Literal already validated the value. This pins the
        end-to-end argv shape so a future adapter rewrite cannot
        silently regress xhigh routing."""
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig(
            provider="openai",
            model="gpt-5.5",
            thinking="xhigh",
            permissions="yolo",
        )
        inv = adapter.build_invocation(config, tmp_path, "p")
        assert "model_reasoning_effort=xhigh" in inv.argv

    def test_invocation_env_resolves_worktree_src_not_main(self, tmp_path):
        # PR-23: the producer subprocess must import `syncade` from its OWN
        # worktree, not MAIN's editable-install .pth. Real child process proof.
        worktree = make_worktree_src(tmp_path / "wt")
        adapter = OpenAIProducerAdapter()
        config = ProducerConfig(
            provider="openai", model="gpt-5.5", thinking="high", permissions="yolo"
        )
        inv = adapter.build_invocation(config, worktree, "p")
        resolved = resolve_syncade_in_child(inv.env, worktree)
        assert resolved.startswith(str((worktree / "src" / "syncade").resolve()))


# ---------------------------------------------------------------------------
# OpenAIProducerAdapter — parse_output
# ---------------------------------------------------------------------------


def _codex_jsonl_stream(text: str) -> str:
    """Synthesize a codex --json JSONL stream ending in an
    item.completed agent_message with the given text."""
    events = [
        {"type": "thread.started", "thread_id": "test-thread"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": text},
        },
        {"type": "turn.completed", "usage": {}},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


class TestOpenAIProducerParseOutput:
    def test_success_returns_narrative_text(self):
        adapter = OpenAIProducerAdapter()
        result = SubprocessResult(
            returncode=0,
            stdout=_codex_jsonl_stream("I made the fix and committed."),
            stderr="",
            duration_seconds=0.5,
        )
        out = adapter.parse_output(result)
        assert isinstance(out, ProducerOutput)
        assert out.narrative_text == "I made the fix and committed."

    def test_turn_failed_raises_invocation_error(self):
        """Mirrors the reviewer codex adapter's
        ``turn.failed`` → ReviewerInvocationError mapping. The
        extract helper is shared via ``CodexAdapter.extract_final_text``."""
        adapter = OpenAIProducerAdapter()
        events = [
            {"type": "thread.started", "thread_id": "t"},
            {
                "type": "turn.failed",
                "error": {"message": "the model refused"},
            },
        ]
        result = SubprocessResult(
            returncode=1,
            stdout="\n".join(json.dumps(e) for e in events),
            stderr="",
            duration_seconds=0.3,
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(result)
        assert "model refused" in str(exc_info.value) or "refused" in str(exc_info.value)

    def test_auth_signature_marks_api_error_401(self):
        adapter = OpenAIProducerAdapter()
        events = [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "error", "message": "401 Unauthorized: Missing bearer"},
        ]
        result = SubprocessResult(
            returncode=1,
            stdout="\n".join(json.dumps(e) for e in events),
            stderr="",
            duration_seconds=0.2,
        )
        with pytest.raises(ReviewerInvocationError) as exc_info:
            adapter.parse_output(result)
        assert exc_info.value.api_error_status == 401

    def test_no_agent_message_raises_output_error(self):
        """A codex run that completed without emitting an
        ``agent_message`` event surfaces as ``ReviewerOutputError``
        (the no-agent-message exception class the producer's
        ``parse_output`` passes)."""
        adapter = OpenAIProducerAdapter()
        events = [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {}},
        ]
        result = SubprocessResult(
            returncode=0,
            stdout="\n".join(json.dumps(e) for e in events),
            stderr="",
            duration_seconds=0.4,
        )
        with pytest.raises(ReviewerOutputError):
            adapter.parse_output(result)


# ---------------------------------------------------------------------------
# FakeProducerAdapter
# ---------------------------------------------------------------------------


def _init_git_worktree(path):
    """Helper: turn ``path`` into a tiny git working tree with one
    commit so the fake's fixture-commit path runs cleanly."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    (path / "starter.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "starter.txt"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed"],
        cwd=path,
        check=True,
    )


class TestFakeProducerAdapter:
    def test_default_canned_output_has_non_empty_narrative(self):
        out = _default_canned_producer_output()
        assert isinstance(out, ProducerOutput)
        assert out.narrative_text

    def test_build_invocation_records_call(self, tmp_path):
        fake = FakeProducerAdapter()
        cfg = ProducerConfig()
        inv = fake.build_invocation(cfg, tmp_path, "prompt-x")
        assert len(fake.invocations) == 1
        assert fake.invocations[0] == (cfg, tmp_path, "prompt-x")
        assert isinstance(inv, Invocation)
        assert inv.cwd == tmp_path

    def test_build_invocation_env_resolves_worktree_src(self, tmp_path):
        # PR-23: the fake producer mirrors the real producer adapters'
        # worktree-scoped env so it stays a faithful double.
        worktree = make_worktree_src(tmp_path / "wt")
        fake = FakeProducerAdapter()
        inv = fake.build_invocation(ProducerConfig(), worktree, "p")
        resolved = resolve_syncade_in_child(inv.env, worktree)
        assert resolved.startswith(str((worktree / "src" / "syncade").resolve()))

    def test_build_invocation_can_skip_recording(self, tmp_path):
        fake = FakeProducerAdapter(record_invocations=False)
        fake.build_invocation(ProducerConfig(), tmp_path, "p")
        assert fake.invocations == []

    def test_parse_output_returns_canned(self, tmp_path):
        canned = ProducerOutput(narrative_text="custom canned")
        fake = FakeProducerAdapter(canned_output=canned)
        result = SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)
        out = fake.parse_output(result)
        assert out is canned
        assert fake.parse_output_calls == 1

    def test_parse_output_raises_canned_exception(self, tmp_path):
        exc = ReviewerInvocationError(
            "simulated failure",
            returncode=1,
            stdout="",
            stderr="",
        )
        fake = FakeProducerAdapter(canned_exception=exc)
        result = SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)
        with pytest.raises(ReviewerInvocationError):
            fake.parse_output(result)

    def test_check_auth_records_call(self):
        fake = FakeProducerAdapter()
        fake.check_auth()
        fake.check_auth()
        assert fake.check_auth_calls == 2

    def test_check_auth_raises_canned_exception(self):
        exc = ReviewerInvocationError(
            "auth broken",
            returncode=1,
            stdout="",
            stderr="",
        )
        fake = FakeProducerAdapter(canned_auth_exception=exc)
        with pytest.raises(ReviewerInvocationError):
            fake.check_auth()

    def test_fixture_commit_advances_head(self, tmp_path):
        """The fixture-commit path lets orchestrator tests exercise
        the producer-committed scenario without spawning a real
        subprocess. ``build_invocation`` writes a real commit to
        the worktree; ``git rev-parse HEAD`` then differs from the
        round-start SHA, which is exactly the signal
        :func:`syncade.producer.run_producer` reads to distinguish
        committed-vs-stalled outcomes."""
        _init_git_worktree(tmp_path)
        start_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        fake = FakeProducerAdapter(commit_message="fix: handle the null case")
        fake.build_invocation(ProducerConfig(), tmp_path, "p")

        end_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert end_sha != start_sha

        # the commit subject is the configured one
        subject = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert subject == "fix: handle the null case"

    def test_no_fixture_commit_leaves_head_unchanged(self, tmp_path):
        """commit_message=None (the default) simulates the
        producer-stall path — the fake's build_invocation runs
        but doesn't commit, so HEAD stays at the round-start SHA."""
        _init_git_worktree(tmp_path)
        start_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        fake = FakeProducerAdapter()  # commit_message defaults to None
        fake.build_invocation(ProducerConfig(), tmp_path, "p")

        end_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert end_sha == start_sha

    def test_multiple_invocations_produce_distinct_commits(self, tmp_path):
        """Multi-commit producer scenario: a producer that
        addresses two findings with two commits. The fake's
        per-call counter ensures each fixture commit has a
        distinct file basename."""
        _init_git_worktree(tmp_path)
        start_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        fake = FakeProducerAdapter(commit_message="fix: A")
        fake.build_invocation(ProducerConfig(), tmp_path, "p1")
        mid_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        fake.build_invocation(ProducerConfig(), tmp_path, "p2")
        end_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert start_sha != mid_sha
        assert mid_sha != end_sha
        # Three distinct SHAs across the chain
        assert len({start_sha, mid_sha, end_sha}) == 3
