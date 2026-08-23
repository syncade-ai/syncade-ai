from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import syncade.producer_import as producer_import_module
from syncade.adapters.fake import FakeAdapter, FakeProducerAdapter
from syncade.config import SyncadeConfig
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.branch_advance import _advance_branch_ref
from syncade.producer import ProducerResult
from syncade.producer_import import import_producer_candidate
from syncade.producer_workspace import ProducerWorkspaceManager
from syncade.snapshot import take_snapshot
from tests.orchestrator._helpers import (
    _factory_returning,
    _no_ship,
    _RoundCyclingSynth,
    _ship,
    _synth_clean,
    _synth_with_blocker,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _operator_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "operator"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.txt").write_text("start\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "start")
    return repo, _git(repo, "rev-parse", "HEAD")


def _actor_repo(tmp_path: Path, operator: Path, starting_sha: str) -> Path:
    manager = ProducerWorkspaceManager(
        operator,
        "candidate",
        base_dir=tmp_path / "actors",
        defer_cleanup=True,
    )
    return manager.create(starting_sha).path


def _commit(actor: Path, text: str, message: str) -> str:
    (actor / "tracked.txt").write_text(text)
    _git(actor, "add", "tracked.txt")
    _git(actor, "commit", "-qm", message)
    return _git(actor, "rev-parse", "HEAD")


@pytest.mark.parametrize("count", [1, 3])
def test_linear_candidate_imports_and_recovery_ref_is_idempotent(tmp_path, count):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = start
    for index in range(count):
        ending = _commit(actor, f"change {index}\n", f"change {index}")

    before = _git(operator, "rev-parse", "refs/heads/work")
    first = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-1",
        round_idx=0,
    )
    second = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-1",
        round_idx=0,
    )

    assert first.status == second.status == "imported"
    assert first.recovery_ref == second.recovery_ref
    assert _git(operator, "rev-parse", first.recovery_ref or "") == ending
    assert _git(operator, "rev-parse", "refs/heads/work") == before
    assert _git(operator, "rev-list", "--count", f"{start}..{ending}") == str(count)


def test_actor_refs_config_hooks_replacements_grafts_and_alternates_are_ignored(tmp_path):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    _git(actor, "checkout", "--orphan", "attacker")
    _git(actor, "rm", "-qrf", ".")
    (actor / "unrelated.txt").write_text("unrelated\n")
    _git(actor, "add", "unrelated.txt")
    _git(actor, "commit", "-qm", "unrelated")
    ending = _git(actor, "rev-parse", "HEAD")
    tree = _git(actor, "rev-parse", f"{ending}^{{tree}}")
    fake = subprocess.run(
        ["git", "commit-tree", tree, "-p", start],
        cwd=actor,
        input="fake parent\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(actor, "replace", ending, fake)
    (actor / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (actor / ".git" / "info" / "grafts").write_text(f"{ending} {start}\n")
    (actor / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (actor / ".git" / "objects" / "info" / "alternates").write_text(
        str(operator / ".git" / "objects") + "\n"
    )
    (actor / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    (actor / ".git" / "hooks" / "post-commit").write_text("#!/bin/sh\nexit 99\n")
    _git(actor, "config", "core.hooksPath", ".git/hooks")
    _git(actor, "update-ref", "refs/heads/work", start)
    _git(actor, "update-ref", "refs/syncade/recovery/run-2/round-0/claimed", start)

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-2",
        round_idx=0,
    )

    assert result.status == "invalid"
    assert "NOT a descendant" in (result.error or "")
    assert _git(operator, "rev-parse", "refs/heads/work") == start
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", "refs/syncade/recovery/run-2/round-0/claimed"],
            cwd=operator,
            capture_output=True,
        ).returncode
        != 0
    )


def test_symbolic_head_and_actor_destination_ref_do_not_select_import_identity(tmp_path):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    _git(actor, "update-ref", "refs/heads/work", start)
    (actor / ".git" / "HEAD").write_text("ref: refs/heads/work\n")
    claimed = f"refs/syncade/recovery/run-3/round-0/{ending}"
    _git(actor, "update-ref", claimed, start)

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-3",
        round_idx=0,
    )

    assert result.status == "imported"
    assert result.recovery_ref == claimed
    assert _git(operator, "rev-parse", claimed) == ending


def test_only_candidate_graph_crosses_from_quarantine(tmp_path):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    extra = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=actor,
        input="unreachable attacker object\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-graph",
        round_idx=0,
    )

    assert result.status == "imported"
    assert (
        subprocess.run(
            ["git", "cat-file", "-e", extra], cwd=operator, capture_output=True
        ).returncode
        != 0
    )


