"""Next-steps + empty-series text tables and small render helpers for
loop-summary.md.

Holds the per-termination-reason next-steps guidance, the empty-commit-series
notes, and the four small render helpers (`_round_verdict_label`,
`_producer_commit_subject`, `_empty_commit_series_note`,
`_round_duration_seconds`). ``loop_summary.py`` keeps the headline labels and
``persist_loop_summary`` and imports these helpers.
"""

from __future__ import annotations

from pathlib import Path

_LOOP_NEXT_STEPS: dict[str, str] = {
    "ship": (
        "- Loop terminated SHIP — the operator's branch has been "
        "advanced to the latest producer commit (if any). Inspect "
        "`loop-summary.md`'s commit series above to see what landed "
        "on your branch; the operator's branch is at the SHIP "
        "round's snapshot SHA. `findings.md` in the SHIP round's "
        "directory carries the consolidated review (zero "
        "blockers); the operator's repo is ready to push."
    ),
    "no_changes_to_review": (
        "- The diff resolved to empty before any reviewer was dispatched: "
        "the base ref resolved but no reviewable changes were found "
        "(either no files changed, or every changed file was a repo-context "
        "file stripped from the reviewer diff). No model work was spent. "
        "If you expected changes to be reviewed, check that the correct "
        "`--base` / `--scope` is specified and that the changed files are "
        "not all listed in `strip_repo_context_files`."
    ),
    "producer_emptied_diff": (
        "- The producer's commits in a prior round removed all reviewable "
        "changes: the diff from the original base to the current HEAD is now "
        "empty (all sections were either legitimate repo-context files or the "
        "producer reverted the reviewable changes). Prior rounds DID spend "
        "model work. If this is unexpected, inspect the per-round commit series "
        "in `loop-summary.md` and the producer's commits on your branch."
    ),
    "findings_present": (
        "- The single-pass run found remaining work. Read the round's "
        "`findings.md` for the active blockers or failed checks, address them, "
        "and re-run syncade when ready."
    ),
    "max_rounds_reached": (
        "- The loop exhausted ``max_rounds`` (the configured cap) "
        "without converging. Read the FINAL round's `findings.md` "
        "for the remaining active blockers — they may require a "
        "human eye to resolve. Consider: (a) bumping ``max_rounds`` "
        "in `.syncade/config.toml`, (b) addressing the findings "
        "manually and re-running, or (c) inspecting the per-round "
        "directories to see what the producer attempted at each "
        "step. The producer's commits ARE on your branch — if you "
        "want to roll them back, use `git reset --hard "
        "<round-0-starting-sha>` (see the commit series above)."
    ),
    "provider_usage_limit": (
        "- The loop stopped because the PROVIDER refused on an exhausted usage limit — not "
        "your configured budget, and not a fault in the code under review. It stopped at a "
        "phase boundary, so completed rounds and their artifacts are intact. Retrying "
        "immediately would fail the same way: the window has not moved. Next: consume a reset "
        "(`codex` → `/usage`) or wait for the window, then `syncade --resume <run-id>` to "
        "continue from the completed rounds rather than paying for them twice. Any producer "
        "commits so far ARE on your branch."
    ),
    "budget_exceeded": (
        "- The loop stopped because the running token or cost tally crossed your "
        "configured ``[loop]`` budget (``--budget-tokens`` / ``--budget-usd``). "
        "It aborted at a phase boundary, so NO provider call was interrupted "
        "and the crossing round's `findings.md` / artifacts are complete. See "
        "the **Budget** section above for the API-EQUIVALENT tally — a "
        "VALUATION of the work, not billed money (PR-v2-24), and a LOWER BOUND "
        "if any actor's cost was unpriced. Next: raise the budget in "
        "`.syncade/config.toml` (or via the flag) and re-run, or address the "
        "round's findings manually. Any producer commits so far ARE on your "
        "branch."
    ),
    "producer_stalled": (
        "- The producer subprocess exited cleanly but didn't commit "
        "— either it made no edits, or it made edits without "
        "committing. Read the producer's `producer.stdout` in the "
        "round directory to see what the producer was trying to "
        "do; common causes are under-specified findings ('this is "
        "wrong but the spec doesn't say what's right') or the "
        "producer concluding the existing code is correct. The "
        "operator's next step is to clarify the spec / address the "
        "finding manually and re-run."
    ),
    "producer_subprocess_error": (
        "- The producer subprocess failed before it could attempt "
        "the fix. Read `producer.stderr` and `producer.error.txt` "
        "in the round directory for the failure shape; common "
        "causes are auth (run `claude login` / `codex login`), "
        "network errors (retry), or a missing CLI binary. The "
        "operator's branch was NOT advanced for this round."
    ),
    "reviewer_failure": (
        "- A reviewer subprocess failed before the loop could even "
        "compute a verdict for the round. Read the per-reviewer "
        "`.error.txt` files in the round directory; the failure "
        "shape (timeout / auth / binary missing) determines the "
        "fix. The loop did not advance any branch — the operator's "
        "tree is unchanged from where it started."
    ),
    "synth_failure": (
        "- The synthesizer subprocess failed. Read "
        "`synthesizer.error.txt` and `synthesizer.stderr` in the "
        "round directory. The loop did not advance any branch."
    ),
    "test_subprocess_error": (
        "- The test re-run subprocess failed (binary missing, "
        "timeout, OS launch error). Read `test-run.stderr` and the "
        "manifest's `test_run.error_type` in the round directory. "
        "The synth was clean for this round; the test failure is "
        "environmental, not code-side."
    ),
    "check_subprocess_error": (
        "- A blocking mechanical check's subprocess could not run to "
        "completion (every reviewer succeeded; the synthesizer "
        "succeeded). The `## Mechanical checks` section of the round's "
        "`findings.md` names which check; read that check's "
        "`<name>.check.stderr` and the manifest's `checks[].error_type` "
        "in the round directory. Common shapes are a missing binary "
        "(the check `command` references a tool not on PATH in the "
        "check worktree) or a timeout. The loop did not advance any "
        "branch — the check signal is indeterminate until the "
        "environment problem is fixed."
    ),
    "decision_needed": (
        "- A NO-SHIP round's producer ESCALATED a finding it determined "
        "is an operator decision (a spec/design conflict), not a code "
        "defect it can fix. The loop checkpointed and terminated (exit "
        "10). Read `decision-needed.md` at the run root for the "
        "producer's case + concrete options; record your decision in "
        "`decision.txt` and run `syncade --resume <run-id>` to continue "
        "(the escalated round re-runs with your decision fed to the "
        "producer). The mechanical verdict is unchanged — the finding "
        "stays open until the decision is applied."
    ),
    "blockers_all_deactivated": (
        "- Two or more independent reviewers EACH raised a blocker, and the "
        "synthesizer deactivated every one of them (dismissed, downgraded, or "
        "split into separate single-reviewer findings). That may be correct, "
        "but discarding all independent corroboration is not a call the "
        "mechanical verdict will make silently, so the loop terminated at exit "
        "10 instead of reporting a SHIP it cannot justify. No producer ran. "
        "Read `decision-needed.md` at the run root: it quotes what each "
        "reviewer actually said next to what the synthesizer did with it. If "
        "the synthesizer was right, this round is effectively a SHIP; if it "
        "was wrong about any one of them, that concern is real and unfixed. "
        "There is nothing to resume — no blocker is active for a producer to "
        "fix, so `decision.txt` and `--resume` do not apply here."
    ),
    "worktree_error": (
        "- A worktree could not be provisioned. Common cause: "
        "stale `<worktree_base>/<run-id>/` from a prior interrupted "
        "run. The loop did not advance any branch."
    ),
    "diff_too_large": (
        "- The reviewer-facing diff exceeded `[loop] max_diff_bytes`, so syncade refused "
        "before dispatching any reviewer — nothing was spent. The measured size and the "
        "ceiling are in the refusing round's `diff-refused.txt`. Syncade refuses rather "
        "than truncating: a verdict on a deliberately partial diff is a verdict on the "
        "wrong code. Narrow `--base` to a smaller range, split the PR, or raise "
        "`[loop] max_diff_bytes` in `.syncade/config.toml`."
    ),
    "prompt_too_large": (
        "- An assembled reviewer prompt exceeded the provider's character ceiling "
        "(1,048,576 chars for codex), so syncade refused before dispatching any reviewer "
        "— nothing was spent. The affected reviewer and the measured size are in the "
        "refusing round's `diff-refused.txt`. The assembled prompt includes the diff, "
        "the reviewer template, and any prior-round context. Reduce prompt size by "
        "narrowing `--base`, trimming the reviewer template, or lowering "
        "`[loop] max_diff_bytes` in `.syncade/config.toml`."
    ),
    "diff_malformed": (
        "- The reviewer-facing diff had section(s) with unidentifiable "
        "headers (unparseable, malformed C-quoted escape, or invalid UTF-8). "
        "The dropped headers are in the refusing round's `diff-refused.txt` and "
        "in its `manifest.json` under `diff_filter_refusal_headers`. "
        "No model cost was incurred."
    ),
    "parse_failure": (
        "- A reviewer or synthesizer ran cleanly but its output "
        "didn't parse. The raw subprocess output is preserved at "
        "the round's `.stdout` / `.error.txt` files; the verdict "
        "may still be readable by hand."
    ),
    "config_error": (
        "- The configuration is invalid. Read the per-reviewer "
        "`.error.txt` files; common causes are unknown provider "
        "names in `[[reviewers]]` blocks."
    ),
}
"""Per-:data:`syncade.orchestrator.TerminationReason` next-steps
guidance for loop-summary.md. Mirrors the per-exit-code
:data:`_NEXT_STEPS` table but keyed on the loop-level termination
reason rather than per-round exit code."""


