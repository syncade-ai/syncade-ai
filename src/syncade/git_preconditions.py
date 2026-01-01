"""Git precondition for the main ``syncade <pr-doc>`` run path: ensure an
enclosing git repository exists, initializing one when it does not.

syncade is built entirely on git — the reviewer diff, the worktree
isolation, SHA-based stall detection, and branch advance all require a
repository. For the novice / "vibe coder" audience, "no ``git init`` yet"
is the common starting state, not an edge case. Rather than hard-stopping
at exit 60 (the historical :func:`~syncade.snapshot.discover_repo_root`
behavior when invoked outside a working tree), the main run path silently
initializes a repo: syncade gets the substrate it requires and the user
gets a version-control safety net they did not know to set up.

This is a *precondition*. It runs before ``take_snapshot`` and touches
nothing in the review loop — reviewers / synth / producer / verdict are
byte-unchanged. Only :func:`syncade.cli._run` (the real review path)
calls it; the diagnostic modes (``--selfcheck`` / ``--auth-check`` /
``--spec-audit`` / ``--resume``) keep ``discover_repo_root``'s hard-stop
behavior — they are not in the "review this work" flow and must not
mutate the operator's directory.

All git goes through :func:`syncade.process.run_subprocess` (the mandated
wrapper), mirroring
:func:`syncade.synthesizer.workspace._init_workspace_git` and the
``selfcheck.py`` init sequence.
"""

from __future__ import annotations

from pathlib import Path

from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    run_subprocess,
)
from syncade.snapshot import SnapshotError, discover_repo_root

_GIT_TIMEOUT_SECONDS: float = 30.0

_GIT_MISSING_MESSAGE = (
    "git is required but was not found on PATH. Install git and re-run "
    "syncade (macOS: `xcode-select --install`; Debian/Ubuntu: "
    "`sudo apt install git`; Windows: https://git-scm.com/download/win)."
)

_BASELINE_COMMIT_MESSAGE = "syncade: baseline commit"

# Conservative ignore patterns. Covers the secrets and junk a baseline commit
# must never sweep in (.env, private keys, syncade's own .syncade/secrets.toml,
# syncade-generated run state under .syncade/runs/ and .syncade/last-reviewed.json,
# dependency/build directories). .syncade/config.toml stays trackable — we ignore
# generated/run-state paths, not all of .syncade/. Kept deliberately short and
# editable — it is a safety floor, not a curated per-language ignore. These
# patterns are written to BOTH surfaces: always to the .git/info/exclude file
# (the secret-exclusion guarantee) and, as a starter-file nicety, to a visible
# .gitignore when that path is unoccupied.
_DEFAULT_GITIGNORE = """\
# syncade baseline ignore (auto-generated — edit freely)
.env
.env.*
credentials.json
.aws/
*.key
*.pem
id_rsa
id_dsa
id_ecdsa
id_ed25519
*.p12
*.pfx
.syncade/secrets.toml
.syncade/secrets.*
.syncade/secrets/
.syncade/runs/
.syncade/last-reviewed.json
.syncade/metrics.db
node_modules/
__pycache__/
*.pyc
.venv/
venv/
dist/
build/
.DS_Store
"""

# First-line marker used to make the .git/info/exclude append idempotent (so a
# re-init or repeated call never duplicates the block).
_EXCLUDE_MARKER = "# syncade baseline ignore"


class GitUnavailableError(Exception):
    """Raised when the ``git`` binary is not on ``PATH``.

    Distinct from :class:`~syncade.snapshot.SnapshotError` so the CLI can
    map "git is not installed" to an actionable install message rather
    than the generic "not inside a git repository" path. ``discover_repo_root``
    collapses both into ``SnapshotError``; this precondition splits them
    back apart so each gets the right remediation.
    """


