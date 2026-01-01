"""Tests for :mod:`syncade.producer` — the PR-8 producer subprocess
phase + stall detection (part 2: run_producer escalation / subprocess
error / setup failure / partial-output paths + prompt substitution).

Uses :class:`~syncade.adapters.fake.FakeProducerAdapter` to avoid
spawning real ``claude`` / ``codex`` subprocesses. The fake's
optional fixture-commit lets these tests exercise the
``committed`` / ``stalled`` outcome split end-to-end without ever
shelling out to a real LLM.
"""

from __future__ import annotations

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeProducerAdapter
from syncade.adapters.producer import ProducerOutput
from syncade.config import ProducerConfig
from syncade.findings import ReviewerOutputError
from syncade.process import SubprocessResult
from syncade.producer import run_producer
from tests.producer._helpers import (
    _git_required,
    _make_findings_md,
    _make_pr_doc,
    _read_head,
    _seed_repo,
)

# ---------------------------------------------------------------------------
# run_producer — escalation path (PR-22)
# ---------------------------------------------------------------------------


class TestRunProducerEscalated:
    def _esc_narrative(self) -> str:
        import json

        from syncade.producer_escalation import ESCALATE_CLOSE, ESCALATE_OPEN

        payload = json.dumps(
            {
                "finding_indices": [0],
                "finding": "run-init byte-identity vs full-schema echo",
                "decision": "Should run-init omit empty checks, or is the echo intentional?",
                "options": ["Omit empty checks", "Redefine the invariant"],
                "rationale": (
                    "Reproduced: zero-config run-init.json gains checks:[]; the brief "
                    "says byte-identical. The two cannot both hold — operator must rule."
                ),
            }
        )
        return f"I cannot resolve this in code.\n{ESCALATE_OPEN}\n{payload}\n{ESCALATE_CLOSE}\n"

    def test_escalation_block_no_commit_yields_escalated(self, tmp_path):
        """An escalation block + no commit → outcome='escalated', HEAD
        unchanged, the structured escalation populated."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            canned_output=ProducerOutput(narrative_text=self._esc_narrative())
        )  # commit_message=None → no commit
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
        assert result.outcome == "escalated"
        assert result.ending_sha == starting_sha
        assert result.escalation is not None
        assert result.escalation.decision.startswith("Should run-init")
        assert len(result.escalation.options) == 2

    def test_stall_without_escalation_block_stays_stalled(self, tmp_path):
        """Regression: a plain stall (no escalation block) must NOT become
        escalated — escalation requires the structured block."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            canned_output=ProducerOutput(narrative_text="I can't fix this; underspecified.")
        )
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
        assert result.escalation is None

    def test_commit_plus_escalation_block_is_committed_not_escalated(self, tmp_path):
        """Orchestrator-level AC5 enforcement (QA finding 1): a producer that
        makes PROGRESS (commits) is 'committed' and the loop CONTINUES (so the
        fixes get re-reviewed), even when its narrative ALSO carries an
        escalation block. Escalation is detected ONLY on the no-commit path, so
        'escalated' means 'no fixable progress this round' — the loop pauses for
        a decision only when the producer had nothing to commit."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            commit_message="fix: the fixable blocker",  # HEAD moves
            canned_output=ProducerOutput(
                narrative_text=self._esc_narrative()
            ),  # + escalation block
        )
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
        assert result.escalation is None

    def test_run_producer_threads_operator_decision_into_prompt(self, tmp_path):
        """PR-22: run_producer passes operator_decision through to the rendered
        producer prompt (the resumed-escalation injection path)."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(commit_message="fix: apply the operator's decision")
        run_producer(
            worktree_path=tmp_path,
            starting_sha=starting_sha,
            pr_doc_path=pr_doc,
            findings_md_path=findings,
            test_run_stdout_path=None,
            producer_config=ProducerConfig(),
            timeout_seconds=30.0,
            round_number=1,
            max_rounds=3,
            repo_root=tmp_path,
            adapter=fake,
            operator_decision="OPERATOR DECISION: omit empty checks from config_snapshot.",
        )
        # The fake recorded the rendered prompt; the decision must be in it.
        assert fake.invocations
        _, _, prompt = fake.invocations[0]
        assert "OPERATOR DECISION: omit empty checks from config_snapshot." in prompt


# ---------------------------------------------------------------------------
# run_producer — subprocess error paths
# ---------------------------------------------------------------------------


