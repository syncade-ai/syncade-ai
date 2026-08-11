"""The installer refuses rather than destroying — PR-h-04.5 items 2 and 6.

Reproduced on `main` before this landed, in a real skill directory:

    BEFORE:  MY-NOTES.md  reference.md  SKILL.md
    AFTER:   README.md    SKILL.md

A hand-edited `SKILL.md` overwritten and two unrelated operator files deleted — at **exit 0**,
with no prompt, warning or backup. `--install-skill` ran `rmtree` over the destination and
rewrote it.

The rule is now: survey every destination first, and if installing would destroy anything,
refuse (exit 60) naming each casualty. Two things deliberately do NOT count as loss, because
treating them as loss would break documented paths to protect data that was never at risk:

- a **symlinked** destination (README documents symlink-to-checkout; unlinking the symlink
  leaves the checkout untouched), and
- a bundled file whose bytes are **identical** to what we would write (rewriting it changes
  nothing), which is what keeps an ordinary re-install from demanding a flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.cli.install_skill import install_skill

_CLAUDE = Path(".claude") / "skills" / "syncade"


def _home(tmp_path):
    home = tmp_path / "home"
    (home / _CLAUDE).mkdir(parents=True)
    return home


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_an_edited_skill_md_is_not_overwritten(tmp_path, capsys):
    """The original harm: the operator's own edit, silently replaced."""
    home = _home(tmp_path)
    skill = home / _CLAUDE / "SKILL.md"
    skill.write_text("# MY CUSTOMIZED SKILL\n")
    before = _tree(home)

    rc = install_skill("claude", home=home, codex_home=tmp_path / "cx")

    assert rc == 60, "the install did not refuse"
    assert _tree(home) == before, "the destination was modified despite the refusal"
    assert skill.read_text() == "# MY CUSTOMIZED SKILL\n"
    err = capsys.readouterr().err
    assert "SKILL.md" in err and "OVERWRITTEN" in err, "the casualty was not named"
    assert "--force-install" in err, "no way forward was offered"


def test_unrelated_operator_files_are_not_deleted(tmp_path, capsys):
    """`rmtree` took everything, not just the files the installer owns."""
    home = _home(tmp_path)
    (home / _CLAUDE / "MY-NOTES.md").write_text("personal\n")
    (home / _CLAUDE / "notes").mkdir()
    (home / _CLAUDE / "notes" / "deep.md").write_text("nested\n")
    before = _tree(home)

    rc = install_skill("claude", home=home, codex_home=tmp_path / "cx")

    assert rc == 60
    assert _tree(home) == before
    err = capsys.readouterr().err
    assert "MY-NOTES.md" in err
    assert "deep.md" in err, (
        "a nested file was not surveyed — rmtree takes whole subdirectories, so an "
        "operator's notes/ directory is exactly as destroyed as their loose file"
    )


def test_an_unchanged_destination_reinstalls_without_a_flag(tmp_path):
    """Rule 5: the common path must stay easy, or operators pass --force reflexively and
    lose the protection entirely."""
    home, cx = tmp_path / "home", tmp_path / "cx"

    assert install_skill("claude", home=home, codex_home=cx) == 0
    assert install_skill("claude", home=home, codex_home=cx) == 0, (
        "re-installing an unmodified destination demanded a flag"
    )


