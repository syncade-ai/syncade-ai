"""Tests for :mod:`syncade.auth_check` (PR-9 Task 4).

Unit tests inject fake probes via ``run_auth_check``'s ``probes``
kwarg so the suite never shells out to a real ``claude`` or
``codex`` CLI. The smoke test at
``tests/smoke/test_auth_check_smoke.py`` covers the real-provider
paths end-to-end.

The brief's four coverage points:

1. Happy path (every probe returns OK) → exit 0
2. Anthropic fail → exit 60
3. Codex fail → exit 60
4. Mixed (anthropic OK, codex fail) → exit 60, both surfaced
"""

from __future__ import annotations

from syncade.auth_check import (
    AUTH_CHECK_SENTINEL,
    DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS,
    AuthCheckResult,
    _collect_unique_providers,
    run_auth_check,
)
from syncade.config import (
    LoopConfig,
    ProducerConfig,
    ReviewerConfig,
    SyncadeConfig,
)
from syncade.exit_codes import SUCCESS, WORKTREE_ERROR

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _config_with_two_providers() -> SyncadeConfig:
    """The canonical config: one anthropic reviewer, one openai
    reviewer, one anthropic producer (so anthropic is exercised
    once via the reviewer block — the producer's anthropic isn't
    re-probed).
    """
    return SyncadeConfig(
        loop=LoopConfig(timeout_seconds=30.0),
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model="claude-opus-4-6",
            ),
            ReviewerConfig(
                name="codex-reviewer",
                provider="openai",
                model="gpt-5.5",
            ),
        ],
        producer=ProducerConfig(
            provider="anthropic",
            model="claude-sonnet-4-6",
        ),
    )


