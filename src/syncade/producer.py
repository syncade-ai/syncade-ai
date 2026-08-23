"""Producer subprocess phase.

Runs ONE producer subprocess after a NO-SHIP round, fed the
just-completed round's ``findings.md`` + (when applicable)
``test-run.stdout`` + the PR spec. The producer is expected to
make file edits and commit them; the orchestrator's stall
detection compares ``git rev-parse HEAD`` before and after the
subprocess to distinguish "committed" (HEAD moved) from "stalled"
(HEAD didn't move, no commit happened).

Module name parallels :mod:`syncade.synthesizer`: the orchestrator
treats reviewer, synthesizer, test-leg, and producer outcomes with
the same vocabulary (output | error, raw_subprocess_result,
duration_seconds, one-shot __post_init__ discipline).

**Architectural invariants this module enforces (vs. relies on):**

- *Producer repository is provisioned by the orchestrator.* This
  module reads the SHA at start + end to detect commits but never
  creates/destroys the repository; the orchestrator owns lifecycle, so
  a round-N stall leaves the standalone repository for inspection.

- *No structured-output parse.* Producers emit free-form narrative,
  not JSON. The adapter's ``parse_output`` extracts the narrative
  text; this module just preserves it on :class:`ProducerOutput` and
  hands the value back to the orchestrator. There's no equivalent
  of :func:`syncade.synthesis.parse_synthesizer_output` here.

- *Stall detection compares SHAs only.* Uncommitted edits are
  invisible to git history and cannot become an importable candidate.
  The prompt tells the model to
  commit; the orchestrator never auto-commits (that would forge a
  commit even with no edits, obscuring the stall signal).

- *The orchestrator passes ``starting_sha`` explicitly.* The repository
  is provisioned at the round-start ``commit_sha`` (detached HEAD);
  this module verifies HEAD matches it at entry, then re-reads HEAD at
  exit to derive ``ending_sha``.

- *Partial-output preservation on subprocess errors.* As in
  ``SynthesizerResult`` / ``ReviewerRunResult``: on timeout,
  ``raw_subprocess_result`` is synthesized from the
  :class:`SubprocessTimeoutError`'s partial stdout/stderr (sentinel
  ``returncode=-1``) so persistence still writes the ``.stdout`` /
  ``.stderr`` files; on ``SubprocessNotFoundError`` the process
  never ran, so it is ``None``.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path

from syncade import retry
from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.producer import ProducerAdapter
from syncade.adapters.producer import ProducerOutput as ProducerOutput
from syncade.config import ProducerConfig
from syncade.findings import ReviewerOutputError
from syncade.pricing_config import PricingConfig
from syncade.process import (
    SubprocessError,
)
from syncade.producer_escalation import ProducerEscalation as ProducerEscalation
from syncade.producer_git import (
    _accept_committed_after_error,
    _authoritative_head,
    _reset_worktree,
)

# ProducerResult is used directly (run_producer constructs it); ProducerOutcome is
# re-exported (redundant-alias form so ruff's F401 autofix keeps it) to preserve
# the `from syncade.producer import ProducerOutcome` public import path.
from syncade.producer_result import ProducerOutcome as ProducerOutcome
from syncade.producer_result import ProducerResult
from syncade.prompts import (
    _NO_OPERATOR_DECISION_SENTINEL,
    _NO_PRIOR_COMMITS_SENTINEL,
    _NO_PRIOR_ROUND_SENTINEL,
)
from syncade.usage import Usage, _add_usage

# Errors that mean the producer's SESSION RAN but its output was the problem — so an isolated
# commit may exist first (unlike a timeout/setup failure, where none could). Gates C1 reconcile.
_SESSION_ERRORS = (ReviewerInvocationError, ReviewerOutputError)

from syncade.producer_attempt import _run_producer_once  # noqa: E402


def run_producer(
    *,
    worktree_path: Path,
    starting_sha: str,
    pr_doc_path: Path,
    findings_md_path: Path,
    test_run_stdout_path: Path | None,
    producer_config: ProducerConfig,
    timeout_seconds: float,
    round_number: int,
    max_rounds: int,
    repo_root: Path,
    adapter: ProducerAdapter | None = None,
    prior_round_output: str = _NO_PRIOR_ROUND_SENTINEL,
    pricing: PricingConfig | None = None,
    prior_round_commits: str = _NO_PRIOR_COMMITS_SENTINEL,
    operator_decision: str = _NO_OPERATOR_DECISION_SENTINEL,
    max_retries: int = retry.MAX_RETRIES,
    capture_dir: Path | None = None,
) -> ProducerResult:
    """Run the producer with a bounded, side-effect-safe transient retry (PR-v2-22).

    Thin wrapper over :func:`_run_producer_once`. A transient blip (429/5xx/dropped socket, a
    ``ReviewerInvocationError``) is retried up to ``max_retries`` times (``[retry]``) with backoff
    instead of aborting at exit 40. The producer has SIDE EFFECTS, so — see the inline notes —
    **C1** accepts a committed-then-errored session as ``committed`` rather than discarding it
    (:func:`_accept_committed_after_error`; a forced timeout is excluded), **Q3** reads HEAD
    authoritatively (:func:`_authoritative_head`) so an unreadable HEAD isn't mistaken for "no
    commit", **C2** resets to ``starting_sha`` per retry, **C3** retries transient errors only.
    ``ProducerResult.retries`` is the extra-attempt count (0 on the happy path).
    """
    once_kwargs = dict(
        worktree_path=worktree_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc_path,
        findings_md_path=findings_md_path,
        test_run_stdout_path=test_run_stdout_path,
        producer_config=producer_config,
        timeout_seconds=timeout_seconds,
        round_number=round_number,
        max_rounds=max_rounds,
        repo_root=repo_root,
        adapter=adapter,
        prior_round_output=prior_round_output,
        pricing=pricing,
        prior_round_commits=prior_round_commits,
        operator_decision=operator_decision,
        capture_dir=capture_dir,
    )
    run_start = time.monotonic()
    retries = 0
    accumulated_usage: Usage | None = None
    result = _run_producer_once(**once_kwargs)
    accumulated_usage = _add_usage(accumulated_usage, result.usage)
    while (
        retries < max_retries
        and result.outcome == "subprocess_error"
        and result.error is not None
        and retry.is_transient_api_error(result.error)
        # Q3: reset+retry ONLY on a PROVEN no-commit. A moved HEAD (committed) or an unreadable
        # HEAD (indeterminate) both fail this equality, so the reset never clobbers a commit.
        and _authoritative_head(worktree_path) == starting_sha
    ):
        # C2: if the reset fails (non-zero exit or launch error), the worktree is in an
        # indeterminate state — do NOT retry on top of partial state. Break and return the
        # last transient error result, preserving C2 (every retry starts from starting_sha).
        try:
            _reset_worktree(worktree_path, starting_sha)
        except SubprocessError:
            break
        retry.backoff_sleep(retries + 1)
        retries += 1
        result = _run_producer_once(**once_kwargs)
        accumulated_usage = _add_usage(accumulated_usage, result.usage)
    # C1: if the session ran and errored on its output (`_SESSION_ERRORS`) it may have committed
    # first — a moved HEAD is a real commit, so accept it instead of dropping the work at exit 40.
    # That error class EXCLUDES a forced timeout (a hung producer's partial commit stays a
    # subprocess_error, per test_producer_timeout), a starting-sha mismatch, and a missing binary —
    # none is a completed-session-then-error. The mismatch precondition is checked BEFORE the
    # subprocess, so reaching a session error proves the worktree started at starting_sha; any HEAD
    # move is thus a genuine descendant commit. An unreadable HEAD (None) stays a subprocess_error:
    # we never fabricate a commit we cannot see (Q3).
    if result.outcome == "subprocess_error" and isinstance(result.error, _SESSION_ERRORS):
        head = _authoritative_head(worktree_path)
        if head is not None and head != starting_sha:
            result = _accept_committed_after_error(result, ending_sha=head)
    # C (PR-v2-22): surface the retry's TRUE cost — usage accumulated across EVERY attempt (a
    # dropped 429 attempt still burned tokens; under-counting could nudge a run past its budget)
    # and, WHEN RETRIED, wall-clock duration spanning all attempts + resets + backoff sleeps. On
    # the happy path (retries == 0) usage sums to the one attempt and duration stays the inner
    # measure, so the result is byte-identical to pre-retry (C5); overriding only on retry.
    return dataclasses.replace(
        result,
        retries=retries,
        usage=accumulated_usage,
        duration_seconds=(
            result.duration_seconds if retries == 0 else time.monotonic() - run_start
        ),
    )
