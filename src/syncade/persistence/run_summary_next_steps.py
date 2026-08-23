"""Per-exit-code "Next steps" guidance for summary.md.

Holds the static next-steps content blocks and the two resolvers
(``_resolve_next_steps`` for non-producer rounds, ``_resolve_next_steps_with_producer``
for rounds where a producer ran).
"""

from __future__ import annotations

from syncade.producer import ProducerResult
from syncade.producer_result import disposition_of
from syncade.synthesizer import SynthesizerResult
from syncade.test_runner import TestRunResult

from .checks import check_aware_next_steps
from .producer import PRODUCER_NAME

# Per-exit-code "Next steps" guidance for summary.md. This rewired
# the SHIP/NO-SHIP guidance around the new findings.md artifact and
# added synth-specific pointers for the synthesizer-failure exit
# codes. Keyed on the exit codes run_review can actually emit
# (0/30/40/50/60/70); 10/20 aren't reachable from a single pass
# but get the generic fallback.
_NEXT_STEPS: dict[int, str] = {
    0: (
        "- Read `findings.md` (the consolidated review across both\n"
        "  reviewers, written by the synthesizer) for the headline\n"
        "  narrative. `summary.md` is the run-level dashboard. Ship it."
    ),
    30: (
        "- Read `findings.md` FIRST — it lists the active blockers\n"
        "  (synthesizer's consolidated view, with per-reviewer\n"
        "  provenance). Each finding names the original reviewer(s)\n"
        "  and their severity calls. Then `summary.md` for run-level\n"
        "  context. The per-reviewer `.parsed.json` files have the raw\n"
        "  structured outputs for deeper inspection."
    ),
    # Exit 40 has TWO meaningful subcases:
    # - reviewer-subprocess-failed → look at the per-reviewer files first
    # - all-reviewers-succeeded + synth-subprocess-failed → look at
    #   synthesizer.* files first
    # The static lookup below is the reviewer-failure variant; the
    # synth-failure variant is _NEXT_STEPS_40_SYNTH, picked between in
    # `_resolve_next_steps`.
    40: (
        "- A reviewer subprocess failed. Check the `.error.txt` and\n"
        "  `.stderr` files for each failed reviewer; the persisted\n"
        "  `.error.txt` carries the exception class + message +\n"
        "  traceback so you can route by failure shape (auth, network,\n"
        "  timeout, binary-not-found). If it's a timeout, re-run with\n"
        "  `--timeout <seconds>` or set `[loop] timeout_seconds` in\n"
        "  `.syncade/config.toml`. The synthesizer phase was skipped\n"
        "  on this path (no silent N-1 degradation), so no\n"
        "  `synthesizer.*` artifacts exist for this run."
    ),
    50: (
        "- A reviewer's `provider` isn't a known adapter. Fix the\n"
        "  `provider` field in `.syncade/config.toml` — the `.error.txt`\n"
        "  files name the offending value."
    ),
    60: (
        "- Workspace provisioning failed for a reviewer export (the\n"
        "  pre-dispatch path). Check the `.error.txt` files; a stale\n"
        "  `<worktree_base>/<run-id>/` directory from a prior failed run\n"
        "  is the usual cause. If the test re-run leg's worktree was\n"
        "  the failing one, see the test_worktree_error variant\n"
        "  rendered when ``test_skip_reason == 'test_worktree_error'``."
    ),
    # Exit 70 has TWO meaningful subcases :
    # - reviewer parse failure → look at per-reviewer .stdout / .error.txt
    # - synth parse failure → look at synthesizer.{stdout,error.txt}
    # The static lookup below is the reviewer-failure variant; the
    # synth-failure variant is _NEXT_STEPS_70_SYNTH, picked between in
    # `_resolve_next_steps` based on whether synth_result is the
    # failing phase. This mirrors the exit-40 split from the fix
    # #10 .
    70: (
        "- A reviewer's output didn't parse as a `ReviewerOutput`.\n"
        "  `<reviewer-name>.stdout` has the raw response; look for a\n"
        "  `result` field in the JSON envelope or inline JSON in the\n"
        "  narrative. The parse exception is in\n"
        "  `<reviewer-name>.error.txt`. The synthesizer phase was\n"
        "  skipped on this path (no silent N-1 degradation), so no\n"
        "  `synthesizer.*` artifacts exist for this run."
    ),
}
_NEXT_STEPS_FALLBACK = (
    "- See `manifest.json` and the per-reviewer files in this directory\n  for details."
)

