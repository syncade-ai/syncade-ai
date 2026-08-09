"""Parallel reviewer dispatcher.

The dispatcher takes N :class:`ReviewerConfig`s, routes each to its
provider's adapter via :func:`syncade.adapters.registry.get_adapter`,
runs the configured pre-flight auth checks in parallel, then dispatches
the reviewer subprocesses in parallel, and aggregates the results into
a :class:`DispatchResult`.

Threading model: :class:`concurrent.futures.ThreadPoolExecutor`. Each
reviewer is mostly I/O-bound (waiting on its CLI subprocess), so
threads are sufficient and simpler than ``asyncio``. We expect 2-3
reviewers per dispatch in practice — not 100 — so max_workers defaults
to ``len(reviewer_configs)``.

Failure surfaces — three categories, deliberately distinct in
``DispatchResult.failures``:

1. **Adapter lookup failure** (unknown provider): the WHOLE batch
   fails immediately. Every reviewer's :class:`ReviewerRunResult`
   carries the same registry error. We don't start any parallel work.
2. **Pre-flight auth failure**: the WHOLE batch fails. Every reviewer
   carries the first auth error from the parallel pre-flight pass.
   No reviewer subprocess actually runs.
3. **Per-reviewer failure during dispatch**: only that reviewer's
   :class:`ReviewerRunResult` carries the error. Others run normally.
   This is the only path where partial success / partial failure
   coexist in the same :class:`DispatchResult`. The orchestrator decides
   whether to abort the round or surface the failure.

The dispatcher itself does NOT degrade silently to N-1 reviewers — the
panel-diversity invariant (the point of running more than one reviewer;
cross-prompt today, cross-lab by design) requires the orchestrator to
make that call explicitly. The dispatcher just records what happened.

The only inputs that make :func:`dispatch_reviewers` raise rather than
return a :class:`DispatchResult` are ``None`` for ``reviewer_configs``
or ``worktree_paths`` — those are caller bugs, not runtime conditions
worth recording per-reviewer. See the function docstring's Raises
section.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from syncade import retry
from syncade.adapters.base import ReviewerAdapter
from syncade.adapters.registry import get_adapter
from syncade.config import ReviewerConfig
from syncade.findings import ReviewerOutput
from syncade.pricing_config import PricingConfig
from syncade.process import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
    terminate_active_child_groups,
)
from syncade.usage import Usage, _add_usage, _auth_mode, usage_for


@dataclass(frozen=True)
class ReviewerRunResult:
    """Outcome of a single reviewer's run within a dispatch batch.

    Exactly one of ``output`` or ``error`` is non-``None``. The
    dispatcher never silently drops a reviewer — every config goes
    in, exactly one result comes out, success or failure recorded.

    Attributes:
        reviewer_name: From :attr:`ReviewerConfig.name`. The orchestrator
            uses this to route findings back to the right worktree.
        provider: From :attr:`ReviewerConfig.provider`. The downstream
            synthesis subprocess may want to know which model family
            each finding came from.
        output: The parsed :class:`ReviewerOutput` on success;
            ``None`` on failure.
        error: The exception that fired on failure;
            :class:`~syncade.adapters.base.ReviewerInvocationError` or
            :class:`~syncade.findings.ReviewerOutputError` or a
            :class:`~syncade.process.SubprocessError` subclass. ``None``
            on success.
        duration_seconds: Wall-clock duration of this reviewer's run.
            For auth-fail or adapter-lookup failures, this is ``0.0``
            (the reviewer never actually ran).
        raw_subprocess_result: The :class:`SubprocessResult` returned
            by :func:`syncade.process.run_subprocess` for this reviewer,
            preserved so the orchestrator can persist the raw
            stdout/stderr to disk under ``.syncade/runs/<run-id>/``.
            ``None`` for any failure that happened before the
            subprocess produced output — adapter lookup, auth
            fail-fast, ``build_invocation`` raising, missing worktree
            path, or the subprocess failing to launch at all (binary
            not found). On a **timeout** this is NOT ``None``:
            the dispatcher synthesizes a :class:`SubprocessResult`
            (sentinel ``returncode=-1``) carrying the partial
            stdout/stderr the reviewer produced before the SIGKILL, so
            persistence still writes the ``.stdout`` / ``.stderr``
            files. Defaults to ``None`` for existing callers/tests that
            construct ``ReviewerRunResult`` directly.
        retries: Number of EXTRA subprocess attempts this reviewer
            consumed riding out transient provider errors (H5) — ``0``
            when the first attempt resolved (success or terminal
            failure). Persistence aggregates this into the round
            manifest's ``"retried"`` annotation. Defaults to ``0`` for
            callers that construct ``ReviewerRunResult`` directly.
    """

    reviewer_name: str
    provider: str
    output: ReviewerOutput | None
    error: Exception | None
    duration_seconds: float
    raw_subprocess_result: SubprocessResult | None = field(default=None)
    retries: int = field(default=0)
    usage: Usage | None = field(default=None)
    model: str = field(default="")


@dataclass(frozen=True)
class DispatchResult:
    """Aggregate of all reviewers' run results for a single dispatch."""

    results: list[ReviewerRunResult]
    total_duration_seconds: float
    # True iff Phase 3 (subprocess dispatch) was actually entered.
    # Adapter-lookup and auth-preflight failures return non-empty ``results`` without
    # starting any reviewer subprocess; this flag distinguishes them from real dispatch.
    reviewer_subprocess_started: bool = False

    @property
    def all_succeeded(self) -> bool:
        """True iff every reviewer produced a :class:`ReviewerOutput`."""
        return bool(self.results) and all(r.output is not None for r in self.results)

    @property
    def successes(self) -> list[ReviewerRunResult]:
        """Reviewers that produced parsed output, in input order."""
        return [r for r in self.results if r.output is not None]

    @property
    def failures(self) -> list[ReviewerRunResult]:
        """Reviewers that recorded an error, in input order."""
        return [r for r in self.results if r.error is not None]


