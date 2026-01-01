"""Declared auth mode is ENFORCED on the child env (PR-v2-24, issue 2).

The env is the only lever syncade has — it cannot reach inside a CLI's credential
resolution. So ``subscription`` must REMOVE the capability (strip the keys), not ask
the CLI nicely. Verified end-to-end against the real claude CLI: with a bogus
ANTHROPIC_API_KEY exported, ``subscription`` strips it and claude falls back to OAuth
and succeeds, while ``auto`` leaves it and claude 401s. The two disagree, which is the
proof that enforcement changes CLI BEHAVIOUR and not just our dict.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from syncade import auth_preflight
from syncade.adapters.anthropic import AnthropicAdapter
from syncade.adapters.openai import CodexAdapter
from syncade.adapters.producer_anthropic import AnthropicProducerAdapter
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.config import ProducerConfig, ReviewerConfig
from syncade.config_auth import apply_auth_to_env
from syncade.dispatcher import ReviewerRunResult
from syncade.findings import Finding, ReviewerOutput
from syncade.worktree_env import worktree_scoped_env

_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/u",
    "ANTHROPIC_API_KEY": "sk-ant-LEAK",
    "ANTHROPIC_AUTH_TOKEN": "tok-LEAK",
    "OPENAI_API_KEY": "sk-oa-LEAK",
}


class _Stop(Exception):
    """Sentinel: the driver got far enough to hand the adapter a config."""


def _reviewer_result() -> ReviewerRunResult:
    """One NO-SHIP reviewer, so the synth driver has something to consolidate and does
    not early-return before it builds an invocation."""
    return ReviewerRunResult(
        reviewer_name="rv",
        provider="anthropic",
        output=ReviewerOutput(
            verdict="NO-SHIP",
            summary="s",
            findings=[
                Finding(severity="blocker", file="a.py", line=1, spec_clause="c", finding="f")
            ],
            priority_order=[0],
            coverage_gaps=[],
            dismissed_concerns=[],
        ),
        error=None,
        duration_seconds=1.0,
    )


def _rev(**kw) -> ReviewerConfig:
    return ReviewerConfig(name="r", model="m", **kw)


class TestSubscriptionStripsTheKeys:
    """The claude footgun, closed. An exported ANTHROPIC_API_KEY otherwise BEATS the
    claude.ai login (the CLI says so, and a bogus key 401s), so a developer on a Max
    plan silently bills the API — fanned out N reviewers x M rounds."""

    def test_anthropic_subscription_strips_every_anthropic_key_var(self) -> None:
        out = apply_auth_to_env(_ENV, _rev(provider="anthropic", auth="subscription"))
        assert "ANTHROPIC_API_KEY" not in out
        assert "ANTHROPIC_AUTH_TOKEN" not in out, "a stale AUTH_TOKEN routes to the API too"
        assert out["PATH"] == "/usr/bin", "unrelated env must survive"

    def test_openai_subscription_strips_the_openai_key(self) -> None:
        out = apply_auth_to_env(_ENV, _rev(provider="openai", auth="subscription"))
        assert "OPENAI_API_KEY" not in out

    def test_only_this_actors_provider_is_touched(self) -> None:
        """An anthropic actor has no business rewriting OPENAI_API_KEY."""
        out = apply_auth_to_env(_ENV, _rev(provider="anthropic", auth="subscription"))
        assert out["OPENAI_API_KEY"] == "sk-oa-LEAK"

    def test_input_env_is_never_mutated(self) -> None:
        before = dict(_ENV)
        apply_auth_to_env(_ENV, _rev(provider="anthropic", auth="subscription"))
        assert _ENV == before


class TestAutoChangesNothing:
    """auto = "whatever the CLI would have done". Stripping by default would break users
    who deliberately run on API keys with no subscription at all. Auto is safe only
    because the preflight always PRINTS the resolved mode — silence is the bug."""

    def test_env_is_untouched(self) -> None:
        assert apply_auth_to_env(_ENV, _rev(provider="anthropic", auth="auto")) == _ENV


class TestApiModeRoutesTheKey:
    def test_api_keeps_the_key_and_drops_the_siblings(self) -> None:
        out = apply_auth_to_env(_ENV, _rev(provider="anthropic", auth="api"))
        assert out["ANTHROPIC_API_KEY"] == "sk-ant-LEAK"
        assert "ANTHROPIC_AUTH_TOKEN" not in out, (
            "a stale AUTH_TOKEN could outrank the key the user just declared"
        )

    def test_custom_api_key_env_is_MAPPED_onto_the_canonical_var(self) -> None:
        """The subtle one. `claude` reads ANTHROPIC_API_KEY and nothing else, so a user
        who keeps their key in WORK_KEY needs it copied across — otherwise api_key_env
        would be a silent no-op and the run would fall back to the subscription."""
        env = {**_ENV, "WORK_KEY": "sk-ant-WORK"}
        out = apply_auth_to_env(env, _rev(provider="anthropic", auth="api", api_key_env="WORK_KEY"))
        assert out["ANTHROPIC_API_KEY"] == "sk-ant-WORK", "the user's key must reach the CLI"

    def test_api_with_no_key_in_env_raises_rather_than_falling_back(self) -> None:
        """Config load already guarantees the key exists, so this is defence in depth.
        Shipping the request anyway would silently bill the subscription — the exact
        failure this PR deletes."""
        bare = {"PATH": "/usr/bin"}
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            apply_auth_to_env(bare, _rev(provider="anthropic", auth="api"))


class TestEveryRealAdapterEnforces:
    """A guarantee that covers four of five actors is not a guarantee."""

    @pytest.mark.parametrize(
        ("adapter", "cfg"),
        [
            (AnthropicAdapter(), _rev(provider="anthropic", auth="subscription")),
            (CodexAdapter(), _rev(provider="openai", auth="subscription")),
            (
                AnthropicProducerAdapter(),
                ProducerConfig(provider="anthropic", auth="subscription", permissions="yolo"),
            ),
            (
                OpenAIProducerAdapter(),
                ProducerConfig(provider="openai", auth="subscription", permissions="yolo"),
            ),
        ],
    )
    def test_build_invocation_env_has_no_key(self, adapter, cfg, tmp_path, monkeypatch) -> None:
        for var, val in _ENV.items():
            monkeypatch.setenv(var, val)
        # The openai adapters' spawn-site guard refuses a non-auto declaration the probed
        # codex login does not honour; simulate the honoured case the CLI gate would have
        # established (a no-op for the anthropic adapters, which never consult it).
        auth_preflight.set_codex_state(cfg.auth)
        inv = adapter.build_invocation(cfg, tmp_path, "prompt")
        leaked = [
            v
            for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
            if v in inv.env and v.lower().startswith(cfg.provider[:4])
        ]
        assert not leaked, f"{type(adapter).__name__} leaked {leaked} into the child env"


class TestOpenaiSpawnRefusesWhenTheGateWasSkipped:
    """The panel's blocker: the auth gate lived only in the CLI wrappers, so a DIRECT
    library call (``run_review`` / ``run_producer`` / a cold driver) spawned codex with no
    probe and no refusal -- silently billing whatever codex was logged in as. codex reads
    no env var, so key-stripping (which the adapters already do structurally) cannot help;
    only the probe-and-refuse can. So the openai adapters enforce it at the spawn site: a
    non-auto declaration the probed state does not honour refuses BEFORE the invocation is
    built. On a gate-skipping path ``_CODEX_STATE`` is its ``unknown`` default, so it
    refuses rather than mis-bills."""

    @pytest.mark.parametrize(
        ("adapter", "cfg"),
        [
            (CodexAdapter(), _rev(provider="openai", auth="api")),
            (
                OpenAIProducerAdapter(),
                ProducerConfig(provider="openai", auth="subscription", permissions="yolo"),
            ),
        ],
    )
    def test_direct_spawn_without_a_gate_refuses(self, adapter, cfg, tmp_path) -> None:
        auth_preflight.set_codex_state("unknown")  # gate never ran
        with pytest.raises(ValueError, match="cannot be honoured"):
            adapter.build_invocation(cfg, tmp_path, "prompt")

    def test_auto_never_refuses_even_unprobed(self, tmp_path) -> None:
        auth_preflight.set_codex_state("unknown")
        # auto accepts whatever codex is, exactly as preflight does -- no refusal.
        CodexAdapter().build_invocation(_rev(provider="openai", auth="auto"), tmp_path, "prompt")

    def test_honoured_declaration_passes(self, tmp_path) -> None:
        auth_preflight.set_codex_state("api")  # what the CLI gate would have established
        CodexAdapter().build_invocation(_rev(provider="openai", auth="api"), tmp_path, "prompt")


class TestTheTestLegIsDeliberatelyExempt:
    """The trap. The test/check legs share `worktree_scoped_env`, but they run the
    OPERATOR'S OWN test command — which may legitimately need API keys. Enforcing there
    would break those users for no benefit, which is exactly why enforcement lives in
    the adapters (which know the provider) and not in the env builder."""

    def test_worktree_scoped_env_still_carries_keys(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAK")
        env = worktree_scoped_env(Path(tmp_path))
        assert env.get("ANTHROPIC_API_KEY") == "sk-ant-LEAK", (
            "the test leg must keep the user's keys; stripping here breaks test suites "
            "that call APIs"
        )


class TestColdActorsCarryTheDeclaration:
    """The judge, drafter and auditor build a SYNTHETIC ReviewerConfig inside their
    drivers. If ``auth`` is not copied onto it, the adapter never sees the declaration
    and those three run UNENFORCED — a leak no adapter-level test would catch, because
    the adapter is doing exactly what it was told.

    Asserted against the real drivers by capturing the config the adapter is handed.
    """

    @pytest.mark.parametrize("actor", ["synthesizer", "drafter", "auditor"])
    def test_auth_reaches_the_adapter(self, actor, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAK")
        seen: list[ReviewerConfig] = []

        class Capture:
            """Records the config, then stops the driver dead. The drivers catch only
            ValueError around build_invocation, so this escapes — which is fine, we have
            what we came for."""

            def build_invocation(self, cfg, worktree_path, prompt):
                seen.append(cfg)
                raise _Stop

        pr = tmp_path / "pr.md"
        pr.write_text("# PR\n\n**Goal:** x\n")

        with contextlib.suppress(_Stop):
            if actor == "synthesizer":
                from syncade.config_cold import SynthesizerConfig
                from syncade.synthesizer import run_synthesizer

                run_synthesizer(
                    [_reviewer_result()],
                    repo_root=tmp_path,
                    pr_doc_path=pr,
                    timeout_seconds=5,
                    config=SynthesizerConfig(provider="anthropic", auth="subscription"),
                    adapter=Capture(),
                )
            elif actor == "drafter":
                from syncade.config_cold import DrafterConfig
                from syncade.spec_draft import run_spec_draft

                run_spec_draft(
                    dialogue="User: do x",
                    diff="--- a\n+++ b\n",
                    repo_root=tmp_path,
                    timeout_seconds=5,
                    config=DrafterConfig(provider="anthropic", auth="subscription"),
                    adapter=Capture(),
                )
            else:
                from syncade.config_cold import AuditorConfig
                from syncade.spec_audit import run_spec_audit

                run_spec_audit(
                    pr_doc_path=pr,
                    repo_root=tmp_path,
                    timeout_seconds=5,
                    config=AuditorConfig(provider="anthropic", auth="subscription"),
                    adapter=Capture(),
                )

        assert seen, f"{actor} never reached build_invocation"
        assert seen[0].auth == "subscription", (
            f"{actor} DROPPED its auth declaration on the way to the adapter: it would "
            f"run unenforced while config.toml claims otherwise"
        )


class TestAuthCheckProbesUnderTheDeclaredMode:
    """`--auth-check` spawned a real `claude -p` with NO env, so it inherited
    ANTHROPIC_API_KEY even when every anthropic actor declared `auth = "subscription"`.

    Two failures in one: the check could BILL the API account it was supposed to be
    keeping the user away from, and it green-lit a credential the actual run would never
    use. Caught by syncade's own panel."""

    def test_subscription_config_probes_without_the_key(self, monkeypatch) -> None:
        import os

        from syncade.auth_check import _provider_actors
        from syncade.config import SyncadeConfig

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-would-bill")
        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="rv", provider="anthropic", model="m", auth="subscription")
            ],
            producer=ProducerConfig(
                provider="anthropic", model="claude-sonnet-4-6", auth="subscription"
            ),
        )
        actor = _provider_actors(cfg)["anthropic"]
        env = apply_auth_to_env(dict(os.environ), actor)
        assert "ANTHROPIC_API_KEY" not in env

    def test_run_auth_check_actually_HANDS_that_env_to_the_probe(self, monkeypatch) -> None:
        """Asserting `apply_auth_to_env` is correct proves nothing about whether
        `run_auth_check` calls it. That is the ingredient, not the wiring — and a mutation
        that reverted the probe to `dict(os.environ)` sailed past the first test.

        This captures the env the probe is REALLY handed.
        """
        from syncade.auth_check import AuthCheckResult, run_auth_check
        from syncade.config import SyncadeConfig

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-would-bill")
        seen: dict[str, dict] = {}

        def spy(model, timeout_seconds, env=None):
            seen["env"] = env or {}
            return AuthCheckResult(
                provider="anthropic", model=model, ok=True, duration_seconds=0.1, detail="ok"
            )

        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="rv", provider="anthropic", model="m", auth="subscription")
            ],
            producer=ProducerConfig(
                provider="anthropic", model="claude-sonnet-4-6", auth="subscription"
            ),
        )
        run_auth_check(cfg, probes={"anthropic": spy}, quiet=True)

        assert "env" in seen, "the probe was never called"
        assert "ANTHROPIC_API_KEY" not in seen["env"], (
            "run_auth_check handed the probe the RAW parent env: the check would hit (and "
            "bill) the API account the config explicitly declared away from"
        )