def _git(cwd: Path, *args: str) -> None:
    """Run ``git <args>`` in ``cwd`` via the mandated wrapper, raising on a
    non-zero exit.

    Mirrors :func:`syncade.synthesizer.workspace._init_workspace_git`: a
    non-zero return code from an init / add / commit step is a real
    failure (workspace permissions, corrupt git config, ...) surfaced as a
    :class:`~syncade.process.SubprocessError`. A missing ``git`` binary
    raises :class:`~syncade.process.SubprocessNotFoundError`, which callers
    that need the distinction handle before reaching here.
    """
    result = run_subprocess(
        ["git", *args],
        cwd=cwd,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        step = args[0] if args else "<none>"
        raise SubprocessError(
            f"git {step} failed in {cwd} (rc={result.returncode}); stderr: {result.stderr[:200]!r}"
        )


def _write_repo_local_exclude(repo_root: Path) -> None:
    """Append the conservative safety patterns to ``.git/info/exclude``.

    This is the secret-exclusion GUARANTEE — not the visible ``.gitignore``.
    ``.git/info/exclude`` is git's per-repo, never-committed supplemental
    ignore: ``git add -A`` honors it exactly like ``.gitignore``, but it lives
    inside ``.git/`` so it neither clobbers nor can be defeated by whatever the
    user has at the ``.gitignore`` path (a missing/partial file, a directory,
    or a symlink). Writing the patterns here is what actually makes the
    baseline commit safe regardless of the ``.gitignore`` situation — the
    AC2-vs-never-clobber conflict the validation surfaced.

    The path is resolved via ``git rev-parse --git-path info/exclude`` so it is
    correct regardless of the ``.git`` layout; the block is appended
    idempotently (guarded by :data:`_EXCLUDE_MARKER`) so git's default
    template and any pre-existing excludes are preserved.
    """
    result = run_subprocess(
        ["git", "rev-parse", "--git-path", "info/exclude"],
        cwd=repo_root,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise SubprocessError(
            f"git rev-parse --git-path info/exclude failed in {repo_root} "
            f"(rc={result.returncode}); stderr: {result.stderr[:200]!r}"
        )
    # --git-path returns a path relative to repo_root (or absolute); Path
    # division handles both.
    exclude_path = (repo_root / result.stdout.strip()).resolve()
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
    if _EXCLUDE_MARKER in existing:
        return  # already present — idempotent
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    exclude_path.write_text(existing + separator + _DEFAULT_GITIGNORE, encoding="utf-8")


def ensure_repo_initialized(start_path: Path) -> Path:
    """Ensure ``start_path`` lives inside a git repo, initializing one if not.

    Returns the repo root in every success case:

    - An enclosing repo (at ``start_path`` or any ancestor) already exists
      → return its root with **no side effects** (today's
      ``discover_repo_root`` behavior; parent-repo discovery preserved).
    - No enclosing repo and ``git`` is present → ``git init`` at
      ``start_path``, write the conservative ignore patterns to
      ``.git/info/exclude`` (the secret-exclusion guarantee) and a starter
      ``.gitignore`` (only when that path is unoccupied), make a safe baseline
      commit, and return the new root.
    - ``git`` is not on ``PATH`` → raise :class:`GitUnavailableError`.

    Detection reuses :func:`~syncade.snapshot.discover_repo_root` (the
    canonical ``git rev-parse --show-toplevel`` path) — no second
    detection routine.

    Safe-add ordering is load-bearing: both ignore surfaces are written
    *before* ``git add -A`` so the baseline commit cannot capture secrets
    (``.env``, ``*.key``) or junk (``node_modules/``, build artifacts). The
    guarantee lives in ``.git/info/exclude`` (see
    :func:`_write_repo_local_exclude`), so it holds regardless of whether — or
    in what form — a ``.gitignore`` exists; an existing ``.gitignore`` is never
    clobbered.
    """
    # Happy path: an enclosing repo already exists (here or in a parent).
    # discover_repo_root reuses the canonical detection and returns the
    # resolved root with no side effects.
    try:
        return discover_repo_root(start_path)
    except SnapshotError:
        # No enclosing repo, OR git itself is missing — discover_repo_root
        # collapses both into SnapshotError. Disambiguate below before
        # deciding to initialize.
        pass

    # Distinguish "git binary missing" from "no repo here". The version
    # probe is cwd-independent, so it isolates "is git on PATH" from
    # "does start_path resolve to a working tree" — only the former is
    # GitUnavailableError.
    try:
        run_subprocess(["git", "--version"], timeout=_GIT_TIMEOUT_SECONDS)
    except SubprocessNotFoundError as exc:
        raise GitUnavailableError(_GIT_MISSING_MESSAGE) from exc

    # git is present and there is no enclosing repo → initialize one. Set the
    # initial branch to `main` via symbolic-ref (supported since git 1.7)
    # instead of `git init --initial-branch=main` (git >= 2.28 only), so
    # auto-init works on older platforms (e.g. Ubuntu 20.04's git 2.25). On an
    # unborn HEAD this just repoints the symbolic ref; the baseline commit then
    # lands on `main`, identical to --initial-branch on modern git.
    _git(start_path, "init", "--quiet")
    _git(start_path, "symbolic-ref", "HEAD", "refs/heads/main")

    # SAFETY GUARANTEE: write the conservative ignore patterns to
    # Write .git/info/exclude BEFORE staging, so the baseline commit cannot sweep in
    # secrets/junk — independent of whether/what .gitignore exists (or whether
    # the .gitignore path is a file, directory, or symlink). See
    # _write_repo_local_exclude.
    _write_repo_local_exclude(start_path)

    # NICETY (not the safety mechanism): give a brand-new project a visible,
    # committed starter .gitignore — but only when the path is genuinely
    # unoccupied. An existing .gitignore (regular file, directory, or symlink
    # incl. dangling — `exists()` follows symlinks, so `is_symlink()` also
    # guards the dangling case) is left untouched: never clobbered, and secret
    # exclusion does not depend on it (guaranteed by .git/info/exclude above).
    gitignore = start_path / ".gitignore"
    if not gitignore.exists() and not gitignore.is_symlink():
        gitignore.write_text(_DEFAULT_GITIGNORE, encoding="utf-8")

    # Stage everything (filtered by .git/info/exclude + any .gitignore) and
    # make the baseline commit. --allow-empty covers the case where nothing is
    # left to stage — e.g. a pre-existing .gitignore that ignores everything
    # (content `*`); it is a no-op when there ARE staged changes, so it never
    # forces a spurious empty commit in the common case.
    _git(start_path, "add", "-A")
    _git(
        start_path,
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        _BASELINE_COMMIT_MESSAGE,
    )

    # PR-v2-26: leave HEAD on a feature branch, not the auto-created `main`. Otherwise a
    # first run in a fresh dir would land on `main` and the default-branch commit guard
    # would refuse the committing loop — bad onboarding. `main` stays the clean baseline;
    # syncade's producer commits go on `syncade-work`.
    _git(start_path, "checkout", "--quiet", "-b", "syncade-work")

    # Re-resolve via the canonical path: confirms the init took and returns
    # git's own toplevel (resolved, symlink-normalized).
    return discover_repo_root(start_path)
