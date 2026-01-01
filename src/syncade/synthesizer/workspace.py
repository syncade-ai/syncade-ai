"""Cold-synth workspace setup helpers.

The load-bearing cold-synth invariant is that the subprocess cannot *read* the
repo root: the workspace lives in a fresh tempdir, cwd/-C/--add-dir are scoped to
it, and ``permissions=trusted-execute`` keeps the codex sandbox scoped there too
(see :mod:`syncade.synthesizer.driver`). That holds independently of the env.

The env scrub is defense-in-depth on top: it removes repo-root path references so
the subprocess cannot even *discover* where the repo lives — with ONE deliberate
exception, :data:`_AUTH_LOCATOR_KEYS` (``CODEX_HOME``). A credential-locator var
must survive the scrub or the login the auth gate probed is not the login that
runs (verified != runtime; see :mod:`syncade.auth_preflight`). So a *repo-local*
``CODEX_HOME`` — an unusual config where the user parked codex's login dir inside
the repo under review — does pass its path (which references repo_root) through to
the cold subprocess. That weakens *discovery* only: the sandbox above still
prevents the synth from reading anything under it, so blindness is intact. The
alternative — stripping it — silently swaps the user's codex credential for
whatever ``~/.codex`` holds, which is the worse failure.

- :data:`_PATH_LIST_ENV_KEYS` — env keys whose values are path lists. Filtered
  per segment so absolute repo-local entries get removed without dropping
  unrelated sibling paths.
- :func:`_path_is_relative_to` / :func:`_value_references_repo_path`
  — boundary-aware path-match helpers used by :func:`_scrub_env_for_cold_synth`.
  Both RESOLVE the candidate path before testing containment, so a symlink alias
  of the repo root (macOS ``/tmp`` -> ``/private/tmp``) cannot slip through.
- :func:`_scrub_env_for_cold_synth` — drops ``PWD`` / ``OLDPWD``,
  filters path-list vars, and drops scalar vars whose value references
  the repo root, EXCEPT the :data:`_AUTH_LOCATOR_KEYS` credential locators
  (see the module note above on why they survive).
- :func:`_init_workspace_git` — runs ``git init -q`` in the workspace
  so codex's ``permissions=trusted-execute`` mode launches.

The two underscore-prefixed names (``_scrub_env_for_cold_synth``,
``_init_workspace_git``) are imported by tests and by :mod:`syncade.spec_audit`.
The package ``__init__.py`` re-exports them so existing imports keep working.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from syncade.process import SubprocessError, run_subprocess

_AUTH_LOCATOR_KEYS = frozenset({"CODEX_HOME"})
"""Vars that tell a provider CLI WHERE its stored credential lives. Kept through the cold
scrub even when repo-local: stripping them re-auths the subprocess, so the mode the auth
gate probed (parent env) would differ from the mode that runs (scrubbed env). CODEX_HOME is
the codex login dir. Anthropic auth is a key VALUE (ANTHROPIC_API_KEY) or the OS keychain,
not an env-var path, so it needs no entry here."""

_PATH_LIST_ENV_KEYS = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "NODE_PATH",
        "CDPATH",
        "MANPATH",
        "INFOPATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "PKG_CONFIG_PATH",
    }
)


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is within ``parent`` after resolution."""
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


_VALUE_TOKEN_SPLIT = re.compile(r"[\s'\"=" + re.escape(os.pathsep) + r"]+")


def _value_references_repo_path(value: str, resolved_repo_root: Path) -> bool:
    """Detect repo-root path references without sibling-prefix false positives.

    Splits ``value`` into path-like tokens and RESOLVES each one before testing
    containment, rather than string-matching spellings of the repo root.

    Resolving the value, not the root, is the only way to catch symlink aliases:
    on macOS ``/tmp`` is a symlink to ``/private/tmp``, so a parent process's
    ``VIRTUAL_ENV=/tmp/<repo>/.venv`` refers to a repo root the orchestrator
    knows as ``/private/tmp/<repo>``. Matching root spellings cannot see that —
    you would have to enumerate every aliased spelling of the root, which is
    unbounded. Resolving the token collapses both spellings to the same real
    path and the containment test just works, in either direction.

    This is also how the path-list branch of :func:`_scrub_env_for_cold_synth`
    has always worked (via :func:`_path_is_relative_to`); the scalar branch was
    the one doing string comparison, and it was the one that leaked.

    Boundary correctness comes free: ``relative_to`` on resolved paths cannot
    produce a sibling-prefix false positive the way a substring scan can
    (``/private/tmp/repo-sibling`` is not within ``/private/tmp/repo``).
    """
    for token in _VALUE_TOKEN_SPLIT.split(value):
        if not token:
            continue
        candidate = Path(token).expanduser()
        if candidate.is_absolute() and _path_is_relative_to(candidate, resolved_repo_root):
            return True
    return False