_NEXT_STEPS_60_DIFF_MALFORMED = (
    "- The diff contained section(s) whose headers could not be identified\n"
    "  (unparseable header, malformed C-quoted escape, or invalid UTF-8 in a\n"
    "  path). Syncade refused to continue because it could not determine whether\n"
    "  these sections are real changes or repo-context files to strip — treating\n"
    "  an unreadable change as 'nothing to review' would be a false SHIP.\n"
    "  The dropped headers are listed in `diff-refused.txt` in this round's\n"
    "  directory and in `manifest.json` under `diff_filter_refusal_headers`.\n"
    "  Common causes: binary paths with non-UTF-8 bytes; git C-quoting of\n"
    "  unusual characters. If the unreadable sections are repo-context files,\n"
    "  add their basenames to `strip_repo_context_files` in\n"
    "  `.syncade/config.toml`; if they are real changes you need reviewed,\n"
    "  the diff encoding itself may need investigation (e.g. a misconfigured\n"
    "  `core.quotepath` or a non-UTF-8 filename)."
)

_NEXT_STEPS_60_DIFF_TOO_LARGE = (
    "- The reviewer-facing diff exceeded `[loop] max_diff_bytes` before any reviewer "
    "was dispatched. The measured size and the ceiling are in `diff-refused.txt`. "
    "Syncade refuses rather than truncating — a verdict on a partial diff is a verdict "
    "on the wrong code. Narrow `--base` to a smaller range, split the PR, or raise "
    "`[loop] max_diff_bytes` in `.syncade/config.toml`."
)

_NEXT_STEPS_60_PROMPT_TOO_LARGE = (
    "- The assembled reviewer prompt exceeded the provider character ceiling before any "
    "reviewer was dispatched. The oversized reviewer and char count are in `diff-refused.txt`. "
    "The assembled prompt includes the diff, the reviewer template, and any prior-round "
    "context. To reduce it: narrow `--base` to a smaller diff range, trim the reviewer "
    "template in `.syncade/templates/`, or lower `[loop] max_diff_bytes` so the diff "
    "contributes fewer characters."
)

_NEXT_STEPS_NO_CHANGES = (
    "- The diff resolved to empty before any reviewer was dispatched: "
    "the base ref resolved but no reviewable changes were found "
    "(either no files changed, or every changed file was a repo-context "
    "file stripped from the reviewer diff). No model work was spent. "
    "If you expected changes to be reviewed, check that the correct "
    "`--base` / `--scope` is specified and that the changed files are "
    "not all listed in `strip_repo_context_files`."
)

# Variant for exit 40 when the failing subprocess was the synthesizer
# (every reviewer succeeded; the synth phase ran and failed). Point the operator
# at synthesizer.error.txt / .stderr first, not at reviewer files which are clean
# in this case.
_NEXT_STEPS_40_SYNTH = (
    "- The synthesizer subprocess failed (every reviewer succeeded;\n"
    "  the consolidation pass crashed). Open `synthesizer.error.txt`\n"
    "  for the exception class + message + traceback, and\n"
    "  `synthesizer.stderr` for any captured codex stderr. Common\n"
    "  shapes: auth (codex token expired / wrong account; run\n"
    "  `codex login`), network (transient — retry), timeout (re-run\n"
    "  with `--timeout <seconds>` or set `[loop] timeout_seconds`\n"
    "  in `.syncade/config.toml`; the same timeout applies to the\n"
    "  synthesizer subprocess in v1). The per-reviewer `.error.txt`\n"
    "  files do not exist for this exit code — every reviewer\n"
    "  succeeded; their `.parsed.json` files have the structured\n"
    "  outputs the synth was asked to consolidate, in case the\n"
    "  synth failure is content-driven (very long combined\n"
    "  reviewer-output blob, etc.)."
)

