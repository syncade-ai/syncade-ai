"""Is the operator's INSTALLED skill still the one this package ships? — PR-h-field-07 item 3.

Read-only. It classifies; it never writes, and it never touches the destination beyond reading
the files the current bundle names.

**Staleness is decided by CONTENT, not by a recorded version number**, which is a deliberate
departure from the brief and the safer of the two designs for three separate reasons:

1. *The record cannot carry a version without being reshaped.* ``.syncade-install.json`` is
   exactly ``{"files": {…}}`` and is protected by a self-hash over its canonical BYTES; any extra
   top-level key fails it closed (measured). Failing closed means "we can prove nothing", which
   makes every differing file the operator's — so the very installs this feature exists to help
   would start REFUSING to upgrade.
2. *It would break every existing install.* Records already on disk are the old shape. Accepting
   both shapes means two canonical forms and two self-hash paths, in the one structure that took
   three consecutive dogfood rounds to make bypass-proof.
3. *Content is a strictly better signal anyway.* The documented install has been an unpinned
   ``git+https://`` URL, so two installs reporting the same ``__version__`` routinely ship
   different skill files. A version compare is blind to exactly that case; hashes are not.

The three states the brief asks for fall straight out of data that already exists — what the
package would write now, versus what is on disk, versus what the record says we wrote.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from syncade.cli.install_skill import (
    _MANIFEST,
    _bundle,
    _dest,
    _read_manifest,
    _sha,
    resolve_homes,
)

HARNESSES = ("claude", "codex")

#: ``absent``   nothing installed here — not a problem, and never reported.
#: ``in_sync``  every packaged file is present and byte-identical.
#: ``stale``    ours, unmodified, but from an older package. THE actionable state.
#: ``modified`` at least one file is not what we wrote — the operator's. Never nagged about.
#: ``unknown``  files differ and there is no usable record, so ownership is unproven.
Status = Literal["absent", "in_sync", "stale", "modified", "unknown"]


@dataclass(frozen=True)
class SkillStatus:
    harness: str
    status: Status
    path: Path


def _classify(dest: Path, bundle: dict[str, bytes], record: dict[str, str]) -> Status:
    # The self-hash entry is the record vouching for itself, not a file we wrote; excluding it
    # is what makes `known` mean "files we can actually account for".
    known = {k: v for k, v in record.items() if k != _MANIFEST}
    differing: list[str] = []

    for name, packaged in bundle.items():
        try:
            on_disk = (dest / name).read_bytes()
        except OSError:
            # Missing or unreadable. If we recorded writing it, someone removed it — theirs.
            # If we did not, the file is new in this package version — that is staleness.
            if name in known:
                return "modified"
            differing.append(name)
            continue
        if _sha(on_disk) != _sha(packaged):
            differing.append(name)
            # `known and` is load-bearing. An ABSENT record proves nothing, so it cannot
            # convict the operator — without this guard every recordless install (which is
            # every install predating the current installer, including both of mine) is
            # misreported as operator-edited and silently skipped. Only a record that
            # CONTRADICTS the file on disk is evidence of an edit.
            if known and known.get(name) != _sha(on_disk):
                # Returns immediately so a mixed tree is "modified" rather than "stale": an
                # update notice aimed at someone's own edits is worse than no notice at all.
                return "modified"

    # Any entry in the destination that neither the current bundle nor the installer manifest
    # accounts for is the operator's. A mixed tree — bundled files that look stale PLUS an
    # extra file we never wrote — resolves to "modified" (silent) rather than "stale"
    # (actionable): pointing at --install-skill for a tree it would immediately refuse is
    # worse than no notice. The conservative direction misses a stale notice; the other
    # costs the operator's trust.
    #
    # Exception: a file proven ours by the install record (hash matches the recorded hash) is
    # syncade-owned — it was in an older bundle but was since removed or renamed. Treat it as
    # a stale difference, not an operator modification, so --install-skill can fix it.
    try:
        for entry in dest.iterdir():
            if entry.name not in bundle and entry.name != _MANIFEST:
                if entry.is_file() and known:
                    try:
                        on_disk_hash = _sha(entry.read_bytes())
                    except OSError:
                        return "modified"
                    if known.get(entry.name) == on_disk_hash:
                        differing.append(entry.name)
                        continue
                return "modified"
    except OSError:
        pass  # cannot scan dest; treat as clean and continue

    if not differing:
        return "in_sync"
    # Every difference is explained by the record, so this is our own older output — unless
    # there is no record, in which case we can prove nothing and must not claim it is ours.
    return "stale" if known else "unknown"


def skill_status(
    harness: str, *, home: Path | None = None, codex_home: Path | None = None
) -> SkillStatus:
    """Classify one harness's installed skill. Never raises."""
    # Resolved by the installer's own function, never re-derived here: an independent default
    # is how CODEX_HOME got ignored at runtime while being honoured at install time.
    home, codex_home = resolve_homes(home, codex_home)
    dest = _dest(harness, home, codex_home)
    try:
        if not dest.is_dir():
            return SkillStatus(harness, "absent", dest)
        return SkillStatus(harness, _classify(dest, _bundle(harness), _read_manifest(dest)), dest)
    except Exception:  # noqa: BLE001
        # Same contract as the update check: a skill-drift notice must never be able to fail a
        # run. An unreadable destination or a missing packaged bundle reads as "nothing to say".
        return SkillStatus(harness, "absent", dest)


def stale_skills(*, home: Path | None = None, codex_home: Path | None = None) -> list[SkillStatus]:
    """Every installed harness whose skill is out of date or of unproven origin.

    ``absent``/``in_sync``/``modified`` are all silent — the first two have nothing to say, and
    the third is the operator's own work.
    """
    found = [skill_status(h, home=home, codex_home=codex_home) for h in HARNESSES]
    return [s for s in found if s.status in ("stale", "unknown")]
