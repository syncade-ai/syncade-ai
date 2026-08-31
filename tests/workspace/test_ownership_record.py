"""Every workspace this repository creates records who owns it.

``worktree_base`` is shared ground (``/tmp/syncade`` by default), so a directory
sitting there proves nothing about which repository made it. These tests pin the
write half of the answer: the record exists, it names this repository, and it
lands at the level GC actually reclaims.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.fake import FakeAdapter, FakeSynthesizerAdapter
from syncade.orchestrator import run_review
from syncade.orchestrator.loop_finalize import _reclaim_shared_run_dir
from syncade.producer_workspace import ProducerWorkspaceManager
from syncade.reviewer_workspace import ReviewerWorkspaceManager
from syncade.workspace_owner import (
    OWNER_RECORD_NAME,
    git_common_dir,
    resolve_best_effort,
)
from syncade.worktree import WorktreeManager
from tests.orchestrator._helpers import _factory_returning, _ship, _two_reviewer_config

_SRC = Path(__file__).resolve().parents[2] / "src" / "syncade"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)
    (root / "a.py").write_text("x = 1\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)
    return root


def _head(repo: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=repo)


def _record(base: Path, run_id: str) -> dict:
    return json.loads((base / run_id / OWNER_RECORD_NAME).read_text())


# --- the three creation paths, exercised for real -------------------------


def test_reviewer_export_records_its_owner(repo: Path, tmp_path: Path) -> None:
    base = tmp_path / "base"
    with ReviewerWorkspaceManager(repo, "run-1/round-0", base_dir=base) as mgr:
        mgr.create("rev", _head(repo))
        assert _record(base, "run-1")["repo_common_dir"] == str(git_common_dir(repo))


def test_producer_repository_records_its_owner(repo: Path, tmp_path: Path) -> None:
    base = tmp_path / "base"
    with ProducerWorkspaceManager(repo, "run-2/round-0/producer-worktree", base_dir=base) as mgr:
        mgr.create(_head(repo))
        assert _record(base, "run-2")["repo_common_dir"] == str(git_common_dir(repo))


def test_trusted_worktree_records_its_owner(repo: Path, tmp_path: Path) -> None:
    base = tmp_path / "base"
    with WorktreeManager(repo, "run-3/round-0", base_dir=base) as mgr:
        mgr.create("check", _head(repo))
        assert _record(base, "run-3")["repo_common_dir"] == str(git_common_dir(repo))


# --- the properties the record has to hold --------------------------------


def test_record_lands_at_the_level_gc_reclaims(repo: Path, tmp_path: Path) -> None:
    """Managers get nested run ids; GC reclaims whole ``<base>/<run-id>/`` trees."""
    base = tmp_path / "base"
    with ReviewerWorkspaceManager(repo, "run-4/round-7", base_dir=base) as mgr:
        mgr.create("rev", _head(repo))
    assert (base / "run-4" / OWNER_RECORD_NAME).is_file()
    assert not (base / "run-4" / "round-7" / OWNER_RECORD_NAME).exists()
    assert _record(base, "run-4")["run_id"] == "run-4"


def test_the_record_survives_the_run(repo: Path, tmp_path: Path) -> None:
    """Cleanup removes the workspaces; the ownership answer has to outlive them."""
    base = tmp_path / "base"
    with ReviewerWorkspaceManager(repo, "run-5/round-0", base_dir=base) as mgr:
        mgr.create("rev", _head(repo))
    assert (base / "run-5" / OWNER_RECORD_NAME).is_file()


def test_two_repositories_record_different_owners(tmp_path: Path) -> None:
    """The measured shape: one shared base, two unrelated repositories."""
    base = tmp_path / "shared"
    owners = []
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        _git("init", "-q", "-b", "main", cwd=root)
        _git("config", "user.email", "t@example.com", cwd=root)
        _git("config", "user.name", "T", cwd=root)
        (root / "f.txt").write_text(name)
        _git("add", "-A", cwd=root)
        _git("commit", "-qm", "seed", cwd=root)
        with ReviewerWorkspaceManager(root, f"{name}-run/round-0", base_dir=base) as mgr:
            mgr.create("rev", _head(root))
        owners.append(_record(base, f"{name}-run")["repo_common_dir"])
    assert owners[0] != owners[1]


def test_creating_a_workspace_under_unverifiable_root_succeeds(repo: Path, tmp_path: Path) -> None:
    """A root with a corrupt ownership record does not block nested provisioning.

    owner_of() returning None means "cannot prove" — not "proven foreign".
    Refusing on an unreadable record would turn a best-effort metadata
    degradation into a user-visible provisioning failure, violating the
    brief's 'creating a workspace does not require reading it' clause.
    GC is also fail-closed: it leaves roots it cannot prove it owns, so
    our workspace is safe even though the record stays corrupt and
    unreclaimed.
    """
    base = tmp_path / "base"
    (base / "run-6").mkdir(parents=True)
    (base / "run-6" / OWNER_RECORD_NAME).write_text("}{ not json")

    with ReviewerWorkspaceManager(repo, "run-6/round-0", base_dir=base) as mgr:
        mgr.create("rev", _head(repo))
    # The corrupt record is not modified (os.link refuses EEXIST).
    assert (base / "run-6" / OWNER_RECORD_NAME).read_text() == "}{ not json"


def test_git_common_dir_ignores_ambient_git_dir(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient GIT_DIR must not poison the ownership record.

    If GIT_DIR points at a different repository, git_common_dir must still
    return the common directory for the path argument, not the ambient one.
    """
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    _git("config", "user.email", "o@example.com", cwd=other)
    _git("config", "user.name", "O", cwd=other)
    (other / "x.py").write_text("x = 2\n")
    _git("add", "-A", cwd=other)
    _git("commit", "-qm", "seed", cwd=other)

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    result = git_common_dir(repo)

    assert result is not None
    assert result != resolve_best_effort(other / ".git"), (
        "git_common_dir returned the ambient GIT_DIR repo instead of the path argument's repo"
    )
    assert result == resolve_best_effort(repo / ".git"), (
        "git_common_dir must return the common dir for the given path, not the ambient GIT_DIR"
    )