class TestRunProducerSubprocessError:
    def test_invocation_error_yields_subprocess_error(self, tmp_path):
        """Adapter's parse_output raising ReviewerInvocationError
        (subprocess-side failure: auth, network, model error) →
        ``outcome="subprocess_error"``."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            canned_exception=ReviewerInvocationError(
                "claude returned is_error",
                returncode=1,
                stdout="envelope-content",
                stderr="",
                api_error_status=401,
            ),
        )
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

        assert result.outcome == "subprocess_error"
        assert isinstance(result.error, ReviewerInvocationError)
        assert result.error.api_error_status == 401
        assert result.output is None
        # raw_subprocess_result preserved so .stdout / .stderr can
        # still be persisted for operator inspection.
        assert result.raw_subprocess_result is not None
        # HEAD didn't move
        assert result.ending_sha == starting_sha

    def test_output_error_yields_subprocess_error(self, tmp_path):
        """Adapter raising ReviewerOutputError (unparseable
        output) also yields subprocess_error — the producer has
        no separate parse-failure exit code, so both
        adapter-side failures route to the same outcome."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            canned_exception=ReviewerOutputError("garbled output"),
        )
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
        assert result.outcome == "subprocess_error"
        assert isinstance(result.error, ReviewerOutputError)


# ---------------------------------------------------------------------------
# run_producer — starting_sha mismatch + bad provider routing
# ---------------------------------------------------------------------------


class TestRunProducerSetupFailures:
    def test_starting_sha_mismatch_yields_subprocess_error(self, tmp_path):
        """If the worktree's actual HEAD doesn't match the caller's
        starting_sha, run_producer doesn't proceed — surfaces as
        subprocess_error so the operator gets a clear .error.txt
        rather than a confused producer log post-mortem."""
        _git_required()
        actual_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        wrong_sha = "0" * 40
        assert wrong_sha != actual_sha

        fake = FakeProducerAdapter(commit_message="fix: x")
        result = run_producer(
            worktree_path=tmp_path,
            starting_sha=wrong_sha,
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
        assert result.outcome == "subprocess_error"
        assert "mismatch" in str(result.error)
        # The fake's fixture commit must NOT have been written (the
        # mismatch is detected BEFORE the adapter's build_invocation
        # runs).
        assert _read_head(tmp_path) == actual_sha

    def test_unknown_provider_routes_to_subprocess_error(self, tmp_path):
        """Provider not in the producer registry → ValueError →
        subprocess_error. The orchestrator persists the error so
        the operator sees the actionable diagnostic in
        producer.error.txt."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        # Bypass the schema (Literal["anthropic","openai"]) so we
        # can drive the registry's runtime failure path.
        bad_cfg = ProducerConfig.model_construct(
            provider="google",
            model="gemini-x",
            thinking="high",
            permissions="yolo",
        )
        result = run_producer(
            worktree_path=tmp_path,
            starting_sha=starting_sha,
            pr_doc_path=pr_doc,
            findings_md_path=findings,
            test_run_stdout_path=None,
            producer_config=bad_cfg,
            timeout_seconds=30.0,
            round_number=0,
            max_rounds=3,
            repo_root=tmp_path,
            # adapter=None → registry lookup runs and fails.
            adapter=None,
        )
        assert result.outcome == "subprocess_error"
        assert isinstance(result.error, ValueError)
        assert "google" in str(result.error)


# ---------------------------------------------------------------------------
# run_producer — partial-output preservation on subprocess errors
# ---------------------------------------------------------------------------


class TestRunProducerPartialOutput:
    """When the underlying subprocess fails partway through, the
    producer module preserves whatever the subprocess emitted so the
    orchestrator's persistence layer can write .stdout / .stderr
    for operator inspection. This mirrors
    :class:`ReviewerRunResult` and :class:`SynthesizerResult`.

    These tests use a stub adapter that pretends the subprocess
    failed mid-execution — using the fake adapter's canned
    exception path."""

    def test_invocation_error_preserves_raw_result(self, tmp_path):
        """When parse_output raises after the subprocess produced
        output, raw_subprocess_result should be non-None and carry
        the captured stdout/stderr."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            canned_exception=ReviewerInvocationError(
                "claude returned is_error",
                returncode=1,
                stdout="ENVELOPE",
                stderr="STDERR",
                api_error_status=None,
            ),
        )
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
        # The fake's parse_output raised, so subprocess_result is the
        # ``_noop_argv`` exit-0 output (empty strings) — but it's
        # NOT None. That's the contract: parse-failure preserves the
        # raw result so persistence has something to write.
        assert result.raw_subprocess_result is not None
        assert isinstance(result.raw_subprocess_result, SubprocessResult)


# ---------------------------------------------------------------------------
# Smoke: render_producer_prompt placeholder substitution
# ---------------------------------------------------------------------------


def test_run_producer_substitutes_test_run_stdout_path_none(tmp_path):
    """When test_run_stdout_path is None, the renderer substitutes
    the literal "(no test failure this round)" sentinel into the
    template — the format_map mapping never sees a Python ``None``.

    This is verified indirectly: the producer template references
    ``{test_run_stdout_path}`` and run_producer should not raise
    when the path is None.
    """
    _git_required()
    starting_sha = _seed_repo(tmp_path)
    pr_doc = _make_pr_doc(tmp_path)
    findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

    fake = FakeProducerAdapter(commit_message="fix: x")
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

    # Inspect the rendered prompt the fake recorded — the sentinel
    # should be in there literally.
    assert len(fake.invocations) == 1
    _, _, prompt = fake.invocations[0]
    assert "(no test failure this round)" in prompt