class TestEachCredentialIsProbedUnderItsOwnEnv:
    """`run_auth_check` built the right per-credential probe list and then THREW THE ACTOR
    AWAY, re-looking-up the provider in a provider-keyed map — which returns the FIRST
    actor. So a second credential on the same provider was probed under the first one's
    env: an `api` actor's probe ran with NO KEY AT ALL, verifying a credential the real run
    would never use, and reporting on auth it had never actually tested.

    Right helper, wrong wiring — the fifth time in this PR. The previous test asserted the
    probe LIST was correct, which said nothing about the env each probe was handed. This
    one captures both.
    """

    def test_api_credential_gets_its_key_and_subscription_does_not(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap
        from syncade.auth_check import AuthCheckResult, run_auth_check
        from syncade.config import SyncadeConfig

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
        ap.set_codex_state("subscription")

        seen: dict[str, bool] = {}

        def spy(model, timeout_seconds, env=None):
            seen[model] = "ANTHROPIC_API_KEY" in (env or {})
            return AuthCheckResult(
                provider="anthropic", model=model, ok=True, duration_seconds=0.1, detail="ok"
            )

        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="s", provider="anthropic", model="M-SUB", auth="subscription"),
                ReviewerConfig(name="a", provider="anthropic", model="M-API", auth="api"),
            ],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        run_auth_check(cfg, probes={"anthropic": spy, "openai": spy}, quiet=True)

        assert seen.get("M-API") is True, (
            "the `api` credential was probed WITHOUT its key — --auth-check verified a "
            "credential the real run would never use"
        )
        assert seen.get("M-SUB") is False, (
            "the `subscription` credential was probed WITH a key — the check would bill "
            "the API account the config declared away from"
        )


