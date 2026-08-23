"""Per-round producer provisioning and dispatch."""

from __future__ import annotations

from pathlib import Path

from syncade.adapters.producer import ProducerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.producer import ProducerResult, run_producer
from syncade.producer_workspace import ProducerWorkspaceManager
from syncade.prompts import (
    _NO_OPERATOR_DECISION_SENTINEL,
    _NO_PRIOR_COMMITS_SENTINEL,
    _NO_PRIOR_ROUND_SENTINEL,
)
from syncade.reviewer_workspace import ReviewerWorkspaceManager
from syncade.snapshot import Snapshot
from syncade.worktree import WorktreeManager

from .prior_round import (
    load_prior_producer_commit_subjects,
    load_prior_producer_response_text,
)
from .results import RoundResult

# Producer-repository basename. Reserved because a reviewer named ``"producer"``
# would collide with the standalone repository path when the loop provisions one.
PRODUCER_WORKTREE_NAME = "producer"


def _run_producer_phase(
    *,
    round_idx: int,
    round_result: RoundResult,
    run_id: str,
    run_dir: Path,
    round_dir: Path,
    snapshot: Snapshot,
    repo_root: Path,
    pr_doc_path: Path,
    config: SyncadeConfig,
    resolved_producer_timeout: float,
    producer_adapter: ProducerAdapter | None,
    worktree_base: Path,
    logger: Logger,
    managers_to_cleanup: list[
        WorktreeManager | ReviewerWorkspaceManager | ProducerWorkspaceManager
    ],
    operator_decision: str | None = None,
) -> tuple[ProducerResult, Path]:
    """Provision a standalone producer repository at the round's snapshot SHA and
    dispatch one producer subprocess.

    ``operator_decision`` is the recorded decision when this round resumes
    after a producer escalation. The default sentinel applies on every
    non-resumed round and is threaded straight through to
    :func:`syncade.producer.run_producer`.

    The standalone repository preserves ``CLAUDE.md`` and ``AGENTS.md``
    because the producer needs the same project context the operator has.
    It lives at
    ``<worktree_base>/<run-id>/round-N/producer-worktree/producer/``
    alongside this round's reviewer workspaces and trusted test worktree.

    Returns the :class:`ProducerResult` and its standalone repository path.
    The trusted importer needs both identities and never derives the actor
    repository from producer-supplied output.
    """
    producer_worktree_id = f"{run_id}/round-{round_idx}/producer-worktree"
    # Defer cleanup so stalled or decision-needed producer repositories stay on
    # disk for inspection. Final cleanup is decided at loop end.
    producer_manager = ProducerWorkspaceManager(
        repo_root,
        producer_worktree_id,
        base_dir=worktree_base,
        defer_cleanup=True,
    )
    managers_to_cleanup.append(producer_manager)
    with producer_manager as manager:
        producer_workspace = manager.create(snapshot.commit_sha)
        logger.event(f"  producer repository ready: {producer_workspace.path}")

        # The findings.md path the round-N persistence wrote. Must
        # exist by here — the producer only runs on NO-SHIP rounds,
        # and findings.md is written on every synth-success path.
        findings_md = round_result.artifacts.findings_md_path
        if findings_md is None:
            # Defensive: NO-SHIP rounds without findings.md
            # shouldn't reach the producer (the only NO-SHIP
            # paths are synth-blocker or test-failed; synth-blocker
            # writes findings.md, test-failed writes findings.md
            # because synth was clean).
            raise RuntimeError(
                f"producer phase reached for round {round_idx} with no "
                f"findings.md — orchestrator contract violated"
            )
        test_stdout = (
            round_result.artifacts.test_run_paths.stdout
            if (
                round_result.artifacts.test_run_paths is not None
                and round_result.test_result is not None
                and round_result.test_result.outcome == "failed"
            )
            else None
        )

        # Round 0 producer sees default prior-round/commit sentinels. Round N>0
        # producer reads its own prior response plus any prior candidate subjects
        # already imported into the operator repo; each producer repository itself
        # is fresh per round.
        #
        # The prior round's starting SHA (needed for the git log
        # range) is read from the prior round's manifest.json
        # inside ``load_prior_producer_commit_subjects`` — keeps
        # the producer-phase signature stable.
        if round_idx == 0:
            prior_output: str = _NO_PRIOR_ROUND_SENTINEL
            prior_commits: str = _NO_PRIOR_COMMITS_SENTINEL
        else:
            prior_round_dir = run_dir / f"round-{round_idx - 1}"
            # Producer.stdout already contains extracted narrative
            # text (persistence writes output.narrative_text, not the
            # raw envelope) — no per-adapter envelope-strip needed.
            # See load_prior_producer_response_text's docstring for
            # the reviewer-vs-producer asymmetry.
            prior_output = load_prior_producer_response_text(
                prior_round_dir=prior_round_dir,
            )
            prior_commits = load_prior_producer_commit_subjects(
                prior_round_dir=prior_round_dir,
                repo_root=repo_root,
            )

        result = run_producer(
            worktree_path=producer_workspace.path,
            starting_sha=snapshot.commit_sha,
            pr_doc_path=pr_doc_path,
            findings_md_path=findings_md,
            test_run_stdout_path=test_stdout,
            producer_config=config.producer,
            pricing=config.pricing,
            timeout_seconds=resolved_producer_timeout,
            round_number=round_idx,
            max_rounds=config.loop.max_rounds,
            repo_root=repo_root,
            adapter=producer_adapter,
            prior_round_output=prior_output,
            prior_round_commits=prior_commits,
            operator_decision=(
                operator_decision
                if operator_decision is not None
                else _NO_OPERATOR_DECISION_SENTINEL
            ),
            max_retries=config.retry.max_retries,
            capture_dir=round_dir,
        )
        return result, producer_workspace.path
