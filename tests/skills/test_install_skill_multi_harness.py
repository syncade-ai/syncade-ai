"""Installing more than one harness at once — PR-h-04.5 item 5 and dogfood 1.

Split from test_install_skill_refuses.py when it passed the 500-LOC cap. Atomicity across
harnesses, destinations that coincide or contain one another, scratch/backup paths, and a
rollback that can actually roll back.

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


# ── `all` is atomic — PR-h-04.5 item 5 ─────────────────────────────────────
#
# The brief's claim 4: "--install-skill all must not leave claude upgraded and codex
# refused." Two halves, and only one was free.
#
# REFUSAL atomicity came from item 2 (survey every destination before writing any). WRITE
# atomicity did not: measured, an unwritable codex parent raised an uncaught PermissionError
# AFTER claude had been installed — a half-applied `all` AND a traceback instead of an exit
# code. Now every harness is built beside its destination first, so all the failure-prone
# work happens before anything is replaced.
#
# The remaining bound is stated rather than papered over: two renames are not one atomic
# operation. Phase 1 shrinks the window from "any write error" to "a failure between two
# renames", having already demonstrated each parent is writable by creating a temp dir in it.


def _codex_skill(cx: Path) -> Path:
    return cx / "skills" / "syncade"


def test_all_refuses_without_touching_the_clean_harness(tmp_path):
    """Refusal atomicity: one dirty harness must not leave the other half-installed."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    _codex_skill(cx).mkdir(parents=True)
    (_codex_skill(cx) / "MY-NOTES.md").write_text("personal\n")

    assert install_skill("all", home=home, codex_home=cx) == 60
    assert not (home / _CLAUDE).exists(), "claude was installed while codex refused"
    assert (_codex_skill(cx) / "MY-NOTES.md").exists()


def test_a_write_failure_on_one_harness_leaves_the_other_untouched(tmp_path):
    """Write atomicity. Before this, claude was already installed when codex blew up."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    (cx / "skills").mkdir(parents=True)
    # A FILE where the directory must go, in a parent that cannot be modified: preparing
    # codex is guaranteed to fail, and claude is perfectly installable.
    _codex_skill(cx).write_text("blocker\n")
    (cx / "skills").chmod(0o555)
    try:
        rc = install_skill("all", home=home, codex_home=cx)
    finally:
        (cx / "skills").chmod(0o755)

    assert rc == 60, "a write failure escaped as something other than a clean exit code"
    assert not (home / _CLAUDE).exists(), "`all` half-applied: claude installed, codex not"


def test_a_failed_install_leaves_no_scratch_directories(tmp_path):
    """Preparation happens beside the destination; a failure must not litter the operator's
    skills directory with half-built trees."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    (cx / "skills").mkdir(parents=True)
    _codex_skill(cx).write_text("blocker\n")
    (cx / "skills").chmod(0o555)
    try:
        install_skill("all", home=home, codex_home=cx)
    finally:
        (cx / "skills").chmod(0o755)

    # Staging dirs are now named ".<dest>.syncade-new-<random>" (from tempfile.mkdtemp)
    # and backups ".<dest>.syncade-old"; glob for any hidden syncade-related path.
    leftovers = (
        list((home / ".claude" / "skills").glob(".*syncade*"))
        if (home / ".claude" / "skills").exists()
        else []
    )
    assert leftovers == [], f"scratch directories left behind: {leftovers}"