def _scrub_env_for_cold_synth(env: dict[str, str], repo_root: Path) -> dict[str, str]:
    """Strip environment variables that would leak ``repo_root`` to
    the synth subprocess.

    Workspace-scoped cwd/``-C``/``--add-dir`` are one half of that; this is the
    other. The parent process's env typically carries:

    - ``PWD`` and ``OLDPWD`` from the shell — both contain the cwd
      at invocation time, which is normally inside the repo
      (``syncade`` is invoked from the repo or one of its
      subdirs). Dropped unconditionally.
    - Path-list vars like ``PATH`` and ``PYTHONPATH`` — split by
      :data:`os.pathsep`; only absolute segments contained by ``repo_root`` are
      removed. Sibling paths such as
      ``/tmp/syncade-dev/bin`` and relative child-workspace segments
      are preserved.
    - Other vars like ``VIRTUAL_ENV`` or tool cache settings that may
      reference a repo-local path. Dropped when their value contains
      ``repo_root`` as a path-boundary-delimited reference, not as an
      arbitrary substring.

    What stays: non-repo ``PATH`` segments (codex needs them to find
    its own binaries and any tools it shells out to), ``HOME`` (codex
    auth state lives under ``~/.codex/``), and everything else that
    doesn't reference the repo.

    Args:
        env: The parent invocation env.
        repo_root: The git repo root whose path string should not
            leak into the synth subprocess's environment.

    Returns:
        A new dict whose values do not expose paths inside
        ``repo_root``.
    """
    resolved_repo_root = repo_root.resolve(strict=False)
    scrubbed: dict[str, str] = {}
    for key, value in env.items():
        if key in {"PWD", "OLDPWD"}:
            continue
        if key in _AUTH_LOCATOR_KEYS:
            # An auth-credential LOCATOR, kept even when it points inside the repo.
            # Stripping CODEX_HOME because a user set it to a repo-local path silently
            # changes which credential codex uses for the cold actor — so the mode the
            # auth gate PROBED (with the parent env) is no longer the mode that RUNS. The
            # guardrail's whole promise is "verified == runtime"; a scrub that re-auths the
            # subprocess breaks it. CODEX_HOME is codex's own config dir, not the source
            # under review, so preserving it does not weaken cold isolation (which is
            # enforced by cwd / -C / --add-dir scope, not by hiding this path).
            scrubbed[key] = value
            continue
        if key in _PATH_LIST_ENV_KEYS:
            kept_segments = [
                segment
                for segment in value.split(os.pathsep)
                if not segment
                or not Path(segment).expanduser().is_absolute()
                or not _path_is_relative_to(Path(segment).expanduser(), resolved_repo_root)
            ]
            if kept_segments:
                scrubbed[key] = os.pathsep.join(kept_segments)
            continue
        if _value_references_repo_path(value, resolved_repo_root):
            continue
        scrubbed[key] = value
    return scrubbed


def _init_workspace_git(workspace: Path) -> None:
    """Initialize ``workspace`` as an empty git working tree.

    Codex with ``permissions=trusted-execute`` refuses to launch unless cwd is a
    git working tree, failing with ``Not inside a trusted directory and
    --skip-git-repo-check was not specified``.
    The synth workspace is a fresh tempdir; this helper runs
    ``git init -q`` in it before launching codex.

    Failures surface as :class:`~syncade.process.SubprocessError`-bucket
    exceptions that the caller maps to a SynthesizerResult failure.

    No commits are made — the workspace stays as a fresh repo
    with one untracked file (the copied PR doc). Codex's trusted-
    mode check is satisfied by the ``.git/`` directory's presence.
    """
    git_init = run_subprocess(
        ["git", "init", "-q"],
        cwd=workspace,
        env=None,
        timeout=10.0,
        input_text=None,
    )
    if git_init.returncode != 0:
        raise SubprocessError(
            f"synthesizer: failed to git-init the cold workspace at "
            f"{workspace}: git init exited with code "
            f"{git_init.returncode}; stderr: {git_init.stderr[:200]!r}"
        )
