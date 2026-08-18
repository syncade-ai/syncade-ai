"""``syncade --install-skill`` — lay the bundled Agent Skill into the harness dirs.

The skill is shipped as package data (``src/syncade/skills/{claude,codex}/``), so a
``pip install``ed user with no repo checkout can still install the ``/syncade`` skill.
Read via :func:`importlib.resources.files` so it works from a wheel or an editable install
alike. This is the ONLY installer: a second, shell copy existed and was deleted in
PR-h-04.5 item 1. Two implementations of the same job is what made a PR-h-04 dogfood
round exist purely because a safety fix landed in one and not the other, and every rule
below would otherwise have to be written twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as _stat
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from syncade.exit_codes import CLI_USAGE_ERROR, SUCCESS, WORKTREE_ERROR

_VALID = ("all", "claude", "codex")

#: Written into each destination: the sha256 of every file this installer put there.
#: It is a RECORD, not a marker. A marker ("syncade was here") cannot tell an upgrade from
#: an edit — both PR-h-04 attempts that used one failed a blind panel. Hashes can: a file
#: whose bytes match what we recorded is ours whatever version wrote it, and a file whose
#: bytes match nothing we recorded is the operator's whatever it is named.
#:
#: **Its trust boundary, stated rather than implied** (PR-h-04.5 item 4). The record is not
#: an authentication token and cannot be one: syncade holds no secret, so a record that
#: parses is believed. That is bounded, and the bound is what makes it acceptable —
#: anyone able to edit the record already has write access to the destination and could
#: delete those files directly, so it confers no capability its writer did not already
#: have. What it must never do is reach FURTHER than that, which is why keys are
#: constrained to plain filenames and are used only for lookup, never as paths.
#:
#: It is also NOT exempt from the survey (the round-4 finding: "the sentinel was excluded
#: from its own loss detection"). It is skipped only when its self-hash verifies — a
#: parseable ``{"files": {…}}`` is necessary but not sufficient. The manifest stores the
#: SHA256 of its own inner dict (bundle hashes only, without the self-hash entry) under
#: its own filename key; ``_scan_casualties`` re-derives that hash from the current bytes
#: and skips the manifest only on a match. A parseable operator edit to any inner hash
#: value invalidates the self-hash and causes the manifest to be flagged as a casualty.
_MANIFEST = ".syncade-install.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``object_pairs_hook`` for ``json.loads`` that rejects duplicate object names.

    A manifest with a duplicate key (e.g. two ``"README.md"`` entries) would be collapsed
    silently by Python's default parser — last value wins, canonical dict unchanged, self-hash
    still verifies, operator edit overwritten at exit 0. Raising here lets the caller's
    ``except ValueError`` catch it and return ``{}``, so the manifest is treated as unknown.
    """
    seen: dict[str, object] = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key: {k!r}")
        seen[k] = v
    return seen


