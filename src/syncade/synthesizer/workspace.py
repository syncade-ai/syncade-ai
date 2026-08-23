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

- :mod:`syncade.env_scrub` owns the shared path-list and boundary-aware path
  matching. It resolves candidates before containment checks, so a symlink
  alias of the repo root (macOS ``/tmp`` -> ``/private/tmp``) cannot slip through.
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

from pathlib import Path

from syncade.env_scrub import scrub_env_for_repo_paths
from syncade.process import SubprocessError, run_subprocess

_AUTH_LOCATOR_KEYS = frozenset({"CODEX_HOME"})
"""Vars that tell a provider CLI WHERE its stored credential lives. Kept through the cold
scrub even when repo-local: stripping them re-auths the subprocess, so the mode the auth
gate probed (parent env) would differ from the mode that runs (scrubbed env). CODEX_HOME is
the codex login dir. Anthropic auth is a key VALUE (ANTHROPIC_API_KEY) or the OS keychain,
not an env-var path, so it needs no entry here."""


def _scrub_env_for_cold_synth(env: dict[str, str], repo_root: Path) -> dict[str, str]:
    """Strip environment variables that would leak ``repo_root`` to
    the synth subprocess.

    Workspace-scoped cwd/``-C``/``--add-dir`` are one half of that; this is the
    other. The parent process's env typically carries:

    - ``PWD`` and ``OLDPWD`` from the shell — both contain the cwd
      at invocation time, which is normally inside the repo
      (``syncade`` is invoked from the repo or one of its
      subdirs). Dropped unconditionally.
    - Path-list vars like ``PATH`` and ``PYTHONPATH`` — split by the platform
      path separator; only absolute segments contained by ``repo_root`` are
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
    return scrub_env_for_repo_paths(env, repo_root, preserve_keys=_AUTH_LOCATOR_KEYS)


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
