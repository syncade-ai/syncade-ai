"""PR-v2-22 Issue 2: run_producer's bounded, side-effect-SAFE transient retry.

The retry logic lives in the thin ``run_producer`` wrapper; ``_run_producer_once`` (the original
single-attempt body) is unchanged and covered by the rest of tests/producer/. So these tests
patch ``_run_producer_once`` with canned :class:`ProducerResult`s AND ``_authoritative_head``
with a controlled HEAD, then assert the wrapper's control flow: which outcomes retry, the Q3
gate (never reset on a moved or unreadable HEAD), the C1 reconcile (accept a committed-then-
errored session, timeout excluded), the bound, and the surfaced ``retries`` count.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.producer import ProducerOutput
from syncade.config import ProducerConfig
from syncade.process import SubprocessTimeoutError
from syncade.producer_escalation import ProducerEscalation
from syncade.producer_result import ProducerResult
from syncade.usage import Usage

_S = "a" * 40  # starting sha
_MOVED = "b" * 40  # a moved HEAD == the producer committed
_INDETERMINATE = object()  # sentinel: the wrapper's authoritative HEAD read raises (unreadable)


def _transient(ending_sha: str = _S) -> ProducerResult:
    return ProducerResult(
        outcome="subprocess_error",
        starting_sha=_S,
        ending_sha=ending_sha,
        duration_seconds=1.0,
        output=None,
        error=ReviewerInvocationError(
            "blip", returncode=1, stdout="", stderr="rate limit", api_error_status=429
        ),
    )


def _committed() -> ProducerResult:
    return ProducerResult(
        outcome="committed",
        starting_sha=_S,
        ending_sha=_MOVED,
        duration_seconds=1.0,
        output=ProducerOutput(narrative_text="fix"),
        error=None,
    )


def _stalled() -> ProducerResult:
    return ProducerResult(
        outcome="stalled",
        starting_sha=_S,
        ending_sha=_S,
        duration_seconds=1.0,
        output=ProducerOutput(narrative_text="no-op"),
        error=None,
    )


def _escalated() -> ProducerResult:
    return ProducerResult(
        outcome="escalated",
        starting_sha=_S,
        ending_sha=_S,
        duration_seconds=1.0,
        output=ProducerOutput(narrative_text="decide"),
        error=None,
        escalation=ProducerEscalation(
            finding_indices=[0], finding="f", decision="d", options=["a"], rationale="r"
        ),
    )


def _nontransient() -> ProducerResult:
    return ProducerResult(
        outcome="subprocess_error",
        starting_sha=_S,
        ending_sha=_S,
        duration_seconds=1.0,
        output=None,
        error=ReviewerInvocationError(
            "bad", returncode=1, stdout="", stderr="bad request", api_error_status=400
        ),
    )


def _timeout() -> ProducerResult:
    return ProducerResult(
        outcome="subprocess_error",
        starting_sha=_S,
        ending_sha=_S,
        duration_seconds=1.0,
        output=None,
        error=SubprocessTimeoutError("timed out", timeout=1800.0, stdout="", stderr=""),
    )


def _drive(monkeypatch, results, *, heads=None, max_retries=None):
    """Patch _run_producer_once to yield `results` in order, capture reset calls, and control what
    the wrapper's AUTHORITATIVE HEAD read (`_authoritative_head` → `_read_worktree_head`) returns
    per attempt. Returns (result, {"once": N, "reset": [(wt, sha), ...], "head_reads": N}).

    `heads`: what the authoritative read sees after each `_run_producer_once` call — a full SHA, or
    `_INDETERMINATE` to make the read raise (an unreadable HEAD). Deliberately INDEPENDENT of each
    result's `ending_sha`, because the whole point of the fix is that the wrapper re-reads HEAD
    itself instead of trusting a possibly-collapsed error `ending_sha`. Defaults to each result's
    `ending_sha` (the honest case where the two agree). The value for the current attempt is stable
    across the (up to two) reads the wrapper does per attempt."""
    import syncade.producer as producer_mod

    ending = heads if heads is not None else [r.ending_sha for r in results]
    calls: dict = {"once": 0, "reset": [], "head_reads": 0}
    it = iter(results)

    def _fake_once(**kwargs):
        calls["once"] += 1
        return next(it)

    def _fake_head(worktree_path):
        # Patches `_authoritative_head` (the name run_producer resolves), which returns the SHA or
        # None — mirroring its str | None contract. `_INDETERMINATE` → None (unreadable HEAD).
        calls["head_reads"] += 1
        val = ending[calls["once"] - 1]  # HEAD as of the latest attempt; stable across reads
        return None if val is _INDETERMINATE else val

    monkeypatch.setattr(producer_mod, "_run_producer_once", _fake_once)
    monkeypatch.setattr(producer_mod, "_authoritative_head", _fake_head)
    monkeypatch.setattr(
        producer_mod, "_reset_worktree", lambda wt, sha: calls["reset"].append((wt, sha))
    )
    monkeypatch.setattr(producer_mod.retry, "backoff_sleep", lambda i: None)  # no real sleep

    extra = {} if max_retries is None else {"max_retries": max_retries}
    result = producer_mod.run_producer(
        worktree_path=Path("/tmp/wt"),
        starting_sha=_S,
        pr_doc_path=Path("/tmp/pr.md"),
        findings_md_path=Path("/tmp/f.md"),
        test_run_stdout_path=None,
        producer_config=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        timeout_seconds=1800.0,
        round_number=0,
        max_rounds=2,
        repo_root=Path("/tmp"),
        **extra,
    )
    return result, calls


class TestProducerRetry:
    def test_c3_transient_then_success_retries_once(self, monkeypatch):
        result, calls = _drive(monkeypatch, [_transient(), _committed()])
        assert result.outcome == "committed" and result.retries == 1
        assert calls["once"] == 2
        assert calls["reset"] == [(Path("/tmp/wt"), _S)]  # reset once, before the retry

    def test_c1_committed_then_transient_is_accepted_not_retried(self, monkeypatch):
        # The producer committed (HEAD moved) and THEN a late transient blip fired. C1: accept the
        # commit — reconcile to `committed` so the orchestrator advances the branch — never reset
        # or retry over it.
        result, calls = _drive(monkeypatch, [_transient(ending_sha=_MOVED)])
        assert result.outcome == "committed" and result.ending_sha == _MOVED
        assert result.output is not None and result.error is None  # committed contract satisfied
        assert result.retries == 0 and calls["once"] == 1 and calls["reset"] == []

    def test_q3_indeterminate_head_blocks_reset(self, monkeypatch):
        # The producer MAY have committed, but the post-error HEAD read is unreadable. Never reset
        # (it would destroy an unobserved commit); surface the error, preserve the worktree.
        result, calls = _drive(monkeypatch, [_transient()], heads=[_INDETERMINATE])
        assert result.outcome == "subprocess_error"  # honestly surfaced, not fabricated
        assert (
            result.retries == 0 and calls["reset"] == []
        )  # CRITICAL: no reset over an unread HEAD

    def test_q3_recovers_commit_missed_by_collapsed_ending_sha(self, monkeypatch):
        # _run_producer_once collapsed ending_sha to starting (its best-effort read failed), but the
        # commit is real — the wrapper's own authoritative read sees the moved HEAD and accepts it.
        result, calls = _drive(monkeypatch, [_transient(ending_sha=_S)], heads=[_MOVED])
        assert result.outcome == "committed" and result.ending_sha == _MOVED
        assert result.retries == 0 and calls["reset"] == []

    def test_c1_committed_then_non_transient_error_is_accepted(self, monkeypatch):
        # A completed session that errored on output is accepted regardless of the error's
        # transient-ness: a 400 after committing is still accepted. No retry, no reset.
        result, calls = _drive(monkeypatch, [_nontransient()], heads=[_MOVED])
        assert result.outcome == "committed" and result.ending_sha == _MOVED
        assert result.retries == 0 and calls["once"] == 1 and calls["reset"] == []

    def test_committed_then_timeout_is_not_accepted(self, monkeypatch):
        # A forced TIMEOUT is a hung/killed producer, not a completed-session-then-error. Even with
        # a moved HEAD, keep it a subprocess_error so the operator sees the hang (exit 40) rather
        # than the loop silently continuing from a possibly-mid-operation partial commit.
        result, _ = _drive(monkeypatch, [_timeout()], heads=[_MOVED])
        assert result.outcome == "subprocess_error"
        assert isinstance(result.error, SubprocessTimeoutError)

    def test_c4_bounded_at_max_retries(self, monkeypatch):
        import syncade.retry as retry_mod

        result, calls = _drive(monkeypatch, [_transient()] * (retry_mod.MAX_RETRIES + 1))
        assert result.retries == retry_mod.MAX_RETRIES  # bounded — not infinite
        assert calls["once"] == retry_mod.MAX_RETRIES + 1
        assert len(calls["reset"]) == retry_mod.MAX_RETRIES

    @pytest.mark.parametrize("bound", [0, 1, 3])
    def test_config_max_retries_threads_to_the_bound(self, monkeypatch, bound):
        """PR-v2-9: ``config.retry.max_retries`` reaches the producer loop bound. A
        never-recovering transient runs exactly ``bound + 1`` attempts — 0 disables retries
        (1 attempt, no reset), and the surfaced ``retries`` equals the configured bound."""
        result, calls = _drive(monkeypatch, [_transient()] * (bound + 1), max_retries=bound)
        assert result.retries == bound
        assert calls["once"] == bound + 1
        assert len(calls["reset"]) == bound

    def test_c3_non_transient_error_not_retried(self, monkeypatch):
        result, calls = _drive(monkeypatch, [_nontransient()])
        assert result.retries == 0 and calls["once"] == 1 and calls["reset"] == []

    def test_c3_timeout_not_retried(self, monkeypatch):
        result, calls = _drive(monkeypatch, [_timeout()])
        assert result.retries == 0 and calls["once"] == 1 and calls["reset"] == []

    def test_c6_stall_not_retried(self, monkeypatch):
        result, calls = _drive(monkeypatch, [_stalled()])
        assert result.outcome == "stalled" and result.retries == 0 and calls["once"] == 1

    def test_c6_escalation_not_retried(self, monkeypatch):
        result, calls = _drive(monkeypatch, [_escalated()])
        assert result.outcome == "escalated" and result.retries == 0 and calls["once"] == 1

    def test_c5_committed_first_try_no_retry_no_reset(self, monkeypatch):
        result, calls = _drive(monkeypatch, [_committed()])
        assert result.outcome == "committed" and result.retries == 0
        assert calls["once"] == 1 and calls["reset"] == []

    def test_c_usage_accumulates_across_retry_attempts(self, monkeypatch):
        # Finding C: a dropped transient attempt still burned tokens. The returned usage must SUM
        # every attempt — reporting only the final one under-counts spend against the budget.
        u1 = Usage(model="m", input_tokens=100, output_tokens=40)
        u2 = Usage(model="m", input_tokens=200, output_tokens=60)
        r1 = dataclasses.replace(_transient(), usage=u1)  # attempt 1: 429, no commit
        r2 = dataclasses.replace(_committed(), usage=u2)  # attempt 2: success
        result, _ = _drive(monkeypatch, [r1, r2])
        assert result.outcome == "committed" and result.retries == 1
        assert result.usage is not None
        assert result.usage.input_tokens == 300  # 100 + 200, not just the final 200
        assert result.usage.output_tokens == 100  # 40 + 60

    def test_c_duration_spans_the_whole_retry_loop(self, monkeypatch):
        # Finding C: duration is wall-clock across all attempts + resets + backoff sleeps, not the
        # final attempt's internal measure (which excludes reset + backoff). A monotonic clock that
        # advances a fixed step per call makes run_producer's two reads (start, end) deterministic.
        import syncade.producer as producer_mod

        clock = [100.0]

        def _fake_monotonic():
            clock[0] += 12.5
            return clock[0]

        monkeypatch.setattr(producer_mod.time, "monotonic", _fake_monotonic)
        result, _ = _drive(monkeypatch, [_transient(), _committed()])
        assert result.duration_seconds == 12.5  # end - start, one step apart, spanning the retry


def test_c2_reset_worktree_raises_on_nonzero_returncode(monkeypatch):
    """Regression: _reset_worktree must raise SubprocessError when git reset/clean exits non-zero.
    Previously it swallowed all errors, letting a failed reset silently pass and allowing a dirty
    worktree into the next attempt (C2 violation). Now it checks the returncode and raises."""
    import syncade.producer_git as pgit
    from syncade.process import SubprocessError, SubprocessResult
    from syncade.producer_git import _reset_worktree

    bad_result = SubprocessResult(
        returncode=1, stdout="", stderr="permission denied", duration_seconds=0.0
    )
    monkeypatch.setattr(pgit, "run_subprocess", lambda *a, **kw: bad_result)

    with pytest.raises(SubprocessError, match="reset"):
        _reset_worktree(Path("/tmp/wt"), "a" * 40)


def test_c2_retry_loop_stops_when_reset_fails(monkeypatch):
    """Regression: when _reset_worktree raises, run_producer must NOT retry (that would run on
    partial state). The last transient error result is returned instead."""
    import syncade.producer as producer_mod
    from syncade.process import SubprocessError

    once_calls = []
    reset_calls = []

    def _fake_once(**kwargs):
        once_calls.append(1)
        return _transient()

    def _failing_reset(wt, sha):
        reset_calls.append((wt, sha))
        raise SubprocessError("git reset failed: permission denied")

    monkeypatch.setattr(producer_mod, "_run_producer_once", _fake_once)
    monkeypatch.setattr(producer_mod, "_reset_worktree", _failing_reset)
    monkeypatch.setattr(producer_mod, "_authoritative_head", lambda wt: _S)
    monkeypatch.setattr(producer_mod.retry, "backoff_sleep", lambda i: None)

    result = producer_mod.run_producer(
        worktree_path=Path("/tmp/wt"),
        starting_sha=_S,
        pr_doc_path=Path("/tmp/pr.md"),
        findings_md_path=Path("/tmp/f.md"),
        test_run_stdout_path=None,
        producer_config=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        timeout_seconds=1800.0,
        round_number=0,
        max_rounds=2,
        repo_root=Path("/tmp"),
    )
    # The reset failed after attempt 1, so attempt 2 must NOT have run.
    assert len(once_calls) == 1  # only the first attempt ran
    assert len(reset_calls) == 1  # the reset was attempted once (then raised)
    assert result.outcome == "subprocess_error"  # last transient error returned as-is
    assert result.retries == 0  # no extra attempt counted


@pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)
def test_c2_reset_worktree_discards_partial_commit_and_edits(tmp_path):
    """C2 substance: _reset_worktree restores the worktree to starting_sha — dropping a partial
    COMMIT, a tracked edit, AND an untracked file — so a retry starts from a clean state."""
    from syncade.producer_git import _read_worktree_head, _reset_worktree

    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@e.com")
    _git("config", "user.name", "t")
    (repo / "f.py").write_text("original\n")
    _git("add", "-A")
    _git("commit", "-qm", "base")
    starting = _read_worktree_head(repo)

    # Simulate a crashed producer attempt: a partial commit + a further tracked edit + junk.
    (repo / "f.py").write_text("half-done\n")
    _git("add", "-A")
    _git("commit", "-qm", "partial")
    (repo / "f.py").write_text("uncommitted\n")
    (repo / "junk.tmp").write_text("untracked\n")

    _reset_worktree(repo, starting)

    assert _read_worktree_head(repo) == starting  # partial commit dropped
    assert (repo / "f.py").read_text() == "original\n"  # tracked edit + commit reverted
    assert not (repo / "junk.tmp").exists()  # untracked file cleaned
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert porcelain == ""  # fully clean — exactly the state the first attempt saw


def test_c2_reset_worktree_removes_nested_git_repo(tmp_path):
    """Regression: _reset_worktree must remove nested (untracked) git repositories.
    ``git clean -fd`` leaves them behind with exit 0; ``git clean -ffd`` removes them."""
    from syncade.producer_git import _reset_worktree

    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*a, cwd=None):
        subprocess.run(["git", *a], cwd=cwd or repo, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@e.com")
    _git("config", "user.name", "t")
    (repo / "f.py").write_text("original\n")
    _git("add", "-A")
    _git("commit", "-qm", "base")

    from syncade.producer_git import _read_worktree_head

    starting = _read_worktree_head(repo)

    # Simulate a crashed producer that left an untracked nested git repo.
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    _git("init", "-q", cwd=nested)
    (nested / "code.py").write_text("x = 1\n")

    _reset_worktree(repo, starting)

    assert not (repo / "vendor").exists(), "nested git repo must be removed by reset"
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert porcelain == ""