def _parse_manifest(raw: bytes) -> dict[str, str]:
    """Parse manifest bytes and verify the self-hash; return the verified prior dict or ``{}``.

    The returned dict includes ``{_MANIFEST: inner_sha}`` when the self-hash verifies.
    ``_scan_casualties`` uses that entry to confirm the manifest is our unmodified record.
    Old-style manifests (no self-hash entry) return only the inner dict; the missing
    ``_MANIFEST`` key causes ``_scan_casualties`` to flag the manifest as a casualty.

    **Fails closed on any non-plain key, non-string value, or duplicate key**, rather than
    filtering or collapsing them silently. Silent filtering or last-wins collapsing would let
    the self-hash verify against a canonical dict that no longer represents the file's actual
    content: an operator who added ``"../note": "value"``, ``"key": 42``, or a duplicate
    ``"README.md"`` before the real one would find their edit invisible to the self-hash
    check, so the manifest would pass as ours and be silently overwritten at exit 0.
    """
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        if set(data.keys()) != {"files"}:
            return {}
        recorded = data["files"]
        if not isinstance(recorded, dict):
            return {}
        # Every key and value must be a string. Non-string filtering would silently hide the
        # entry from the self-hash check, making the self-hash verify against edited content.
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in recorded.items()):
            return {}
        raw_self = recorded.get(_MANIFEST)
        inner = {k: v for k, v in recorded.items() if k != _MANIFEST}
        # All non-manifest keys must be plain filenames. Filtering traversal keys silently
        # has the same defect: self-hash verifies against a dict that hides the edit.
        if any(not _is_plain_name(k) for k in inner):
            return {}
        if isinstance(raw_self, str):
            canonical = json.dumps({"files": inner}, indent=2) + "\n"
            if _sha(canonical.encode()) == raw_self:
                # Verify raw bytes match the expected canonical form. A lexical edit
                # (extra whitespace, alternative JSON escaping) preserves the parsed dict
                # and inner hash, so the self-hash alone cannot detect it — the operator's
                # edit looks identical to our canonical form after re-parsing. Byte
                # comparison closes the gap: any deviation from canonical means operator.
                expected = (
                    json.dumps({"files": {**inner, _MANIFEST: raw_self}}, indent=2) + "\n"
                ).encode()
                if raw != expected:
                    return {}  # lexical edit: raw bytes diverge from canonical form
                return {**inner, _MANIFEST: raw_self}
            return {}  # tampered: inner changed but self-hash not updated
        return inner  # old-style manifest; _MANIFEST absent from returned dict
    except (ValueError, KeyError, AttributeError, TypeError):
        return {}


def _read_manifest(dest: Path) -> dict[str, str]:
    """``{filename: sha256}`` this installer last wrote here, or ``{}`` if unknown.

    Unreadable or malformed reads as ``{}``, which means "we can prove nothing", which
    means every differing file is treated as the operator's. Failing closed here costs one
    refusal; failing open costs their file.

    A parseable file with ANY extra top-level keys (e.g. an operator-added
    ``"operator_note"``) also reads as ``{}`` — the manifest must have exactly the shape
    ``{"files": {…}}`` this installer writes.

    **lstats before reading.** A FIFO at the manifest path would block forever on
    ``read_bytes``; a symlink could redirect to a blocking target. Both bypass the
    ``_scan_casualties`` special-file protections for the manifest path specifically.
    Returning ``{}`` here lets ``_scan_casualties`` classify the entry normally.
    """
    mpath = dest / _MANIFEST
    try:
        st = mpath.lstat()
    except OSError:
        return {}
    if not _stat.S_ISREG(st.st_mode):
        return {}
    try:
        return _parse_manifest(mpath.read_bytes())
    except OSError:
        return {}


def _is_plain_name(key: str) -> bool:
    """A record key must be a bare filename — no separator, no ``..``, not absolute.

    The bundle is flat, so those are the only keys this installer ever writes. Today the
    keys are used ONLY for dictionary lookup against names derived from walking the
    destination, so a crafted `../../../.bashrc` already matches nothing — measured. This
    makes that safety structural instead of incidental: if a future change ever joins a key
    onto a path, the traversal it would open is already closed here.
    """
    return bool(key) and key not in (".", "..") and "/" not in key and not Path(key).is_absolute()


def resolve_homes(home: Path | None, codex_home: Path | None) -> tuple[Path, Path]:
    """The ONE place harness home directories are resolved.

    Extracted in PR-h-field-07 because it was not one place: the installer honoured
    ``CODEX_HOME`` while the new runtime skill-status path re-derived ``~/.codex`` by hand. A
    Codex skill installed under a custom home then read as ABSENT, so no stale notice fired and
    ``--update`` skipped re-installing it — the exact drift this PR exists to close, hidden by
    the copy. Both reviewers flagged it independently.
    """
    home = home or Path.home()
    if codex_home is None:
        env_cx = os.environ.get("CODEX_HOME")
        codex_home = Path(env_cx) if env_cx else home / ".codex"
    return home, codex_home