class TestOpenAiApiModeBuildsAnEnvWithoutAKey:
    """The unanimous round-7 blocker. Config load exempts openai from the key requirement
    (codex reads CODEX_HOME, not the env) — but `apply_auth_to_env` STILL RAISED, so the
    config loaded and then the reviewer/judge DIED at subprocess-build time.

    Fixed one layer, missed its twin — the seventh time in this PR. And I did not pin it:
    the mutation restoring the raise sailed through the whole suite until I mutated it."""

    def test_no_key_in_env_still_builds(self) -> None:
        from syncade.config import SynthesizerConfig

        actor = SynthesizerConfig(provider="openai", auth="api")
        env = apply_auth_to_env({"PATH": "/usr/bin"}, actor)
        assert env["PATH"] == "/usr/bin", (
            "openai `api` raised with no OPENAI_API_KEY — the config loads and then the "
            "judge dies at build time, which is worse than refusing at load"
        )

    def test_the_ignored_var_is_stripped_anyway(self) -> None:
        """codex ignores it, so stripping is harmless defence in depth — and keeps the
        subprocess env free of a credential it has no business seeing."""
        from syncade.config import SynthesizerConfig

        actor = SynthesizerConfig(provider="openai", auth="api")
        env = apply_auth_to_env({"PATH": "/x", "OPENAI_API_KEY": "sk-ignored"}, actor)
        assert "OPENAI_API_KEY" not in env

    def test_anthropic_api_mode_STILL_raises_without_its_key(self) -> None:
        """claude DOES read the env. Exempting openai must not weaken the provider where
        the key is load-bearing."""
        actor = _rev(provider="anthropic", auth="api")
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            apply_auth_to_env({"PATH": "/usr/bin"}, actor)


class TestAuthCheckReportsRatherThanTracebacks:
    """`run_auth_check` calls `apply_auth_to_env`, which RAISES when an `api` actor has no
    key. `_credential_key` degrades gracefully; the probe-env build did not, so
    --auth-check would TRACEBACK.

    A diagnostic command must never traceback — reporting the problem IS its job. (The
    normal CLI path catches this at config load, which is exactly why it went unnoticed.)"""

    def test_missing_key_is_a_failed_probe_not_a_crash(self, monkeypatch, capsys) -> None:
        from syncade.auth_check import run_auth_check
        from syncade.config import SyncadeConfig
        from syncade.exit_codes import WORKTREE_ERROR

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="rv", provider="anthropic", model="m", auth="api")],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )

        def never_called(*a, **k):
            raise AssertionError("the probe should not run without a credential")

        rc = run_auth_check(cfg, probes={"anthropic": never_called}, quiet=True)

        assert rc == WORKTREE_ERROR, "a missing credential must FAIL the check, not crash it"
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
