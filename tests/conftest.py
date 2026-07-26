"""Suite-wide fixtures.

The producer's zero-config defaults are harness-aware — ``syncade.config
._invoking_harness`` reads ``CLAUDE_CODE_SESSION_ID`` / ``CODEX_THREAD_ID`` from
the ambient environment. Without this fixture that environment leaks into every
default-assertion in the suite: the same test sees an Anthropic producer when
pytest runs inside Claude Code and an OpenAI one in CI, so a green local run
would mean nothing about the CI run.

Neutralize both markers by default. Tests that care about a specific harness set
it explicitly (see ``tests/config/test_config_schema.py``'s harness matrix).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _neutral_harness(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch, tmp_path_factory):
    """No test may pick up a developer's real ``~/.syncade/config.toml`` — that global layer would
    leak into every default-assertion, exactly like the harness markers above. Point the resolver at
    an absent path; tests that exercise the global layer pass an explicit ``global_config_path``."""
    absent = tmp_path_factory.mktemp("no-global-config") / "config.toml"
    monkeypatch.setattr("syncade.config_loader._default_global_config_path", lambda: absent)