def test_a_symlinked_destination_is_not_loss(tmp_path):
    """The README documents symlink-to-checkout. Unlinking a symlink destroys nothing —
    the checkout it pointed at is untouched — so refusing there would break a documented
    path to protect data that was never at risk."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "SKILL.md").write_text("from a checkout\n")
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / _CLAUDE).symlink_to(checkout)

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 0, "a symlinked destination was refused"
    assert (checkout / "SKILL.md").read_text() == "from a checkout\n", "the checkout was touched"


def test_force_install_overwrites_AND_still_names_the_casualties(tmp_path, capsys):
    """Consent to destroying files is not consent to being told nothing about which ones.

    The first cut of this feature skipped the survey entirely under --force, which also made
    the flag's own help text false. Caught before it shipped.
    """
    home = _home(tmp_path)
    (home / _CLAUDE / "MY-NOTES.md").write_text("personal\n")

    rc = install_skill("claude", home=home, codex_home=tmp_path / "cx", force=True)

    assert rc == 0
    assert not (home / _CLAUDE / "MY-NOTES.md").exists(), "--force-install did not overwrite"
    err = capsys.readouterr().err
    assert "MY-NOTES.md" in err, "--force-install destroyed a file without naming it"


def test_force_install_without_install_skill_is_a_cli_error(tmp_path, capsys):
    """The flag's help says so; a documented flag that does not behave as documented is the
    defect class PR-h-03 exists to prevent.

    Paired with an OTHERWISE-VALID command, not passed alone. A bare `--force-install` exits
    2 because no command was given at all — so asserting on that proves nothing about this
    guard, and the calibration caught exactly that: removing the guard left the bare-flag
    test green.
    """
    from syncade.cli import main

    brief = tmp_path / "pr.md"
    brief.write_text("# PR\n")

    assert main([str(brief), "--force-install"]) == 2
    assert "--force-install requires --install-skill" in capsys.readouterr().err


def test_an_unreadable_file_counts_as_a_loss(tmp_path):
    """Fail closed: if its bytes cannot be read, it cannot be proven to be ours."""
    home = _home(tmp_path)
    victim = home / _CLAUDE / "SKILL.md"
    victim.write_bytes(b"x")
    victim.chmod(0o000)
    try:
        rc = install_skill("claude", home=home, codex_home=tmp_path / "cx")
    finally:
        victim.chmod(0o644)

    assert rc == 60, "an unreadable destination file was treated as safe to destroy"


@pytest.mark.parametrize("target", ["claude", "codex", "all"])
def test_a_fresh_install_never_refuses(target, tmp_path):
    """The control. Without it, a function that refused everything would pass this file."""
    assert install_skill(target, home=tmp_path / "home", codex_home=tmp_path / "cx") == 0


# ── A symlinked DESTINATION is not loss — PR-h-04.5 item 6 ─────────────────
#
# Established in PR-h-04 item D and carried forward so it is not re-litigated: the skill
# bundle's own README documents `ln -sfn "$PWD/.claude/skills/syncade" ~/.claude/skills/...`,
# re-running the installer over that link has always replaced it, and unlinking a symlink
# destroys nothing — the checkout it points at is untouched. Refusing there would break a
# documented path to protect data that was never at risk.
#
# (The brief attributes that documentation to the top-level README, which does not contain
# it. The skill bundle's README does, for both harnesses, and it ships into the operator's
# skills directory — so the justification holds and only the file reference was wrong.)
#
# The rule must NOT be stretched to symlinks INSIDE the destination, which is a different
# thing entirely and where over-applying it caused a silent loss.


def test_a_symlinked_destination_pointing_at_a_checkout_is_replaced_not_refused(tmp_path):
    home, cx = tmp_path / "home", tmp_path / "cx"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "SKILL.md").write_text("checkout skill\n")
    (checkout / "MY-NOTES.md").write_text("mine\n")
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / _CLAUDE).symlink_to(checkout)

    assert install_skill("claude", home=home, codex_home=cx) == 0, "a documented path was refused"
    assert sorted(p.name for p in checkout.iterdir()) == ["MY-NOTES.md", "SKILL.md"], (
        "the checkout behind the symlink was modified"
    )
    assert (home / _CLAUDE).is_dir() and not (home / _CLAUDE).is_symlink()


def test_a_dangling_symlink_destination_is_replaced(tmp_path):
    """A broken link points at nothing, so unlinking it can destroy nothing."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / _CLAUDE).symlink_to(tmp_path / "nowhere")

    assert install_skill("claude", home=home, codex_home=cx) == 0
    assert (home / _CLAUDE / "SKILL.md").is_file()


def test_a_symlink_INSIDE_the_destination_is_a_casualty(tmp_path, capsys):
    """The gap the rule opened, and the reason it is scoped to the DESTINATION.

    Measured before this: an operator's symlink-to-directory inside the skill dir was
    destroyed at exit 0, silently — `is_dir()` is true for it so it was skipped as "a
    directory", and rglob does not recurse through symlinks either, so nothing under it was
    surveyed. rmtree then removed the link.
    """
    home, cx = tmp_path / "home", tmp_path / "cx"
    assert install_skill("claude", home=home, codex_home=cx) == 0
    target = tmp_path / "operator-dir"
    target.mkdir()
    (target / "work.md").write_text("mine\n")
    link = home / _CLAUDE / "mystuff"
    link.symlink_to(target)

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 60, "an operator's symlink inside the destination was destroyed silently"
    assert link.is_symlink(), "the symlink was removed despite the refusal"
    assert (target / "work.md").exists()
    assert "mystuff" in capsys.readouterr().err, "the casualty was not named"


