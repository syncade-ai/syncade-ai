"""A terminal no-change run leaves NO trace on disk — PR-h-02d item 3, independent check.

The companion suite asserts the dispatch COUNT, which is the right primary claim. This
file asserts the physical side effect instead: no worktree directory is ever created. A
counter can be satisfied by a stubbed adapter; a directory that never appears cannot, so
the two together pin "zero subprocesses" from both ends.

The control in each case is the ABSENT-base run, which must still create worktrees — it
proves the assertion can actually observe them, so an empty result means "nothing
happened" rather than "the harness was looking in the wrong place".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import syncade.orchestrator.round as round_module
from syncade.adapters.fake import FakeAdapter
from syncade.exit_codes import SUCCESS
from syncade.logging import Logger
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config
from tests.orchestrator.test_empty_diff import _init_repo

# Worktrees are torn down before run_review returns, so inspecting the filesystem
# afterwards observes nothing even on a run that provisioned two — the control below
# caught exactly that. Count PROVISIONING instead, at the class the round actually uses.


def _run(tmp_path: Path, *, base_ref: str | None, prep=None, monkeypatch=None):
    repo, pr_doc, head = _init_repo(tmp_path)
    provisioned: list[str] = []

    real_manager = round_module.WorktreeManager

    class CountingManager(real_manager):
        def __init__(self, *args, **kwargs):
            provisioned.append(kwargs.get("run_id", "?"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(round_module, "WorktreeManager", CountingManager)
    if prep is not None:
        prep(repo)
    dispatched: list[str] = []

    class Tracking(FakeAdapter):
        def build_invocation(self, reviewer_config, worktree_path, prompt):
            dispatched.append(reviewer_config.name)
            return super().build_invocation(reviewer_config, worktree_path, prompt)

    worktree_base = tmp_path / "wt"
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=_two_reviewer_config(),
        logger=Logger("quiet"),
        adapter_factory=_factory_returning(*[Tracking(canned_output=_ship()) for _ in range(2)]),
        base_ref=head if base_ref == "HEAD" else base_ref,
        worktree_base=worktree_base,
    )
    return result, dispatched, provisioned


def test_absent_base_still_provisions_worktrees_the_control(tmp_path, monkeypatch):
    """The control: without it, an empty worktree list proves nothing."""
    result, dispatched, made = _run(tmp_path, base_ref=None, monkeypatch=monkeypatch)
    assert result.termination_reason == "ship"
    assert len(dispatched) == 2
    assert made, "harness cannot observe provisioning; the assertions below would be vacuous"


def test_known_empty_creates_no_worktree_on_disk(tmp_path, monkeypatch):
    result, dispatched, made = _run(tmp_path, base_ref="HEAD", monkeypatch=monkeypatch)
    assert result.exit_code == SUCCESS
    assert result.termination_reason == "no_changes_to_review"
    assert dispatched == []
    assert made == [], f"a terminal no-change run provisioned worktrees: {made}"


def test_all_repo_context_change_creates_no_worktree(tmp_path, monkeypatch):
    """D3 case 3a: a CLAUDE.md-only change filters to empty and must not spend."""

    def only_context(repo: Path) -> None:
        (repo / "CLAUDE.md").write_text("ctx\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "ctx"], cwd=repo, check=True)

    result, dispatched, made = _run(
        tmp_path, base_ref="HEAD", prep=only_context, monkeypatch=monkeypatch
    )
    assert result.termination_reason == "no_changes_to_review"
    assert dispatched == []
    assert made == []


def test_malformed_header_passes_through_when_no_strip_targets(tmp_path):
    """Regression: unidentifiable diff headers must not refuse when strip list is empty.

    With an empty strip_repo_context_files, filter_diff_for_reviewer keeps every
    section byte-for-byte, so an undecodable header cannot hide any change from the
    reviewer. Refusing the run anyway was spurious exit 60.
    """
    from syncade.config import SyncadeConfig
    from syncade.orchestrator.loop import _diff_will_dispatch
    from syncade.snapshot import Snapshot

    # A diff header with an invalid octal escape — unidentifiable by diff_filter.
    malformed = (
        'diff --git "a/x\\3" "b/x\\3"\n--- "a/x\\3"\n+++ "b/x\\3"\n@@ -1 +1 @@\n-old\n+new\n'
    )
    snapshot = Snapshot(
        repo_root=tmp_path,
        commit_sha="a" * 40,
        branch="feature",
        base_ref="HEAD",
        base_oid="b" * 40,  # non-None so known-empty check runs
        diff_text=malformed,
        dirty_state="clean",
    )
    config_no_strip = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        review={"strip_repo_context_files": []},
    )
    assert config_no_strip.review.strip_repo_context_files == []
    # No strip targets → should NOT refuse (malformed header cannot hide anything).
    assert _diff_will_dispatch(snapshot, config_no_strip) is True

    config_with_strip = SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        review={"strip_repo_context_files": ["CLAUDE.md"]},
    )
    # With strip targets → should refuse (could be hiding a CLAUDE.md-like section).
    assert _diff_will_dispatch(snapshot, config_with_strip) is False


def test_no_changes_durable_artifacts_record_exit_zero(tmp_path, monkeypatch):
    """The durable record must say exit 0 — PR-h-02d round-1 blocker.

    The first cut persisted a private sentinel ``1`` as the round exit code. The top-level
    API was correct (exit 0, ``no_changes_to_review``) while ``round-0/manifest.json`` and
    ``summary.md`` carried ``1`` — the "record is honest" acceptance criterion failing
    behind a correct-looking API.

    This asserts the VALUE, not the rendered label. An earlier version of this test
    checked for the string "UNKNOWN" and was VACUOUS: the fix also added an
    ``if no_changes_to_review`` branch that renders ``NO_CHANGES_TO_REVIEW`` whatever the
    exit code is, so the label can never expose the sentinel. Only the number can.
    """
    import json

    result, dispatched, provisioned = _run(tmp_path, base_ref="HEAD", monkeypatch=monkeypatch)
    assert result.termination_reason == "no_changes_to_review"
    assert dispatched == [] and provisioned == []

    run_dir = result.artifacts.run_dir
    manifests = sorted(run_dir.rglob("manifest.json"))
    assert manifests, "no round manifest was written"
    for path in manifests:
        recorded = json.loads(path.read_text()).get("round_exit_code")
        assert recorded == SUCCESS, f"{path} records round_exit_code={recorded!r}, expected 0"

    for path in sorted(run_dir.rglob("summary.md")):
        text = path.read_text()
        assert "**Exit code:** 0" in text, f"{path.name} does not record exit 0:\n{text[:400]}"
