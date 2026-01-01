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

- *Producer worktree is provisioned by the orchestrator.* This
  module reads the SHA at start + end to detect commits but never
  creates/destroys the worktree; the orchestrator owns lifecycle, so
  a round-N stall leaves the worktree for round N+1 (or inspection).

- *No structured-output parse.* Producers emit free-form narrative,
  not JSON. The adapter's ``parse_output`` extracts the narrative
  text; this module just preserves it on :class:`ProducerOutput` and
  hands the value back to the orchestrator. There's no equivalent
  of :func:`syncade.synthesis.parse_synthesizer_output` here.

- *Stall detection compares SHAs only.* Uncommitted edits are
  invisible to git history and thus to the next round's reviewers —
  they didn't happen for the loop. The prompt tells the model to
  commit; the orchestrator never auto-commits (that would forge a
  commit even with no edits, obscuring the stall signal).

- *The orchestrator passes ``starting_sha`` explicitly.* The worktree
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
from syncade.adapters.producer import ProducerAdapter, get_producer_adapter
from syncade.adapters.producer import ProducerOutput as ProducerOutput
from syncade.config import ProducerConfig
from syncade.findings import ReviewerOutputError
from syncade.pricing_config import PricingConfig
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.producer_escalation import ProducerEscalation as ProducerEscalation
from syncade.producer_escalation import parse_producer_escalation
from syncade.producer_git import (
    _accept_committed_after_error,
    _authoritative_head,
    _best_effort_head_after_failure,
    _read_worktree_head,
    _reset_worktree,
    _stage_producer_input,
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
    load_producer_template,
    render_producer_prompt,
)
from syncade.usage import Usage, _add_usage, _auth_mode, usage_for

# Errors that mean the producer's SESSION RAN but its output was the problem — so a commit may
# have landed first (unlike a timeout/setup failure, where none could). Gates the C1 reconcile.
_SESSION_ERRORS = (ReviewerInvocationError, ReviewerOutputError)


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


