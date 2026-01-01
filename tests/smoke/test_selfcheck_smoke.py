"""End-to-end ``syncade --selfcheck`` smoke against real ``claude`` and
``codex`` producer adapters (PR-8.5 Task 3).

Exercises :func:`syncade.selfcheck.run_selfcheck` against the actual
producer CLIs to confirm the selfcheck's marker-add-and-commit flow
composes end-to-end. The selfcheck would have caught PR-8's
deviation #1 (producer ``permissions="yolo"`` discovery) if it had
existed; these tests prove it would.

Gated behind ``@pytest.mark.smoke``. Default ``pytest`` runs
deselect via ``addopts = "-m 'not smoke'"``. Tests skip cleanly
when the required CLI is missing.

Two tests, one per producer provider. Each runs ONE real LLM
subprocess against a one-line marker fix; typical real-CLI
runtime is 15-45 seconds.
"""

from __future__ import annotations

import shutil

import pytest

from syncade.adapters.producer_anthropic import AnthropicProducerAdapter
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.config import LoopConfig, ProducerConfig, ReviewerConfig, SyncadeConfig
from syncade.exit_codes import SUCCESS
from syncade.selfcheck import run_selfcheck


def _skip_if_missing(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.skip(f"required CLI not on PATH: {binary}")


def _selfcheck_config(provider: str, model: str) -> SyncadeConfig:
    """Build a :class:`SyncadeConfig` whose ``[producer]`` block
    routes to a real producer adapter.

    Mirrors the smoke fixture in ``test_producer_smoke.py``: the
    ``permissions="yolo"`` default is the only mode where real
    headless ``claude -p`` / ``codex exec`` reliably commit; the
    selfcheck verifies whatever the operator configured, but the
    smoke test runs the default which is what most operators use.
    """
    return SyncadeConfig(
        loop=LoopConfig(timeout_seconds=180.0),
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model="claude-sonnet-4-6",
            )
        ],
        producer=ProducerConfig(
            provider=provider,
            model=model,
            thinking="low",
            permissions="yolo",
        ),
    )


@pytest.mark.smoke
def test_anthropic_producer_selfcheck_is_green():
    """Real ``claude`` producer + ``syncade --selfcheck``. Asserts the
    selfcheck exits 0 — the producer reads findings.md, edits
    selfcheck.py to add the marker, and commits."""
    _skip_if_missing("claude")

    config = _selfcheck_config("anthropic", "sonnet")
    exit_code = run_selfcheck(
        config,
        timeout_seconds=180.0,
        adapter=AnthropicProducerAdapter(),
    )

    assert exit_code == SUCCESS, (
        f"anthropic producer selfcheck failed with exit {exit_code}; "
        "this means the real `claude` producer cannot make a headless "
        "marker-commit in its current configuration. Inspect the "
        "preserved workspace path printed to stderr."
    )


@pytest.mark.smoke
def test_openai_producer_selfcheck_is_green():
    """Real ``codex`` producer + ``syncade --selfcheck``. Asserts the
    selfcheck exits 0 — same marker-edit-and-commit shape as the
    anthropic case."""
    _skip_if_missing("codex")

    config = _selfcheck_config("openai", "gpt-5.5")
    exit_code = run_selfcheck(
        config,
        timeout_seconds=180.0,
        adapter=OpenAIProducerAdapter(),
    )

    assert exit_code == SUCCESS, (
        f"openai producer selfcheck failed with exit {exit_code}; "
        "this means the real `codex` producer cannot make a headless "
        "marker-commit in its current configuration. Inspect the "
        "preserved workspace path printed to stderr."
    )