def _ok_probe(provider: str, duration: float = 0.5) -> object:
    """Return a closure that produces an OK :class:`AuthCheckResult`
    for the given ``provider``. Calls record their model so tests
    can assert the right representative model flowed through.
    """
    calls: list[tuple[str, float]] = []

    def probe(
        model: str, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> AuthCheckResult:
        calls.append((model, timeout_seconds))
        return AuthCheckResult(
            provider=provider,
            model=model,
            ok=True,
            duration_seconds=duration,
            detail=f"OK ({duration:.1f}s)",
        )

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def _fail_probe(provider: str, message: str = "FAILED (401)") -> object:
    """Return a closure that produces a failure
    :class:`AuthCheckResult` with the supplied message."""
    calls: list[tuple[str, float]] = []

    def probe(
        model: str, timeout_seconds: float, env: dict[str, str] | None = None
    ) -> AuthCheckResult:
        calls.append((model, timeout_seconds))
        return AuthCheckResult(
            provider=provider,
            model=model,
            ok=False,
            duration_seconds=0.1,
            detail=message,
        )

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


# ---------------------------------------------------------------------------
# _collect_unique_providers
# ---------------------------------------------------------------------------


def test_collect_unique_providers_dedups_by_provider():
    """Two reviewers + producer using two unique providers → two
    (provider, model) tuples. The first reviewer's model wins for
    its provider; the producer contributes only if its provider
    isn't already represented."""
    config = _config_with_two_providers()
    pairs = _collect_unique_providers(config)
    # anthropic appears once (from the first reviewer — opus); the
    # producer's anthropic + sonnet is dedup'd out.
    # openai appears once (from the codex reviewer).
    assert pairs == [
        ("anthropic", "claude-opus-4-6"),
        ("openai", "gpt-5.5"),
    ]


def test_collect_unique_providers_covers_the_cold_actors():
    """The judge's provider MUST be probed, or --auth-check is a false green.

    Before PR-v2-23 the cold actors were hardwired to codex and invisible to
    config, so this probed reviewers + producer and returned exit 0 on a machine
    that could not finish a run. That is the most expensive kind of false green:
    the user discovers it only AFTER both reviewers have run and billed.

    Now the judge's provider is configurable, so an anthropic judge behind OpenAI
    reviewers and an OpenAI producer is a perfectly reasonable config — and it must
    add an anthropic probe.
    """
    config = SyncadeConfig(
        reviewers=[
            ReviewerConfig(name="rv1", provider="openai", model="gpt-5.5"),
        ],
        producer={"provider": "openai", "model": "gpt-5.6-terra"},
        synthesizer={"provider": "anthropic", "model": "claude-sonnet-4-6"},
    )
    pairs = _collect_unique_providers(config)
    assert ("anthropic", "claude-sonnet-4-6") in pairs, (
        "the judge's provider was never probed — --auth-check would return a false green"
    )


def test_collect_unique_providers_cold_actors_add_no_probe_when_already_covered():
    """Cost check: the cold actors must not add a probe for a provider a reviewer
    already covers. Auth is per-token, not per-model, so the default single-provider
    roster must probe exactly what it did before."""
    config = SyncadeConfig(
        reviewers=[ReviewerConfig(name="rv1", provider="openai", model="gpt-5.5")],
        producer={"provider": "openai", "model": "gpt-5.6-terra"},
    )
    # judge/drafter/auditor all default to openai — already covered by the reviewer.
    assert _collect_unique_providers(config) == [("openai", "gpt-5.5")]


def test_collect_unique_providers_producer_supplies_unique_provider():
    """If no reviewer uses provider X but the producer does, the
    producer's model is the representative for that provider."""
    config = SyncadeConfig(
        reviewers=[
            ReviewerConfig(
                name="anthropic-reviewer",
                provider="anthropic",
                model="claude-opus-4-6",
            )
        ],
        producer=ProducerConfig(provider="openai", model="gpt-5-codex"),
    )
    pairs = _collect_unique_providers(config)
    assert pairs == [
        ("anthropic", "claude-opus-4-6"),
        ("openai", "gpt-5-codex"),
    ]


def test_collect_unique_providers_dedups_producer_but_still_probes_the_default_judge():
    """Reviewer + producer both anthropic: the producer's anthropic dedups out (its
    model differs, first occurrence wins) — but OPENAI is still probed, because the
    JUDGE defaults to openai even when every actor the user configured is Anthropic.

    This test previously asserted the anthropic-only list, and in doing so it was
    asserting the false green: that config runs `codex` for its judge every single
    round, so a machine without codex auth CANNOT complete it — yet `--auth-check`
    said OK. The user found out only after both reviewers had run and billed.

    Probing the openai judge here is the fix, not a regression. If you are tempted to
    "clean up" this extra entry, you are re-introducing the bug.
    """
    config = SyncadeConfig(
        reviewers=[
            ReviewerConfig(
                name="claude-reviewer",
                provider="anthropic",
                model="claude-opus-4-6",
            )
        ],
        producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    assert _collect_unique_providers(config) == [
        ("anthropic", "claude-opus-4-6"),
        ("openai", "gpt-5.5"),  # the default judge — really does need codex auth
    ]


# ---------------------------------------------------------------------------
# run_auth_check happy path
# ---------------------------------------------------------------------------


def test_run_auth_check_happy_path_returns_success(capsys):
    """Every configured provider's probe returns OK → exit 0.
    Per-provider OK lines are printed to stdout; nothing on stderr.
    """
    config = _config_with_two_providers()
    probes = {
        "anthropic": _ok_probe("anthropic", duration=2.3),
        "openai": _ok_probe("openai", duration=0.7),
    }
    rc = run_auth_check(config, probes=probes)
    assert rc == SUCCESS

    captured = capsys.readouterr()
    assert "anthropic" in captured.out
    assert "openai" in captured.out
    assert "OK (2.3s)" in captured.out
    assert "OK (0.7s)" in captured.out
    # Nothing on stderr on the success path.
    assert captured.err == ""


def test_run_auth_check_passes_representative_model_to_probe():
    """The probe receives the (provider, representative_model)
    pair, not arbitrary strings — verifies _collect_unique_providers
    feeds run_auth_check the right model per provider."""
    config = _config_with_two_providers()
    ok_anthropic = _ok_probe("anthropic")
    ok_openai = _ok_probe("openai")
    rc = run_auth_check(
        config,
        probes={"anthropic": ok_anthropic, "openai": ok_openai},
    )
    assert rc == SUCCESS
    assert ok_anthropic.calls == [("claude-opus-4-6", DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS)]
    assert ok_openai.calls == [("gpt-5.5", DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS)]


def test_run_auth_check_timeout_override_propagates_to_probes():
    """``timeout_seconds`` kwarg overrides the default 30s and
    flows through to each probe."""
    config = _config_with_two_providers()
    ok_anthropic = _ok_probe("anthropic")
    ok_openai = _ok_probe("openai")
    rc = run_auth_check(
        config,
        timeout_seconds=10.0,
        probes={"anthropic": ok_anthropic, "openai": ok_openai},
    )
    assert rc == SUCCESS
    assert ok_anthropic.calls[0][1] == 10.0
    assert ok_openai.calls[0][1] == 10.0


# ---------------------------------------------------------------------------
# run_auth_check failure paths
# ---------------------------------------------------------------------------


def test_run_auth_check_anthropic_fail_returns_worktree_error(capsys):
    """Anthropic probe fails, codex probe passes → exit 60.

    Both per-provider lines surface; failure detail lands on stderr
    so an operator running --quiet still sees what broke. The
    summary FAILED line on stderr makes it grep-friendly.
    """
    config = _config_with_two_providers()
    probes = {
        "anthropic": _fail_probe("anthropic", "FAILED (401 — token expired). Run 'claude'."),
        "openai": _ok_probe("openai"),
    }
    rc = run_auth_check(config, probes=probes)
    assert rc == WORKTREE_ERROR

    captured = capsys.readouterr()
    # OK provider's line lands on stdout (operator-visible).
    assert "openai" in captured.out
    # FAILED provider's line lands on stderr (always-visible, even under --quiet).
    assert "anthropic" in captured.err
    assert "FAILED (401" in captured.err
    assert "Run 'claude'" in captured.err
    # Trailing summary FAILED line on stderr.
    assert "auth-check FAILED" in captured.err


def test_run_auth_check_openai_fail_returns_worktree_error(capsys):
    """Codex probe fails, anthropic passes → exit 60."""
    config = _config_with_two_providers()
    probes = {
        "anthropic": _ok_probe("anthropic"),
        "openai": _fail_probe("openai", "codex auth check failed: Not logged in"),
    }
    rc = run_auth_check(config, probes=probes)
    assert rc == WORKTREE_ERROR

    captured = capsys.readouterr()
    assert "openai" in captured.err
    assert "Not logged in" in captured.err
    assert "anthropic" in captured.out


def test_run_auth_check_both_fail_returns_worktree_error(capsys):
    """Every probe fails → exit 60, every failure surfaced.
    Important for an operator whose machine has both auth tokens
    rotated: a single ``--auth-check`` run names every provider
    needing attention, not just the first one."""
    config = _config_with_two_providers()
    probes = {
        "anthropic": _fail_probe("anthropic", "anthropic broken"),
        "openai": _fail_probe("openai", "openai broken"),
    }
    rc = run_auth_check(config, probes=probes)
    assert rc == WORKTREE_ERROR

    captured = capsys.readouterr()
    assert "anthropic broken" in captured.err
    assert "openai broken" in captured.err


def test_run_auth_check_unknown_provider_returns_worktree_error(capsys):
    """A provider configured in TOML but missing from the probe
    registry → exit 60 with an explicit message naming the
    unrecognized provider. Defensive: the schema's
    ``ReviewerConfig.provider`` is a free-form ``str`` (not a
    Literal), so a config could specify e.g. ``provider="google"``
    even though the runtime adapter registry has no Google entry.
    Auth-check should refuse rather than silently skip."""
    config = SyncadeConfig(
        reviewers=[
            ReviewerConfig(
                name="google-reviewer",
                provider="google",
                model="gemini-2.5",
            )
        ],
        producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )
    probes = {
        "anthropic": _ok_probe("anthropic"),
        "openai": _ok_probe("openai"),
    }
    rc = run_auth_check(config, probes=probes)
    assert rc == WORKTREE_ERROR

    captured = capsys.readouterr()
    assert "google" in captured.err
    assert "no probe registered" in captured.err


# ---------------------------------------------------------------------------
# Quiet mode
# ---------------------------------------------------------------------------


def test_run_auth_check_quiet_suppresses_stdout_progress(capsys):
    """``quiet=True`` silences the per-provider OK lines on stdout
    AND the trailing summary success line. Stderr is unaffected —
    failure paths still surface."""
    config = _config_with_two_providers()
    probes = {
        "anthropic": _ok_probe("anthropic"),
        "openai": _ok_probe("openai"),
    }
    rc = run_auth_check(config, probes=probes, quiet=True)
    assert rc == SUCCESS

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_run_auth_check_quiet_does_not_suppress_failure_stderr(capsys):
    """``quiet=True`` does NOT suppress failure stderr. Auth-check
    is a diagnostic; failures that go silent defeat the purpose."""
    config = _config_with_two_providers()
    probes = {
        "anthropic": _fail_probe("anthropic", "broken"),
        "openai": _ok_probe("openai"),
    }
    rc = run_auth_check(config, probes=probes, quiet=True)
    assert rc == WORKTREE_ERROR

    captured = capsys.readouterr()
    assert "anthropic" in captured.err
    assert "broken" in captured.err
    assert "auth-check FAILED" in captured.err


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_default_timeout_is_30_seconds():
    """The brief's spec'd timeout. Pinned here so a refactor that
    changes it has to update this test too — the 30s ceiling
    balances "generous for a slow provider" against "fast diagnostic
    feedback" and shouldn't move silently."""
    assert DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS == 30.0


def test_auth_check_sentinel_constant():
    """The sentinel string the probe expects. Pinned because the
    probe prompt instruction text and the verifier must match;
    a refactor that changes one but not the other would silently
    let auth-check pass on a broken probe."""
    assert AUTH_CHECK_SENTINEL == "AUTH OK"