def test_a_preexisting_staging_directory_is_not_silently_deleted(tmp_path):
    """The installer previously used a predictable staging path (.<dest>.syncade-new) and
    called shutil.rmtree on it unconditionally. Operator content at that path was deleted
    before ownership was proven. Now tempfile.mkdtemp produces a unique name so the
    predictable path is never touched."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    old_staging = skills_dir / ".syncade.syncade-new"
    old_staging.mkdir()
    (old_staging / "my-data.txt").write_text("mine\n")

    rc = install_skill("claude", home=home, codex_home=cx)

    assert rc == 0, "install failed unexpectedly"
    assert old_staging.exists(), "pre-existing staging directory was deleted"
    assert (old_staging / "my-data.txt").read_text() == "mine\n"


def test_reinstall_over_readonly_dest_directory_succeeds(tmp_path):
    """Phase 2 previously called shutil.rmtree on the old destination, which fails when
    the directory itself is read-only (0o555). This left a half-applied state after claude
    was already swapped. Phase 1 now renames the old destination to a backup; rename
    only needs write permission on the PARENT, so a read-only destination is not a
    blocker."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    assert install_skill("all", home=home, codex_home=cx) == 0

    # Make the codex skill directory read-only — shutil.rmtree would fail to remove
    # files inside it, but rename needs only parent write permission.
    codex_dest = _codex_skill(cx)
    codex_dest.chmod(0o555)
    try:
        rc = install_skill("all", home=home, codex_home=cx)
    finally:
        # Restore permissions for pytest cleanup. Backup path is now unique (mkdtemp-
        # derived prefix ".syncade.syncade-old-"), so glob for any leftover backup.
        if codex_dest.is_dir():
            codex_dest.chmod(0o755)
        for bk in (cx / "skills").glob(".syncade.syncade-old*"):
            if bk.is_dir():
                bk.chmod(0o755)

    assert rc == 0, "reinstall failed because the old destination was read-only"
    assert (home / _CLAUDE / "SKILL.md").is_file()
    assert (codex_dest / "SKILL.md").is_file()


def test_all_installs_both_harnesses_when_both_are_clean(tmp_path):
    """The control. Without it, an installer that refused every `all` would pass above."""
    home, cx = tmp_path / "home", tmp_path / "cx"

    assert install_skill("all", home=home, codex_home=cx) == 0
    assert (home / _CLAUDE / "SKILL.md").is_file()
    assert (_codex_skill(cx) / "SKILL.md").is_file()


# ── Unique backup path — finding in round-1 blind review ────────────────────
#
# The backup path was predictable (".syncade.syncade-old") and any pre-existing entry
# there was deleted unconditionally before proving ownership — the same defect class as
# the old fixed staging path. Now mkdtemp generates a unique backup name, so content at
# the old path is never touched.


def test_a_preexisting_backup_path_is_not_deleted_on_reinstall(tmp_path):
    """A directory sitting at the old predictable backup name is not deleted on reinstall.

    Before this fix: backup = dest.parent / ".syncade.syncade-old"; any entry there was
    unlink'd or rmtree'd before ownership was proven. Now the backup path is unique so the
    predictable name is never touched."""
    home, cx = tmp_path / "home", tmp_path / "cx"
    assert install_skill("claude", home=home, codex_home=cx) == 0

    skills = home / ".claude" / "skills"
    old_backup = skills / ".syncade.syncade-old"
    old_backup.mkdir()
    (old_backup / "notes.txt").write_text("precious\n")

    assert install_skill("claude", home=home, codex_home=cx) == 0, (
        "reinstall failed when operator content exists at the old predictable backup path"
    )
    assert old_backup.is_dir(), "operator backup directory was silently deleted"
    assert (old_backup / "notes.txt").read_text() == "precious\n"


