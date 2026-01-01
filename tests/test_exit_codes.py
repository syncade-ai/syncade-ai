"""Pin each exit-code constant to its PRD-mandated value.

These tests exist to catch accidental renumbering — the integer values are
a contract between the CLI and the Claude Code skill, so a typo here is
a wire-breaking change disguised as a refactor.
"""

import pydoc

from syncade import exit_codes
from syncade.orchestrator.resume_types import _RESUMABLE_EXIT_CODES


def test_success_is_zero():
    assert exit_codes.SUCCESS == 0


def test_cli_usage_error_is_two():
    assert exit_codes.CLI_USAGE_ERROR == 2


def test_clarification_needed_is_ten():
    assert exit_codes.CLARIFICATION_NEEDED == 10


def test_max_rounds_reached_is_twenty():
    assert exit_codes.MAX_ROUNDS_REACHED == 20


def test_findings_present_is_thirty():
    assert exit_codes.FINDINGS_PRESENT == 30


def test_reviewer_failure_is_forty():
    assert exit_codes.REVIEWER_FAILURE == 40


def test_config_error_is_fifty():
    assert exit_codes.CONFIG_ERROR == 50


def test_budget_exceeded_is_twenty_five():
    assert exit_codes.BUDGET_EXCEEDED == 25


def test_worktree_error_is_sixty():
    assert exit_codes.WORKTREE_ERROR == 60


def test_reviewer_output_unparseable_is_seventy():
    assert exit_codes.REVIEWER_OUTPUT_UNPARSEABLE == 70


def test_all_codes_are_distinct():
    codes = [
        exit_codes.SUCCESS,
        exit_codes.CLI_USAGE_ERROR,
        exit_codes.CLARIFICATION_NEEDED,
        exit_codes.MAX_ROUNDS_REACHED,
        exit_codes.BUDGET_EXCEEDED,
        exit_codes.FINDINGS_PRESENT,
        exit_codes.REVIEWER_FAILURE,
        exit_codes.CONFIG_ERROR,
        exit_codes.WORKTREE_ERROR,
        exit_codes.REVIEWER_OUTPUT_UNPARSEABLE,
    ]
    assert len(set(codes)) == len(codes), "exit codes must be distinct"


def test_resumable_exit_codes_follow_named_exit_constants():
    assert _RESUMABLE_EXIT_CODES == frozenset(
        {
            exit_codes.CLARIFICATION_NEEDED,
            exit_codes.BUDGET_EXCEEDED,
            exit_codes.REVIEWER_FAILURE,
            exit_codes.WORKTREE_ERROR,
            exit_codes.REVIEWER_OUTPUT_UNPARSEABLE,
        }
    )


def test_help_module_lists_every_constant_with_its_prd_description():
    """``help(syncade.exit_codes)`` must surface every constant name AND a
    PRD-style usage description for each. Attribute-style docstrings after
    plain ``int`` assignments are *not* picked up by pydoc, so this guards
    that the module docstring carries the canonical descriptions instead.
    """
    text = pydoc.plain(pydoc.render_doc(exit_codes))

    expected_names = [
        "SUCCESS",
        "CLI_USAGE_ERROR",
        "CLARIFICATION_NEEDED",
        "MAX_ROUNDS_REACHED",
        "BUDGET_EXCEEDED",
        "FINDINGS_PRESENT",
        "REVIEWER_FAILURE",
        "CONFIG_ERROR",
        "WORKTREE_ERROR",
        "REVIEWER_OUTPUT_UNPARSEABLE",
    ]
    for name in expected_names:
        assert name in text, f"help() output missing constant name: {name}"

    # PR-7 widened codes 40 and 70 to cover both reviewer AND
    # synthesizer subprocess/parse failures; the help() text reflects
    # the both-phases regime explicitly.
    expected_phrases = [
        "Clean",
        "Invalid command shape",
        "Clarification needed",
        "Max rounds reached",
        "Findings present",
        "Reviewer OR synthesizer subprocess failure",
        "Configuration error",
        "Worktree provisioning",
        "Reviewer OR synthesizer output unparseable",
    ]
    for phrase in expected_phrases:
        assert phrase in text, f"help() output missing PRD description phrase: {phrase!r}"


def test_exit_70_help_does_not_mention_stale_raw_txt_or_retry():
    """PR-5.6 review fix (P0.2): the pre-PR-5.6 docstrings for exit 70
    referenced ``<reviewer>.raw.txt`` (file never written) and a
    "retry the offending reviewer with stricter prompt enforcement"
    behavior (never implemented). Both are stale; ``help()`` output
    must reflect the shipped behavior — raw response at
    ``<reviewer-name>.stdout``, parse exception at the matching
    ``.error.txt``, and no implied retry."""
    text = pydoc.plain(pydoc.render_doc(exit_codes))
    # No stale `.raw.txt` references anywhere
    assert ".raw.txt" not in text, "help() still references the never-written .raw.txt artifact"
    # The exit-70 paragraph must name the actual artifacts
    assert ".stdout" in text
    assert ".error.txt" in text
    # And must NOT imply a retry path that doesn't exist for exit 70
    assert "stricter prompt enforcement" not in text, (
        "help() implies a retry behavior for exit 70 that PR-5.6 does not implement"
    )


def test_exit_40_help_does_not_mention_stale_retry_behavior():
    """Round-2 review fix (item 5): exit 40's pre-PR-5.6 docstrings
    said "the orchestrator retries the failing reviewer exactly once"
    — like the exit-70 stale retry, never implemented in any PR.
    Same kind of stale exit-code doc as the exit-70 issue we fixed
    last round; same kind of regression guard."""
    text = pydoc.plain(pydoc.render_doc(exit_codes))
    # No "retries the failing reviewer" / "retries the offending"
    # phrasing anywhere — PR-5.6 ships zero retry behavior.
    assert "retries the failing reviewer" not in text, (
        "help() implies retry behavior for exit 40 that PR-5.6 does not implement"
    )
    assert "retries the offending reviewer" not in text
    # And must NOT say "if the retry also fails" — same stale claim
    assert "if the retry also fails" not in text.lower()
