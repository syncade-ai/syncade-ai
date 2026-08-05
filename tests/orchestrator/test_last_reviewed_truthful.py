"""last-reviewed records a SHA a reviewer saw — PR-h-02e, audit rank 19's R6.

Finalization used to re-read ``refs/heads/<branch>`` and persist whatever the ref pointed
at when the run ended. That equals the reviewed SHA only because the TERMINAL round never
runs a producer (``loop_round_step`` special-cases ``round_idx == max_rounds - 1``). Measured
across every run in ``.syncade/runs/``: 1 of 336 had a final round whose producer committed,
and that one diverged because a commit landed on the branch mid-run.

So the reachable shape is an EXTERNAL commit during the run, which this test injects at a
real point inside ``_finalize_run`` — after the rounds are done, before last-reviewed is
written. Recording an unreviewed SHA makes ``--scope since-last-review`` bound the next diff
from work nobody reviewed, silently skipping it.

Also guards a trap in the fix: the ``snapshot`` PARAMETER reaching ``_finalize_run`` is
round 0's, not the final round's, and they differ on any multi-round run. Using it would
record the wrong SHA — worse than the bug being fixed.

Two additional cases (round-0 blockers):
- ``no_changes_to_review`` and ``producer_emptied_diff`` exit 0 with zero reviewers
  dispatched; recording the SHA would anchor the next ``since-last-review`` at an
  unreviewed point, silently skipping the work.
- OSError in ``persist_last_reviewed`` must be visible even under ``--quiet``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.logging import Logger
from syncade.orchestrator import loop_finalize, run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config
from tests.orchestrator.test_empty_diff import _init_repo

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_external_commit_during_run_is_not_recorded_as_reviewed(tmp_path, monkeypatch):
    repo, pr_doc, head = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "the work under review")
    base = head
    reviewed = _git(repo, "rev-parse", "HEAD")

    # Inject the external commit at a real point in _finalize_run: persist_loop_manifest
    # runs after the rounds and before the last-reviewed block.
    real_manifest = loop_finalize.persist_loop_manifest

    def _commit_then_delegate(*args, **kwargs):
        if _git(repo, "rev-parse", "HEAD") == reviewed:
            (repo / "unrelated.py").write_text("y = 2\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "landed mid-run, never reviewed")
        return real_manifest(*args, **kwargs)

    monkeypatch.setattr(loop_finalize, "persist_loop_manifest", _commit_then_delegate)

    recorded: dict = {}
    import syncade.persistence as persistence

    real_persist = persistence.persist_last_reviewed

    def _capture(repo_root, *, branch, sha, run_id, recorded_at_utc):
        recorded["sha"] = sha
        return real_persist(
            repo_root, branch=branch, sha=sha, run_id=run_id, recorded_at_utc=recorded_at_utc
        )

    monkeypatch.setattr(persistence, "persist_last_reviewed", _capture)

    run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[FakeAdapter(canned_output=_ship()) for _ in range(2)]),
        base_ref=base,
        worktree_base=tmp_path / "wt",
    )

    tip = _git(repo, "rev-parse", "HEAD")
    assert tip != reviewed, "injection did not move the ref; the test would be vacuous"
    assert recorded.get("sha") == reviewed, (
        f"recorded {recorded.get('sha')!r} — expected the REVIEWED sha {reviewed!r}, "
        f"not the mid-run tip {tip!r}"
    )


def test_multi_round_records_the_FINAL_rounds_sha_not_round_zeros(tmp_path):
    """The round-0 trap, which the single-round case above cannot see.

    ``_finalize_run`` receives ``snapshot`` (round 0's) AND ``round_results``, whose last
    entry carries the final round's. On a multi-round run they differ, so reading the
    parameter would persist a SHA that was reviewed but is no longer current — a different
    wrong answer from the one being fixed, and invisible to any single-round test.
    """
    from syncade.adapters.fake_producer_audit_draft import FakeProducerAdapter
    from syncade.config import SyncadeConfig
    from tests.orchestrator._helpers import (
        _no_ship,
        _RoundCyclingSynth,
        _synth_clean,
        _synth_with_blocker,
    )

    repo, pr_doc, head = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")
    base = head
    round0_sha = _git(repo, "rev-parse", "HEAD")

    config = SyncadeConfig.model_validate(
        {
            "loop": {"max_rounds": 2},
            "reviewers": [
                {"name": "rv1", "provider": "anthropic", "model": "m"},
                {"name": "rv2", "provider": "anthropic", "model": "m"},
            ],
        }
    )
    recorded: dict = {}
    import syncade.persistence as persistence

    real_persist = persistence.persist_last_reviewed
    try:
        persistence.persist_last_reviewed = lambda repo_root, *, branch, sha, **kw: recorded.update(
            sha=sha
        )
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            logger=Logger("quiet"),
            adapter_factory=_factory_returning(
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_no_ship()),
                FakeAdapter(canned_output=_ship()),
                FakeAdapter(canned_output=_ship()),
            ),
            synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
            producer_adapter=FakeProducerAdapter(commit_message="fix: round 0"),
            base_ref=base,
            worktree_base=tmp_path / "wt",
        )
    finally:
        persistence.persist_last_reviewed = real_persist

    final_sha = _git(repo, "rev-parse", "HEAD")
    assert final_sha != round0_sha, "producer did not advance the branch; test is vacuous"
    assert recorded.get("sha") == final_sha, (
        f"recorded {recorded.get('sha')!r} — expected the FINAL round's sha {final_sha!r}, "
        f"not round 0's {round0_sha!r}"
    )


def test_no_changes_to_review_does_not_record_last_reviewed(tmp_path, monkeypatch):
    """no_changes_to_review exits 0 with zero reviewers; must not write an unreviewed anchor.

    The pre-fix code recorded HEAD on any exit 0/20/30 regardless of whether any reviewer
    ran. base_ref=HEAD produces this terminal on a base that coincides with HEAD.
    """
    import syncade.persistence as persistence

    repo, pr_doc, head = _init_repo(tmp_path)

    recorded: list = []
    monkeypatch.setattr(
        persistence,
        "persist_last_reviewed",
        lambda *a, **kw: recorded.append(kw.get("sha")),
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[FakeAdapter(canned_output=_ship()) for _ in range(2)]),
        base_ref=head,  # diff against HEAD → empty → no_changes_to_review
        worktree_base=tmp_path / "wt",
    )

    assert result.exit_code == 0
    assert result.termination_reason == "no_changes_to_review"
    assert recorded == [], (
        f"persist_last_reviewed was called with {recorded} — must not record on "
        "no_changes_to_review (no reviewer ran)"
    )


def test_persistence_oserror_visible_under_quiet(tmp_path, monkeypatch, capsys):
    """An OSError from persist_last_reviewed must appear on stderr even with --quiet.

    The pre-fix code used logger.warning, which is suppressed in quiet mode. The fix
    prints directly to stderr so the operator always sees the failure.
    """
    import syncade.persistence as persistence

    repo, pr_doc, head = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "work"], cwd=repo, check=True)

    monkeypatch.setattr(
        persistence,
        "persist_last_reviewed",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[FakeAdapter(canned_output=_ship()) for _ in range(2)]),
        base_ref=head,
        worktree_base=tmp_path / "wt",
    )

    assert result.exit_code == 0  # OSError must not abort the run
    captured = capsys.readouterr()
    assert "disk full" in captured.err, (
        f"OSError message not in stderr under --quiet; got: {captured.err!r}"
    )
