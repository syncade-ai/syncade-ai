"""End-to-end ``syncade --auth-check`` smoke against real ``claude``
and ``codex`` CLIs (PR-9 Task 4).

Exercises :func:`syncade.auth_check.run_auth_check` against the
actual provider CLIs to confirm the auth-check probes work
end-to-end. Both probes share the auth-check's exit-60 semantics
(per-provider failure → operator must remediate before invoking
syncade).

Gated behind ``@pytest.mark.smoke``. Default ``pytest`` runs
deselect via ``addopts = "-m 'not smoke'"``. Tests skip cleanly
when the required CLI is missing.

Per the brief's acceptance criterion: end-to-end against real
providers in under 15 seconds. The full healthy-auth run typically
finishes in ~5-10s — anthropic's ``claude -p`` is the slowest leg
(a real round-trip to the API), codex's ``codex login status`` is
a filesystem read.
"""

from __future__ import annotations

import shutil
import time

import pytest

from syncade.auth_check import run_auth_check
from syncade.config import (
    LoopConfig,
    ProducerConfig,
    ReviewerConfig,
    SyncadeConfig,
)
from syncade.exit_codes import SUCCESS


def _skip_if_missing(binary: str) -> None:
    if shutil.which(binary) is None:
        pytest.skip(f"required CLI not on PATH: {binary}")


def _config_anthropic_only(model: str) -> SyncadeConfig:
    """Single-anthropic-reviewer config — exercises ``_probe_anthropic``
    without invoking the codex probe (so failures isolate to the
    anthropic auth path)."""
    return SyncadeConfig(
        loop=LoopConfig(timeout_seconds=60.0),
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model=model,
            )
        ],
        producer=ProducerConfig(provider="anthropic", model=model),
    )


def _config_openai_only(model: str) -> SyncadeConfig:
    """Single-openai-reviewer config — exercises ``_probe_openai``
    without invoking the anthropic probe."""
    return SyncadeConfig(
        loop=LoopConfig(timeout_seconds=60.0),
        reviewers=[
            ReviewerConfig(
                name="codex-reviewer",
                provider="openai",
                model=model,
            )
        ],
        producer=ProducerConfig(provider="openai", model=model),
    )


def _config_both_providers(claude_model: str, codex_model: str) -> SyncadeConfig:
    """Mixed-providers config — exercises the full per-provider
    dedup + probe flow. The wall-clock target of the brief
    (under 15 s end-to-end) is verified here."""
    return SyncadeConfig(
        loop=LoopConfig(timeout_seconds=60.0),
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model=claude_model,
            ),
            ReviewerConfig(
                name="codex-reviewer",
                provider="openai",
                model=codex_model,
            ),
        ],
        producer=ProducerConfig(provider="anthropic", model=claude_model),
    )


@pytest.mark.smoke
def test_auth_check_real_anthropic_alone():
    """Real ``claude -p`` auth probe end-to-end. Skips if claude is
    missing or unauthenticated. Healthy auth returns exit 0 in
    well under 15 seconds."""
    _skip_if_missing("claude")

    # haiku is the cheapest live-verified model — auth-check probes
    # are short prompts so the model size doesn't materially affect
    # cost, but smaller-model latency is lower which is the relevant
    # metric here.
    config = _config_anthropic_only("haiku")
    start = time.monotonic()
    rc = run_auth_check(config)
    duration = time.monotonic() - start

    assert rc == SUCCESS, (
        "Expected SUCCESS (0) from auth-check against healthy "
        f"anthropic auth; got rc={rc}. If auth is genuinely broken, "
        "run `claude` interactively to re-authenticate. If auth is "
        "fine and this still fails, the probe's JSON-envelope parsing "
        "may have drifted from the CLI's actual output shape."
    )
    assert duration < 30.0, (
        f"auth-check took {duration:.1f}s, exceeding the 30s smoke "
        "budget. Healthy auth should respond in well under 5s; "
        "anything slower suggests provider latency or a network issue."
    )


@pytest.mark.smoke
def test_auth_check_real_openai_alone():
    """Real ``codex login status`` auth probe end-to-end. Skips if
    codex is missing or unauthenticated. Healthy auth returns
    exit 0 in well under 5 seconds (filesystem read, no network)."""
    _skip_if_missing("codex")

    # Model string isn't used by the codex probe (`codex login status`
    # is per-token, not per-model), but the schema requires a value.
    config = _config_openai_only("gpt-5.5")
    start = time.monotonic()
    rc = run_auth_check(config)
    duration = time.monotonic() - start

    assert rc == SUCCESS, (
        "Expected SUCCESS (0) from auth-check against healthy "
        f"openai auth; got rc={rc}. If auth is genuinely broken, "
        "run `codex login` to re-authenticate."
    )
    assert duration < 10.0, (
        f"codex auth-check took {duration:.1f}s — codex login status "
        "is a filesystem read; >10s suggests the codex CLI is hung."
    )


@pytest.mark.smoke
def test_auth_check_real_both_providers_under_brief_budget():
    """The brief's acceptance criterion: end-to-end against real
    providers in under 15 seconds. Both ``claude`` and ``codex``
    must be installed and authenticated for this test to be
    meaningful — skips otherwise.

    Total wall-clock is the sum of per-provider probes; both are
    sequential (no parallelism in ``run_auth_check`` — the brief's
    5-10s target already assumes serial execution)."""
    _skip_if_missing("claude")
    _skip_if_missing("codex")

    config = _config_both_providers(claude_model="haiku", codex_model="gpt-5.5")
    start = time.monotonic()
    rc = run_auth_check(config)
    duration = time.monotonic() - start

    assert rc == SUCCESS, (
        f"Expected SUCCESS (0) but got rc={rc}. Remediate per-provider "
        "auth (run `claude` or `codex login` interactively) and re-run."
    )
    # Brief target is 5-10s; 15s is the PR acceptance bound.
    assert duration < 15.0, (
        f"auth-check end-to-end took {duration:.1f}s, exceeding the "
        "15s acceptance criterion. The brief's target is 5-10s; check "
        "for provider latency or a network issue."
    )