# variant for exit 30 when the failing leg was the test
# re-run (not the synthesizer's consolidated_findings). The
# synthesizer was clean; the operator's tests reported failures.
# Different fix path: read test-run.stdout for the test runner's
# own output, not findings.md (which renders the synth's clean
# verdict with zero blockers).
_NEXT_STEPS_30_TEST_FAILED = (
    "- The independent test re-run leg reported failures (the\n"
    "  cold synthesizer was clean — no consolidated\n"
    "  blockers). Open `test-run.stdout` FIRST for the operator-\n"
    "  configured test command's output; `test-run.stderr` for\n"
    "  any captured stderr; `test-run.exit-code.txt` for the\n"
    "  one-line exit code. `findings.md` still renders the\n"
    "  synthesizer's consolidated review (zero active blockers)\n"
    "  plus a Test Suite section pointing back at this artifact.\n"
    "  `summary.md` is the run-level dashboard."
)

# variant for exit 40 when the failing subprocess was the
# test-leg (not a reviewer or the synthesizer). The reviewer and
# synthesizer subprocesses succeeded; only the test command's
# subprocess failed (binary missing, timeout, OS launch error).
# findings.md renders Verdict: ABORT on this path (not SHIP / NO-SHIP).
_NEXT_STEPS_40_TEST_SUBPROCESS = (
    "- The test re-run leg's subprocess failed (every reviewer\n"
    "  succeeded; the synthesizer succeeded; the operator-\n"
    "  configured test command couldn't run to completion). Open\n"
    "  `test-run.stderr` for any captured stderr from before the\n"
    "  kill; `manifest.json`'s `test_run.error_type` names the\n"
    "  failure shape (`SubprocessTimeoutError`,\n"
    "  `SubprocessNotFoundError`, etc.). Common shapes:\n"
    "  - timeout: re-run with `--timeout <seconds>` or set\n"
    "    `[loop] test_timeout_seconds` in `.syncade/config.toml`.\n"
    "  - binary not found: the operator's `test_command` references\n"
    "    a tool that isn't on PATH in the test worktree. Install\n"
    "    the tool, or adjust `test_command` to use the installed\n"
    "    one.\n"
    "  The synthesizer artifacts are present and valid for this\n"
    "  exit code; `findings.md` renders Verdict: ABORT (the test\n"
    "  signal is indeterminate until the environment problem is\n"
    "  fixed)."
)


# exit-60 variant when the failing worktree provisioning
# was the test leg's (not a reviewer's). Reviewer + synth phases
# already produced valid artifacts; the only thing missing is the
# test re-run output. Different fix path than the reviewer-
# workspace-error case.
_NEXT_STEPS_60_TEST_WORKTREE = (
    "- The test re-run leg's worktree could not be provisioned\n"
    "  (every reviewer succeeded; the synthesizer succeeded; only\n"
    "  the test-leg worktree-add failed). Reviewer + synthesizer\n"
    "  artifacts are valid and on disk — read them as usual:\n"
    "  `findings.md` (Verdict: ABORT, indeterminate until the\n"
    "  provisioning failure is fixed), the per-reviewer\n"
    "  `.parsed.json` files for the structured outputs, and\n"
    "  `manifest.json` for the run-level structured view\n"
    "  (`test_skip_reason: test_worktree_error`).\n"
    "\n"
    "  No `test-run.*` files were written (the leg never ran).\n"
    "  The provisioning diagnostic was emitted to the CLI's\n"
    "  stderr at the moment of failure (the `[syncade]` worktree-\n"
    "  error line) and via the `test re-run skipped (test\n"
    "  worktree provisioning failed: ...)` log line in this run's\n"
    "  stdout. Re-running after addressing the worktree failure\n"
    "  (typical causes: stale `<worktree_base>/<run-id>/` from a\n"
    "  prior interrupted run; `.git/worktrees/tests/` metadata\n"
    "  drift) will exercise the test leg fresh."
)