def _dest(harness: str, home: Path, codex_home: Path) -> Path:
    if harness == "claude":
        return home / ".claude" / "skills" / "syncade"
    return codex_home / "skills" / "syncade"


def _bundle(harness: str) -> dict[str, bytes]:
    """The files this installer would write, by name."""
    src = files("syncade") / "skills" / harness
    return {e.name: e.read_bytes() for e in src.iterdir() if e.is_file()}


def _scan_casualties(
    root: Path,
    current: Path,
    bundle: dict[str, bytes],
    prior: dict[str, str],
    out: list[str],
) -> None:
    """Walk ``current`` and append casualty descriptions to ``out``.

    ``root`` is the top-level destination — used to compute names relative to it for
    bundle/prior lookup. Does NOT report ``current`` itself; only its contents.

    Uses ``os.scandir`` rather than ``rglob`` so that unreadable subdirectories are
    explicitly caught (``rglob`` silently skips them, dropping their contents from the
    survey). Special files (FIFOs, sockets) are classified by stat mode without opening
    them — ``read_bytes`` on a FIFO blocks indefinitely. Empty subdirectories are reported
    as casualties because ``rmtree`` removes them along with everything else.
    """
    try:
        with os.scandir(current) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError as exc:
        out.append(f"{current} — would be LOST (directory unreadable: {exc})")
        return
    for entry in entries:
        path = Path(entry.path)
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            out.append(f"{path} — would be LOST (unreadable, cannot be proven syncade's)")
            continue
        mode = st.st_mode
        if _stat.S_ISLNK(mode):
            # A symlink INSIDE the destination is not the same as a symlinked DESTINATION,
            # and the "symlinks are not loss" rule must not be stretched to cover it.
            out.append(f"{path} — would be DELETED (a symlink you made; its target is untouched)")
        elif _stat.S_ISDIR(mode):
            # Bundle is flat; any subdirectory is operator-added and rmtree removes it.
            # Recurse so its contents are individually reported as casualties.
            prev = len(out)
            _scan_casualties(root, path, bundle, prior, out)
            if len(out) == prev:
                # Recursion added nothing: directory scanned successfully with 0 entries.
                out.append(
                    f"{path}/ — would be DELETED (an empty directory syncade did not create)"
                )
        elif not _stat.S_ISREG(mode):
            # FIFO, socket, device file — do not open; classify by type without reading.
            out.append(f"{path} — would be DELETED (a special file syncade did not create)")
        else:
            name = str(path.relative_to(root))
            try:
                data = path.read_bytes()
            except OSError:
                out.append(f"{path} — would be LOST (unreadable, cannot be proven syncade's)")
                continue
            if name == _MANIFEST:
                # Recognised by self-hash verification, not just by parsing. prior[_MANIFEST]
                # is set only when _read_manifest verified self-consistency. An operator edit
                # that keeps valid JSON invalidates the self-hash, causing _parse_manifest to
                # return {} and prior[_MANIFEST] to be absent — so the manifest is flagged.
                self_hash = prior.get(_MANIFEST)
                if self_hash is not None and _parse_manifest(data).get(_MANIFEST) == self_hash:
                    continue
            if bundle.get(name) == data:
                continue  # byte-identical to what we would write: rewriting changes nothing
            if prior.get(name) == _sha(data):
                continue  # WE wrote these exact bytes on a previous install — an upgrade
            verb = "OVERWRITTEN" if name in bundle else "DELETED"
            why = (
                "differs from the bundled copy"
                if name in bundle
                else "not part of the syncade skill"
            )
            out.append(f"{path} — would be {verb} ({why}, and syncade did not write it)")


