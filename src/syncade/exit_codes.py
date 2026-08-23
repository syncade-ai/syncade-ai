"""Exit codes for the ``syncade`` CLI.

These integer values are the CLI contract:

SUCCESS = 0
    Clean run. With ``[loop] test_command`` unset, the synthesizer found no
    active blockers. With ``test_command`` set, the clean worktree test command
    also exited 0.

CLI_USAGE_ERROR = 2
    Invalid command shape or argument syntax, matching argparse's usage-error
    convention.

CLARIFICATION_NEEDED = 10
    Clarification needed. Three producers, all of which write an operator-facing
    ``decision-needed.md`` (except the first, which is its own report):

    - ``--spec-audit`` found blocking ambiguity in the brief;
    - a producer escalation covering every active blocker;
    - **two or more distinct reviewers each raised a blocker and the
      synthesizer deactivated every one of them** (PR-h-01 increment D). Not a
      defect claim — independent corroboration is the strongest signal syncade
      produces, and discarding all of it may be right but is not a call a
      machine should make silently. The document quotes each reviewer verbatim
      next to what the synthesizer did with it.

MAX_ROUNDS_REACHED = 20
    Max rounds reached before convergence. Latest findings are written for the
    user to inspect.

BUDGET_EXCEEDED = 25
    The per-run token/dollar ceiling (``--budget-tokens`` / ``--budget-usd`` or
    the ``[loop]`` twins) was crossed at a dispatch boundary, so the loop aborted
    before spending more. Distinct from 20 so a caller can tell "ran out of
    rounds" (raise ``--max-rounds``) from "hit the cost ceiling" (raise the
    budget). Whatever findings the completed rounds produced are preserved. The
    budget gates FURTHER dispatch only: a terminal round keeps its own verdict —
    a converged SHIP stays 0, a max-rounds/single-pass NO-SHIP stays 20/30 — and
    is never relabeled 25, so this code means "stopped early to save spend," not
    "a finished run happened to cost a lot."

    The code is SHARED with a second cause the operator did not configure: a
    provider refusing to serve because the account's usage window is exhausted
    (``termination_reason="provider_usage_limit"``). Both stop cleanly at a phase
    boundary and both resume, which is why they share a code — but they are told
    apart by ``termination_reason``, and they want opposite responses: raise the
    ceiling versus wait for the window to reset. Anything reporting this code to a
    person should read the reason rather than assume the budget.

FINDINGS_PRESENT = 30
    Findings present. A synthesizer blocker remains, the configured test command
    failed, or producer/branch promotion could not complete safely.

REVIEWER_FAILURE = 40
    Reviewer OR synthesizer subprocess failure. This also covers producer and
    test-leg subprocess failures such as missing binaries, timeouts, process
    crashes, provider/network errors, and model refusals.

CONFIG_ERROR = 50
    Configuration error. ``.syncade/config.toml`` failed to parse or failed
    schema validation.

WORKTREE_ERROR = 60
    Worktree provisioning or reviewer-export error. ``git worktree add`` or
    equivalent actor-workspace setup failed before the requested phase could run.

REVIEWER_OUTPUT_UNPARSEABLE = 70
    Reviewer OR synthesizer output unparseable. The raw response is preserved in
    the matching ``.stdout`` artifact and the parse exception is preserved in the
    matching ``.error.txt`` artifact.
"""

SUCCESS: int = 0
CLI_USAGE_ERROR: int = 2
CLARIFICATION_NEEDED: int = 10
MAX_ROUNDS_REACHED: int = 20
BUDGET_EXCEEDED: int = 25
FINDINGS_PRESENT: int = 30
REVIEWER_FAILURE: int = 40
CONFIG_ERROR: int = 50
WORKTREE_ERROR: int = 60
REVIEWER_OUTPUT_UNPARSEABLE: int = 70


WORKSPACE_PRESERVING_EXITS = frozenset({CLARIFICATION_NEEDED, MAX_ROUNDS_REACHED, FINDINGS_PRESENT})
"""Exit codes that keep actor workspaces on disk for inspection.

One authority. This list lived in ``loop_finalize`` as constants and was then re-typed as the
literals ``(10, 20, 30)`` in ``loop_summary_text``, so the summary described a preservation policy
it did not read — a third source of truth for the question the summary exists to answer. Change
the policy here and every consumer moves with it.
"""
