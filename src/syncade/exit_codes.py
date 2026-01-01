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
    Clarification needed: ``--spec-audit`` found blocking ambiguity, or the
    loop wrote ``decision-needed.md`` for a producer escalation that covers every
    active blocker.

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
    Worktree provisioning error. ``git worktree add`` or equivalent setup failed
    before the requested phase could run.

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
