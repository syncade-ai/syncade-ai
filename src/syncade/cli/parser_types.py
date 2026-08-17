"""argparse ``type=`` coercers for syncade's numeric and structured CLI values.

Split out of ``parser.py`` (PR-h-04 item B), which sat exactly AT the 500-LOC cap — so
adding any flag broke the gate, and trimming help text to fit would have been squeezing
rather than engineering. The seam is real: these answer *how do I coerce and validate one
CLI scalar*, while ``parser.py`` answers *what flags exist*. They share no state and only
these raise ``ArgumentTypeError``.

Strictness is deliberate and load-bearing (PR-v2-9): a quoted number, a float where an int
is required, or a boolean must FAIL rather than silently coerce, because a config value
that quietly becomes something else is how a run ends up with settings nobody chose.
"""

from __future__ import annotations

import argparse
import math


def _positive_finite_float(noun: str, value: str, *, allow_zero: bool = False) -> float:
    """Shared body for the finite float ``type`` validators. ``float()`` parses ``nan`` /
    ``inf``, neither of which is a usable bound, so non-finite values are rejected too;
    ``noun`` makes the message fit the flag (seconds vs dollars). ``allow_zero`` admits 0 for
    the budget flags, where it is the no-ceiling opt-out rather than an absurd bound."""
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from None
    floor_ok = parsed >= 0 if allow_zero else parsed > 0
    if not math.isfinite(parsed) or not floor_ok:
        bound = "non-negative" if allow_zero else "positive"
        raise argparse.ArgumentTypeError(f"must be a {bound}, finite {noun} (got {value!r})")
    return parsed


def _positive_float(value: str) -> float:
    """argparse ``type`` for ``--timeout``: a strictly-positive, finite number of seconds.

    Mirrors :class:`~syncade.config.LoopConfig`'s ``gt=0`` validation at the CLI boundary, so
    ``--timeout 0`` / ``--timeout -1`` are rejected up front (exit 2) rather than reaching the
    orchestrator and getting every reviewer SIGKILL'd instantly with a nonsensical timeout."""
    return _positive_finite_float("number of seconds", value)


def _positive_usd(value: str) -> float:
    """argparse ``type`` for ``--budget-usd``: a non-negative, finite dollar amount, 0 = OFF.

    Mirrors ``budget_usd``'s ``ge=0`` + isfinite bound. 0 is the no-ceiling opt-out, symmetric
    with ``--budget-tokens 0`` (PR-h-field-06) — and necessary rather than tidy: omitting the
    key does NOT remove a ceiling, because --resume re-inherits one the current config leaves
    unset. Only an explicit value says "I decided this".
    """
    parsed = _positive_finite_float("dollar amount", value, allow_zero=True)
    return parsed


def _non_negative_int(value: str) -> int:
    """argparse ``type`` for ``--gc-keep`` / ``--gc-max-age-days``: a
    non-negative integer.

    GC is a destructive maintenance command, so a negative count/age is a
    nonsensical, dangerous input (a negative ``--gc-keep`` would slice from the
    end and prune the transcripts of the NEWEST runs). Reject it up front
    (exit 2) rather than letting Python slicing semantics quietly do the wrong
    thing.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer (got {value!r})") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return n


def _positive_int(value: str) -> int:
    """argparse ``type`` for ``--budget-tokens``: a non-negative integer, where 0 means OFF.

    Mirrors :class:`~syncade.config.LoopConfig`'s ``ge=0`` bound at the CLI boundary.
    ``budget_tokens`` has a DEFAULT ceiling since PR-h-field-06, so ``0`` had to stop meaning
    "invalid" and start meaning "no ceiling" — it is the only way left to express unlimited
    once an omitted value means the default. A zero ceiling would otherwise abort before the
    first phase dispatched, which is nonsensical, so the value is free to carry the opposite
    sense. Negatives are still rejected up front (exit 2) with argparse's legible message
    rather than the config's exit 50.
    """
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer (got {value!r})") from None
    if n < 0:
        raise argparse.ArgumentTypeError(
            f"--budget-tokens must be >= 0, 0 = no ceiling (got {value!r})"
        )
    return n


def _max_rounds(value: str) -> int:
    """argparse ``type`` for ``--max-rounds``: an integer in
    ``[1, 10]``.

    Mirrors :class:`~syncade.config.LoopConfig`'s ``ge=1, le=10``
    bounds at the CLI boundary so ``--max-rounds 0`` /
    ``--max-rounds 11`` are rejected up front (exit 2) rather than
    surfacing the same error as a config-loaded value (exit 50). The
    distinction matters because the CLI flag is the immediate cause
    of the rejection — argparse's exit-2 path with the type name
    in the error message is more legible than the schema's
    ``ValidationError`` rendered through ``ConfigError``.

    The ceiling was raised from 3 to 10 (PR-v2-31); it is a
    typo-guard, not the runaway-protection mechanism —
    budget_tokens/budget_usd and the per-subprocess timeout are.
    """
    try:
        rounds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--max-rounds must be an integer (got {value!r})"
        ) from None
    if rounds < 1 or rounds > 10:
        raise argparse.ArgumentTypeError(f"--max-rounds must be in [1, 10] (got {value!r})")
    return rounds


def _reviewer_override(value: str) -> tuple[str, str]:
    """argparse ``type`` for the name-qualified ``--reviewer-*`` flags: parse ``NAME=VALUE`` into
    ``(name, value)``, splitting on the FIRST ``=`` so a value may itself contain ``=``. A missing
    ``=`` or empty name is a malformed FLAG → exit 2 here; whether the NAME exists and the VALUE is
    valid for the knob is a config-level question resolved later against the loaded reviewers
    (unknown name / bad value → exit 50), so this stays deliberately minimal (PR-v2-9)."""
    name, sep, val = value.partition("=")
    if not sep or not name or not val:
        raise argparse.ArgumentTypeError(
            f"expected NAME=VALUE (e.g. codex-reviewer=gpt-5.5), got {value!r}"
        )
    return name, val
