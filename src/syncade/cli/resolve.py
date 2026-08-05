"""Diff-base / spec-source resolution helpers for the CLI.

These turn a ``--scope`` token or an ``--openspec`` request into the concrete
inputs the loop consumes, printing an operator-facing message and returning
``None`` when the request can't be resolved.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from syncade.logging import Logger


def _current_branch(repo_root: Path) -> str | None:
    """The current branch name via ``git rev-parse --abbrev-ref HEAD``, or
    ``None`` for detached HEAD / failure (used to key the last-reviewed lookup)."""
    from syncade.process import run_subprocess

    try:
        result = run_subprocess(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root, timeout=10.0
        )
    except Exception:
        return None
    name = result.stdout.strip()
    return name if result.returncode == 0 and name and name != "HEAD" else None


def _resolve_scope_base(repo_root: Path, scope: str, logger: Logger) -> str | None:
    """Resolve a ``--scope`` token to a concrete base SHA, or ``None`` on
    an unresolvable scope (a message is printed; the caller stops before the
    loop). ``since-last-review`` reads the per-branch recorded SHA."""
    from syncade.base_resolution import BaseResolutionError, resolve_scope
    from syncade.persistence import read_last_reviewed

    branch = _current_branch(repo_root)
    last = read_last_reviewed(repo_root, branch) if branch else None
    try:
        resolved = resolve_scope(repo_root, scope, last_reviewed_sha=last)
    except BaseResolutionError as exc:
        print(f"[syncade] scope error: {exc}", file=sys.stderr)
        return None
    if resolved.note:
        # Scope fallback notes are operator-facing invariants that must surface
        # even in --quiet mode; bypass logger.warning (suppressed when quiet).
        print(f"[syncade] scope: {resolved.note}", file=sys.stderr)
    return resolved.base_sha


def _cli_proves_commit(
    repo_root: Path,
    base_ref: str | None,
    strip_files: Iterable[str],
    *,
    two_dot: bool = False,
) -> bool:
    """True ONLY when the CLI can prove this run will produce reviewable changes.

    The pre-auth commit guard exists so a doomed committing run does not first pay for a
    provider auth probe. Whether a run commits depends on the FILTERED diff, which the CLI
    cannot compute — it has not snapshotted. Four earlier attempts asked the opposite
    question ("can I prove there is NO change?") and each was wrong for a different input,
    because being wrong there REFUSES A VALID RUN (PR-h-02d.5).

    So the question is inverted and the answer is conservative: return True only when
    certain, and let ``run_review`` decide otherwise. A wrong answer here costs one auth
    probe; it can never refuse a run the library would accept.

    Certain case: no base — reviewers see full HEAD, non-empty in any repo with a commit.

    Everything else (any diff-shaping base) defers. ``git diff --name-only`` cannot
    replicate the authoritative classifier: it C-quotes non-ASCII paths (so strip-list
    basenames do not match the quoted form), and paths containing `` b/`` produce ambiguous
    ``diff --git`` headers that ``run_review`` classifies as ``diff_malformed`` with zero
    dispatches. Both shapes produce false proofs of commit under the basename approach,
    causing the CLI to refuse runs the library accepts.
    """
    return base_ref is None


def _resolve_openspec_pr_doc(repo_root: Path, change_id: str | None, logger: Logger) -> Path | None:
    """Resolve a ``--openspec`` request to a concrete ``pr_doc_path``, or
    ``None`` on an unresolvable proposal (a message is printed; the caller stops
    before the loop). Reads the OpenSpec proposal folder's markdown directly
    (consume-don't-depend — no ``openspec`` binary), assembles it into a spec, and
    writes it to a staging ``.md`` tempfile. The CLI asks ``run_review`` to copy
    that staged file into the run directory before ``run-init.json`` is written,
    then removes the staging tempfile after the run."""
    import tempfile

    from syncade.spec_source import (
        SpecSourceError,
        assemble_openspec_spec,
        resolve_openspec_change,
    )

    try:
        resolved_id = resolve_openspec_change(repo_root, change_id)
        spec = assemble_openspec_spec(repo_root, resolved_id)
    except SpecSourceError as exc:
        print(f"[syncade] openspec error: {exc}", file=sys.stderr)
        return None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix=f"syncade-openspec-{resolved_id}-",
        delete=False,
        encoding="utf-8",
    )
    tmp.write(spec)
    tmp.close()
    logger.warning(f"openspec: reviewing proposal {resolved_id!r} (assembled spec)")
    return Path(tmp.name)