# --- the attack: a creation path that skips the record --------------------


def test_nothing_outside_workspace_owner_creates_a_run_directory() -> None:
    """Creating a run directory and recording its owner must be inseparable.

    Ownership used to be stamped by each creator calling ``record_owner`` after
    its own ``mkdir``, which made recording a thing to REMEMBER. A creator that
    forgot produced exactly the unowned tree the record exists to prevent, and
    that is not hypothetical: the orchestrator's own fresh-run reservation
    (``tmp_run_dir.mkdir``) was missed on the first pass. So the rule is that
    :func:`create_run_dir` is the only code that may make one.

    CEILING: this is keyed on the repository's ``run_dir`` naming convention for
    these paths, so it is a tripwire, not a proof — a creator that names its
    variable something else slips past. The behavioural tests above are what
    prove each creator records; this catches the creator nobody wrote a test for.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "workspace_owner.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "mkdir"
            ):
                continue
            receiver = node.func.value
            # ``self.run_dir`` is a manager's workspace root; a bare local
            # ``tmp_run_dir`` is the orchestrator's reservation of the same
            # thing. A bare ``run_dir`` is NOT one of these — in ``loop.py``
            # that name means ``.syncade/runs/<id>``, tier-1 run history under
            # the repository, which ownership records have nothing to do with.
            if isinstance(receiver, ast.Attribute) and receiver.attr == "run_dir":
                name = "self.run_dir"
            elif isinstance(receiver, ast.Name) and receiver.id.endswith("_run_dir"):
                name = receiver.id
            else:
                continue
            offenders.append(f"{path.relative_to(_SRC)}:{node.lineno} ({name}.mkdir)")
    assert offenders == [], (
        "these create a run directory directly instead of via create_run_dir, so "
        f"the directory exists before anyone owns it: {offenders}"
    )


# --- the record must not become the accumulation it exists to reclaim ------


def test_a_fully_cleaned_tree_keeps_nothing_behind(tmp_path: Path) -> None:
    """A clean run leaves NO directory: the record goes with the last entry.

    Without this the record survives every clean run as a one-file directory
    forever — the exact accumulation ownership records exist to let GC reclaim.
    """
    base = tmp_path / "base"
    run_root = base / "run-8"
    (run_root / "round-0" / "rev").mkdir(parents=True)
    (run_root / "round-0" / "rev").rmdir()
    (run_root / OWNER_RECORD_NAME).write_text("{}")
    _reclaim_shared_run_dir(run_root)
    assert not run_root.exists()


def test_a_surviving_tree_keeps_its_record(tmp_path: Path) -> None:
    """The record outlives cleanup whenever anything it speaks for outlives it.

    Dropping it here would strand the tree: GC could no longer prove the tree is
    ours, so tier 3 would never reclaim it.
    """
    base = tmp_path / "base"
    run_root = base / "run-9"
    (run_root / "round-0" / "rev").mkdir(parents=True)
    (run_root / "round-0" / "rev" / "left-behind.txt").write_text("operator state")
    (run_root / OWNER_RECORD_NAME).write_text("{}")
    _reclaim_shared_run_dir(run_root)
    assert (run_root / OWNER_RECORD_NAME).is_file()
    assert (run_root / "round-0" / "rev" / "left-behind.txt").is_file()


# --- orchestrator fresh-run reservation path --------------------------------


def test_orchestrator_reservation_records_owner_before_managers_run(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The top-level <worktree_base>/<run-id>/ directory created by the orchestrator
    reservation path (loop.py tmp_run_dir.mkdir) stamps an ownership record even when
    the run fails before any workspace manager runs.

    Before the fix this was zero: the record was only written inside managers, so a
    pre-manager failure (e.g. invalid pr_doc_artifact_name) left an empty run directory
    with no .syncade-owner.json.
    """
    base = tmp_path / "base"
    pr_doc = repo / "REVIEW.md"
    pr_doc.write_text("# spec\n")

    # Inject a fake synthesizer so no real CLI is needed.
    monkeypatch.setattr(
        "syncade.synthesizer.driver.get_adapter",
        lambda _provider: FakeSynthesizerAdapter(),
    )

    adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
    config = _two_reviewer_config()

    with pytest.raises(ValueError, match="pr_doc_artifact_name must include a filename"):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=config,
            adapter_factory=_factory_returning(*adapters),
            worktree_base=base,
            pr_doc_artifact_name="/",  # triggers ValueError after tmp_run_dir is reserved
        )

    # The ownership record must exist in the reserved tmp_run_dir.
    run_dirs = [d for d in base.iterdir() if d.is_dir()] if base.exists() else []
    assert len(run_dirs) == 1, f"expected exactly one run dir under {base}, got {run_dirs}"
    record_file = run_dirs[0] / OWNER_RECORD_NAME
    assert record_file.is_file(), (
        f"no ownership record in orchestrator-reserved {run_dirs[0]}; "
        "tmp_run_dir.mkdir() path must call record_owner() before any manager runs"
    )
    assert git_common_dir(repo) is not None
    data = json.loads(record_file.read_text())
    assert data["repo_common_dir"] == str(git_common_dir(repo))