# Variant for exit 70 when the failing parser was the synthesizer.
# Reviewer files are clean on this path; don't send the operator
# there. The common-shapes list names ghost reviewer provenance and
# out-of-range original indices because those are cross-input
# provenance-validation failures.
_NEXT_STEPS_70_SYNTH = (
    "- The synthesizer's output didn't parse as a `SynthesizerOutput`\n"
    "  (every reviewer succeeded; only the synth phase failed at\n"
    "  parse). `synthesizer.stdout` has the codex JSONL stream; the\n"
    "  final `agent_message` event's text is what the parser tried\n"
    "  to validate. The parse exception is in `synthesizer.error.txt`.\n"
    "  Common shapes: invented findings (empty `provenance`),\n"
    "  attempted dismissal of a unanimous blocker (schema-rejected),\n"
    "  missing required fields, ghost reviewer name (provenance\n"
    "  references a reviewer that doesn't exist in this run), and\n"
    "  out-of-range `original_index` (provenance points past the\n"
    "  source reviewer's findings list). The per-reviewer `.error.txt`\n"
    "  files do not exist for this exit code; their `.parsed.json`\n"
    "  files have the structured outputs the synth was asked to\n"
    "  consolidate."
)


def _resolve_next_steps(
    exit_code: int,
    synth_result: SynthesizerResult | None,
    test_result: TestRunResult | None = None,
    test_skip_reason: str | None = None,
    check_results: list[TestRunResult] | None = None,
    *,
    no_changes_to_review: bool = False,
    fail_closed_headers: list[str] | None = None,
    oversize_diff_bytes: int | None = None,
    oversize_prompt_chars: int | None = None,
) -> str:
    """Pick the right Next-steps content for the given exit code +
    synthesizer outcome + test outcome + test-skip reason.

    Exits 30, 40, 60, and 70 each have multiple meaningful
    flavors that demand distinct first instructions. The split
    rules:

    - Exit 30: ``test_failed`` (clean synth, test reported
      failures) → ``_NEXT_STEPS_30_TEST_FAILED`` (read
      test-run.stdout); else (synth had blocker — the original
      recorded reason) keep the synth-blocker variant from
      ``_NEXT_STEPS``.
    - Exit 40: ``test_subprocess_errored`` → test-subprocess
      variant; else ``synth_failed`` → synth variant; else
      reviewer-failed variant from ``_NEXT_STEPS``.
    - Exit 60 : ``test_skip_reason == "test_worktree_error"``
      → test-leg worktree variant naming the preserved
      reviewer + synth artifacts; else the generic workspace-
      provisioning variant for reviewer-side failures.
    - Exit 70: unchanged — the test leg has no parse path, so
      this code only ever points at reviewer or synth output.

    Args:
        exit_code: The run's exit code.
        synth_result: The :class:`SynthesizerResult`, or ``None``
            if the synth phase was skipped.
        test_result: The :class:`TestRunResult`, or ``None`` if
            the test phase was skipped.
        test_skip_reason: Why the test leg was skipped. Used to
            distinguish the test-worktree-error variant of
            exit 60 from the reviewer-workspace variant.

    Returns:
        The Next-steps content string for this exit-code / phase
        combination, or :data:`_NEXT_STEPS_FALLBACK` for any code
        not covered above (10/20 today).
    """
    # a blocking mechanical check that drove the round's
    # exit code (failed → 30 on a synth-clean round; subprocess_error → 40)
    # takes precedence over the generic per-exit-code guidance, which would
    # otherwise point at the wrong artifact. Returns None for every
    # non-check-driven round (including all zero-check rounds), keeping the
    # rest of this routing byte-identical.
    if fail_closed_headers is not None:
        return _NEXT_STEPS_60_DIFF_MALFORMED
    if oversize_diff_bytes is not None:
        return _NEXT_STEPS_60_DIFF_TOO_LARGE
    if oversize_prompt_chars is not None:
        return _NEXT_STEPS_60_PROMPT_TOO_LARGE
    if no_changes_to_review:
        return _NEXT_STEPS_NO_CHANGES

    check_override = check_aware_next_steps(exit_code, synth_result, test_result, check_results)
    if check_override is not None:
        return check_override

    synth_failed = synth_result is not None and synth_result.error is not None
    test_failed = test_result is not None and test_result.outcome == "failed"
    test_subprocess_errored = test_result is not None and test_result.outcome == "subprocess_error"

    if exit_code == 30 and test_failed:
        return _NEXT_STEPS_30_TEST_FAILED
    if exit_code == 40 and test_subprocess_errored:
        return _NEXT_STEPS_40_TEST_SUBPROCESS
    if exit_code == 40 and synth_failed:
        return _NEXT_STEPS_40_SYNTH
    # exit 60 splits between reviewer-workspace-error
    # (generic provisioning variant) and test-leg-worktree-error
    # (preserved reviewer + synth artifacts variant).
    if exit_code == 60 and test_skip_reason == "test_worktree_error":
        return _NEXT_STEPS_60_TEST_WORKTREE
    if exit_code == 70 and synth_failed:
        return _NEXT_STEPS_70_SYNTH
    return _NEXT_STEPS.get(exit_code, _NEXT_STEPS_FALLBACK)