def dispatch_reviewers(
    reviewer_configs: list[ReviewerConfig],
    *,
    worktree_paths: dict[str, Path],
    prompt: str | dict[str, str],
    timeout_seconds: float = 1800,
    skip_auth_check: bool = False,
    adapter_factory: Callable[[str], ReviewerAdapter] | None = None,
    pricing: PricingConfig | None = None,
    max_retries: int = retry.MAX_RETRIES,
) -> DispatchResult:
    """Dispatch each ``ReviewerConfig`` to its provider's adapter in parallel.

    Order of operations:

    1. Look up each adapter via :func:`adapter_factory` (default:
       :func:`syncade.adapters.registry.get_adapter`). Unknown provider
       → fail the whole dispatch immediately (don't start parallel
       runs only to find out one is misrouted). Every reviewer's
       :class:`ReviewerRunResult` carries the same registry error.
    2. Call each adapter's :meth:`ReviewerAdapter.check_auth` in
       parallel. Any auth failure fails the whole dispatch
       immediately — we don't want one reviewer to burn tokens while
       another is dead in the water. Skipped when ``skip_auth_check``
       is ``True`` (for tests using :class:`FakeAdapter`, where
       ``check_auth`` is a no-op anyway).
    3. For each (config, adapter, worktree_path), build the
       :class:`Invocation`, run the subprocess via
       :func:`syncade.process.run_subprocess`, parse the output.
       Capture either :class:`ReviewerOutput` or whichever exception
       was raised.
    4. Return :class:`DispatchResult` with one
       :class:`ReviewerRunResult` per input config, **in input order**
       (so a caller can ``zip(reviewer_configs, result.results)``).

    Args:
        reviewer_configs: One or more reviewer configurations. Each
            ``provider`` must be a key the adapter factory recognizes.
        worktree_paths: Dict keyed by ``ReviewerConfig.name`` — the
            worktree each reviewer should run in. Two reviewers from
            the same provider would otherwise collide on the same
            worktree; keying by name avoids that. The orchestrator populates this dict via
            :class:`~syncade.worktree.WorktreeManager`. If a
            ``reviewer_name`` is missing from this dict, only that
            reviewer fails (with a clear ``ValueError``); others run.
        prompt: The fully-rendered reviewer prompt. Two shapes:

            - ``str``: every reviewer gets the same prompt. Still
              accepted by the dispatcher, but the orchestrator no longer
              uses this shape — it always passes the dict below.
            - ``dict[str, str]``: per-reviewer prompts keyed by
              :attr:`ReviewerConfig.name`. The orchestrator always uses
              this shape: each reviewer renders its OWN provider-specific
              template (claude vs codex get differentiated adversarial
              prompts), and on multi-round runs each reviewer's
              ``{prior_round_output}`` placeholder also gets that
              reviewer's OWN prior-round response text (per-reviewer
              isolation across rounds — round-1 claude sees claude's
              round-0 output, NOT codex's). The dict MUST contain an entry for every
              :attr:`ReviewerConfig.name`; a missing key surfaces as
              ``KeyError`` during dispatch (a caller bug, not a
              runtime condition).
        timeout_seconds: The FALLBACK reviewer wall-clock timeout — used
            for any reviewer whose own :attr:`ReviewerConfig.timeout_seconds`
            is unset (the common case). A reviewer that sets its own value
            overrides this per-reviewer (PR-v2-9). The subprocess for any
            reviewer that exceeds its resolved timeout is SIGKILL'd and the
            result records :class:`~syncade.process.SubprocessTimeoutError`.
            Default 1800s (30 minutes), matching
            :attr:`syncade.config.LoopConfig.timeout_seconds`. This
            default is the floor for callers that invoke the
            dispatcher directly, bypassing the orchestrator (none
            today — it exists for future code); the orchestrator
            always passes an explicit resolved value (CLI ``--timeout``
            > ``.syncade/config.toml`` > the ``LoopConfig`` default).
        skip_auth_check: When ``True``, the pre-flight auth phase is
            skipped. Intended for tests; production callers leave this
            False.
        adapter_factory: A callable that takes a provider string and
            returns a :class:`ReviewerAdapter`. Defaults to the
            production registry. Tests inject a factory that returns
            :class:`FakeAdapter` instances so the dispatcher's
            integration with the registry can be exercised without
            real CLIs.

    Returns:
        :class:`DispatchResult` with one :class:`ReviewerRunResult`
        per input config, in input order. ``total_duration_seconds``
        is the wall-clock time for the whole dispatch — for parallel
        runs this is roughly the slowest reviewer's duration plus a
        small auth-check overhead, not the sum of durations.

    Raises:
        TypeError: If ``reviewer_configs`` or ``worktree_paths`` is
            ``None``. These are caller bugs (the public API requires
            real containers), not runtime conditions worth turning
            into ``DispatchResult.failures`` — the orchestrator can't
            sensibly recover from "you passed None instead of a list."
            Every other failure mode (adapter lookup, auth, build,
            subprocess, parse) is captured in the returned
            :class:`DispatchResult` so the orchestrator can decide
            whether to abort, retry, or proceed with partial results.
    """
    if reviewer_configs is None:
        raise TypeError(
            "dispatch_reviewers: reviewer_configs must be a list (got None). "
            "Pass [] if you legitimately have no reviewers to dispatch."
        )
    if worktree_paths is None:
        raise TypeError(
            "dispatch_reviewers: worktree_paths must be a dict (got None). "
            "Pass {} if you have no worktrees to map — every reviewer will "
            "fail individually with a missing-worktree ValueError."
        )
    if isinstance(prompt, dict):
        missing_prompt_names = [cfg.name for cfg in reviewer_configs if cfg.name not in prompt]
        if missing_prompt_names:
            missing = ", ".join(repr(name) for name in missing_prompt_names)
            raise KeyError(f"dispatch_reviewers: prompt mapping missing reviewer key(s): {missing}")
    if adapter_factory is None:
        adapter_factory = get_adapter

    start = time.monotonic()

    # --- Phase 1: adapter lookup -----------------------------------
    # Build pairs of (config, adapter). If any lookup raises (unknown
    # provider), every reviewer in the batch gets that same error and
    # we skip auth + dispatch entirely.
    pairs: list[tuple[ReviewerConfig, ReviewerAdapter]] = []
    lookup_error: Exception | None = None
    for config in reviewer_configs:
        try:
            adapter = adapter_factory(config.provider)
        except Exception as exc:  # noqa: BLE001 — surface ANY factory error
            lookup_error = exc
            break
        pairs.append((config, adapter))

    if lookup_error is not None:
        total = time.monotonic() - start
        return DispatchResult(
            results=[
                ReviewerRunResult(
                    reviewer_name=cfg.name,
                    provider=cfg.provider,
                    output=None,
                    error=lookup_error,
                    duration_seconds=0.0,
                    model=cfg.model,
                )
                for cfg in reviewer_configs
            ],
            total_duration_seconds=total,
        )

    # Defensive: empty configs list yields an empty result.
    # ThreadPoolExecutor(max_workers=0) is illegal; guard before
    # constructing one.
    if not pairs:
        return DispatchResult(
            results=[],
            total_duration_seconds=time.monotonic() - start,
        )

    # --- Phase 2: parallel auth pre-flight -------------------------
    if not skip_auth_check:
        first_auth_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
            futures = {executor.submit(adapter.check_auth): config for config, adapter in pairs}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    if first_auth_error is None:
                        first_auth_error = exc
                    # Continue draining the others to avoid leaked
                    # futures, but the whole batch fails below.

        if first_auth_error is not None:
            total = time.monotonic() - start
            return DispatchResult(
                results=[
                    ReviewerRunResult(
                        reviewer_name=config.name,
                        provider=config.provider,
                        output=None,
                        error=first_auth_error,
                        duration_seconds=0.0,
                        model=config.model,
                    )
                    for config, _ in pairs
                ],
                total_duration_seconds=total,
            )

    # --- Phase 3: parallel reviewer dispatch -----------------------
    # Resolve the prompt argument to a per-reviewer mapping. When a
    # ``str`` is passed, every reviewer gets the same prompt. When a
    # ``dict`` is passed, each reviewer gets ``prompt[name]``; KeyError
    # on missing names surfaces as a caller bug rather than a silent
    # fallback. Round > 0 dispatch uses the dict path so each reviewer sees
    # its own prior-round response text.
    if isinstance(prompt, dict):
        per_reviewer_prompts: dict[str, str] = prompt
    else:
        per_reviewer_prompts = {cfg.name: prompt for cfg, _ in pairs}

    with ThreadPoolExecutor(max_workers=len(pairs)) as executor:
        # Submit in input order, collect in input order (futures list
        # preserves submission order; .result() blocks per-future).
        futures = [
            executor.submit(
                _run_single_reviewer,
                config,
                adapter,
                worktree_paths,
                per_reviewer_prompts[config.name],
                # Per-reviewer timeout (PR-v2-9): a reviewer's own ``timeout_seconds`` wins; None
                # (the default) falls back to the resolved loop/CLI global passed in here.
                config.timeout_seconds if config.timeout_seconds is not None else timeout_seconds,
                pricing,
                max_retries,
            )
            for config, adapter in pairs
        ]
        try:
            results = [future.result() for future in futures]
        except BaseException:
            # A signal (or any interrupt) reached the main thread while workers are
            # blocked in communicate(); their own killpg cleanup can't fire. Kill the
            # in-flight reviewer groups here so communicate() returns and the executor's
            # shutdown(wait=True) on __exit__ is prompt instead of hanging to timeout.
            terminate_active_child_groups()
            raise

    return DispatchResult(
        results=results,
        total_duration_seconds=time.monotonic() - start,
        reviewer_subprocess_started=True,
    )


