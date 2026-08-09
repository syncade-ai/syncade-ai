"""WHAT undo removes — `.git`, and nothing else (PR-h-04.6).

The companion suite `test_undo_auto_init.py` covers WHEN undo fires. This one covers its
SCOPE, which three dogfoods kept widening: `.git` + everything -> + ownership proof by inode,
path name, then content -> `.git` + `.syncade/runs` -> `.git` alone.

Two things survive on purpose and each has its own reason, so each has its own test:
`.gitignore` (bytes an operator could have edited after auto-init) and `.syncade/` (run state,
which GC is already forbidden from deleting because metrics rebuild from that tree).
"""

from __future__ import annotations

import subprocess

import pytest

import syncade.cli as cli_module
import syncade.git_preconditions as git_preconditions
from syncade.cli import main
from tests.cli._undo_helpers import _fresh

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def test_undo_never_touches_a_pre_existing_repository(tmp_path, monkeypatch):
    """L1, and the property that outranks every other one here.

    Getting this wrong destroys operator data, which is worse than the defect being fixed.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    for argv in (
        ["git", "init", "-q", "-b", "work"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(argv, cwd=repo, capture_output=True, check=True)
    (repo / "brief.md").write_text("# PR\n")
    (repo / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, capture_output=True, check=True)
    (repo / "uncommitted.txt").write_text("work in progress\n")
    monkeypatch.setattr(cli_module, "_resolve_scope_base", lambda *a, **k: None)

    rc = main(["--repo-root", str(repo), str(repo / "brief.md"), "--scope", "everything"])

    assert rc == 60
    assert (repo / ".git").is_dir(), "a PRE-EXISTING repository was deleted"
    assert (repo / "uncommitted.txt").read_text() == "work in progress\n"
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True)
    assert "seed" in log.stdout, "the operator's history was destroyed"


def test_a_populated_directory_keeps_both_its_files_AND_its_repository(tmp_path, monkeypatch):
    """Accepted limitation: --allow-auto-init in a populated dir leaves the repo behind.

    Ownership is unprovable there (L6), so syncade deletes nothing. If .git ever disappears
    here, ownership proof has crept back in.
    """
    work, brief = _fresh(tmp_path)
    (work / ".gitignore").write_text("*.log\n")
    (work / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(cli_module, "_resolve_scope_base", lambda *a, **k: None)

    rc = main(
        [
            "--repo-root",
            str(work),
            str(brief),
            "--scope",
            "everything",
            "--allow-auto-init",
        ]
    )

    assert rc == 60
    assert (work / ".gitignore").read_text() == "*.log\n", "the operator's file was deleted"
    assert (work / "app.py").read_text() == "x = 1\n", "the operator's file was deleted"
    assert (work / ".git").is_dir(), (
        "the repository was removed from a POPULATED directory — ownership proof has crept "
        "back in; see the accepted limitation in pr-h-04.6"
    )


def test_undo_inspects_nothing_it_is_about_to_delete(tmp_path):
    """Attack #7 — rewritten, because MY version of it failed the way the code did.

    It banned a LIST of names (`st_ino`, `inode_map`, `RepoInit`, ...). The dogfood producer
    then reintroduced ownership inference twice — once by path name, once by reading
    `.gitignore`'s content — and this guard stayed green through both, because neither used a
    banned word. An absence-assertion built from an enumeration is the very failure mode this
    PR exists to delete, one level up in the test layer.

    So pin what is ALLOWED instead. `undo_auto_init` may remove the names in `_UNDO_TARGETS`;
    it may not read, stat, compare, hash, or enumerate anything. Any mechanism — including one
    nobody has thought of — changes this body and fails here, which puts a person in the loop
    for the one function that has regressed three times.

    **What this pin does NOT do, demonstrated rather than theorised.** It stops UNNOTICED
    drift, not approved drift. Dogfood 3 caught the producer widening the body to
    `rmtree(root / ".syncade" / "runs")` and updating this pin in the same commit — the suite
    stayed green over a data-loss bug, and a reviewer had to find it: *"the source-pinned
    guard currently requires this blanket rmtree, so the suite passes while locking in the
    unsafe behaviour."* A pin makes a change deliberate; it cannot make it correct. So the
    failure message points at the reasoning, and the `_UNDO_TARGETS` assertion below carries
    the WHY for each exclusion — an updater who has to delete a justification is likelier to
    notice they are removing one.
    """
    import inspect
    import re

    from syncade.git_preconditions import _UNDO_TARGETS, undo_auto_init

    body = inspect.getsource(undo_auto_init)
    body = body[body.index('"""', body.index('"""') + 3) + 3 :]  # drop the docstring
    body = re.sub(r"\s*#.*", "", body)  # drop comments
    normalized = " ".join(body.split())

    assert normalized == (
        "failed: list[str] = [] "
        "for name in _UNDO_TARGETS: "
        "path = root / name "
        "try: "
        "if path.is_symlink() or path.is_file(): path.unlink() "
        "elif path.is_dir(): shutil.rmtree(path) "
        "except OSError: failed.append(str(path)) "
        "return failed"
    ), (
        "undo_auto_init's body changed. If the change adds ANY inspection of what it is "
        "deleting — stat, read, hash, name comparison, iterdir — that is ownership proof "
        "returning, and three dogfoods say it will have a hole. If it deletes a path OUTSIDE "
        "_UNDO_TARGETS, read the pin's docstring before updating it."
    )
    assert _UNDO_TARGETS == (".git",), (
        "the removal set changed. `.gitignore` stays out because an operator can edit it "
        "mid-run; `.syncade` stays out because run state is not auto-init's to remove and "
        "GC is already forbidden from deleting a run directory."
    )


# ── undo_auto_init removes .git only — no content checks, no named-path logic ──────────


def test_undo_never_deletes_a_file_it_did_not_write(tmp_path, monkeypatch):
    """Item 2, and the reason `.gitignore` survives: emptiness settles who CREATED a file,
    not who last WROTE it.

    A file can appear — or `.gitignore` can be edited — between the emptiness check and the
    cleanup. Emptying the directory would delete it on the strength of an ownership claim
    about live content, which is the exact reasoning three failed proofs were built on. Undo
    removes two fixed syncade-owned names instead, so anything else is safe by construction.
    """

    work, brief = _fresh(tmp_path)
    concurrent = work / "operator-concurrent.txt"
    original_undo = git_preconditions.undo_auto_init

    def _write_then_undo(root):
        concurrent.write_text("written after .git appeared\n")
        (root / ".gitignore").write_text("# edited by the operator\n*.log\n", encoding="utf-8")
        return original_undo(root)

    monkeypatch.setattr(git_preconditions, "undo_auto_init", _write_then_undo)
    monkeypatch.setattr(cli_module, "undo_auto_init", _write_then_undo)
    monkeypatch.setattr(cli_module, "_resolve_scope_base", lambda *a, **k: None)

    rc = main(["--repo-root", str(work), str(brief), "--scope", "everything"])

    assert rc == 60
    assert not (work / ".git").exists(), "the repository survived the refusal"
    assert not (work / ".syncade").exists(), "run state survived the refusal"
    assert concurrent.read_text() == "written after .git appeared\n", (
        "undo deleted a file it did not write"
    )
    assert (work / ".gitignore").read_text() == "# edited by the operator\n*.log\n", (
        "undo deleted operator-edited bytes — emptiness does not license that"
    )


def test_the_permitted_residue_is_gitignore_and_run_state(tmp_path, monkeypatch):
    """The cost of item 2, pinned so it is a decision and not drift.

    A refusal in an empty directory leaves the starter `.gitignore` behind. That is litter,
    not damage, and it is the price of never deleting content an operator could have edited.
    Anything MORE than that is a regression — `_assert_undone` fails on any other residue.
    """
    work, brief = _fresh(tmp_path)
    monkeypatch.setattr(cli_module, "_resolve_scope_base", lambda *a, **k: None)

    rc = main(["--repo-root", str(work), str(brief), "--scope", "everything"])

    assert rc == 60
    assert sorted(p.name for p in work.iterdir()) == [".gitignore"]
    assert "syncade" in (work / ".gitignore").read_text(), (
        "the survivor is not syncade's starter file — something else is being left behind"
    )


def test_the_limitation_is_documented_where_an_operator_meets_it():
    """Attack #8. A non-guarantee that lives only in a PR brief is not disclosed.

    `--allow-auto-init` leaves the repository behind on a refusal. That is deliberate (L6:
    never delete what cannot be proven ours) but it IS surprising, so it has to be in the
    flag's own help — the place someone reads before deciding to pass it.
    """
    from syncade.cli.parser import build_parser

    help_text = next(
        a.help for a in build_parser()._actions if "--allow-auto-init" in a.option_strings
    )
    assert "LEFT BEHIND" in help_text, "the flag does not disclose that a refusal keeps the repo"
    assert "empty directory" in help_text, "the flag does not say when cleanup DOES happen"
