"""Every ``TerminationReason`` is rendered everywhere it must be — PR-h-01 carry-forward.

``TerminationReason`` is a ``Literal`` in one place, but it is CONSUMED by several
independently-keyed dicts scattered across ``persistence/``. Adding a reason updates the
type and, silently, none of them: every renderer falls back with ``.get(reason, reason)``,
so a new reason renders as its raw snake_case identifier instead of prose. Nothing fails,
nothing warns — the operator just gets ``no_changes_to_review`` where a sentence belongs.

That is not hypothetical. **7 of the 15 findings in the PR-h-01 dogfood shared this single
root cause**, which is why a drift test was carried forward as the highest-value follow-up
from that campaign. PR-h-02d adds a new reason, so it lands first.

The test is deliberately two-tier, because "every dict must be total" would be false:

- **Total renderers** describe every terminal state, so a missing key is always a bug.
- **``handoff``** is written ONLY when ``final_exit_code in (20, 30)``
  (see ``persist_handoff``), so reasons that cannot reach those codes are legitimately
  absent. Each exclusion below is recorded with the exit code that justifies it, verified
  against the ``final_exit_code=`` paired with it at its raise site.

A new reason therefore forces a decision at every site: add it to the total renderers, and
either key it for handoff or state why it cannot reach exit 20/30.
"""

from __future__ import annotations

import typing

from syncade.orchestrator.results import TerminationReason
from syncade.persistence import handoff, loop_summary, loop_summary_text

_REASONS = frozenset(typing.get_args(TerminationReason))

# Renderers that describe EVERY terminal state. A missing key here degrades silently to
# the raw identifier; a stale key is dead code that outlived its reason.
_TOTAL_RENDERERS: dict[str, dict[str, object]] = {
    "loop_summary._TERMINATION_REASON_LABELS": loop_summary._TERMINATION_REASON_LABELS,
    "loop_summary_text._LOOP_NEXT_STEPS": loop_summary_text._LOOP_NEXT_STEPS,
    "loop_summary_text._EMPTY_SERIES_REASON_NOTES": loop_summary_text._EMPTY_SERIES_REASON_NOTES,
}

# Reasons that cannot reach exit 20/30, so `handoff.md` is never written for them.
# The exit code is the justification — re-verify it before adding an entry here.
_HANDOFF_UNREACHABLE: dict[str, str] = {
    "no_changes_to_review": "exit 0 (SUCCESS — no diff, no reviewers dispatched in round 0)",
    "producer_emptied_diff": "exit 0 (SUCCESS — diff empty in a later round; prior rounds ran)",
    "blockers_all_deactivated": "exit 10 (CLARIFICATION_NEEDED)",
    "decision_needed": "exit 10 (CLARIFICATION_NEEDED)",
    "budget_exceeded": "exit 25 (BUDGET_EXCEEDED)",
    "provider_usage_limit": "exit 25 (BUDGET_EXCEEDED — provider quota, not the operator's)",
    "check_subprocess_error": "exit 40 (subprocess-failure family)",
}


def test_every_reason_is_rendered_by_every_total_renderer():
    problems: list[str] = []
    for name, renderer in _TOTAL_RENDERERS.items():
        missing = _REASONS - set(renderer)
        stale = set(renderer) - _REASONS
        if missing:
            problems.append(f"{name}: MISSING {sorted(missing)} (renders as the raw identifier)")
        if stale:
            problems.append(f"{name}: STALE {sorted(stale)} (no longer a TerminationReason)")

    assert not problems, "TerminationReason renderers drifted from the type:\n  " + "\n  ".join(
        problems
    )


def test_handoff_covers_exactly_the_reasons_that_can_produce_one():
    """`handoff.md` is written only at exit 20/30, so its dict is deliberately partial —
    but the partition must be explicit, not accidental."""
    expected = _REASONS - set(_HANDOFF_UNREACHABLE)
    actual = set(handoff._HANDOFF_TERMINATION_REASON_LABELS)

    missing = expected - actual
    unexpected = actual - expected
    assert not missing, (
        f"handoff cannot label {sorted(missing)}; they reach exit 20/30 and would render as "
        f"the raw identifier. Add a label, or document why it is unreachable in "
        f"_HANDOFF_UNREACHABLE."
    )
    assert not unexpected, (
        f"handoff labels {sorted(unexpected)}, which _HANDOFF_UNREACHABLE says cannot occur. "
        f"One of the two is wrong."
    )


def test_exclusions_name_only_real_reasons():
    """A typo'd exclusion would silently grant a real reason a free pass."""
    unknown = set(_HANDOFF_UNREACHABLE) - _REASONS
    assert not unknown, f"_HANDOFF_UNREACHABLE names non-existent reason(s): {sorted(unknown)}"


# --- The sibling surface: exit-code labels -------------------------------------------
# `_EXIT_CODE_LABELS` is the same shape of hazard as the reason renderers above — a keyed
# dict consumed with `.get(..., "UNKNOWN")`. It is guarded here because PR-h-02d's first
# cut passed a private sentinel `1` as a round exit code; `1` had no label, so durable
# artifacts rendered `Exit code: 1 (UNKNOWN)` and `ERROR (exit 1)` while the top-level API
# correctly reported exit 0. Two blind reviewers found it; this test family had not.
#
# HONEST LIMIT, stated so nobody over-trusts this: the coverage test below would NOT have
# caught that bug. It asserts every CANONICAL exit code has a label, and `1` was not
# canonical — it was invented. `test_no_changes_summary_renders_a_real_label` is the one
# that catches it, by asserting the SYMPTOM (a rendered "UNKNOWN") rather than the shape.

_ROUND_UNREACHABLE_CODES: dict[str, str] = {
    "CLI_USAGE_ERROR": "exit 2 — a bad command shape, raised before any run starts",
    "BUDGET_EXCEEDED": "exit 25 — a LOOP-level terminal state; never a round exit code",
}


def test_every_round_reachable_exit_code_has_a_label():
    """Every ``persist_run_summary`` call passes a ROUND exit code, so the label table
    must be total over the codes a round can actually carry."""
    import syncade.exit_codes as exit_codes
    from syncade.persistence.run_summary import _EXIT_CODE_LABELS

    canonical = {
        name: value
        for name, value in vars(exit_codes).items()
        if name.isupper() and isinstance(value, int)
    }
    expected = {v for n, v in canonical.items() if n not in _ROUND_UNREACHABLE_CODES}
    actual = set(_EXIT_CODE_LABELS)

    missing = expected - actual
    assert not missing, (
        f"exit code(s) {sorted(missing)} have no label and would render as 'UNKNOWN' in "
        f"round summaries. Add a label, or document why a round cannot carry the code in "
        f"_ROUND_UNREACHABLE_CODES."
    )
    stale = actual - {v for v in canonical.values()}
    assert not stale, f"_EXIT_CODE_LABELS labels non-existent exit code(s): {sorted(stale)}"


def test_exclusions_name_only_real_exit_codes():
    import syncade.exit_codes as exit_codes

    unknown = {n for n in _ROUND_UNREACHABLE_CODES if not hasattr(exit_codes, n)}
    assert not unknown, f"_ROUND_UNREACHABLE_CODES names non-existent constant(s): {unknown}"