def _casualties(dest: Path, bundle: dict[str, bytes], prior: dict[str, str]) -> list[str]:
    """Everything under ``dest`` that installing would DESTROY, described for a human.

    The installer replaces the destination wholesale, so "destroy" means two things: a file
    that is not part of the bundle gets DELETED, and a bundled name whose bytes differ gets
    OVERWRITTEN. Both are losses; neither was announced before PR-h-04.5.

    A SYMLINKED destination is not loss and returns nothing. The README documents
    symlink-to-checkout, re-running the installer over it has always replaced the link, and
    unlinking a symlink destroys nothing — the checkout it pointed at is untouched. Refusing
    there would break a documented path to protect data that was never at risk.

    Scanned RECURSIVELY via ``_scan_casualties``: the bundle is flat, but ``rmtree`` takes
    whole subdirectories, so an operator's ``notes/`` directory is exactly as destroyed as
    their loose file. Empty and unreadable subdirectories are both reported as casualties.
    Non-regular entries (FIFOs, sockets) are classified without opening them.
    """
    if dest.is_symlink():
        return []
    if not dest.exists():
        return []
    if dest.is_file():
        # A regular file at the skill destination path will be unlinked and replaced with
        # a directory. This is not a documented usage; a file here cannot be proven
        # installer-owned, so it is always a casualty.
        return [f"{dest} — would be DELETED (a regular file where the skill directory is expected)"]
    out: list[str] = []
    _scan_casualties(dest, dest, bundle, prior, out)
    return out


def _restore(dest: Path, backup: Path) -> None:
    """Move ``backup`` back to ``dest``, clearing anything syncade put in the way.

    Restoring must not be blocked by an obstruction SYNCADE created. Reproduced: with
    CODEX_HOME inside the claude destination, preparing codex made directories under a path
    claude had already renamed to its backup, so ``dest`` existed again and rollback declined
    — leaving the operator's working skill deleted and their backup stranded under a hidden
    name. **A rollback that refuses to roll back turns a recoverable failure into data loss**,
    which is worse than having no rollback at all.

    Anything at ``dest`` now post-dates the backup being taken, so it cannot be the
    operator's original — that was moved to ``backup``. Clear it, then restore. If even that
    fails, say where the backup is rather than failing silently.
    """
    try:
        if dest.is_symlink() or dest.is_file():
            dest.unlink(missing_ok=True)
        elif dest.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
        backup.rename(dest)
    except OSError:
        print(
            f"[syncade] could not restore {dest} — your previous skill is preserved at "
            f"{backup}; move it back by hand.",
            file=sys.stderr,
        )


