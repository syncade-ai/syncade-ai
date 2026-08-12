"""Argparse parser construction + CLI-boundary ``type`` validators.

``build_parser`` is the single source of the CLI's argument shape. The two
``type=`` validators (`_positive_float`, `_max_rounds`) mirror the corresponding
config-schema bounds at the CLI boundary.
"""

from __future__ import annotations

import argparse

from syncade import __version__
from syncade.base_resolution import VALID_SCOPES
from syncade.presets import PRESET_NAMES

from .parser_types import (  # noqa: E402
    _max_rounds,
    _non_negative_int,
    _positive_float,
    _positive_int,
    _positive_usd,
    _reviewer_override,
)


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Factored out for testability."""
    parser = argparse.ArgumentParser(
        prog="syncade",
        description=(
            "External blind multi-judge review orchestrator "
            "for AI-assisted coding. Invoked from inside Claude Code via "
            "the syncade skill."
        ),
    )
    parser.add_argument(
        "pr_doc",
        nargs="?",
        metavar="PR_DOC",
        help="Path to the PR doc that describes the work to review.",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="RUN_ID",
        help="Resume an aborted, interrupted, or decision-needed run. Pass a "
        "specific run-id (the timestamped directory name under "
        ".syncade/runs/), the literal string 'latest', or pass "
        "--resume alone (equivalent to --resume latest). Eligibility: "
        "the original run was aborted by an environment failure "
        "(exit 40/60/70), stopped at a budget ceiling or a provider usage "
        "limit (exit 25), needs an "
        "operator decision after a producer escalation (exit 10), OR was "
        "interrupted before the loop terminator wrote loop-manifest.json. "
        "Mutually exclusive with PR_DOC, "
        "--selfcheck, --auth-check, --spec-audit, --draft-spec, --base.",
    )
    parser.add_argument(
        "--force-drift",
        action="store_true",
        help="With --resume only: accept tree drift between the "
        "original run's expected snapshot SHA and the operator's "
        "current HEAD. The resumed round will snapshot from current "
        "HEAD; cross-round context from prior rounds may reference "
        "findings against a different SHA than the new tree. "
        "Without --resume, passing --force-drift is a CLI error "
        "(exit 2).",
    )
    parser.add_argument(
        "--spec-audit",
        metavar="PR_DOC",
        default=None,
        help="Audit the PR brief at PR_DOC for spec-level issues (unverified "
        "claims, internal contradictions, ambiguous acceptance criteria, "
        "missing references, scope drift, missing structural sections). "
        "Advisory-only for v1 — exits 0 on a clean brief, 10 when blocker-"
        "severity findings are present, 40 on subprocess error, 50 on config "
        "load error, 60 on path/worktree error, 70 on parse failure. "
        "Mutually exclusive with PR_DOC positional, --selfcheck, "
        "--auth-check, --draft-spec, --resume.",
    )
    parser.add_argument(
        "--repo-root",
        metavar="PATH",
        default=None,
        help="Directory to run from (default: current working directory). "
        "Used to locate .syncade/config.toml and as the starting hint for "
        "git repo-root discovery — syncade writes run artifacts to the "
        "actual repo root (git rev-parse --show-toplevel) regardless of "
        "which subdirectory this points at. Relative PR_DOC, --spec-audit, "
        "and --transcript paths resolve against the discovered repo root first, "
        "then cwd as a compatibility fallback.",
    )
    parser.add_argument(
        "--preset",
        choices=list(PRESET_NAMES),
        default=None,
        help="Start from a bundled config preset: `cheap` (single pass, no "
        "producer loop), `balanced` (the shipped defaults), or `thorough` "
        "(full rounds + double the per-subprocess timeout). Your "
        ".syncade/config.toml still layers on top (user file wins). Presets "
        "vary only rounds/timeout — never the reviewer model or effort tier.",
    )
    parser.add_argument(
        "--worktree-base",
        metavar="PATH",
        default=None,
        help="Base directory under which per-run git worktrees are created "
        "(overrides [worktree_base] in config; default /tmp/syncade). Use a "
        "fast local disk when /tmp is small or slow. Applies to review runs, "
        "--gc, --resume, and --doctor's writability preview.",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        default=None,
        help="Git ref to render the reviewer's diff against (e.g. "
        "`main`, `HEAD~3`, a tag or commit SHA). When omitted, "
        "reviewers run against the full HEAD state with no diff "
        "included in the prompt — the model decides what to review "
        "from the repo contents alone.",
    )
    parser.add_argument(
        "--scope",
        metavar="SCOPE",
        choices=list(VALID_SCOPES),
        default=None,
        help="Derive the diff base from scope instead of an explicit --base: "
        "`everything` (branch point off the default branch), `local` (your "
        "local-ahead commits vs the branch's upstream), `since-last-review` "
        "(the recorded last-reviewed SHA for this branch). Mutually exclusive "
        "with --base and --resume.",
    )
    parser.add_argument(
        "--openspec",
        nargs="?",
        const="",
        default=None,
        metavar="CHANGE_ID",
        help="Review an existing OpenSpec proposal folder instead of a PR_DOC: "
        "assemble openspec/changes/<CHANGE_ID>/ (proposal + spec deltas) into "
        "the spec the loop reviews against. Pass a proposal-id, or pass --openspec "
        "alone to auto-resolve when exactly one active proposal exists (else it "
        "lists them and asks). Reads the markdown directly — no openspec binary "
        "required. Mutually exclusive with PR_DOC, --selfcheck, --auth-check, "
        "--spec-audit, --draft-spec, --resume. --base/--scope still set the diff base.",
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=_positive_float,
        default=None,
        help="Per-subprocess timeout in seconds (must be > 0) — the fallback "
        "wall-clock cap for every leg (reviewers, judge, test, checks, producer). "
        "Overrides `[loop] timeout_seconds` in .syncade/config.toml (default 1800, "
        "i.e. 30 minutes).",
    )
    parser.add_argument(
        "--max-rounds",
        metavar="INT",
        type=_max_rounds,
        default=None,
        help="Per-run maximum rounds of (reviewers → synthesizer → "
        "optional test → producer-if-NO-SHIP). Must be in [1, 10]. "
        "Overrides `[loop] max_rounds` in .syncade/config.toml. "
        "Default 3. Set to 1 for single-pass review without producer "
        "code changes.",
    )
    parser.add_argument(
        "--budget-tokens",
        metavar="N",
        type=_positive_int,
        default=None,
        help="Per-run total-token ceiling. When the running tally of every actor's usage "
        "crosses it at a phase boundary, the loop aborts gracefully (budget_exceeded). This "
        "is the TIGHTEST bound — exact when all actors report usage, a lower bound only if an "
        "actor reports none. Overrides `[loop] budget_tokens`. "
        "Default: no token ceiling (only --max-rounds and --timeout bound a run).",
    )
    parser.add_argument(
        "--budget-usd",
        metavar="USD",
        type=_positive_usd,
        default=None,
        help="Per-run cost ceiling on the API-EQUIVALENT valuation (NOT billed money — the "
        "marginal dollar is $0 on a subscription), matching what `syncade --doctor` previews. "
        "A LOWER-BOUND tally: actors with incomplete cost are uncounted, so a dollar-budgeted "
        "run can overshoot — use --budget-tokens for the tighter cap. Overrides `[loop] "
        "budget_usd`. Default: no cost ceiling.",
    )
    parser.add_argument(
        "--reviewer-model",
        action="append",
        metavar="NAME=MODEL",
        type=_reviewer_override,
        default=None,
        help="Override ONE reviewer's model for this run: NAME is a reviewer's `name` in "
        ".syncade/config.toml. Repeatable (once per reviewer). An unknown NAME fails exit 50 "
        "naming the configured reviewers. E.g. --reviewer-model codex-reviewer-adv=gpt-5.6-sol.",
    )
    parser.add_argument(
        "--reviewer-thinking",
        action="append",
        metavar="NAME=TIER",
        type=_reviewer_override,
        default=None,
        help="Override ONE reviewer's thinking/effort tier for this run (the tiers accepted by "
        "`[reviewers] thinking`). NAME is a reviewer's `name`; repeatable. Bad TIER or unknown "
        "NAME fails exit 50.",
    )
    parser.add_argument(
        "--reviewer-timeout",
        action="append",
        metavar="NAME=SECONDS",
        type=_reviewer_override,
        default=None,
        help="Override ONE reviewer's wall-clock timeout for this run (seconds, > 0). NAME is a "
        "reviewer's `name`; repeatable. Overrides that reviewer's `timeout_seconds` (else the "
        "loop timeout). Bad value or unknown NAME fails exit 50.",
    )
    parser.add_argument(
        "--two-dot",
        action="store_true",
        help="Diff the literal <base>..HEAD range instead of from the branch "
        "point (the default, equivalent to git's <base>...HEAD). Use when you "
        "want everything between the two commits. WARNING: if your branch is "
        "behind its base, commits that landed on the base but not on your "
        "branch appear as DELETIONS in the reviewed diff, and the producer is "
        "handed those phantom deletions as work.",
    )
    parser.add_argument(
        "--force-dirty",
        action="store_true",
        help="Allow loop mode (max_rounds > 1) to start even when "
        "the working tree has tracked-modified files. WARNING: the "
        "producer will commit on top of the operator's WIP, which "
        "may interleave with their uncommitted edits in confusing "
        "ways. Use only when you understand the consequences. The "
        "same refusal applies to a resumed loop-mode run (--resume), "
        "where --force-dirty is likewise the only escape. "
        "max_rounds=1 (single-pass) bypasses this guard entirely.",
    )
    parser.add_argument(
        "--allow-default-branch",
        action="store_true",
        help="Allow loop mode (max_rounds > 1) to run while HEAD is the repo's "
        "default branch. By default syncade REFUSES this, because the producer "
        "fast-forwards the current branch and would land commits directly on "
        "your default branch. Pass this to commit there deliberately. "
        "Single-pass (max_rounds=1) commits nothing and bypasses the guard.",
    )
    parser.add_argument(
        "--allow-auto-init",
        action="store_true",
        help="Let syncade `git init` + baseline-commit a directory that ALREADY HAS "
        "FILES. Refused by default: that commit captures whatever it finds, and the "
        "exclusions are defeatable (a key under an unknown name; your own `!` negation). "
        "ALSO: if the run is then refused, the repository is LEFT BEHIND — syncade cannot "
        "tell which files are its own in a directory that was not empty, so it deletes "
        "nothing. Remove it by hand if you do not want it. An empty directory needs no "
        "flag: a refused run there removes the repository it created. It leaves the starter "
        "`.gitignore` (syncade never deletes bytes you could have edited) and any "
        "`.syncade/` run record (run history is never deleted).",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Verify the configured producer can headless-commit. Provisions "
        "a tmp_path git repo + stub findings.md, runs the producer once, "
        "asserts HEAD moved + the requested edit is present. Useful after a "
        "claude/codex CLI update to detect sandbox-semantics drift before a "
        "real loop hits it. Mutually exclusive with PR_DOC, --auth-check, "
        "--spec-audit, --draft-spec, --resume, --openspec, --gc.",
    )
    parser.add_argument(
        "--auth-check",
        action="store_true",
        help="Verify every configured credential can authenticate. Faster "
        "than --selfcheck (~5-10s total vs ~30s/producer); covers the "
        "common 'did my OAuth token rotate?' diagnostic. Mutually "
        "exclusive with PR_DOC, --selfcheck, --resume, --spec-audit, --draft-spec.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Preflight a run without dispatching one. Read-only: prints a green/red "
        "table of readiness checks (resolved config; each configured provider's CLI on "
        "PATH; worktree root + disk; branch/dirty-tree refusal preview; run-plan + cost "
        "preview; credential auth; producer headless-commit) and exits 0 iff every check is "
        "green, else 60. Mutates nothing. The auth + producer-commit legs make real provider "
        "calls (~30s) and are skipped when a cheap check already reds — pass --quick to skip "
        "them outright. Unlike the other one-shot modes it ACCEPTS --base/--scope (it previews "
        "that diff). Mutually exclusive with PR_DOC, --selfcheck, --auth-check, --spec-audit, "
        "--draft-spec, --resume, --openspec, --gc, --metrics.",
    )
    # --quick / --quiet share the prefix `--qui`, so that bare abbreviation is ambiguous
    # (argparse errors). Both FULL spellings parse fine and that is what everything uses; the
    # tiny abbreviation collision is the accepted cost of matching the brief's `--quick`.
    parser.add_argument(
        "--quick",
        action="store_true",
        help="With --doctor only: skip the two LIVE legs (the auth credential probe and the "
        "producer headless-commit smoke), which make real provider calls and take ~30s. "
        "Leaves the instant config / PATH / worktree / branch / plan / cost checks. The "
        "skipped legs are reported as 'skipped', never as passed.",
    )
    parser.add_argument(
        "--install-skill",
        nargs="?",
        const="all",
        choices=["all", "claude", "codex"],
        default=None,
        help="Install the bundled /syncade Agent Skill into your harness skill "
        "directories (~/.claude/skills and $CODEX_HOME/skills), then exit. Works from a "
        "pip install (no checkout needed). Default target 'all'; pass 'claude' or 'codex' "
        "to install just one. REFUSES (exit 60) if the destination holds files syncade did "
        "not write — an edited SKILL.md, or anything else you put there — naming each one "
        "instead of destroying it.",
    )
    parser.add_argument(
        "--force-install",
        action="store_true",
        help="With --install-skill only: overwrite a destination that holds files syncade "
        "did not write. The casualties are still listed; this is informed consent, not a "
        "safe mode. Without --install-skill, passing it is a CLI error (exit 2).",
    )
    parser.add_argument(
        "--config",
        nargs="*",
        default=None,
        metavar="ARG",
        help="Inspect or edit configuration, then exit. `--config` (no verb) opens an interactive "
        "arrow-key menu. `--config list` shows the effective settings and which layer "
        "(default/global/repo) set each; `--config get <key>` prints one value. "
        "`--config set <key> <value>` writes the global ~/.syncade/config.toml (or the "
        "repo's with --repo).",
    )
    parser.add_argument(
        "--repo",
        action="store_true",
        help="With `--config set`, target the current repo's .syncade/config.toml instead of the "
        "global ~/.syncade/config.toml.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="With `--config list`, show the FULL settable surface — every actor/section field, "
        "including advanced retry/gc/checks and CLI-only knobs — not just the curated common set.",
    )
    parser.add_argument(
        "--draft-spec",
        action="store_true",
        help="Manufacture a ratifiable spec from a session transcript. Requires "
        "--transcript. Reads the transcript for INTENT + the diff "
        "(via --base/--scope, optional), runs a cold drafter, and writes an "
        "OpenSpec-shaped .syncade/draft-spec-<session>[-N].md with an "
        "'Assumptions to confirm' section. Advisory — review/edit it, then run "
        "`syncade <file>`. "
        "Mutually exclusive with PR_DOC, --selfcheck, --auth-check, --spec-audit, "
        "--resume, --openspec.",
    )
    parser.add_argument(
        "--transcript",
        metavar="PATH",
        default=None,
        help="Path to a Claude Code session JSONL transcript. Required with "
        "--draft-spec; the cold drafter reads it for intent.",
    )
    parser.add_argument(
        "--gc",
        action="store_true",
        help="Run a one-shot maintenance pass. Retention is TWO-TIER: run "
        "history is kept FOREVER (loop-manifest.json, findings.md, run-init.json, "
        "round manifests, summaries) and only the bulky subprocess transcripts "
        "(round-*/*.stdout, *.stderr - 90%% of the corpus) are pruned, for runs "
        "beyond --gc-keep and any --gc-max-age-days floor. NO run directory is "
        "ever deleted: .syncade/metrics.db is a derived view over .syncade/runs/, "
        "so deleting a run would destroy its history the next time that view "
        "rebuilds. Also removes matching <worktree_base>/<run-id>/ worktree leftovers "
        "whose directory identity still matches the GC plan, and safely reaps "
        "orphaned reviewer/producer subprocesses whose working dir is INSIDE a "
        "worktree being removed. Runs also auto-prune their transcripts at the "
        "start of every fresh loop, so --gc is for worktree/process cleanup and "
        "one-off maintenance rather than routine disk hygiene. Resume-eligible "
        "runs are ALWAYS protected (they keep even their transcripts); non-run "
        "state (config.toml, last-reviewed.json, draft-spec-*.md) is never "
        "touched. "
        "Mutually exclusive with PR_DOC, --selfcheck, --auth-check, "
        "--spec-audit, --draft-spec, --resume, --openspec.",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="Aggregate the .syncade/runs/ artifact corpus into "
        ".syncade/metrics.db (read-only over the artifacts) and print a "
        "cumulative report: run count, ship-rate, blockers/minors/nits, rounds, "
        "handoffs, and per-model reviewer stats. Rebuildable + idempotent. "
        "Mutually exclusive with PR_DOC, --selfcheck, --auth-check, --spec-audit, "
        "--draft-spec, --resume, --openspec, --gc.",
    )
    parser.add_argument(
        "--metrics-last",
        metavar="N",
        type=_non_negative_int,
        default=None,
        help="With --metrics only: also print a billed/API-equivalent breakdown scoped to the N "
        "most recent runs (e.g. --metrics-last 20).",
    )
    parser.add_argument(
        "--gc-keep",
        metavar="N",
        type=_non_negative_int,
        default=None,
        help="With --gc only: number of most-recent (non-protected) runs whose "
        "TRANSCRIPTS are kept; older runs have their transcripts pruned (their "
        "history is kept either way). Default 20. (Passing this WITHOUT --gc is "
        "an error — it is meaningful only with --gc.)",
    )
    parser.add_argument(
        "--gc-max-age-days",
        metavar="D",
        type=_non_negative_int,
        default=None,
        help="With --gc only: optional age floor in days. 0 (default) "
        "disables the age gate (prune transcripts for every candidate beyond "
        "--gc-keep); D>0 only prunes a beyond-keep candidate that is ALSO older "
        "than D days. (Passing this WITHOUT --gc is an error.)",
    )
    parser.add_argument(
        "--gc-dry-run",
        action="store_true",
        help="With --gc only: report exactly what would be pruned/removed/reaped "
        "(including the bytes that would be freed) and modify NOTHING on disk "
        "(prune nothing, delete nothing, kill nothing).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress phase-level progress output; print only the "
        "final summary line, plus exit-70 artifact pointers and any "
        "error messages.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"syncade {__version__}",
    )
    return parser
