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
        default=5,
        ge=1,
        le=10,
        description=(
            "Per-run maximum rounds of (reviewers → synthesizer → "
            "optional test → producer-if-NO-SHIP). The loop terminates "
            "as soon as SHIP fires (exit 0, termination_reason='ship') "
            "at any round, or the diff is known-empty before dispatch "
            "(exit 0, termination_reason='no_changes_to_review') — "
            "``max_rounds`` is the ceiling, not a target. Bounded to "
            "[1, 10] (PR-v2-31, raised from 3); values outside that range "
            "are rejected at config load. 10 is a typo-ceiling, not the "
            "safety mechanism — budget_tokens/budget_usd (PR-v2-11) and the "
            "per-subprocess timeout are the real runaway guards. Set to 1 for "
            "single-pass operation with no producer subprocess."
        ),
    )
    timeout_seconds: float = Field(
        # 1800.0, not 1800: the annotation says float, and an int default makes pydantic 2.0
        # emit a serializer warning on every `config.model_dump()` ("Expected `float` but got
        # `int`"). Harmless in isolation, fatal in context — the loop's own test leg runs
        # `pytest -W error`, so at the declared floor that one int turned 279 tests red while
        # the dev machine (pydantic 2.13) saw nothing (PR-h-10 item 4).
        default=1800.0,
        gt=0,
        description="Per-subprocess wall-clock timeout in seconds. The fallback "
        "wall-clock cap for every leg: each reviewer, the judge, the test run, "
        "each mechanical check, and the producer. Default 1800 (30 minutes) — "
        "sized for a thorough real review. Must be > 0 and finite (NaN and "
        "infinity rejected via a field_validator; pydantic's gt=0 admits both "
        "unaided). The CLI's --timeout flag overrides this per-invocation.",
    )
    max_diff_bytes: int = Field(
        default=1_000_000,
        gt=0,
        description=(
            "Ceiling on the REVIEWER-FACING diff, in UTF-8 bytes — measured after "
            "repo-context stripping and binary elision, i.e. exactly what reaches the "
            "model's context, not what the repo produced. A run whose diff exceeds it is "
            "refused before any subprocess is dispatched (exit 60, "
            "termination_reason='diff_too_large'), consistent with diff_malformed: if we "
            "cannot show reviewers the change, we decline to render a verdict on it. "
            "Narrow --base or split the PR.\n\n"
            "The default is calibrated, not guessed, in two directions. Upper bound: "
            "`codex exec` hard-refuses input over 1,048,576 CHARACTERS "
            "(input_error_code=input_too_large) — a UTF-8 byte cap of 1,000,000 keeps the "
            "diff under that in every encoding, since bytes >= characters, so syncade's own "
            "actionable message always fires before the provider's opaque one. Lower bound: "
            "across the 30 measured rounds in this repo's run corpus the largest "
            "reviewer-facing diff was 147,171 B (median 77,026), so the default has ~7x "
            "headroom over observed reality and refuses nothing that works today. Lower it "
            "deliberately if you want a tighter review surface — this wave's evidence is "
            "that large diffs converge poorly (a 25-commit diff gave 4/4/4/3/5 findings and "
            "never converged; a 3-commit one gave 1/1/0 to SHIP on the same panel).\n\n"
            "NOT a token cap: `claude -p` limits by TOKENS (1,000,000), which bytes cannot "
            "predict, so a token-limit refusal is still possible on that provider."
        ),
    )
    budget_tokens: int | None = Field(
        default=50_000_000,
        ge=0,
        description=(
            "Optional per-RUN total-token ceiling (PR-v2-11). When the running tally of every "
            "actor's usage crosses it at a phase boundary, the loop aborts gracefully with "
            "termination_reason='budget_exceeded'. total_tokens is recorded for every actor "
            "whose provider returned usage (the norm — captured even when the reviewer's OUTPUT "
            "fails to parse), so this is the TIGHTEST cap available: exact when all actors "
            "report usage, and a lower bound only if an actor reports none (a provider envelope "
            "with no usage block). Always at least as tight as budget_usd, which ALSO drops "
            "actors whose COST could not be priced. 0 = no token ceiling (opt-out sentinel; "
            "TOML has no null, so 0 is the only way to express 'unlimited' once a default "
            "exists). The CLI's --budget-tokens flag overrides this per-invocation. An int, "
            "so NaN/inf cannot arise and ge=0 already rejects negatives.\n\n"
            "DEFAULT 50,000,000 (PR-h-field-06). Previously unset, which left a first run "
            "bounded only by max_rounds x the per-subprocess timeout — and the round default "
            "moved 3 -> 5 in v0.6.1, widening that ~66%. Chosen against this repo's corpus "
            "rather than picked round: across 102 priced runs the median run spent 11.0M "
            "tokens and p90 spent 38.8M, so 50M would have stopped 4 of 102 (4%). Tokens "
            "rather than dollars because cost_usd is an API-EQUIVALENT VALUATION and most "
            "traffic is subscription, where a dollar ceiling would interrupt a run over money "
            "nobody spent.\n\n"
            "SET TO 0 FOR NO CEILING. TOML has no null, so once a default exists an omitted "
            "key can no longer mean 'unlimited' — 0 is the opt-out. It reads as a ceiling of "
            "zero only if you ignore that a zero ceiling would stop every run before it "
            "started, which is why it is free to mean the opposite."
        ),
    )
    budget_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional per-RUN cost ceiling (PR-v2-11) on the API-EQUIVALENT VALUATION "
            "(PR-v2-24), NOT billed money — on a subscription the marginal dollar is $0, so "
            "this bounds the WORK, matching what `syncade --doctor` previews and `--metrics` "
            "reports. A LOWER-BOUND tally: actors with incomplete cost contribute uncounted, "
            "so a dollar-budgeted run can overshoot silently (use budget_tokens for a hard "
            "cap). None (default) = no cost ceiling. Must be >= 0 and finite (NaN/inf rejected "
            "via a field_validator; pydantic's gt=0 admits both). The CLI's --budget-usd flag "
            "overrides this per-invocation.\n\n"
            "SET TO 0 FOR NO CEILING, symmetric with budget_tokens. Added in PR-h-field-06 "
            "after a blind reviewer caught the asymmetry: the stop message offered a dollar "
            "opt-out that did not exist, and 'remove the key' does not work because --resume "
            "RE-INHERITS a ceiling the current config omits. An explicit 0 is in "
            "model_fields_set, so resume treats it as a decision rather than an absence."
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
            "entirely — exit 0 reflects synth-clean "
            "(termination_reason='ship') only; a no_changes_to_review "
            "early exit takes exit 0 regardless of this setting. The string "
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

    @field_validator("budget_tokens", mode="before")
    @classmethod
    def _strict_budget_tokens(cls, value: object) -> object:
        # Same rule as max_diff_bytes: pydantic's lax mode silently coerces false->0 (the
        # opt-out sentinel), true->1, 0.0->0, and "0"->0. A TOML typo of
        # `budget_tokens = false` would silently disable the default safety ceiling rather
        # than producing a config error — the opposite of a safe failure. Reject all
        # wrong-type forms (exit 50). None is allowed (the no-default case).
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(
                f"budget_tokens must be a plain integer or omitted (got {value!r}); "
                "quoted numbers, floats, and booleans are rejected"
            )
        return value

    @field_validator("max_diff_bytes", mode="before")
    @classmethod
    def _strict_max_diff_bytes(cls, value: object) -> object:
        # Same rule as [gc]'s integers (PR-v2-9): pydantic's lax mode silently coerces a
        # quoted number ("0"->0), an exact float (1.0->1), and a boolean (bool subclasses
        # int, so true->1). Any of those here is a typo with a severe outcome — true->1
        # would refuse EVERY run for a one-byte ceiling. Reject all three (exit 50).
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"max_diff_bytes must be a plain integer (got {value!r}); quoted numbers, "
                "floats, and booleans are rejected"
            )
        return value

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

    @field_validator("budget_usd", mode="before")
    @classmethod
    def _strict_budget_usd(cls, value: object) -> object:
        # Same rule as budget_tokens: pydantic's lax mode coerces false->0.0 (the opt-out
        # sentinel), true->1.0, and "1.5"->1.5. Because explicit 0 is now the no-ceiling
        # sentinel and lands in model_fields_set, `budget_usd = false` in a hand-edited repo
        # config can silently disable an inherited dollar ceiling rather than surfacing a config
        # error. Reject booleans and non-numeric strings; allow plain int/float and None.
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(
                f"budget_usd must be a plain numeric value >= 0 or omitted (got {value!r}); "
                "quoted numbers and booleans are rejected"
            )
        return value

    @field_validator("budget_usd")
    @classmethod
    def _budget_usd_isfinite(cls, value: float | None) -> float | None:
        """Same ``math.isfinite`` guard as the timeouts: pydantic's ``gt=0`` admits NaN/inf,
        and an infinite budget would silently mean 'no ceiling' (it never trips) rather than
        erroring — hiding a config typo. ``budget_tokens`` needs no twin: it is an int."""
        if value is not None and not math.isfinite(value):
            raise ValueError(
                f"budget_usd must be a finite non-negative number (0 = no ceiling); got {value!r}"
            )
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
