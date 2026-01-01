"""OpenSpec tier-B consumption.

Turns an existing **OpenSpec change folder** into the one artifact the syncade
core already consumes — a markdown spec at ``pr_doc_path`` — so a team that
already lives in OpenSpec can run syncade without re-writing a brief
("if you have openspec, we work with openspec").

**Consume, don't depend** (the load-bearing front-door constraint): this module
reads the OpenSpec markdown files *directly* with :mod:`pathlib`. It never invokes
the ``openspec`` binary, imports its tooling, adds a dependency, or mutates the
``openspec/`` folder. syncade stays Python / minimal-deps / format-agnostic — the
core cannot tell an OpenSpec-derived spec from a hand-written brief.

Folder shape consumed (verified against OpenSpec CLI v0.13.0)::

    openspec/changes/<change-id>/
        proposal.md                       # ## Why / ## What Changes / ## Impact
        specs/<capability>/spec.md        # ## ADDED|MODIFIED|REMOVED Requirements

The assembled spec concatenates ``proposal.md`` + every capability's spec delta
**verbatim** under generated headers (zero interpretation — the reviewers read
the OpenSpec markdown as-is; nothing is manufactured). Capabilities are ordered
deterministically (sorted) so the assembled spec is stable across runs.

The diff base is unchanged — it still comes from ``--base`` / ``--scope``.
OpenSpec deltas are relative to a living-spec base; syncade's diff is relative to
a git SHA. change does NOT reconcile those anchors: it consumes the change folder as
*intent* (the spec) only.
"""

from __future__ import annotations

from pathlib import Path

_ARCHIVE_DIRNAME = "archive"
"""OpenSpec archives completed changes under ``changes/archive/``; it is never an
active change to review."""


class SpecSourceError(Exception):
    """A spec source could not be resolved or assembled (no ``openspec/`` folder,
    unknown / ambiguous change-id, or a change folder missing ``proposal.md``).
    The CLI surfaces it as a stop-before-the-loop per CLAUDE.md's "Exit-code
    convention for CLI mode handlers"."""


def _changes_dir(repo_root: Path) -> Path:
    return repo_root / "openspec" / "changes"


def find_openspec_changes(repo_root: Path) -> list[str]:
    """Return the active OpenSpec change-ids under ``<repo>/openspec/changes/``,
    sorted. An active change is a subdirectory containing a ``proposal.md`` (the
    ``archive/`` subdir and any dir without a proposal are excluded). Returns
    ``[]`` when there is no ``openspec/changes/`` folder at all."""
    changes_dir = _changes_dir(repo_root)
    if not changes_dir.is_dir():
        return []
    ids: list[str] = []
    for child in sorted(changes_dir.iterdir()):
        if child.name == _ARCHIVE_DIRNAME:
            continue
        if child.is_dir() and (child / "proposal.md").is_file():
            ids.append(child.name)
    return ids


def resolve_openspec_change(repo_root: Path, change_id: str | None) -> str:
    """Pick the change-id to consume.

    - ``change_id`` given → it must be an active change, else
      :class:`SpecSourceError` (names the unknown id + lists the active ones).
    - ``change_id is None`` → auto-resolve IFF exactly one active change exists;
      zero or several → :class:`SpecSourceError` (ask, never guess — names the
      candidates so the operator can pass ``--openspec <id>``).
    """
    changes = find_openspec_changes(repo_root)
    if change_id is not None:
        if change_id not in changes:
            active = ", ".join(changes) if changes else "none"
            raise SpecSourceError(
                f"openspec change {change_id!r} not found under openspec/changes/ "
                f"(active changes: {active})"
            )
        return change_id
    if len(changes) == 1:
        return changes[0]
    if not changes:
        raise SpecSourceError(
            "no active openspec changes found under openspec/changes/ — pass a PR "
            "brief path, or create an openspec change proposal first"
        )
    raise SpecSourceError(
        "multiple active openspec changes ("
        + ", ".join(changes)
        + "); pass one explicitly: --openspec <change-id>"
    )


def assemble_openspec_spec(repo_root: Path, change_id: str) -> str:
    """Assemble ``openspec/changes/<change_id>/`` into a single self-contained
    markdown spec string: the proposal + every capability's spec delta, verbatim,
    under generated headers. Capabilities are sorted for determinism.

    Raises :class:`SpecSourceError` if the change folder has no ``proposal.md``.
    """
    change_dir = _changes_dir(repo_root) / change_id
    proposal = change_dir / "proposal.md"
    if not proposal.is_file():
        raise SpecSourceError(
            f"openspec change {change_id!r} has no proposal.md at {proposal} — "
            "cannot assemble a spec from it"
        )

    parts: list[str] = [
        f"# OpenSpec change: {change_id}",
        (
            "> Assembled by syncade from the OpenSpec change folder "
            f"`openspec/changes/{change_id}/` (read-only). The sections below are "
            "the change's proposal and spec deltas verbatim; review the diff "
            "against this intent."
        ),
        "## Proposal",
        proposal.read_text(encoding="utf-8").strip(),
    ]

    specs_dir = change_dir / "specs"
    if specs_dir.is_dir():
        delta_files = sorted(specs_dir.rglob("spec.md"), key=lambda p: p.as_posix())
        for delta in delta_files:
            capability = delta.parent.relative_to(specs_dir).as_posix()
            parts.append(f"## Spec delta: {capability}")
            parts.append(delta.read_text(encoding="utf-8").strip())

    return "\n\n".join(parts) + "\n"
