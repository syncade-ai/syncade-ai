"""Suite-wide fixtures.

The producer's zero-config defaults are harness-aware — ``syncade.config
._invoking_harness`` reads ``CLAUDE_CODE_SESSION_ID`` / ``CODEX_THREAD_ID`` from
the ambient environment. Without this fixture that environment leaks into every
default-assertion in the suite: the same test sees an Anthropic producer when
pytest runs inside Claude Code and an OpenAI one in CI, so a green local run
would mean nothing about the CI run.

Neutralize both markers by default. Tests that care about a specific harness set
it explicitly (see ``tests/config/test_config_schema.py``'s harness matrix).

The same principle covers the developer's **git** configuration (PR-h-10 item 3), and it
cost more than the harness markers did. Fixtures ``git init`` throwaway repositories and
then refer to ``main``, which exists only because the developer's ``init.defaultBranch``
says so; a runner has no such setting, so git's built-in default applies and every
``git rev-parse main`` exits 128. Measured on CI: **81 failed, 287 errors, one identical
cause**, on both Python versions, on every push since the repository went public.

Note the product is RIGHT here and must not change — ``tests/test_git_preconditions.py``
pins that syncade never passes ``--initial-branch`` (a deliberate git<2.28 compatibility
choice), so it correctly inherits whatever the operator configured. It is the SUITE that
must stop depending on the ambient value, so the fix is one hermetic config for the whole
run rather than an edit at each of the 14 call sites, which would drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HERMETIC_GITCONFIG = """\
[init]
\tdefaultBranch = main
[user]
\tname = syncade tests
\temail = tests@syncade.invalid
[commit]
\tgpgsign = false
"""


@pytest.fixture(autouse=True)
def _neutral_harness(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


@pytest.fixture(scope="session")
def _hermetic_gitconfig(tmp_path_factory) -> Path:
    """One git config for the whole run, so no test outcome depends on the developer's."""
    cfg = tmp_path_factory.mktemp("gitconfig") / "gitconfig"
    cfg.write_text(_HERMETIC_GITCONFIG, encoding="utf-8")
    return cfg


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch, _hermetic_gitconfig):
    """Pin ``init.defaultBranch`` (and an identity, so commits do not need the ambient one).

    ``GIT_CONFIG_GLOBAL`` replaces ``~/.gitconfig`` for every git child, and
    ``GIT_CONFIG_NOSYSTEM`` drops ``/etc/gitconfig`` — together they make the suite's git
    behaviour identical on a laptop and a runner. The identity matters as much as the branch
    name: without it, replacing the global config would strip the developer's ``user.email``
    and break every committing fixture that currently relies on it.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_hermetic_gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch, tmp_path_factory):
    """No test may pick up a developer's real ``~/.syncade/config.toml`` — that global layer would
    leak into every default-assertion, exactly like the harness markers above. Point the resolver at
    an absent path; tests that exercise the global layer pass an explicit ``global_config_path``."""
    absent = tmp_path_factory.mktemp("no-global-config") / "config.toml"
    monkeypatch.setattr("syncade.config_loader._default_global_config_path", lambda: absent)


@pytest.fixture(autouse=True)
def _isolated_worktree_base(request, tmp_path, monkeypatch):
    """No test may touch the SHARED worktree base — PR-h-12 item 1b.

    Moved here from ``tests/orchestrator/conftest.py``, where it was package-scoped. Its own
    docstring said it existed to prevent cross-test flakes (two tests in the same wall-clock
    second picking one run-id and colliding), and six packages never received it. Measured before
    the move: ``tests/cli`` and ``tests/persistence`` each created ``/tmp/syncade``. Empty, and no
    worktrees leaked — so the brief's original 4.4 GB attribution was wrong, exactly as it later
    corrected itself to say. The gap was real; the magnitude was not.

    ``run_review`` falls back to ``config.worktree_base`` (PR-v2-9), so the config default is what
    must move: a bare ``SyncadeConfig()`` has to inherit the per-test path too. The field uses a
    default factory so patching its module-level source works across the supported Pydantic range
    without mutating Pydantic's compiled schema.
    """
    from syncade import config as config_module

    # A DRIFT TEST must be able to see the real default. Two of them assert exactly that
    # (`SyncadeConfig().worktree_base == DEFAULT_WORKTREE_BASE`, "byte-identical"), and this
    # fixture defeated both when it went repo-wide -- correctly, since it is doing what it says.
    # The opt-out is a declared marker rather than a package exemption, because a package
    # exemption is what put six packages outside the isolation in the first place.
    if request.node.get_closest_marker("real_worktree_base"):
        yield
        return

    monkeypatch.setattr(
        config_module,
        "DEFAULT_WORKTREE_BASE",
        tmp_path.parent / f"{tmp_path.name}-syncade-worktrees",
    )
    yield


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    """`manifest_once` memoises for the life of a PROCESS, and pytest is one process.

    Without this, the first test to reach a real or patched fetch fixes the answer for every
    later test in the run — and the failure would look like a patch that "did not take", which
    is exactly the kind of ghost that costs an afternoon. Cleared both sides so ordering cannot
    matter.
    """
    from syncade import update_check

    update_check._manifest_cache = update_check._UNFETCHED
    yield
    update_check._manifest_cache = update_check._UNFETCHED