def _round_verdict_label(round_result) -> str:
    """Human-readable verdict label for one round."""
    if getattr(round_result, "no_changes_to_review", False):
        return "nothing to review"
    if round_result.round_exit_code == 0:
        return "SHIP"
    if round_result.round_exit_code == 30:
        return "NO-SHIP"
    if round_result.round_exit_code == 10:
        return "DECISION NEEDED"
    return f"ERROR (exit {round_result.round_exit_code})"


def _producer_commit_subject(repo_root: Path | None, ending_sha: str) -> str:
    """look up the commit subject for the
    producer's commit so the loop-summary commit series can render
    it inline.

    Returns the commit's subject line on success, or an empty
    string on any failure (missing repo_root, git not on PATH,
    ref not found, etc.). Failures degrade the rendering to the
    subject-less form rather than crashing the summary write —
    the loop-summary.md is operator-facing and best-effort is
    appropriate for the subject lookup.

    Uses :func:`syncade.process.run_subprocess` for the same
    timeout / process-group cleanup discipline the rest of
    syncade relies on. Capped at a 5-second timeout — looking up
    a commit subject is sub-second on every reasonable repo, and
    timing out is a clearer signal than blocking the summary
    write indefinitely.
    """
    if repo_root is None or not ending_sha:
        return ""
    try:
        from syncade.process import run_subprocess

        result = run_subprocess(
            ["git", "log", "-1", "--pretty=format:%s", ending_sha],
            cwd=repo_root,
            timeout=5.0,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


_EMPTY_SERIES_REASON_NOTES: dict[str, str] = {
    "ship": "- (no producer commits — round 0 shipped without needing a fix)",
    "no_changes_to_review": (
        "- (no producer commits — the diff was empty; no reviewers or producer were dispatched)"
    ),
    "producer_emptied_diff": (
        "- (see prior-round producer commits above"
        " — they reduced the reviewable change set to empty)"
    ),
    "findings_present": "- (no producer commits — single-pass run ended with findings present)",
    "max_rounds_reached": (
        "- (no producer commits landed — every producer round stalled or errored before committing)"
    ),
    "budget_exceeded": (
        "- (no producer commits — the budget was crossed before a producer round committed)"
    ),
    "provider_usage_limit": (
        "- (no producer commits — the provider's usage limit was hit before a producer round "
        "committed)"
    ),
    "producer_stalled": (
        "- (no producer commits — the producer subprocess stalled without committing on this round)"
    ),
    "producer_subprocess_error": (
        "- (no producer commits — the producer subprocess failed before it could commit)"
    ),
    "reviewer_failure": (
        "- (no producer commits — a reviewer subprocess failed before any producer round could run)"
    ),
    "synth_failure": (
        "- (no producer commits — the synthesizer subprocess failed "
        "before any producer round could run)"
    ),
    "test_subprocess_error": (
        "- (no producer commits — the test re-run subprocess failed "
        "before any producer round could run)"
    ),
    "check_subprocess_error": (
        "- (no producer commits — a blocking mechanical check's "
        "subprocess failed before any producer round could run)"
    ),
    "decision_needed": (
        "- (no producer commits — the producer escalated a finding for an "
        "operator decision instead of committing a fix)"
    ),
    "blockers_all_deactivated": (
        "- (no producer commits — the round ended at the reviewers/synthesizer "
        "stage; no producer ran)"
    ),
    "worktree_error": (
        "- (no producer commits — worktree provisioning failed before any producer round could run)"
    ),
    "diff_malformed": (
        "- (no producer commits — the diff filter refused the run before any reviewer dispatched)"
    ),
    "diff_too_large": (
        "- (no producer commits — the diff exceeded [loop] max_diff_bytes and the run was "
        "refused before any reviewer dispatched)"
    ),
    "prompt_too_large": (
        "- (no producer commits — an assembled reviewer prompt exceeded the provider ceiling "
        "and the run was refused before any reviewer dispatched)"
    ),
    "parse_failure": (
        "- (no producer commits — a reviewer / synthesizer output "
        "couldn't be parsed; no producer round ran)"
    ),
    "config_error": (
        "- (no producer commits — the configuration is invalid; no producer round ran)"
    ),
}
"""Per-:data:`TerminationReason` empty-commit-series wording."""


def _empty_commit_series_note(termination_reason: str) -> str:
    """render the empty-commit-series line for
    the loop summary based on the loop's termination reason.

    Falls back to a neutral string when the reason isn't in the
    table — defensive against a future :data:`TerminationReason`
    expansion that lands before this table is updated.
    """
    return _EMPTY_SERIES_REASON_NOTES.get(
        termination_reason,
        "- (no producer commits this run)",
    )


def _round_duration_seconds(round_result) -> float:
    """Sum the per-phase durations the round captured. Approximate
    (excludes worktree provisioning and persistence overhead) but
    gives an operator-meaningful "how long did this round take to
    review" number."""
    total = 0.0
    if round_result.dispatch_result is not None:
        total += round_result.dispatch_result.total_duration_seconds
    if round_result.synth_result is not None:
        total += round_result.synth_result.duration_seconds
    if round_result.test_result is not None:
        total += round_result.test_result.duration_seconds
    if round_result.producer_result is not None:
        total += round_result.producer_result.duration_seconds
    return total


def _run_usages(rounds: list) -> list:
    """Every model-actor ``Usage`` across all rounds (reviewers + judge + producer).

    Mirrors :func:`syncade.orchestrator.budget.round_usages` — duplicated (it is ~8 lines)
    because persistence must NOT import ``orchestrator`` (the orchestrator imports
    persistence; the reverse would cycle)."""
    usages: list = []
    for r in rounds:
        usages.extend(x.usage for x in r.dispatch_result.results if x.usage is not None)
        if r.synth_result is not None and r.synth_result.usage is not None:
            usages.append(r.synth_result.usage)
        if r.producer_result is not None and r.producer_result.usage is not None:
            usages.append(r.producer_result.usage)
    return usages


def _budget_section(
    usages: list,
    budget_tokens: int | None,
    budget_usd: float | None,
    budget_ceiling: str | None = None,
) -> list:
    """The ``## Budget`` block for a ``budget_exceeded`` run (PR-v2-11).

    ``usages`` is the loop's ENFORCEMENT tally — the exact list of ``Usage`` records the budget
    check summed. On a ``--resume`` this is the FRESH resumed-run spend, NOT the rehydrated
    original rounds (the enforcement tally starts empty on resume), so the reported number is
    the one that actually tripped the resumed budget. The configured ceiling(s) + that tally
    are rendered through :mod:`syncade.billing` so the numbers AND the money-vs-valuation
    wording are IDENTICAL to ``--metrics`` and each round's ``summary.md`` (C1: one number,
    three surfaces). ``billing.render`` supplies the lower-bound honesty when any actor's cost
    is unpriced (C4)."""
    from syncade import billing

    total_tokens = sum(u.total_tokens for u in usages)
    # Name the ceiling that actually tripped. Both budgets set → first-to-trip (tokens are
    # checked first); ``budget_ceiling`` carries over_budget's authoritative answer, so a
    # token-only crossing never mis-reports "cost" and vice versa.
    if budget_ceiling == "budget_tokens":
        crossed = "the running TOKEN tally crossed your configured `budget_tokens` ceiling"
    elif budget_ceiling == "budget_usd":
        crossed = "the running COST tally crossed your configured `budget_usd` ceiling"
    else:
        crossed = "the running tally crossed your configured budget"
    lines: list = [
        "## Budget",
        "",
        f"The loop stopped because {crossed}. It aborted at a dispatch boundary — no running "
        "provider call was interrupted — so the tally can exceed the ceiling by up to one "
        "review-bundle (reviewers + judge) or one producer, whichever was in flight when it "
        "crossed.",
        "",
        "**Configured ceiling:**",
    ]
    tok_mark = "  ← CROSSED" if budget_ceiling == "budget_tokens" else ""
    usd_mark = "  ← CROSSED" if budget_ceiling == "budget_usd" else ""
    if budget_tokens is not None:
        lines.append(
            f"- total tokens ≤ {budget_tokens:,} (`budget_tokens` — tightest bound; exact "
            f"unless an actor reported no usage){tok_mark}"
        )
    if budget_usd is not None:
        lines.append(
            f"- API-equivalent cost ≤ ${budget_usd:.4f} (`budget_usd`, a LOWER-BOUND "
            f"tally){usd_mark}"
        )
    if budget_tokens is None and budget_usd is None:
        lines.append("- (ceiling value not recorded)")
    lines += ["", f"**Tally this run:** {total_tokens:,} tokens", ""]
    lines += billing.render(billing.from_usages(usages), bullet=True)
    lines.append("")
    return lines
