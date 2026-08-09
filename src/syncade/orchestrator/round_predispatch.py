"""Every gate a round passes BEFORE it is allowed to spend anything.

Split out of ``round.py`` (PR-h-field-01): `_run_one_round` had grown to 388 code LOC and a
reviewer flagged it in all five dogfood rounds, tying it to "coordination failures around
prompt rendering, guard ordering, and persistence". That diagnosis was right, and the
coupling is the point of this module — **every way a round can refuse before dispatching now
lives in one place, in priority order**, next to the result constructors in
``round_no_changes``.

The order is load-bearing and is the reason these belong together:

1. **unidentifiable headers** (D2) before **known-empty** (D1/D3) — a diff with both is a
   refusal, not a no-op: we could not read it, so we cannot ship it.
2. **known-empty** before the size gates — nothing to review beats too much to review.
3. **diff size** before **prompt render** — no point rendering a prompt for a diff already
   over the cap.
4. **prompt size** last, because it is the only gate that needs the rendered prompt.

Everything here runs before any worktree is provisioned, so a refusal costs nothing. That
is the whole claim: five dogfood rounds' worth of blockers were about a refusal reaching
this point correctly and being reported honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from syncade.config import SyncadeConfig
from syncade.diff_filter import (
    concealed_destinations,
    elide_binary_hunks,
    filter_diff_for_reviewer,
    unidentifiable_sections,
)
from syncade.logging import Logger
from syncade.snapshot import Snapshot

from .results import RoundResult
from .reviewer_template_failure import _reviewer_template_failure_result
from .round_no_changes import (
    _CODEX_CHAR_CEILING,
    _assembled_prompt_too_large_result,
    _diff_too_large_result,
    _fail_closed_refusal_result,
    _no_changes_to_review_result,
)


@dataclass(frozen=True)
class ReadyToDispatch:
    """The pre-dispatch gates passed; here is what dispatch needs."""

    prompt_arg: str | dict[str, str]
    pr_doc_ref: str
    pr_doc_is_out_of_repo: bool
    filtered_for_check: str


def run_predispatch_gates(
    *,
    repo_root: Path,
    snapshot: Snapshot,
    config: SyncadeConfig,
    round_idx: int,
    run_dir: Path,
    round_dir: Path,
    pr_doc_path: Path,
    started_at: datetime,
    resumed_under_drift: bool,
    logger: Logger,
    build_prompt,
) -> RoundResult | ReadyToDispatch:
    """Run every pre-dispatch gate. A :class:`RoundResult` means REFUSED; otherwise proceed.

    ``build_prompt`` is injected rather than imported to keep this module free of a cycle
    back into ``round``; it is ``round._build_reviewer_prompt``.
    """
    # --- Pre-dispatch diff checks (PR-h-02d) ------------------------
    # Both checks run before any worktree is provisioned — zero subprocesses
    # is the whole point (acceptance claim 1). Priority: D2 (unidentifiable)
    # before D1/D3 (known-empty), because a diff with BOTH conditions is a
    # refusal, not a no-changes exit (we could not read it → cannot ship it).
    # Unidentifiable headers are only a problem when strip_repo_context_files is
    # non-empty: with an empty strip list every section is kept byte-for-byte, so
    # an undecodable header cannot hide any change from the reviewer.
    _unidentifiable = (
        unidentifiable_sections(snapshot.diff_text)
        if config.review.strip_repo_context_files
        else []
    )
    if _unidentifiable:
        return _fail_closed_refusal_result(
            round_idx=round_idx,
            snapshot=snapshot,
            round_dir=round_dir,
            config=config,
            started_at=started_at,
            resumed_under_drift=resumed_under_drift,
            unidentifiable=_unidentifiable,
            logger=logger,
        )
    _filtered_for_check, _elided_binaries = elide_binary_hunks(
        filter_diff_for_reviewer(snapshot.diff_text, config.review.strip_repo_context_files)
    )
    if _elided_binaries:
        _shown = ", ".join(_elided_binaries[:5])
        _more = f", … ({len(_elided_binaries) - 5} more)" if len(_elided_binaries) > 5 else ""
        logger.event(
            f"omitted binary file content from the reviewer diff: "
            f"{len(_elided_binaries)} file(s) — {_shown}{_more}. "
            f"Paths and withheld byte counts are disclosed in the diff itself."
        )
    # A drop is only legitimate when the DESTINATION is a strip target. A boundary
    # rename (`git mv CLAUDE.md app.py`) empties the filtered diff while concealing a
    # path a reviewer must see — concluding "nothing to review" there is a false SHIP
    # over a real change (PR-h-02c.5).
    _concealed = concealed_destinations(snapshot.diff_text, config.review.strip_repo_context_files)
    if snapshot.base_oid is not None and not _filtered_for_check and not _concealed:
        # D1: base resolved + empty diff (no real changes).
        # D3: all sections were legitimate repo-context — same outcome.
        logger.event(
            "diff is empty after filtering — base resolved but no reviewable changes; "
            "terminating without dispatching reviewers (no_changes_to_review)"
        )
        return _no_changes_to_review_result(
            round_idx=round_idx,
            snapshot=snapshot,
            round_dir=round_dir,
            config=config,
            started_at=started_at,
            resumed_under_drift=resumed_under_drift,
        )

    # Diff-size ceiling (PR-h-02e D2, landed in PR-h-field-01). Measured on the REVIEWER-FACING
    # text — after stripping and binary elision — because that is what reaches the model's
    # context; capping the raw diff would threshold bytes nobody sends. Checked here, after
    # the known-empty classification and before any worktree or subprocess, so a refusal
    # costs nothing. Without it the ceiling is enforced by the provider instead: `codex exec`
    # returns `input_too_large` naming only a character count, after the operator has
    # already confirmed the run.
    _reviewed_bytes = len(_filtered_for_check.encode("utf-8"))
    if _reviewed_bytes > config.loop.max_diff_bytes:
        return _diff_too_large_result(
            round_idx=round_idx,
            snapshot=snapshot,
            round_dir=round_dir,
            config=config,
            started_at=started_at,
            resumed_under_drift=resumed_under_drift,
            diff_bytes=_reviewed_bytes,
            logger=logger,
        )

    # --- Render reviewer prompt(s) ----------------------------------
    # Template load + render runs BEFORE any worktree is provisioned. A
    # malformed operator override (`.syncade/templates/reviewer.md` with an
    # unknown placeholder) raises out of the render. Mirror the producer/synth
    # template-render contract: catch it, record the phase failure, persist the
    # round artifacts, and return a phase-failure RoundResult — rather than
    # letting the exception escape run_review with no manifest/summary written.
    try:
        prompt_arg, pr_doc_ref, pr_doc_is_out_of_repo = build_prompt(
            repo_root=repo_root,
            snapshot=snapshot,
            config=config,
            round_idx=round_idx,
            run_dir=run_dir,
            pr_doc_path=pr_doc_path,
        )
    except (KeyError, ValueError, FileNotFoundError) as exc:
        logger.event(f"reviewer template render failed: {exc}", error=True)
        return _reviewer_template_failure_result(
            round_idx=round_idx,
            snapshot=snapshot,
            round_dir=round_dir,
            config=config,
            started_at=started_at,
            resumed_under_drift=resumed_under_drift,
            error=exc,
            filtered_diff_bytes=len(_filtered_for_check.encode("utf-8")),
            raw_diff_bytes=len((snapshot.diff_text or "").encode("utf-8")),
        )

    # --- Assembled-prompt size guard --------------------------------
    # The diff-size cap ([loop] max_diff_bytes) gates on the reviewer-facing diff, but
    # the assembled prompt also includes the template and prior-round context. A verbose
    # round-0 reviewer or a large custom template can push the full prompt over the codex
    # ceiling (1,048,576 chars) while the diff alone stays under the cap. Refuse here,
    # before any worktree is provisioned, so the operator gets a useful message rather
    # than an opaque provider-side `input_too_large` error after paying for the round.
    for _reviewer_name, _prompt_text in prompt_arg.items():
        if len(_prompt_text) > _CODEX_CHAR_CEILING:
            return _assembled_prompt_too_large_result(
                round_idx=round_idx,
                snapshot=snapshot,
                round_dir=round_dir,
                config=config,
                started_at=started_at,
                resumed_under_drift=resumed_under_drift,
                reviewer_name=_reviewer_name,
                prompt_chars=len(_prompt_text),
                logger=logger,
            )

    return ReadyToDispatch(
        prompt_arg=prompt_arg,
        pr_doc_ref=pr_doc_ref,
        pr_doc_is_out_of_repo=pr_doc_is_out_of_repo,
        filtered_for_check=_filtered_for_check,
    )
