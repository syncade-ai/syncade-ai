"""[loop] convergence-loop config.

:class:`LoopConfig` for the ``[loop]`` block. Imports only stdlib + pydantic
(no dependency on ``config`` itself), so it extracts without a circular import.
Re-exported from ``config`` so the ``syncade.config.LoopConfig`` import path is
unchanged.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LoopConfig(BaseModel):
    """Convergence-loop tuning."""

    model_config = ConfigDict(extra="forbid")

    max_rounds: int = Field(
        default=3,
        ge=1,
        le=3,
        description=(
            "Per-run maximum rounds of (reviewers → synthesizer → "
            "optional test → producer-if-NO-SHIP). The loop terminates "
            "as soon as SHIP fires (exit 0) at any round — "
            "``max_rounds`` is the ceiling, not a target. PRD Appendix "
            "C caps this at 3; values outside [1, 3] are rejected at "
            "config load. Set to 1 for single-pass operation with no "
            "producer subprocess. "
            "Runaway protection: rounds is the outer cap; "
            "budget_tokens/budget_usd add optional token/cost ceilings (PR-v2-11)."
        ),
    )
    timeout_seconds: float = Field(
        default=1800,
        gt=0,
        description="Per-reviewer wall-clock timeout in seconds. The dispatcher "
        "SIGKILLs any reviewer subprocess that exceeds this. Default 1800 "
        "(30 minutes) — sized for a thorough real review. Must be > 0 and "
        "finite (NaN and infinity rejected via a field_validator; pydantic's "
        "gt=0 admits both unaided). The CLI's --timeout flag overrides this "
        "per-invocation.",
    )
    budget_tokens: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional per-RUN total-token ceiling (PR-v2-11). When the running tally of every "
            "actor's usage crosses it at a phase boundary, the loop aborts gracefully with "
            "termination_reason='budget_exceeded'. total_tokens is recorded for every actor "
            "whose provider returned usage (the norm — captured even when the reviewer's OUTPUT "
            "fails to parse), so this is the TIGHTEST cap available: exact when all actors "
            "report usage, and a lower bound only if an actor reports none (a provider envelope "
            "with no usage block). Always at least as tight as budget_usd, which ALSO drops "
            "actors whose COST could not be priced. None (default) = no token ceiling; "
            "max_rounds + timeout_seconds stay the only bounds. The CLI's --budget-tokens "
            "flag overrides this per-invocation. An int, so NaN/inf cannot arise and gt=0 "
            "already rejects 0 / negatives."
        ),
    )
    budget_usd: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional per-RUN cost ceiling (PR-v2-11) on the API-EQUIVALENT VALUATION "
            "(PR-v2-24), NOT billed money — on a subscription the marginal dollar is $0, so "
            "this bounds the WORK, matching what `syncade --doctor` previews and `--metrics` "
            "reports. A LOWER-BOUND tally: actors with incomplete cost contribute uncounted, "
            "so a dollar-budgeted run can overshoot silently (use budget_tokens for a hard "
            "cap). None (default) = no cost ceiling. Must be > 0 and finite (NaN/inf rejected "
            "via a field_validator; pydantic's gt=0 admits both). The CLI's --budget-usd flag "
            "overrides this per-invocation."
        ),
    )
    test_command: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Shell command to run as the third convergence leg, "
            "after all reviewers succeed and the synthesizer produces a "
            "clean consolidated finding set. Runs in a fresh worktree "
            "(same blindness mechanism as reviewers: WorktreeManager + "
            "CLAUDE.md/AGENTS.md stripped). Non-zero exit → exit 30 "
            "(treated as a blocker); subprocess failure (binary missing, "
            "timeout) → exit 40. Unset (default) skips the test leg "
            "entirely — exit 0 reflects synth-clean only. The string "
            "is passed verbatim to ``sh -c`` so operators can use pipes, "
            "env exports, and multi-command sequences "
            '(``"npm test && playwright test"``). shell=True is '
            "intentional: the command comes from the operator's own "
            "config file, not from untrusted input — same threat model "
            'as a Makefile or package.json "scripts" entry. '
            "Whitespace-only strings are rejected at config-load via "
            "``Field(min_length=1)`` plus a ``field_validator`` so a "
            'misconfigured ``test_command = "   "`` surfaces as a '
            "ValidationError rather than silently SIGKILLing every run."
        ),
    )
    test_timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Per-test-run wall-clock timeout in seconds. "
            "``None`` (default) reuses :attr:`timeout_seconds`, which is "
            "also the reviewer timeout. Set explicitly when the test "
            "suite has a different expected runtime profile than the "
            "reviewers (e.g. fast unit suite at 300s vs reviewers at "
            "1800s). Must be > 0 when set; NaN and infinity are "
            "rejected via a ``field_validator`` so the same "
            "``math.isfinite`` guard pattern from the reviewer "
            "``timeout_seconds`` applies here. The "
            "``test_timeout_seconds = None`` → reuse-``timeout_seconds`` "
            "resolution happens in the orchestrator, not in pydantic — "
            "keeping the field nullable means 'use the default' is "
            "distinguishable from 'explicitly set to the same value as "
            "timeout_seconds.'"
        ),
    )

    @field_validator("test_command")
    @classmethod
    def _test_command_not_whitespace(cls, value: str | None) -> str | None:
        """Reject ``"   "`` / ``"\\n\\t"`` etc. — schema-only ``min_length``
        passes them through (length is non-zero) but they'd silently
        SIGKILL every run when passed to ``sh -c``.

        Mirrors the required-string validation pattern used elsewhere. Keeping
        ``None`` as the disabled sentinel — only NON-None values are checked for
        whitespace
        emptiness.
        """
        if value is not None and not value.strip():
            raise ValueError(
                "test_command must not be empty or whitespace-only "
                "(use ``test_command = `` <unset> to disable the test "
                "leg; the empty / whitespace string is rejected so a "
                "config typo can't silently disable the leg)"
            )
        return value

    @field_validator("budget_usd")
    @classmethod
    def _budget_usd_isfinite(cls, value: float | None) -> float | None:
        """Same ``math.isfinite`` guard as the timeouts: pydantic's ``gt=0`` admits NaN/inf,
        and an infinite budget would silently mean 'no ceiling' (it never trips) rather than
        erroring — hiding a config typo. ``budget_tokens`` needs no twin: it is an int."""
        if value is not None and not math.isfinite(value):
            raise ValueError(f"budget_usd must be a finite positive number; got {value!r}")
        return value

    @field_validator("test_timeout_seconds")
    @classmethod
    def _test_timeout_seconds_isfinite(cls, value: float | None) -> float | None:
        """Reject ``float('nan')`` and ``float('inf')`` — pydantic's
        ``gt=0`` admits both (inf > 0 is True; nan comparisons are
        defined to return False so the gt check actually passes
        with a warning shape in some pydantic versions). The same
        ``math.isfinite`` guard also applies to the reviewer
        timeout applies here so a runaway operator config can't
        produce an unkillable test-run subprocess."""
        if value is not None and not math.isfinite(value):
            raise ValueError(
                f"test_timeout_seconds must be a finite positive number; got {value!r}"
            )
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_seconds_isfinite(cls, value: float) -> float:
        """Apply the same ``math.isfinite`` guard for reviewer timeouts.

        pydantic's ``gt=0`` admits NaN/inf unaided, which would produce an
        unkillable reviewer subprocess: the reviewer phase would block forever
        waiting on the dispatcher's ``communicate(timeout=...)`` since
        ``timeout=inf`` reads as "wait forever" and ``timeout=nan`` produces
        undefined comparator behavior. Same field-validator pattern as
        :meth:`_test_timeout_seconds_isfinite`.
        """
        if not math.isfinite(value):
            raise ValueError(f"timeout_seconds must be a finite positive number; got {value!r}")
        return value