def test_a_symlink_to_a_file_inside_the_destination_is_also_a_casualty(tmp_path):
    """Both shapes, so the fix cannot regress to handling only one."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    assert install_skill("claude", home=home, codex_home=cx) == 0
    outside = tmp_path / "precious.txt"
    outside.write_text("do not touch\n")
    (home / _CLAUDE / "link.md").symlink_to(outside)

    assert install_skill("claude", home=home, codex_home=cx) == 60
    assert outside.read_text() == "do not touch\n"


def test_symlink_destination_leaves_no_backup_symlink_behind(tmp_path):
    """Installing over a symlinked destination leaves no stale backup symlink in the parent.

    Before the fix: backup.is_dir() followed the symlink, shutil.rmtree refused the symlink
    and silently ignored the error (ignore_errors=True), leaving .syncade.syncade-old-* in
    the skills directory.  Fix: check is_symlink() before is_dir() so symlinks are unlinked.
    """
    home, cx = tmp_path / "home", tmp_path / "cx"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "SKILL.md").write_text("checkout content\n")
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "syncade").symlink_to(checkout)

    assert install_skill("claude", home=home, codex_home=cx) == 0
    assert (checkout / "SKILL.md").read_text() == "checkout content\n", "checkout was touched"

    leftover = [p for p in skills_dir.iterdir() if p.name.startswith(".syncade.syncade-old-")]
    assert not leftover, f"stale backup symlink left behind: {leftover}"


def test_a_symlink_loop_inside_the_destination_does_not_hang(tmp_path):
    """rglob over a self-referential link must terminate. A hang is a worse failure than a
    refusal because there is no exit code to react to."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    assert install_skill("claude", home=home, codex_home=cx) == 0
    (home / _CLAUDE / "loop").symlink_to(home / _CLAUDE)

    assert install_skill("claude", home=home, codex_home=cx) == 60


# ── Parseable manifest with extra keys — finding in PR-h-04.5 dogfood ───────
#
# A manifest with extra top-level keys (e.g. {"files": {...}, "operator_note": "x"})
# still parses, still produces a non-empty "prior", and would have exempted itself from
# the casualty survey — so the operator's additions were silently overwritten at exit 0.
# _read_manifest now requires exactly {"files": {...}} at the top level.


# ── Regular file at destination — finding in PR-h-04.5 dogfood ──────────────
#
# _casualties() returned [] when dest.is_file(), so a regular file at the skill
# destination path was treated as safe and then silently unlinked in phase 2.
# Now dest.is_file() is a casualty like any other operator file.


def test_a_regular_file_at_the_skill_destination_is_a_casualty(tmp_path, capsys):
    """A file sitting at the destination path (not a directory, not a symlink) will be
    unlinked and replaced with a directory. The casualty survey previously returned []
    for this case, so the file was destroyed at exit 0 with no warning."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    dest = home / ".claude" / "skills" / "syncade"
    dest.parent.mkdir(parents=True)
    dest.write_text("operator file at destination path\n")

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 60, "a regular file at the skill destination was not refused"
    assert dest.is_file(), "the file was removed despite the refusal"
    assert dest.read_text() == "operator file at destination path\n"
    err = capsys.readouterr().err
    assert "DELETED" in err
    assert "--force-install" in err


def test_force_install_replaces_a_regular_file_at_the_destination(tmp_path):
    """--force-install must still work when the destination is a regular file."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    dest = home / ".claude" / "skills" / "syncade"
    dest.parent.mkdir(parents=True)
    dest.write_text("old file\n")

    rc = install_skill("claude", home=home, codex_home=cx, force=True)

    assert rc == 0
    assert (dest / "SKILL.md").is_file(), (
        "--force-install did not replace the file with a directory"
    )


# ── Safe casualty traversal — finding in round-1 blind review ───────────────
#
# rglob("*") had three failure modes:
#   1. Empty operator directories: silently deleted (rglob yields them but the code skipped
#      all dirs via `is_dir() → continue`; nothing reported).
#   2. Unreadable directories: silently skipped by rglob (PermissionError suppressed),
#      so their contents were lost at exit 0.
#   3. Special files (FIFOs): read_bytes() blocks indefinitely on a named pipe.
# The traversal now uses os.scandir with explicit error handling; special files are
# classified by stat mode without opening them.