def install_skill(
    target: str = "all",
    *,
    home: Path | None = None,
    codex_home: Path | None = None,
    force: bool = False,
) -> int:
    """Copy the bundled skill into the harness skill directories. Returns a CLI exit code.

    **Refuses rather than destroying.** Before PR-h-04.5 this ran ``rmtree`` over the
    destination and rewrote it: measured on a real tree, a hand-edited ``SKILL.md`` was
    overwritten and two unrelated operator files were deleted, at exit 0 with no prompt,
    warning or backup. Now every file that installing would destroy is surveyed FIRST; if
    there are any, the install refuses (exit 60), names each one and what would happen to
    it, and points at ``--force-install``.

    ``force=True`` restores the old destructive behaviour deliberately. It is informed
    consent, not a safe mode — the casualties are still printed.
    """
    if target not in _VALID:
        print(
            f"[syncade] unknown --install-skill target {target!r}; expected one of {_VALID}",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR

    home, codex_home = resolve_homes(home, codex_home)

    harnesses = ["claude", "codex"] if target == "all" else [target]
    plan = [
        (h, d, _bundle(h), _read_manifest(d))
        for h, d in ((h, _dest(h, home, codex_home)) for h in harnesses)
    ]

    # Refuse if any two harnesses resolve to the same destination. Duplicate destinations
    # arise when CODEX_HOME == ~/.claude (or a symlink that resolves there): phase 1 stages
    # both, then phase 2 crashes renaming the second tree over the already-populated dest
    # and leaves staging artifacts behind. Detecting this before any I/O keeps it clean.
    if len(plan) > 1:
        # OVERLAP, not just equality. Equal destinations were the obvious case; CONTAINMENT
        # is the damaging one, and it was reproduced: with CODEX_HOME inside the claude
        # destination, preparing codex creates directories underneath a path claude had
        # already renamed to a backup, phase 2 then cannot rename its staged tree over the
        # now-non-empty destination, and rollback declined to restore because "the
        # destination exists". Exit 60 with the operator's working skill GONE and the backup
        # stranded under a hidden name — a refusal that destroys, which is the worst outcome
        # this PR can produce. One harness's directory tree must never sit inside another's.
        resolved = [(h, Path(str(d.resolve()))) for h, d, _, _ in plan]
        for i, (h_a, a) in enumerate(resolved):
            for h_b, b in resolved[i + 1 :]:
                if a == b or a in b.parents or b in a.parents:
                    relation = (
                        "are the same directory" if a == b else "overlap — one contains the other"
                    )
                    print(
                        f"[syncade] cannot install 'all': the {h_a!r} and {h_b!r} destinations "
                        f"{relation}:\n  {h_a}: {a}\n  {h_b}: {b}\n"
                        "Installing both would have one harness write inside the other. Set "
                        "CODEX_HOME to a directory outside ~/.claude, or install one harness "
                        "at a time.",
                        file=sys.stderr,
                    )
                    return WORKTREE_ERROR

    # Survey EVERY destination before writing ANY of them. The survey runs even under
    # --force: consent to destroying files is not consent to being told nothing about
    # which ones. Skipping it there would also make this function's own docstring false.
    losses = [line for _, dest, bundle, prior in plan for line in _casualties(dest, bundle, prior)]
    if losses:
        listing = "\n".join(f"  {line}" for line in losses)
        if not force:
            print(
                f"[syncade] refusing to install: {len(losses)} file(s) would be lost.\n"
                f"{listing}\n\n"
                "syncade did not write these and will not destroy them. Move or delete them "
                "yourself, or re-run with --force-install to overwrite deliberately.",
                file=sys.stderr,
            )
            return WORKTREE_ERROR
        print(
            f"[syncade] --force-install: destroying {len(losses)} file(s) syncade did not "
            f"write.\n{listing}",
            file=sys.stderr,
        )

    # PHASE 1 — build every harness beside its destination, touching none of them.
    #
    # Two improvements over the previous version:
    #
    # (a) tempfile.mkdtemp produces a unique staging directory so a pre-existing path
    #     named ".<dest>.syncade-new" (operator content, stale run) is never deleted
    #     before ownership is proven. The old fixed name was blindly rmtree'd.
    #
    # (b) Any existing destination is RENAMED to a backup path before phase 2, proving
    #     it is removable while still reversible. The previous approach called
    #     shutil.rmtree in phase 2 AFTER the first harness was already swapped — a
    #     read-only destination (or one with a protected subdirectory) raised an
    #     uncaught PermissionError and left a half-applied install. Rename needs only
    #     write permission on the PARENT directory, not on the destination itself, so
    #     it succeeds in cases where rmtree would fail.
    staged: list[tuple[str, Path, Path, Path | None]] = []
    _current_tmp: Path | None = None  # staging dir created but not yet in staged
    try:
        for harness, dest, bundle, _prior in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _current_tmp = Path(
                tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.syncade-new-")
            )
            tmp = _current_tmp
            for name, data in bundle.items():
                (tmp / name).write_bytes(data)
            # The record of what we wrote, so the NEXT install can tell an upgrade from an
            # edit. Without it every version bump refuses (measured: exit 60 on an ordinary
            # upgrade), which trains operators to pass --force-install reflexively and loses
            # the protection entirely — rule 5's failure mode.
            #
            # The manifest also records its own SHA256 (the self-hash) so _scan_casualties
            # can verify it is unmodified on re-install. The self-hash is the SHA of the
            # INNER canonical form (bundle hashes only, without the self-hash entry itself),
            # breaking the circularity: compute inner → hash it → embed that hash.
            inner_files = {n: _sha(d) for n, d in bundle.items()}
            inner_sha = _sha((json.dumps({"files": inner_files}, indent=2) + "\n").encode())
            (tmp / _MANIFEST).write_text(
                json.dumps({"files": {**inner_files, _MANIFEST: inner_sha}}, indent=2) + "\n",
                encoding="utf-8",
            )
            # Move any existing destination to a UNIQUE backup path — proves it is removable
            # while still reversible. The path is unique (mkdtemp-derived) so an operator file
            # that happens to sit at the old predictable ".syncade-old" path is never deleted
            # before ownership is proven. Rename needs only parent write permission, so a
            # read-only destination that would block shutil.rmtree is still movable.
            backup: Path | None = None
            if dest.exists() or dest.is_symlink():
                _bk = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.syncade-old-"))
                _bk.rmdir()  # free the path before rename; race is within the parent we control
                backup = _bk
                dest.rename(backup)  # raises OSError if dest cannot be moved
            staged.append((harness, dest, tmp, backup))
            _current_tmp = None  # now tracked by staged; don't double-clean on exception
    except OSError as exc:
        # Clean up any staging dir created for the current harness but not yet in staged.
        if _current_tmp is not None:
            shutil.rmtree(_current_tmp, ignore_errors=True)
        # Restore any destinations already moved to backup, then remove staged dirs.
        for _, d, t, bk in staged:
            shutil.rmtree(t, ignore_errors=True)
            if bk is not None:
                _restore(d, bk)
        print(
            f"[syncade] could not prepare the skill install: {exc}\n"
            "Nothing was changed — no harness was modified.",
            file=sys.stderr,
        )
        return WORKTREE_ERROR

    # PHASE 2 — rename each staged tree into place. Phase 1 already moved every prior
    # destination to a backup, so only renames remain here — no rmtree of the old dest.
    # Wrapped in try/except so a mid-phase failure (e.g. a race that repopulates the dest
    # between phase 1 and phase 2) rolls back cleanly rather than leaving a partial install.
    completed: list[tuple[str, Path, Path, Path | None]] = []
    try:
        for harness, dest, tmp, backup in staged:
            tmp.rename(dest)
            completed.append((harness, dest, tmp, backup))
            print(f"[syncade] installed {harness} skill -> {dest}", file=sys.stderr)
    except OSError as exc:
        # Undo every successfully placed tree and restore its backup; remove remaining
        # staged dirs. Best-effort: an operator can recover any surviving backup manually.
        completed_dests = {d for _, d, _, _ in completed}
        for _, d, t, bk in staged:
            if d in completed_dests:
                shutil.rmtree(d, ignore_errors=True)
            else:
                shutil.rmtree(t, ignore_errors=True)
            if bk is not None:
                _restore(d, bk)
        print(
            f"[syncade] could not complete the skill install: {exc}\n"
            "Rolling back — attempting to restore the previous state.",
            file=sys.stderr,
        )
        return WORKTREE_ERROR

    # Clean up backups now that all swaps succeeded. ignore_errors because a backup
    # with a protected subdirectory (0o555) may not fully rmtree; leaving it behind
    # is preferred over blocking the install on cleanup.
    # is_symlink() must precede is_dir(): Path.is_dir() follows symlinks, so a symlink
    # to a directory evaluates to True and shutil.rmtree refuses the symlink silently,
    # leaving a stale .syncade.syncade-old-* symlink in the skills directory.
    for _, _, _, backup in staged:
        if backup is None:
            pass
        elif backup.is_symlink() or backup.is_file():
            backup.unlink(missing_ok=True)
        elif backup.is_dir():
            shutil.rmtree(backup, ignore_errors=True)

    print("[syncade] done — restart your harness to pick up the skill.", file=sys.stderr)
    return SUCCESS