def test_distinct_destinations_still_install_cleanly(tmp_path, monkeypatch):
    """The control: distinct claude and codex homes must succeed after the dedup check."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home, cx = tmp_path / "home", tmp_path / "cx"

    assert install_skill("all", home=home, codex_home=cx) == 0
    assert (home / ".claude" / "skills" / "syncade" / "SKILL.md").is_file()
    assert (cx / "skills" / "syncade" / "SKILL.md").is_file()


# ── Overlapping destinations, and a rollback that can roll back ─────────────
#
# Dogfood 1's terminal blocker, and the worst outcome this PR could produce: a REFUSAL THAT
# DESTROYS. Round 2 caught destinations that are EQUAL; this is CONTAINMENT. Reproduced with
# CODEX_HOME inside the claude destination: preparing codex created directories under a path
# claude had already renamed to its backup, phase 2 could not rename its staged tree over the
# now-non-empty destination, and rollback DECLINED to restore because "the destination
# exists". Exit 60, the operator's working skill gone, the backup stranded under
# `.syncade.syncade-old-*`.


def test_destinations_that_contain_one_another_are_refused_before_staging(tmp_path, capsys):
    home = tmp_path / "home"
    assert install_skill("claude", home=home, codex_home=tmp_path / "cx") == 0
    claude = home / _CLAUDE
    before = _tree(claude)

    # CODEX_HOME *inside* the claude destination — overlapping, not equal.
    rc = install_skill("all", home=home, codex_home=claude)

    assert rc == 60
    assert _tree(claude) == before, "the claude skill was modified by a refused install"
    assert sorted(p.name for p in (home / ".claude" / "skills").iterdir()) == ["syncade"], (
        "a refused install left staging or backup artifacts behind"
    )
    assert "overlap" in capsys.readouterr().err


def test_identical_destinations_are_still_refused(tmp_path):
    """The equality case round 2 closed, kept so the containment fix cannot regress it.

    CODEX_HOME must be `~/.claude` for the two to COINCIDE: codex appends `skills/syncade`
    just as claude does. `~/.claude/skills` would make them siblings
    (`.../skills/skills/syncade`), which is a perfectly installable layout — my first version
    of this test used it and asserted a refusal that should not happen.
    """
    home = tmp_path / "home"
    assert install_skill("all", home=home, codex_home=home / ".claude") == 60


def test_rollback_restores_over_an_obstruction_syncade_created(tmp_path):
    """A rollback that refuses to roll back turns a recoverable failure into data loss —
    which is worse than having no rollback at all.

    Anything at the destination post-dates the backup being taken, so it cannot be the
    operator's original: that was moved to the backup. It is cleared, then restored.
    """
    from syncade.cli.install_skill import _restore

    dest, backup = tmp_path / "dest", tmp_path / "backup"
    backup.mkdir()
    (backup / "SKILL.md").write_text("the operator's original\n")
    dest.mkdir()
    (dest / "junk").mkdir()  # an obstruction created after the backup was taken

    _restore(dest, backup)

    assert (dest / "SKILL.md").read_text() == "the operator's original\n"
    assert not backup.exists(), "the backup was left stranded"


def test_a_failed_restore_says_where_the_backup_is(tmp_path, capsys):
    """Failing silently would leave the operator hunting for a hidden directory."""
    from syncade.cli.install_skill import _restore

    dest, backup = tmp_path / "locked" / "dest", tmp_path / "backup"
    backup.mkdir()
    (backup / "SKILL.md").write_text("original\n")
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked").chmod(0o555)
    try:
        _restore(dest, backup)
    finally:
        (tmp_path / "locked").chmod(0o755)

    err = capsys.readouterr().err
    assert str(backup) in err, "a failed restore did not say where the backup is"


# ── Phase-1 staging dir cleanup — minor fixed in round-1 review ───────────────
#
# Before the fix, the phase-1 exception handler only removed staging dirs for
# entries already appended to ``staged``. If the current harness failed AFTER
# its tmp dir was created but BEFORE ``staged.append`` (e.g. during the backup
# mkdtemp), its tmp dir was left under the skills parent while the error message
# said "Nothing was changed".
#
# The fix: ``_current_tmp`` tracks the active staging dir; the except block
# cleans it up before iterating ``staged``.


def test_phase1_staging_dir_cleaned_up_on_pre_append_failure(tmp_path, monkeypatch, capsys):
    """A staging dir for the second harness is removed even when phase-1 fails
    before staged.append — i.e. when the backup mkdtemp raises OSError."""
    import tempfile as _real_tempfile  # noqa: PLC0415

    from syncade.cli import install_skill as mod

    home, cx = tmp_path / "home", tmp_path / "cx"
    # Both destinations must exist so the backup-rename path is taken for both.
    assert install_skill("all", home=home, codex_home=cx) == 0

    codex_parent = cx / "skills"

    call_n = [0]
    real_mkdtemp = _real_tempfile.mkdtemp

    def patched_mkdtemp(*args, **kwargs):
        call_n[0] += 1
        # Calls per install_skill("all") with two existing dests:
        #   1: claude staging tmp, 2: claude backup tmp,
        #   3: codex staging tmp,  4: codex backup tmp  ← inject here
        if call_n[0] == 4:
            raise OSError("injected: codex backup mkdtemp failed")
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(mod.tempfile, "mkdtemp", patched_mkdtemp)

    rc = install_skill("all", home=home, codex_home=cx)
    assert rc == 60, "a phase-1 failure should exit 60"

    # No staging temp dirs should remain anywhere under the codex skills parent.
    leftovers = list(codex_parent.glob(".syncade.syncade-new-*"))
    assert not leftovers, f"phase-1 staging dir leaked: {leftovers}"