def test_run_producer_substitutes_test_run_stdout_path_present(tmp_path):
    """When test_run_stdout_path is supplied, the renderer
    substitutes its string form into the prompt."""
    _git_required()
    starting_sha = _seed_repo(tmp_path)
    pr_doc = _make_pr_doc(tmp_path)
    findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")
    test_stdout = tmp_path / "test-run.stdout"
    test_stdout.write_text("FAIL: test_x\n", encoding="utf-8")

    fake = FakeProducerAdapter(commit_message="fix: x")
    run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings,
        test_run_stdout_path=test_stdout,
        producer_config=ProducerConfig(),
        timeout_seconds=30.0,
        round_number=0,
        max_rounds=3,
        repo_root=tmp_path,
        adapter=fake,
    )
    _, _, prompt = fake.invocations[0]
    # Staged into the worktree and rendered as a worktree-relative ref
    # (H4 confinement) — the basename appears, the sentinel does not.
    assert "test-run.stdout" in prompt
    assert "(no test failure this round)" not in prompt


# ---------------------------------------------------------------------------
# run_producer — transient retry, end-to-end through a REAL _run_producer_once (PR-v2-22)
# ---------------------------------------------------------------------------
class TestRunProducerTransientRetryEndToEnd:
    def test_transient_parse_error_retries_and_resets_the_real_worktree(
        self, tmp_path, monkeypatch
    ):
        """The full path: a REAL _run_producer_once runs a real subprocess, its parse raises a
        transient ReviewerInvocationError (429), and that surfaces as the exact shape the wrapper
        keys on (subprocess_error + transient error + HEAD unchanged). The wrapper then retries
        up to MAX_RETRIES, resetting the real worktree each time, and returns retries==MAX_RETRIES.
        Confirms the _run_producer_once ↔ run_producer integration the unit tests mock away."""
        import subprocess

        import syncade.producer as producer_mod
        import syncade.retry as retry_mod

        _git_required()
        _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")
        # Commit the inputs so they are in ``starting_sha``'s tree — a reset (reset --hard +
        # clean) then PRESERVES them, modelling a real run where inputs live outside the
        # producer worktree (external pr_doc, gitignored findings.md) and survive/re-stage.
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "inputs"], cwd=tmp_path, check=True, capture_output=True
        )
        starting_sha = _read_head(tmp_path)
        monkeypatch.setattr(producer_mod.retry, "backoff_sleep", lambda i: None)  # no real sleep

        fake = FakeProducerAdapter(
            canned_exception=ReviewerInvocationError(
                "blip", returncode=1, stdout="", stderr="rate limit", api_error_status=429
            )
        )
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
        assert result.outcome == "subprocess_error"
        assert retry_mod.is_transient_api_error(result.error)  # the transient error surfaced
        assert result.retries == retry_mod.MAX_RETRIES  # exhausted the bound end-to-end
        assert _read_head(tmp_path) == starting_sha  # worktree stayed clean across resets

    def test_transient_after_commit_is_accepted_as_committed(self, tmp_path):
        """C1 end-to-end with REAL git: the producer actually COMMITS (fixture commit moves HEAD),
        then its output parse raises a transient 429. run_producer must ACCEPT the commit —
        outcome=committed with the real moved HEAD, error cleared, no retry — rather than dropping
        the work at exit 40 as it did before PR-v2-22's C1 fix. The HEAD reads are real (they live
        in producer_git), so this proves the wrapper's authoritative read reconciles the commit."""
        _git_required()
        starting_sha = _seed_repo(tmp_path)
        pr_doc = _make_pr_doc(tmp_path)
        findings = _make_findings_md(tmp_path / ".syncade" / "runs" / "r" / "round-0")

        fake = FakeProducerAdapter(
            commit_message="fix: committed before the blip",
            canned_exception=ReviewerInvocationError(
                "blip", returncode=1, stdout="", stderr="rate limit", api_error_status=429
            ),
        )
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
        moved = _read_head(tmp_path)
        assert moved != starting_sha  # the producer really committed
        assert result.outcome == "committed"  # ...and the commit is ACCEPTED, not discarded
        assert result.ending_sha == moved
        assert result.error is None and result.output is not None  # committed contract satisfied
        assert result.retries == 0  # a commit landed → no retry
        assert "429" in result.output.narrative_text or "ReviewerInvocationError" in (
            result.output.narrative_text
        )  # the trailing error is surfaced in the narrative, not silently dropped