def test_reservation_is_owned_when_the_repo_side_run_dir_cannot_be_created(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """The window round 1 of the item-1 dogfood reproduced, closed.

    The reservation under ``worktree_base`` and the artifact directory under
    ``.syncade/runs/`` are created one after the other, and the second can fail
    on its own: a committed ``.syncade`` FILE makes ``.syncade/runs`` impossible,
    so ``mkdir(parents=True)`` raises ``NotADirectoryError`` after the
    reservation already exists. Stamping after BOTH succeed — which is where the
    first fix put it — leaves that reservation unowned forever.
    """
    base = tmp_path / "base"
    (repo / ".syncade").write_text("a file where the run-state directory belongs")
    pr_doc = repo / "REVIEW.md"
    pr_doc.write_text("# spec\n")
    monkeypatch.setattr(
        "syncade.synthesizer.driver.get_adapter",
        lambda _provider: FakeSynthesizerAdapter(),
    )

    with pytest.raises(OSError):
        run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(
                *[FakeAdapter(canned_output=_ship()) for _ in range(2)]
            ),
            worktree_base=base,
        )

    stranded = [d for d in base.iterdir() if d.is_dir()] if base.exists() else []
    assert len(stranded) == 1, f"expected one reserved run dir, got {stranded}"
    record = stranded[0] / OWNER_RECORD_NAME
    assert record.is_file(), (
        f"the reservation at {stranded[0]} outlived the run with no owner; "
        "nothing can ever prove it is ours, so GC must leave it forever"
    )
    assert json.loads(record.read_text())["repo_common_dir"] == str(git_common_dir(repo))