def test_invalid_ranges_never_create_recovery_or_move_branch(tmp_path):
    operator, start = _operator_repo(tmp_path)

    def import_actor(actor: Path, ending: str, run_id: str):
        return import_producer_candidate(
            repo_root=operator,
            producer_repo=actor,
            starting_sha=start,
            ending_sha=ending,
            run_id=run_id,
            round_idx=0,
        )

    empty_actor = _actor_repo(tmp_path / "empty", operator, start)
    _git(empty_actor, "commit", "--allow-empty", "-qm", "empty")
    empty = import_actor(empty_actor, _git(empty_actor, "rev-parse", "HEAD"), "empty")

    merge_actor = _actor_repo(tmp_path / "merge", operator, start)
    _git(merge_actor, "checkout", "-qb", "left")
    left = _commit(merge_actor, "left\n", "left")
    _git(merge_actor, "checkout", "-qb", "right", start)
    (merge_actor / "right.txt").write_text("right\n")
    _git(merge_actor, "add", "right.txt")
    _git(merge_actor, "commit", "-qm", "right")
    _git(merge_actor, "merge", "--no-ff", "-qm", "merge", left)
    merge = import_actor(merge_actor, _git(merge_actor, "rev-parse", "HEAD"), "merge")

    malformed_actor = _actor_repo(tmp_path / "malformed", operator, start)
    malformed_end = _commit(malformed_actor, "bad\n", "bad")
    object_path = malformed_actor / ".git" / "objects" / malformed_end[:2] / malformed_end[2:]
    object_path.chmod(0o644)
    object_path.write_bytes(b"not a git object")
    malformed = import_actor(malformed_actor, malformed_end, "malformed")

    abbreviated = import_actor(malformed_actor, malformed_end[:12], "abbrev")
    for result in (empty, merge, malformed, abbreviated):
        assert result.status == "invalid"
        assert result.recovery_ref is None
    assert _git(operator, "rev-parse", "refs/heads/work") == start


def test_conflicting_recovery_ref_is_never_overwritten(tmp_path):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    recovery = f"refs/syncade/recovery/run-4/round-0/{ending}"
    _git(operator, "update-ref", recovery, start)

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-4",
        round_idx=0,
    )

    assert result.status == "error"
    assert "collision" in (result.error or "")
    assert _git(operator, "rev-parse", recovery) == start
    assert _git(operator, "rev-parse", "refs/heads/work") == start


def test_recovery_ref_collision_cleans_up_quarantine_ref(tmp_path):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    recovery = f"refs/syncade/recovery/run-4b/round-0/{ending}"
    quarantine = f"refs/syncade/quarantine/run-4b/round-0/{ending}"
    _git(operator, "update-ref", recovery, start)

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-4b",
        round_idx=0,
    )

    assert result.status == "error"
    assert (
        subprocess.run(
            ["git", "show-ref", "--verify", quarantine],
            cwd=operator,
            capture_output=True,
        ).returncode
        != 0
    ), "quarantine ref must be deleted when recovery ref creation fails"


def test_operator_grafts_do_not_affect_import_validation(tmp_path):
    """Operator .git/info/grafts must not cause valid candidates to be rejected.

    An orphan graft for the ending SHA makes Git see it as having no parents,
    which would cause the post-fetch _validate_range in the operator repo to
    conclude that starting_sha is NOT an ancestor and reject a valid candidate.
    GIT_GRAFT_FILE=devnull must suppress this.
    """
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")

    graft_path = operator / ".git" / "info" / "grafts"
    graft_path.parent.mkdir(parents=True, exist_ok=True)
    # Orphan graft: makes ending appear to have no parents in the operator repo.
    graft_path.write_text(f"{ending}\n")

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-graft",
        round_idx=0,
    )

    assert result.status == "imported", (
        f"operator grafts must not cause a valid candidate to be rejected: {result.error}"
    )
    assert result.recovery_ref is not None


def test_symbolic_recovery_collision_cannot_redirect_an_operator_ref(tmp_path):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    recovery = f"refs/syncade/recovery/run-symbolic/round-0/{ending}"
    _git(operator, "symbolic-ref", recovery, "refs/heads/work")

    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-symbolic",
        round_idx=0,
    )

    assert result.status == "error"
    assert "symbolic" in (result.error or "")
    assert _git(operator, "rev-parse", "refs/heads/work") == start
    assert _git(operator, "symbolic-ref", recovery) == "refs/heads/work"


def test_failure_after_recovery_creation_keeps_inspectable_anchor(tmp_path, monkeypatch):
    operator, start = _operator_repo(tmp_path)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    original = producer_import_module._run_git

    def fail_quarantine_delete(argv, *, cwd):
        if len(argv) >= 5 and argv[:4] == ["git", "update-ref", "--no-deref", "-d"]:
            from syncade.process import SubprocessResult

            return SubprocessResult(1, "", "injected cleanup failure", 0)
        return original(argv, cwd=cwd)

    monkeypatch.setattr(producer_import_module, "_run_git", fail_quarantine_delete)
    result = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-after-anchor",
        round_idx=0,
    )

    assert result.status == "error"
    assert result.recovery_ref is not None
    assert _git(operator, "rev-parse", result.recovery_ref) == ending
    assert _git(operator, "rev-parse", "refs/heads/work") == start


