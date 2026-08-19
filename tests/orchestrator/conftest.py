"""Shared fixtures for the orchestrator test subdir.

A conftest in this subdir scopes its fixtures to ``tests/orchestrator/``
only — ``_default_to_fake_synthesizer`` applies to every orchestrator test and
to nothing else (no bleed to the other test files).

``_isolated_worktree_base`` MOVED to ``tests/conftest.py`` in PR-h-12 item 1b.
Package scope was the bug: its docstring said it existed to prevent cross-test
flakes, and six other packages never got it. Measured before the move —
``tests/cli`` and ``tests/persistence`` each created the shared
``/tmp/syncade`` (empty, no worktrees leaked). ``repo_with_pr_doc``
is an opt-in fixture requested by name.

Moved verbatim from the former ``tests/test_orchestrator.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.adapters.fake import FakeSynthesizerAdapter
from tests.orchestrator._helpers import _init_git_repo


@pytest.fixture(autouse=True)
def _default_to_fake_synthesizer(monkeypatch):
    """Auto-inject a :class:`FakeSynthesizerAdapter` as the default
    synthesizer adapter for every test in this module.

    The orchestrator runs the synthesizer phase whenever every
    reviewer succeeded. Almost every happy-path test in this module
    would otherwise spawn the real codex CLI (slow, network-dependent,
    auth-dependent — exactly what unit tests need to avoid).

    Since PR-v2-23 the driver resolves its default adapter from the ADAPTER
    REGISTRY (``get_adapter(config.provider)``) instead of naming
    ``CodexAdapter``, so that is the lookup this patches — per the decomposition
    rule in CLAUDE.md, patch the concrete site the function body reads. Patching
    the registry module itself would NOT work: ``driver`` does
    ``from ... import get_adapter``, binding the name into the driver's globals.

    Tests that want to exercise synthesizer-specific behavior
    (failure paths, custom canned outputs) pass
    ``synthesizer_adapter=FakeSynthesizerAdapter(canned_exception=...)``
    explicitly; the explicit kwarg takes precedence over the autouse
    default (it never goes through the patched resolver).
    """
    import syncade.synthesizer.driver as synth_driver

    monkeypatch.setattr(synth_driver, "get_adapter", lambda _provider: FakeSynthesizerAdapter())


@pytest.fixture
def repo_with_pr_doc(tmp_path: Path) -> tuple[Path, Path]:
    """Ephemeral git repo + a synthetic PR doc. Returns (repo_root, pr_doc_path)."""
    repo = (tmp_path / "repo").resolve()
    _init_git_repo(repo)
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# Synthetic PR\n\n**Goal:** test fixture\n")
    return repo, pr_doc