def _run_producer_once(
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
) -> ProducerResult:
    """Dispatch ONE producer subprocess against ``worktree_path``.

    Lifecycle:

    1. Verify the worktree's current HEAD equals ``starting_sha``
       (catches a misuse where the orchestrator handed a worktree
       that doesn't match the round-start snapshot).
    2. Stage each input (PR doc, findings.md, optional test trace)
       INSIDE the worktree and render the producer prompt with
       worktree-relative paths: load the template via
       :func:`syncade.prompts.load_producer_template` (per-repo
       override at ``.syncade/templates/producer.md`` wins over the
       packaged default), substitute the worktree-relative placeholders.
    3. Build the producer invocation via the adapter (default:
       fresh adapter from :func:`syncade.adapters.producer.get_producer_adapter`
       routed by ``producer_config.provider``).
    4. Run the subprocess via :func:`syncade.process.run_subprocess`.
       Producer-side errors (timeout, binary missing, generic OS
       launch failure) yield ``outcome="subprocess_error"``.
    5. Adapter parses the output — failures
       (:class:`ReviewerInvocationError`,
       :class:`ReviewerOutputError`) also yield
       ``outcome="subprocess_error"``.
    6. Read the worktree's HEAD post-subprocess; if it equals
       ``starting_sha`` → ``outcome="stalled"``, else
       ``outcome="committed"``.

    Stall detection compares git SHAs only. File edits without
    commits don't count — uncommitted work is invisible to git
    history and therefore invisible to the next round's reviewers.
    This module does NOT auto-commit on the producer's behalf;
    commit-message discipline lives in the producer prompt template
    (see ``syncade/templates/producer.md``).

    Args:
        worktree_path: The producer worktree's path. Provisioned by
            the orchestrator at ``starting_sha`` (detached HEAD).
            This module does not create or destroy it; the
            orchestrator owns the lifecycle so a stalled producer's
            worktree stays inspectable on disk.
        starting_sha: The full object ID the worktree was checked out
            at. Must match the worktree's HEAD at function entry —
            mismatch is treated as a subprocess-error outcome.
        pr_doc_path: Path to the PR spec the producer should
            implement against. Staged into the worktree and substituted
            into the template's ``{pr_doc_path}`` placeholder as a
            worktree-relative path (confinement: the producer never sees
            the absolute main-repo path).
        findings_md_path: Path to the just-completed round's
            ``findings.md``. Staged into the worktree and substituted
            into the template's ``{findings_md_path}`` placeholder as a
            worktree-relative path — the producer reads it for the
            synthesizer's consolidated findings + per-reviewer narrative
            summaries.
        test_run_stdout_path: Path to the just-completed round's
            ``test-run.stdout`` when the test leg ran and failed
            (``test_result.outcome == "failed"`` on the preceding
            round). Staged into the worktree and substituted as a
            worktree-relative path when present; ``None`` otherwise —
            substituted as the literal ``"(no test failure this round)"``
            sentinel (the renderer handles the None → sentinel
            replacement strictly through format_map).
        producer_config: The :class:`ProducerConfig` from
            ``.syncade/config.toml``. Used for argv construction
            (provider, model, thinking, permissions) and
            ``timeout_seconds`` (when None, the orchestrator passes
            its resolved fallback; this module does not re-resolve).
        timeout_seconds: Per-producer-round wall-clock timeout.
            Already resolved by the orchestrator (CLI override >
            ``producer.timeout_seconds`` > ``loop.timeout_seconds``).
        round_number: Which round this producer is for (0-indexed,
            so the first producer-after-round-0 receives
            ``round_number=0``). Substituted into the template's
            ``{round_number}`` placeholder for operator-context
            clarity.
        max_rounds: The configured ``[loop] max_rounds`` value.
            Substituted into the template's ``{max_rounds}``
            placeholder so the producer can see "you're in round 1
            of 3" and budget effort accordingly.
        repo_root: The git repo root. Used to look up the per-repo
            template override at
            ``<repo_root>/.syncade/templates/producer.md`` and to compute
            worktree-relative refs for the staged inputs.
        adapter: Optional adapter implementing
            :class:`~syncade.adapters.producer.ProducerAdapter`. When
            ``None``, a fresh adapter is constructed via
            :func:`syncade.adapters.producer.get_producer_adapter`
            using ``producer_config.provider``. Tests pass a
            :class:`~syncade.adapters.fake.FakeProducerAdapter`.
        prior_round_output: cross-round context. The producer's
            OWN prior-round response text (extracted from
            ``<run-id>/round-(N-1)/producer.stdout`` by the
            orchestrator's
            :mod:`syncade.orchestrator.prior_round` helpers). Defaults
            to :data:`syncade.prompts._NO_PRIOR_ROUND_SENTINEL` so
            ``round_number == 0`` callers work unchanged. Substituted into the
            template's
            ``{prior_round_output}`` placeholder.
        prior_round_commits: cross-round context. The commit
            subjects of the prior round's producer commits (derived
            via ``git log -1 --format=%s`` in the operator's repo by
            the orchestrator). Defaults to
            :data:`syncade.prompts._NO_PRIOR_COMMITS_SENTINEL` for
            ``round_number == 0``. Substituted into the template's
            ``{prior_round_commits}`` placeholder.

    Returns:
        :class:`ProducerResult` with the three-outcome contract
        enforced in ``__post_init__``.

    Raises:
        No exceptions — all failure modes map to ``outcome="subprocess_error"``
        so persistence writes artifacts uniformly.
    """
    run_start = time.monotonic()

    def _result(**kwargs: object) -> ProducerResult:
        kwargs.setdefault("provider", producer_config.provider)
        kwargs.setdefault("model", producer_config.model)
        return ProducerResult(**kwargs)

    # --- Adapter lookup ----------------------------------------------
    if adapter is None:
        try:
            adapter = get_producer_adapter(producer_config.provider)
        except ValueError as exc:
            # Bad provider string — same surface as the reviewer
            # registry: ValueError naming the bad input. Map to
            # subprocess_error so the persistence layer writes the
            # error.txt with the actionable message.
            return _result(
                outcome="subprocess_error",
                starting_sha=starting_sha,
                ending_sha=starting_sha,
                duration_seconds=time.monotonic() - run_start,
                output=None,
                error=exc,
                raw_subprocess_result=None,
            )

    # --- Verify the worktree HEAD matches starting_sha ---------------
    # Catches the orchestrator misuse case where a wrong worktree
    # was passed. Surfaces as subprocess_error so the operator gets
    # a clean .error.txt rather than a confused diff post-mortem.
    try:
        observed_starting = _read_worktree_head(worktree_path)
    except SubprocessError as exc:
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=exc,
            raw_subprocess_result=None,
        )
    if observed_starting != starting_sha:
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=SubprocessError(
                f"producer: worktree {worktree_path} HEAD is "
                f"{observed_starting!r} but expected "
                f"{starting_sha!r} (starting_sha mismatch). "
                "The orchestrator must hand a worktree checked out "
                "at the round-start snapshot's commit_sha."
            ),
            raw_subprocess_result=None,
        )

    # --- Render the prompt -------------------------------------------
    # Confinement (H4): stage every input INSIDE the worktree and render
    # WORKTREE-RELATIVE refs. A yolo producer handed an absolute main-repo
    # or run-artifact path can be lured out of its isolated worktree to
    # touch the live repo (the reviewer analog is closed structurally in
    # round.py). ``worktree_path`` itself stays absolute — it is the
    # producer's own sandbox, not a main-repo path, and the template's
    # boundary checks compare it against ``git rev-parse --show-toplevel``.
    try:
        template = load_producer_template(repo_root)
        pr_doc_ref = _stage_producer_input(
            pr_doc_path, worktree_path=worktree_path, repo_root=repo_root
        )
        findings_ref = _stage_producer_input(
            findings_md_path, worktree_path=worktree_path, repo_root=repo_root
        )
        # the renderer accepts None directly and substitutes the
        # "(no test failure this round)" sentinel itself.
        test_ref = (
            _stage_producer_input(
                test_run_stdout_path, worktree_path=worktree_path, repo_root=repo_root
            )
            if test_run_stdout_path is not None
            else None
        )
        prompt = render_producer_prompt(
            template,
            pr_doc_path=pr_doc_ref,
            findings_md_path=findings_ref,
            test_run_stdout_path=test_ref,
            worktree_path=str(worktree_path),
            round_number=round_number,
            max_rounds=max_rounds,
            prior_round_output=prior_round_output,
            prior_round_commits=prior_round_commits,
            operator_decision=operator_decision,
        )
    except (KeyError, ValueError, OSError) as exc:
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=SubprocessError(f"producer prompt setup failed: {exc}"),
            raw_subprocess_result=None,
        )

    # --- Build the invocation ----------------------------------------
    try:
        invocation = adapter.build_invocation(producer_config, worktree_path, prompt)
    except ValueError as exc:
        # Same narrow catch as the synthesizer: only ValueError
        # legitimately comes out of build_invocation (provider /
        # permissions validation). Programming bugs (TypeError,
        # AttributeError) bubble through so they get a stack trace
        # instead of being bucketed as a polite subprocess_error.
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=exc,
            raw_subprocess_result=None,
        )

    # --- Run the subprocess ------------------------------------------
    subprocess_result: SubprocessResult | None = None
    try:
        subprocess_result = run_subprocess(
            invocation.argv,
            cwd=invocation.cwd,
            env=invocation.env,
            timeout=timeout_seconds,
            input_text=invocation.stdin_text,
        )
    except SubprocessTimeoutError as exc:
        elapsed = time.monotonic() - run_start
        ending_sha = _best_effort_head_after_failure(worktree_path, starting_sha)
        partial = SubprocessResult(
            returncode=-1,
            stdout=exc.stdout,
            stderr=exc.stderr,
            duration_seconds=elapsed,
        )
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=ending_sha,
            duration_seconds=elapsed,
            output=None,
            error=exc,
            raw_subprocess_result=partial,
            usage=usage_for(
                partial,
                producer_config.provider,
                producer_config.model,
                pricing,
                _auth_mode(producer_config),
            ),
        )
    except SubprocessNotFoundError as exc:
        # Binary missing — subprocess never started.
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=exc,
            raw_subprocess_result=None,
        )
    except SubprocessError as exc:
        # Other launch failures (bad cwd, OS error).
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=exc,
            raw_subprocess_result=None,
        )

    # The subprocess completed — extract usage BEFORE parse/HEAD-read so those
    # failure paths still record it too (dogfood finding #5).
    producer_usage = usage_for(
        subprocess_result,
        producer_config.provider,
        producer_config.model,
        pricing,
        _auth_mode(producer_config),
    )

    # --- Parse the output --------------------------------------------
    try:
        producer_output = adapter.parse_output(subprocess_result)
    except (ReviewerInvocationError, ReviewerOutputError) as exc:
        ending_sha = _best_effort_head_after_failure(worktree_path, starting_sha)
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=ending_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=exc,
            raw_subprocess_result=subprocess_result,
            usage=producer_usage,
        )

    # --- Stall detection: did HEAD move? -----------------------------
    try:
        ending_sha = _read_worktree_head(worktree_path)
    except SubprocessError as exc:
        # Reading HEAD after a successful subprocess is unusual to
        # fail. Treat as subprocess_error — the orchestrator can't
        # advance the branch if it can't read the SHA.
        return _result(
            outcome="subprocess_error",
            starting_sha=starting_sha,
            ending_sha=starting_sha,
            duration_seconds=time.monotonic() - run_start,
            output=None,
            error=exc,
            raw_subprocess_result=subprocess_result,
            usage=producer_usage,
        )

    elapsed = time.monotonic() - run_start
    if ending_sha == starting_sha:
        # No commit. The producer subprocess exited cleanly but didn't
        # move HEAD. if the producer emitted a well-formed
        # escalation block, this is a deliberate "operator decision
        # needed" signal (escalated), not a silent stall. A malformed /
        # absent block parses to None → ordinary stall (next round would
        # see identical input).
        escalation = parse_producer_escalation(producer_output.narrative_text)
        if escalation is not None:
            return _result(
                outcome="escalated",
                starting_sha=starting_sha,
                ending_sha=ending_sha,
                duration_seconds=elapsed,
                output=producer_output,
                error=None,
                raw_subprocess_result=subprocess_result,
                usage=producer_usage,
                escalation=escalation,
            )
        return _result(
            outcome="stalled",
            starting_sha=starting_sha,
            ending_sha=ending_sha,
            duration_seconds=elapsed,
            output=producer_output,
            error=None,
            raw_subprocess_result=subprocess_result,
            usage=producer_usage,
        )

    # HEAD moved → the producer committed.
    return _result(
        outcome="committed",
        starting_sha=starting_sha,
        ending_sha=ending_sha,
        duration_seconds=elapsed,
        output=producer_output,
        error=None,
        raw_subprocess_result=subprocess_result,
        usage=producer_usage,
    )