def test_branch_cas_race_keeps_recovery_and_does_not_reset_worktree(tmp_path):
    operator, start = _operator_repo(tmp_path)
    snapshot = take_snapshot(operator, base_ref=start)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    imported = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-5",
        round_idx=0,
    )
    tree = _git(operator, "rev-parse", f"{start}^{{tree}}")
    concurrent = subprocess.run(
        ["git", "commit-tree", tree, "-p", start],
        cwd=operator,
        input="concurrent\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(operator, "update-ref", "refs/heads/work", concurrent, start)
    worktree_bytes = (operator / "tracked.txt").read_bytes()
    result = ProducerResult(
        outcome="committed",
        starting_sha=start,
        ending_sha=ending,
        duration_seconds=0,
        output=FakeProducerAdapter().canned_output,
        error=None,
        candidate_import=imported,
    )

    status = _advance_branch_ref(
        repo_root=operator,
        snapshot=snapshot,
        producer_result=result,
        logger=Logger("quiet"),
    )

    assert status == "update_ref_failed"
    assert _git(operator, "rev-parse", "refs/heads/work") == concurrent
    assert _git(operator, "rev-parse", imported.recovery_ref or "") == ending
    assert (operator / "tracked.txt").read_bytes() == worktree_bytes


def test_detached_head_imports_and_anchors_without_branch_advance(tmp_path):
    operator, start = _operator_repo(tmp_path)
    _git(operator, "checkout", "--detach", "-q", start)
    snapshot = take_snapshot(operator, base_ref=start)
    actor = _actor_repo(tmp_path, operator, start)
    ending = _commit(actor, "candidate\n", "candidate")
    imported = import_producer_candidate(
        repo_root=operator,
        producer_repo=actor,
        starting_sha=start,
        ending_sha=ending,
        run_id="run-6",
        round_idx=0,
    )
    result = ProducerResult(
        outcome="committed",
        starting_sha=start,
        ending_sha=ending,
        duration_seconds=0,
        output=FakeProducerAdapter().canned_output,
        error=None,
        candidate_import=imported,
    )

    status = _advance_branch_ref(
        repo_root=operator,
        snapshot=snapshot,
        producer_result=result,
        logger=Logger("quiet"),
    )

    assert status == "skipped_detached_head"
    assert _git(operator, "rev-parse", "HEAD") == start
    assert _git(operator, "rev-parse", imported.recovery_ref or "") == ending


def test_loop_cas_race_persists_recovery_ref_and_truthful_terminal_artifacts(
    repo_with_pr_doc, monkeypatch
):
    repo, pr_doc = repo_with_pr_doc
    original = FakeProducerAdapter.build_invocation

    # The operator repo comes from the fixture, NOT from a parameter threaded through the
    # production adapter protocol: no adapter needs it, and carrying it there only to serve
    # this test would hand every producer adapter the operator path this PR takes away.
    def race(self, producer_config, worktree_path, prompt):
        invocation = original(self, producer_config, worktree_path, prompt)
        old = _git(repo, "rev-parse", "HEAD")
        tree = _git(repo, "rev-parse", f"{old}^{{tree}}")
        moved = subprocess.run(
            ["git", "commit-tree", tree, "-p", old],
            cwd=repo,
            input="concurrent\n",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = _git(repo, "symbolic-ref", "HEAD")
        _git(repo, "update-ref", branch, moved, old)
        return invocation

    monkeypatch.setattr(FakeProducerAdapter, "build_invocation", race)
    config = SyncadeConfig.model_validate(
        {
            "reviewers": [
                {"name": "rv1", "provider": "openai", "model": "gpt-5.5"},
                {"name": "rv2", "provider": "openai", "model": "gpt-5.5"},
            ],
            "loop": {"max_rounds": 2},
        }
    )
    result = run_review(
        repo_root=repo,
        pr_doc_path=pr_doc,
        config=config,
        adapter_factory=_factory_returning(
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_no_ship()),
            FakeAdapter(canned_output=_ship()),
            FakeAdapter(canned_output=_ship()),
        ),
        synthesizer_adapter=_RoundCyclingSynth(_synth_with_blocker(), _synth_clean()),
        producer_adapter=FakeProducerAdapter(commit_message="candidate"),
    )

    assert result.exit_code == 30
    assert result.termination_reason == "producer_stalled"
    candidate_import = result.rounds[0].producer_result.candidate_import
    assert candidate_import.status == "imported"
    assert _git(repo, "rev-parse", candidate_import.recovery_ref or "") == (
        result.rounds[0].producer_result.ending_sha
    )
    manifest = json.loads((result.artifacts.run_dir / "round-0" / "manifest.json").read_text())
    assert manifest["producer"]["candidate_import"]["recovery_ref"] == (
        candidate_import.recovery_ref
    )
    assert candidate_import.recovery_ref in result.artifacts.loop_summary_path.read_text()
