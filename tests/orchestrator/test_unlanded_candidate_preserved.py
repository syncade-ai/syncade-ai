"""A candidate that did not land must survive the terminal cleanup.

The terminal preserve-vs-clean decision was keyed on the exit code alone: exits 10/20/30 keep
actor workspaces, everything else cleans them. A trusted-import failure exits **60**, so a run
that committed a real candidate, failed to import it, and printed "inspect ... the preserved
producer repository" then DELETED that repository on its way out. The objects never crossed into
the operator store, so nothing else in the world could reproduce them.

The fix is derived from the retention policy rather than bolted onto the exit-code list. Tier 3
permits removing an actor workspace *because* it is reconstructible from a recorded SHA — which is
true exactly when the import succeeded and the objects sit under a recovery ref. When the import
failed that premise is false, so the deletion must not happen. Adding `60` to the list would have
been the enumeration answer, and wrong in both directions: it would preserve a genuine
provisioning failure that holds nothing, and still lose a candidate on any future exit code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import syncade.orchestrator.loop_round_step as loop_round_step
from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.producer_import import CandidateImportResult
from syncade.producer_result import candidate_disposition
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)


def _config() -> SyncadeConfig:
    return SyncadeConfig.model_validate(
        {
            "reviewers": [
                {"name": "rv1", "provider": "openai", "model": "gpt-5.5"},
                {"name": "rv2", "provider": "openai", "model": "gpt-5.5"},
            ],
            "loop": {"max_rounds": 2},
        }
    )


def _worktree_base() -> Path:
    """The per-test base that `_isolated_worktree_base` (tests/conftest.py) pins the
    config default to."""
    return Path(SyncadeConfig().worktree_base)


def _run(repo: Path, pr_doc: Path, *, quiet: bool = False):
    return run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        logger=Logger("quiet") if quiet else None,
        config=_config(),
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
        producer_adapter=FakeProducerAdapter(commit_message="candidate"),
    )


def _producer_repos(worktree_base: Path) -> list[Path]:
    return sorted(worktree_base.rglob("producer-worktree/producer"))


@pytest.mark.parametrize("status", ["error", "invalid"])
def test_a_failed_import_keeps_the_only_copy_of_the_candidate(
    repo_with_pr_doc, monkeypatch, tmp_path, status
):
    """Both non-imported outcomes leave the actor store as the sole holder of the work.

    Only ``error`` was the regression: it maps to exit 60, outside the preserve set. ``invalid``
    maps to `producer_stalled` / exit 30, which was already preserved — it is here as the guard
    that the new rule did not disturb the path that already worked, since a fix keyed on
    "a candidate exists" rather than "a candidate is unrecoverable" would change both.
    """
    repo, pr_doc = repo_with_pr_doc
    monkeypatch.setattr(
        loop_round_step,
        "import_producer_candidate",
        lambda **_: CandidateImportResult(status=status, error="injected import failure"),
    )

    result = _run(repo, pr_doc)

    assert result.rounds[0].producer_result.outcome == "committed"
    assert result.rounds[0].producer_result.candidate_import.status == status
    surviving = _producer_repos(_worktree_base())
    assert surviving, "the candidate's only copy was deleted by the terminal cleanup"
    # And it really is the candidate, not an empty directory.
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=surviving[0],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == result.rounds[0].producer_result.ending_sha


def test_error_with_recovery_ref_does_not_preserve_workspace(repo_with_pr_doc, monkeypatch):
    """An error result that carries a recovery_ref means the candidate IS in the operator repo.

    ``import_producer_candidate`` returns ``status='error'`` with a populated ``recovery_ref``
    when the candidate was imported and recovery-anchored but quarantine cleanup subsequently
    failed. In that state the candidate is reconstructible from the operator repository, so the
    standalone producer workspace must NOT be preserved as the "only copy" — it would be
    incorrect and cause the false preservation this function guards against.
    """
    repo, pr_doc = repo_with_pr_doc
    monkeypatch.setattr(
        loop_round_step,
        "import_producer_candidate",
        lambda **_: CandidateImportResult(
            status="error",
            error="quarantine cleanup failed after anchoring",
            recovery_ref="refs/syncade/recovery/test-run/round-0/abc123",
        ),
    )

    result = _run(repo, pr_doc)

    assert result.rounds[0].producer_result.outcome == "committed"
    import_result = result.rounds[0].producer_result.candidate_import
    assert import_result.status == "error"
    assert import_result.recovery_ref is not None
    surviving = _producer_repos(_worktree_base())
    assert not surviving, (
        "error+recovery_ref means the candidate is in the operator repo; "
        "preserving the standalone workspace treats it as the only copy when it is not"
    )


def test_a_successful_import_still_cleans_up(repo_with_pr_doc):
    """The preserve rule must stay narrow: an imported candidate IS reconstructible.

    Without this, "preserve when a candidate exists" would preserve on every producer run and
    quietly reintroduce the multi-gigabyte accumulation tier 3 exists to bound.
    """
    repo, pr_doc = repo_with_pr_doc
    result = _run(repo, pr_doc)

    assert result.rounds[0].producer_result.candidate_import.status == "imported"
    assert result.exit_code == 0, "this fixture should reach a clean SHIP"
    assert _producer_repos(_worktree_base()) == []


def test_subprocess_error_with_moved_head_is_treated_as_unlanded():
    """A subprocess_error after HEAD moved is an unreconstructible producer commit.

    The producer subprocess failed (timeout, crash) after it had already committed. Those
    objects were never trusted-imported and have no recovery ref, so the standalone producer
    repository is the ONLY copy, so the shared disposition must say so and stop the terminal
    cleanup from deleting it.
    """
    producer_result = SimpleNamespace(
        outcome="subprocess_error",
        starting_sha="aaa" * 13 + "a",
        ending_sha="bbb" * 13 + "b",
        candidate_import=None,
    )
    round_result = SimpleNamespace(producer_result=producer_result)
    assert candidate_disposition([round_result]).workspace_is_only_copy, (
        "subprocess_error with moved HEAD must be treated as an unlanded candidate "
        "— the only copy lives in the standalone producer repository"
    )


def test_subprocess_error_without_moved_head_is_not_unlanded():
    """A subprocess_error that never moved HEAD left nothing to preserve.

    The producer failed before committing anything; there is no candidate object
    anywhere so the shared disposition must NOT report the workspace as the only copy.
    """
    sha = "aaa" * 13 + "a"
    producer_result = SimpleNamespace(
        outcome="subprocess_error",
        starting_sha=sha,
        ending_sha=sha,
        candidate_import=None,
    )
    round_result = SimpleNamespace(producer_result=producer_result)
    assert not candidate_disposition([round_result]).workspace_is_only_copy, (
        "subprocess_error with no HEAD movement means nothing was committed; "
        "there is nothing to preserve"
    )


def test_the_operator_is_told_where_the_only_copy_is_even_under_quiet(
    repo_with_pr_doc, monkeypatch, capsys
):
    """Preserving the store silently would be half a fix.

    `tests/test_safety_disclosures_survive_quiet.py` pins the CALL SITE, and this carries the
    same claim behaviourally — because a pin alone is exactly what this repo has already watched
    a producer defeat by widening the pinned body and re-pinning it in one commit.
    """
    repo, pr_doc = repo_with_pr_doc
    monkeypatch.setattr(
        loop_round_step,
        "import_producer_candidate",
        lambda **_: CandidateImportResult(status="error", error="injected import failure"),
    )

    _run(repo, pr_doc, quiet=True)

    err = capsys.readouterr().err
    assert "did NOT reach this repository" in err, "the notice was suppressed by --quiet"
    assert str(_worktree_base()) in err, "the notice must name where the only copy lives"