def _resolve_next_steps_with_producer(
    exit_code: int,
    producer_result: ProducerResult,
    escalation_honored: bool = False,
    branch_already_advanced: bool = False,
) -> str:
    """Next-steps guidance for a per-round
    summary.md AFTER a producer ran on this round.

    On a round where the producer already attempted to fix the findings, the
    producer's outcome is the actionable signal for this round.

    Branches by producer outcome:

    - ``committed`` — the producer made an isolated candidate commit; its
      trusted-import result determines whether branch landing can proceed.
    - ``stalled`` — the producer subprocess completed cleanly
      but didn't commit. The loop terminated with
      ``producer_stalled`` (exit 30 + that termination reason);
      operator should clarify the spec or fix manually.
    - ``subprocess_error`` — the producer subprocess failed.
      Loop terminated with exit 40. Operator reads
      ``producer.error.txt`` for the exception trace.
    - ``escalated`` — splits on ``escalation_honored``: when
      True (the escalation covered every active blocker), the loop
      checkpointed at exit 10 and the operator records a decision and
      resumes; when False (the coverage guard rejected it for leaving
      a blocker uncovered), the round is treated as a stall (exit 30,
      no decision-needed.md) and the operator is pointed at the open
      findings, not a checkpoint that doesn't exist.
    """
    if producer_result.outcome == "committed":
        candidate_import = producer_result.candidate_import
        # Whether the workspace survives is NOT decided here. `candidate_disposition` is the one
        # authority the terminal cleanup also reads, so this text cannot promise a repository
        # that gets deleted — which is precisely what a hand-rolled copy of the rule did, twice.
        # The previous revision reached the right answer while CITING that authority in a comment
        # instead of calling it: a fourth independent derivation that merely agreed for now, and
        # read as if it were unified. Agreement that is re-derived is not agreement.
        disposition = disposition_of(producer_result)
        if candidate_import is not None and candidate_import.status == "imported":
            return (
                f"- The producer candidate `{producer_result.ending_sha[:12]}` passed "
                f"trusted import and is anchored at `{disposition.reachable_ref}`. "
                f"The operator-branch compare-and-swap runs after this artifact is written; "
                f"the top-level loop summary records whether it advanced."
            )
        if candidate_import is not None:
            if not disposition.workspace_is_only_copy:
                return (
                    f"- The producer candidate `{producer_result.ending_sha[:12]}` was not "
                    f"fully accepted by trusted import (`{candidate_import.status}`): "
                    f"{candidate_import.error}. It IS anchored at "
                    f"`{disposition.reachable_ref}` in the operator repository — "
                    f"the standalone workspace is NOT the only copy and is not preserved. "
                    f"Inspect `producer.import.error.txt`; the operator branch remains unchanged."
                )
            return (
                f"- The producer candidate `{producer_result.ending_sha[:12]}` was not "
                f"accepted by trusted import (`{candidate_import.status}`): "
                f"{candidate_import.error}. The operator branch remains unchanged. "
                f"Inspect `producer.import.error.txt` and the preserved standalone repository."
            )
        return (
            f"- The producer subprocess created isolated candidate "
            f"`{producer_result.ending_sha[:12]}` for this round's findings. "
            f"It was not imported because no trusted-import result was recorded. Read "
            f"`{PRODUCER_NAME}.stdout` "
            f"and inspect the preserved standalone producer repository. The operator "
            f"branch and working tree remain unchanged."
        )
    if producer_result.outcome == "stalled":
        return (
            f"- The producer subprocess completed but did NOT commit "
            f"(HEAD stayed at `{producer_result.starting_sha[:12]}`). The "
            f"loop terminated with `producer_stalled`. Read "
            f"`{PRODUCER_NAME}.stdout` for the producer's narrative; "
            f"common causes are under-specified findings (the spec doesn't "
            f"say what 'right' looks like) or the producer concluding the "
            f"existing code is already correct. The operator's next step "
            f"is to clarify the spec / address the finding manually and "
            f"re-run."
        )
    if producer_result.outcome == "escalated" and escalation_honored:
        _branch_note = (
            "An earlier round already advanced your branch — producer "
            "commits from that round are on it. This round's producer "
            "did not commit."
            if branch_already_advanced
            else "No branch was advanced by this round."
        )
        return (
            "- The producer ESCALATED a finding it determined is an operator "
            "decision (a spec/design conflict), not a code defect. The loop "
            f"checkpointed and terminated (exit 10). {_branch_note} "
            "Read `decision-needed.md` at the run root for the "
            "producer's case + concrete options, record your decision in "
            "`decision.txt`, and run `syncade --resume <run-id>` — the "
            "escalated round re-runs with your decision fed to the producer."
        )
    if producer_result.outcome == "escalated":
        # the escalation did NOT cover every active blocker, so the
        # coverage guard rejected it and the round is treated as a stall
        # (NO-SHIP, exit 30; no decision checkpoint was created). Point the
        # operator at the open findings, not at a `decision-needed.md` that
        # was never written.
        return (
            f"- The producer attempted to ESCALATE a finding as an operator "
            f"decision, but its escalation did not cover every active blocker "
            f"this round, so it was NOT honored — the round is treated as a "
            f"stall (NO-SHIP, exit 30; no branch advanced and no decision "
            f"checkpoint created). Read `findings.md` for the active blockers "
            f"and `{PRODUCER_NAME}.stdout` for the producer's narrative. The "
            f"uncovered blocker(s) carry forward; re-run after the producer can "
            f"fix or fully cover them, or address the finding manually."
        )
    if producer_result.ending_sha != producer_result.starting_sha:
        return (
            f"- The producer subprocess failed after moving HEAD to "
            f"`{producer_result.ending_sha[:12]}`. This is an indeterminate "
            f"isolated producer candidate, not a successful imported round: the operator's "
            f"branch was not advanced. Read `{PRODUCER_NAME}.stdout`, "
            f"`{PRODUCER_NAME}.stderr`, and `{PRODUCER_NAME}.error.txt`; inspect "
            f"the preserved standalone producer repository named by the run."
        )
    return (
        f"- The producer subprocess failed before it could attempt a fix. "
        f"Read `{PRODUCER_NAME}.stderr` and `{PRODUCER_NAME}.error.txt` for "
        f"the failure shape. Common causes are auth (run `claude login` / "
        f"`codex login`), network errors (retry), or a missing CLI binary. "
        f"The operator's branch was NOT advanced for this round; "
        f"`findings.md` still reflects this round's NO-SHIP signal and is "
        f"the operator's manual-fix target."
    )
