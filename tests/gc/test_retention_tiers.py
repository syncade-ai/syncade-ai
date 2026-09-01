"""PR-h-12 item 1 — GC's real targets must match the four documented tiers.

`CLAUDE.md` now states the policy as four tiers: run history is never deleted, transcripts are
pruned beyond the keep window, worktrees go once a run can no longer plausibly be resumed, and
the derived view is droppable. Before that the behaviour was emergent rather than chosen, and
the emergent version cost 4.4 GB.

**This file is a PIN plus a BEHAVIOURAL TWIN, deliberately.** The `_UNDO_TARGETS` pin in
`tests/cli/test_undo_scope.py` records why one is not enough: a dogfood producer widened the
pinned body and updated the pin in the SAME commit, and the suite stayed green over data loss
until a human read it. A pin makes a change deliberate; it cannot make it correct. So the
structural assertions below are paired with a real corpus that proves what survives.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from syncade.gc import execute_gc, plan_gc
from syncade.gc_types import GcPlan

#: What a GC PLAN is allowed to name for destruction, mapped to its tier. The point of pinning
#: this set is that tier 1 has no entry: a plan cannot even EXPRESS deleting a run directory, so
#: adding that capability means adding a field here and failing this test in front of a person.
_DESTRUCTIVE_PLAN_FIELDS = {
    "runs_to_slim": "tier 2 — transcripts inside a run; the run directory survives",
    "worktree_trees_to_remove": "tier 3 — worktrees, reconstructible from a recorded SHA",
    "orphan_worktree_trees": "tier 3 — worktrees whose run directory is already gone",
}
#: Non-destructive bookkeeping a plan may also carry.
#:
#: ``worktree_age_released`` is here rather than above deliberately, and the distinction is the
#: point of the pin: it names RUN-IDS whose tier 3 has aged out of protection, not paths to
#: delete — `worktree_trees_to_remove` is still the only thing that names a tree. It ENABLES
#: destruction without describing any, and it must never release tiers 1 or 2, which
#: `test_the_age_release_frees_only_worktrees` asserts behaviourally.
#: The ``unclaimable_*`` fields are inert for the opposite reason to
#: ``worktree_age_released``: they name PATHS, but as a NOTICE rather than a target. These trees
#: are either recordless syncade-shaped workspaces (which need manual removal) or unreadable
#: known-run roots (which may become classifiable after inspection succeeds). GC must not remove
#: either from this plan because neither has positive ownership proof at planning time.
#: `test_gc_never_removes_an_unclaimable_tree` asserts that behaviourally — a pin alone
#: could not, since the danger is a future change quietly feeding this list to the executor.
_INERT_PLAN_FIELDS = {
    "protected_run_ids",
    "worktree_tree_identities",
    "worktree_age_released",
    "unclaimable_recordless_trees",
    "unclaimable_unreadable_trees",
    "unclaimable_bytes",
}


def test_a_gc_plan_cannot_express_deleting_a_run_directory() -> None:
    """Tier 1 is enforced by the TYPE, not by remembering not to do it.

    If a future change wants GC to remove a run directory it must add a field here, which fails
    this test — putting a human in front of the one rule `metrics.db` depends on, since that view
    drop-and-recreates itself from `.syncade/runs/` and would lose the history it rebuilds from.
    """
    fields = {f.name for f in dataclasses.fields(GcPlan)}
    assert fields == set(_DESTRUCTIVE_PLAN_FIELDS) | _INERT_PLAN_FIELDS, (
        "GcPlan's fields changed. Every destructive field must map to a documented tier in "
        "CLAUDE.md. A field naming run directories for deletion would break tier 1, which is "
        "load-bearing: metrics.db rebuilds from that tree."
    )


def _corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A repo with one old, COMPLETED run: structured artifacts and transcripts.

    `run-init.json` plus a `loop-manifest.json` carrying a non-resumable `final_exit_code` are
    what make a run unprotected — `gc_should_conservatively_protect` fails CLOSED on a missing or
    unreadable either. An earlier version of this fixture omitted both and was silently protected,
    which is worth recording twice over: it is the fail-closed default working, and it is item 2's
    complaint arriving unprompted, since a run dated 2020 was still shielded from GC.
    """
    repo = tmp_path / "repo"
    run = repo / ".syncade" / "runs" / "2020-01-01T00-00-00" / "round-0"
    run.mkdir(parents=True)
    # tier 1 — structured artifacts
    (run / "manifest.json").write_text("{}")
    (run / "findings.md").write_text("# findings\n")
    (run.parent / "run-init.json").write_text("{}")
    (run.parent / "loop-manifest.json").write_text('{"final_exit_code": 0}')  # SHIP: not resumable
    # tier 2 — transcripts
    (run / "codex-reviewer.stdout").write_text("x" * 4096)
    (run / "codex-reviewer.stderr").write_text("y" * 4096)
    # non-run state, which is not a tier at all and must never be touched
    (repo / ".syncade" / "config.toml").write_text("[loop]\n")
    (repo / ".syncade" / "last-reviewed.json").write_text("{}")
    return repo, run.parent, run


def test_slimming_removes_transcripts_and_nothing_else(tmp_path: Path) -> None:
    """The behavioural twin for tiers 1 and 2.

    A pin over `GcPlan` says a run directory cannot be NAMED for deletion. This says the
    execution path does not delete one anyway — including the structured artifacts inside it and
    the non-run state beside it.
    """
    repo, run_dir, round_dir = _corpus(tmp_path)
    plan = plan_gc(repo, keep=0, max_age_days=0, skip_worktrees=True)
    assert run_dir.name in plan.runs_to_slim, "an old run should be slimmable"

    execute_gc(plan, dry_run=False, repo_root=repo)

    assert not (round_dir / "codex-reviewer.stdout").exists(), "tier 2 should be pruned"
    assert not (round_dir / "codex-reviewer.stderr").exists(), "tier 2 should be pruned"
    for survivor in (
        run_dir,
        run_dir / "loop-manifest.json",
        round_dir / "manifest.json",
        round_dir / "findings.md",
        repo / ".syncade" / "config.toml",
        repo / ".syncade" / "last-reviewed.json",
    ):
        assert survivor.exists(), f"GC deleted something outside tier 2: {survivor}"


def test_a_dry_run_removes_nothing_at_all(tmp_path: Path) -> None:
    """`--gc-dry-run` is how an operator inspects the policy before trusting it. If it deletes
    while reporting, the tiers are unverifiable by the person they exist to protect."""
    repo, run_dir, round_dir = _corpus(tmp_path)
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))

    plan = plan_gc(repo, keep=0, max_age_days=0, skip_worktrees=True)
    execute_gc(plan, dry_run=True, repo_root=repo)

    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before