def test_an_empty_operator_directory_is_a_casualty(tmp_path, capsys):
    """An empty directory would be silently deleted by rmtree. rglob skipped all dirs
    so there was nothing to report; the traversal now reports empty subdirs explicitly."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    dest = home / ".claude" / "skills" / "syncade"
    dest.mkdir(parents=True)
    (dest / "empty-notes").mkdir()

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 60, "an empty operator directory was not refused"
    assert (dest / "empty-notes").is_dir(), "the empty directory was removed despite the refusal"
    assert "empty-notes" in capsys.readouterr().err, "the empty directory was not named"


def test_an_unreadable_operator_directory_is_a_casualty(tmp_path, capsys):
    """An unreadable subdirectory cannot be enumerated; rglob silently dropped its contents.
    The traversal catches the scandir OSError and reports the directory itself."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    dest = home / ".claude" / "skills" / "syncade"
    dest.mkdir(parents=True)
    private = dest / "private"
    private.mkdir()
    (private / "data.txt").write_text("secret\n")
    private.chmod(0o000)
    try:
        rc = install_skill("claude", home=home, codex_home=cx)
    finally:
        private.chmod(0o755)

    assert rc == 60, "an unreadable directory was not refused"
    assert "private" in capsys.readouterr().err, "the unreadable directory was not named"


def test_a_fifo_inside_the_destination_is_refused_not_opened(tmp_path, capsys):
    """read_bytes() on a named pipe blocks indefinitely. The traversal classifies
    non-regular files by stat mode and never opens them."""
    import os

    home, cx = tmp_path / "home", tmp_path / "cx"
    assert install_skill("claude", home=home, codex_home=cx) == 0
    fifo = home / ".claude" / "skills" / "syncade" / "myfifo"
    os.mkfifo(fifo)

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 60, "a FIFO inside the destination was not refused"
    assert fifo.exists(), "the FIFO was removed despite the refusal"
    assert "myfifo" in capsys.readouterr().err, "the FIFO was not named as a casualty"


# ── Duplicate destination aliasing — round-2 blind review ───────────────────
#
# When CODEX_HOME == ~/.claude (or a symlink that resolves there), both harnesses
# target the same skills/syncade path. Phase 1 stages both, then phase 2 crashes
# renaming the second tree over the already-populated destination, leaving staging
# artifacts behind and violating the all-or-nothing invariant.
#
# The fix: resolve canonical destination paths before staging; refuse if any two
# harnesses share one.


def test_duplicate_destination_is_refused_before_staging(tmp_path, capsys, monkeypatch):
    """When CODEX_HOME resolves to the claude home, both harnesses target the same path.
    The installer must refuse at exit 60 and leave no staging artifacts behind."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    # Point codex_home at the claude base directory — makes both targets identical.
    codex_home = home / ".claude"

    rc = install_skill("all", home=home, codex_home=codex_home)

    assert rc == 60, "duplicate destination was not refused"
    err = capsys.readouterr().err
    assert "same" in err.lower() or "identical" in err.lower() or "resolv" in err.lower(), (
        f"refusal message did not explain the collision: {err!r}"
    )
    # No staging artifacts: home should not exist at all (nothing was written).
    if home.exists():
        leftovers = list(home.rglob("*.syncade-new-*")) + list(home.rglob("*.syncade-old-*"))
        assert leftovers == [], f"staging artifacts left behind: {leftovers}"


def test_duplicate_destination_via_symlink_is_refused(tmp_path, capsys, monkeypatch):
    """When CODEX_HOME is a symlink that resolves to the claude directory, the canonical
    comparison must catch it (string comparison of unresolved paths would miss this)."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    real_claude = home / ".claude"
    real_claude.mkdir(parents=True)
    # A symlink to the claude dir — unresolved paths differ, resolved paths match.
    cx_via_symlink = tmp_path / "cx-link"
    cx_via_symlink.symlink_to(real_claude)

    rc = install_skill("all", home=home, codex_home=cx_via_symlink)

    assert rc == 60, "a symlinked duplicate destination was not detected"