def _run_single_reviewer(
    config: ReviewerConfig,
    adapter: ReviewerAdapter,
    worktree_paths: dict[str, Path],
    prompt: str,
    timeout_seconds: float,
    pricing: PricingConfig | None = None,
    max_retries: int = retry.MAX_RETRIES,
) -> ReviewerRunResult:
    """Run one reviewer end-to-end: build_invocation, run_subprocess,
    parse_output. Capture either the parsed output or the exception
    that fired.

    Worktree lookup is per-reviewer-name — if the orchestrator forgot
    to provision a worktree for this reviewer, that's a clear
    ``ValueError`` recorded in this reviewer's run result. Other
    reviewers proceed normally.

    Timeout is special-cased: :func:`run_subprocess` raises
    :class:`~syncade.process.SubprocessTimeoutError` *before* returning
    a :class:`SubprocessResult`, but the exception carries the partial
    stdout/stderr the reviewer produced before the SIGKILL. The
    dedicated ``except`` clause synthesizes a :class:`SubprocessResult`
    from it so persistence still sees the partial output.
    """
    run_start = time.monotonic()
    worktree = worktree_paths.get(config.name)
    if worktree is None:
        return ReviewerRunResult(
            reviewer_name=config.name,
            provider=config.provider,
            output=None,
            error=ValueError(
                f"dispatcher: no worktree_paths entry for reviewer "
                f"{config.name!r} (provider={config.provider!r}). "
                f"The caller must populate worktree_paths with one "
                f"entry per ReviewerConfig.name before dispatching."
            ),
            duration_seconds=time.monotonic() - run_start,
            model=config.model,
        )
    # Track the subprocess result separately so we can attach it to
    # the ReviewerRunResult on both success AND parse-failure paths.
    # On launch-failure (binary missing, OS error) it stays None and
    # the result records only the exception. A timeout is special-cased
    # below: run_subprocess raises before assigning subprocess_result,
    # but SubprocessTimeoutError carries the partial output, which the
    # dedicated except clause synthesizes into a SubprocessResult.
    subprocess_result: SubprocessResult | None = None
    retries = 0
    accumulated_usage: Usage | None = None
    for attempt in range(1, max_retries + 2):
        try:
            invocation = adapter.build_invocation(config, worktree, prompt)
            subprocess_result = run_subprocess(
                invocation.argv,
                cwd=invocation.cwd,
                env=invocation.env,
                timeout=timeout_seconds,
                input_text=invocation.stdin_text,
            )
            attempt_usage = usage_for(
                subprocess_result, config.provider, config.model, pricing, _auth_mode(config)
            )
            output = adapter.parse_output(subprocess_result)
            return ReviewerRunResult(
                reviewer_name=config.name,
                provider=config.provider,
                output=output,
                error=None,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
                retries=retries,
                usage=_add_usage(accumulated_usage, attempt_usage),
                model=config.model,
            )
        except SubprocessTimeoutError as exc:
            # run_subprocess raised before assigning subprocess_result, so
            # the partial output the reviewer produced before the SIGKILL
            # lives ONLY on the exception's .stdout / .stderr. Synthesize a
            # SubprocessResult from it — sentinel returncode -1, since the
            # process was killed and never exited cleanly — so persistence
            # still writes the partial .stdout / .stderr files. The error
            # field keeps the exception, so the orchestrator's decision
            # table still sees SubprocessTimeoutError -> REVIEWER_FAILURE.
            # (SubprocessNotFoundError, the other SubprocessError subclass,
            # carries no partial output — the process never started — so it
            # falls through to the generic handler with raw_result None.)
            elapsed = time.monotonic() - run_start
            partial = SubprocessResult(
                returncode=-1,
                stdout=exc.stdout,
                stderr=exc.stderr,
                duration_seconds=elapsed,
            )
            attempt_usage = usage_for(
                partial, config.provider, config.model, pricing, _auth_mode(config)
            )
            return ReviewerRunResult(
                reviewer_name=config.name,
                provider=config.provider,
                output=None,
                error=exc,
                duration_seconds=elapsed,
                raw_subprocess_result=partial,
                retries=retries,
                usage=_add_usage(accumulated_usage, attempt_usage),
                model=config.model,
            )
        except Exception as exc:  # noqa: BLE001 — capture every failure shape
            # Bounded retry (H5): a transient provider blip — a 429/5xx or a
            # dropped socket wrapped in ReviewerInvocationError — gets up to
            # max_retries extra fresh attempts with jittered backoff,
            # rather than aborting the whole round at exit 40. Parse/contract
            # failures, missing binaries, and timeouts are terminal and never
            # retried (see syncade.retry.is_transient_api_error).
            attempt_usage = usage_for(
                subprocess_result, config.provider, config.model, pricing, _auth_mode(config)
            )
            if retry.is_transient_api_error(exc) and attempt <= max_retries:
                accumulated_usage = _add_usage(accumulated_usage, attempt_usage)
                retries += 1
                retry.backoff_sleep(attempt)
                subprocess_result = None
                continue
            return ReviewerRunResult(
                reviewer_name=config.name,
                provider=config.provider,
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
                retries=retries,
                usage=_add_usage(accumulated_usage, attempt_usage),
                model=config.model,
            )

    raise AssertionError("unreachable reviewer retry loop exit")
