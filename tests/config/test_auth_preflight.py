"""Detect-and-refuse when a declaration cannot be honoured (PR-v2-24, issue 3).

``codex`` IGNORES ``OPENAI_API_KEY``. Not "prefers its stored login over it" — ignores
it. Verified live (codex-cli 0.144.1): with no stored login, ``codex exec`` fails
``401 Missing bearer`` whether the key is exported or not, IDENTICALLY, even with
``-c preferred_auth_method="apikey"``. Its own help says auth always comes from
``CODEX_HOME``.

So the env — syncade's only lever — cannot influence codex at all, and an ``auth = "api"``
declaration on a ChatGPT login is unenforceable. Running it anyway would silently bill the
subscription the user was trying to spare. The only honest move is to refuse.
"""

from __future__ import annotations

import pytest

from syncade.auth_preflight import (
    preflight_problems,
    probe_codex_state,
    reality_problems,
    resolve_auth_mode,
)
from syncade.config import ProducerConfig, ReviewerConfig, SyncadeConfig

# The EXACT strings `codex login status` emits, captured from the real CLI by pointing
# CODEX_HOME at throwaway dirs (never touching the operator's real login).
_REAL_CHATGPT = "Logged in using ChatGPT"
_REAL_APIKEY = "Logged in using an API key - sk-test-***l-key"
_REAL_NONE = "Not logged in"


def _cfg(auth: str, provider: str = "openai") -> SyncadeConfig:
    return SyncadeConfig(
        reviewers=[ReviewerConfig(name="rv", provider=provider, model="m", auth=auth)],
        producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
    )


class TestProbeParsesTheRealStrings:
    """Pinned to output captured from the real CLI, not to strings I invented."""

    @pytest.mark.parametrize(
        ("raw", "returncode", "expected"),
        [
            (_REAL_CHATGPT, 0, "subscription"),
            (_REAL_APIKEY, 0, "api"),
            # Real codex returns rc=1 for the logged-out state, NOT rc=0. Modelling it as 0
            # is what hid the bug where a logged-out user got `unknown` instead of `none`.
            (_REAL_NONE, 1, "none"),
            ("Some future rewording nobody predicted", 0, "unknown"),
        ],
    )
    def test_states(self, raw, returncode, expected, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        monkeypatch.setattr(
            ap,
            "run_subprocess",
            lambda *a, **k: type(
                "R", (), {"returncode": returncode, "stdout": raw, "stderr": ""}
            )(),
        )
        assert probe_codex_state()[0] == expected

    def test_probe_failure_is_unknown_not_a_crash(self, monkeypatch) -> None:
        """A missing codex binary must not traceback — it must degrade to `unknown`,
        which (for a non-auto declaration) is then refused."""
        import syncade.auth_preflight as ap

        def boom(*a, **k):
            raise OSError("codex not found")

        monkeypatch.setattr(ap, "run_subprocess", boom)
        state, raw = probe_codex_state()
        assert state == "unknown"
        assert "codex" in raw


class TestRefusalRules:
    """Both directions refused. Running `api` on a ChatGPT login silently bills a
    subscription (and burns the quota the user was escaping); running `subscription` on a
    stored API key silently bills real money. Neither is 'close enough'."""

    @pytest.mark.parametrize(
        ("declared", "state", "must_refuse"),
        [
            ("api", "subscription", True),  # the headline bug
            ("subscription", "api", True),  # the reverse: real money, never agreed to
            ("api", "none", True),
            ("subscription", "none", True),
            ("api", "unknown", True),
            ("subscription", "unknown", True),
            ("api", "api", False),  # reality matches
            ("subscription", "subscription", False),  # reality matches
            ("auto", "subscription", False),  # auto never refuses
            ("auto", "api", False),
            ("auto", "none", False),
        ],
    )
    def test_table(self, declared, state, must_refuse) -> None:
        problems = reality_problems(_cfg(declared), {}, state)
        assert bool(problems) is must_refuse, f"declared={declared} reality={state}"

    def test_api_on_chatgpt_names_the_exact_fix(self) -> None:
        """A refusal that doesn't tell you what to run is just an obstacle."""
        (problem,) = reality_problems(_cfg("api"), {}, "subscription")
        assert "codex login --with-api-key" in problem
        assert "ChatGPT subscription" in problem

    def test_subscription_on_apikey_warns_about_real_money(self) -> None:
        (problem,) = reality_problems(_cfg("subscription"), {}, "api")
        assert "codex login" in problem
        assert "real money" in problem

    def test_anthropic_actors_never_land_here(self) -> None:
        """For claude the env IS the lever, so apply_auth_to_env enforces the declaration
        outright and a contradiction is impossible by construction."""
        cfg = _cfg("api", provider="anthropic")
        assert reality_problems(cfg, {"ANTHROPIC_API_KEY": "k"}, "subscription") == []


class TestResolveAuthMode:
    def test_anthropic_auto_follows_the_env(self) -> None:
        """claude uses a key if one is present, else its OAuth login — verified live."""
        actor = ReviewerConfig(name="r", provider="anthropic", model="m", auth="auto")
        assert resolve_auth_mode(actor, {"ANTHROPIC_API_KEY": "k"}) == "api"
        assert resolve_auth_mode(actor, {}) == "subscription"

    def test_anthropic_declared_mode_is_the_resolved_mode(self) -> None:
        actor = ReviewerConfig(name="r", provider="anthropic", model="m", auth="subscription")
        assert resolve_auth_mode(actor, {"ANTHROPIC_API_KEY": "k"}) == "subscription"

    def test_openai_ignores_the_env_entirely(self) -> None:
        """The finding that shapes this whole module: no amount of env makes codex use an
        API key. Reality is whatever it is logged in as, full stop."""
        actor = ReviewerConfig(name="r", provider="openai", model="m", auth="auto")
        assert resolve_auth_mode(actor, {"OPENAI_API_KEY": "k"}, "subscription") == "subscription"


class TestWhenTheProbeRuns:
    """It runs whenever an openai actor EXISTS -- not only when one declares a mode.

    Cost honesty (issue 4) needs it either way: codex's stored login is the ONLY thing
    that decides whether a zero-config run's codex tokens were money or a subscription,
    and a dollar figure we cannot classify is a dollar figure we must not report as
    spend. One ~100ms local subprocess per run buys that.
    """

    def test_zero_config_probes_once(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        called: list[int] = []

        def fake(*a, **k):
            called.append(1)
            return ("subscription", "")

        monkeypatch.setattr(ap, "probe_codex_state", fake)
        assert ap.preflight(SyncadeConfig(), {}) == []  # all auto -> nothing to refuse
        assert len(called) == 1, "the default roster is openai; its reality must be known"
        assert ap.get_codex_state() == "subscription", "the resolved state must be recorded"

    def test_no_openai_actor_pays_nothing(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        called: list[int] = []
        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: called.append(1) or ("x", ""))
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="rv", provider="anthropic", model="m")],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
            synthesizer={"provider": "anthropic"},
            drafter={"provider": "anthropic"},
            auditor={"provider": "anthropic"},
        )
        assert ap.preflight(cfg, {}) == []
        assert not called, "an all-anthropic config has no reason to shell out to codex"

    def test_resolve_never_probes_lazily(self, monkeypatch) -> None:
        """Hermetic: no unit test may accidentally spawn codex. An unprobed state stays
        `unknown` -- honest ignorance beats a hidden subprocess."""
        import syncade.auth_preflight as ap

        def boom(*a, **k):
            raise AssertionError("resolve_auth_mode must never probe")

        monkeypatch.setattr(ap, "probe_codex_state", boom)
        ap.set_codex_state("unknown")
        actor = ReviewerConfig(name="r", provider="openai", model="m", auth="auto")
        assert ap.resolve_auth_mode(actor, {}) == "unknown"

    def test_declared_openai_actor_is_checked(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))
        assert preflight_problems(_cfg("api"), {}), "a declaration must be checked"


class TestAuthCheckProbesEveryCredential:
    """Deduplicating the probe list by PROVIDER kept only the first actor, so a config with
    one anthropic actor on `subscription` and another on `api` never probed the second —
    --auth-check returned green on auth it had not checked. Auth is per actor.

    Keyed on the RESOLVED credential, so `auto` (which with no key simply IS subscription)
    does not spawn a redundant third probe of the same credential."""

    def test_two_credentials_on_one_provider_are_both_probed(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap
        from syncade.auth_check import _collect_unique_providers

        # the `api` actor declares a key, so give it one — this test is about DEDUP, not
        # about the missing-key path (see test_missing_key_does_not_traceback).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="sub", provider="anthropic", model="m1", auth="subscription"),
                ReviewerConfig(name="api", provider="anthropic", model="m2", auth="api"),
            ],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        probes = _collect_unique_providers(cfg)
        anthropic = [p for p in probes if p[0] == "anthropic"]
        assert len(anthropic) == 2, (
            "the second anthropic credential was never probed — --auth-check would return "
            "green on auth it never checked"
        )

    def test_auto_does_not_spawn_a_redundant_probe(self, monkeypatch) -> None:
        """auto with no key resolves to subscription, so it shares that probe."""
        import syncade.auth_preflight as ap
        from syncade.auth_check import _collect_unique_providers

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ap.set_codex_state("subscription")
        cfg = SyncadeConfig(
            reviewers=[
                ReviewerConfig(name="sub", provider="anthropic", model="m1", auth="subscription"),
            ],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),  # auto
        )
        anthropic = [p for p in _collect_unique_providers(cfg) if p[0] == "anthropic"]
        assert len(anthropic) == 1, "auto resolved to subscription; it must reuse that probe"


class TestCredentialIsREADNotDERIVED:
    """`(provider, mode, key_var)` is a PROXY for the credential, not the credential.

    With both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN exported, an explicit `auth="api"`
    actor has the AUTH_TOKEN stripped while an `auto` actor keeps it — same provider, same
    resolved mode, same key var, but genuinely DIFFERENT credentials. They collapsed into
    one probe and the other went unverified.

    The key is now the auth-bearing slice of the env the actor will REALLY receive. Derive
    nothing; read what the CLI will see."""

    def test_auto_and_explicit_api_are_distinct_credentials(self, monkeypatch) -> None:
        from syncade.auth_check import _credential_key

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-key")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-different")
        auto = ReviewerConfig(name="auto", provider="anthropic", model="m")
        explicit = ReviewerConfig(name="api", provider="anthropic", model="m", auth="api")

        assert _credential_key(auto) != _credential_key(explicit), (
            "auto keeps ANTHROPIC_AUTH_TOKEN; api strips it. Two different credentials "
            "collapsed into one probe and the other was never verified"
        )

    def test_identical_envs_still_share_one_probe(self, monkeypatch) -> None:
        """The dedup must still work, or every actor spawns its own probe."""
        from syncade.auth_check import _credential_key

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-key")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        a = ReviewerConfig(name="a", provider="anthropic", model="m", auth="api")
        b = ReviewerConfig(name="b", provider="anthropic", model="m2", auth="api")
        assert _credential_key(a) == _credential_key(b)

    def test_a_secret_never_sits_in_the_key(self, monkeypatch) -> None:
        from syncade.auth_check import _credential_key

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
        key = _credential_key(ReviewerConfig(name="a", provider="anthropic", model="m", auth="api"))
        assert "sk-super-secret-value" not in repr(key), "the raw key leaked into a dict key"


class TestMissingKeyDoesNotTraceback:
    """`_credential_key` applies the auth env, which RAISES when an `api` actor has no key.
    Config load normally catches that — but --auth-check must degrade to a clean failed
    probe rather than a traceback if it ever doesn't. Introduced by my own round-6 fix and
    caught by the suite, not by me."""

    def test_api_actor_with_no_key_still_produces_a_probe_list(self, monkeypatch) -> None:
        from syncade.auth_check import _collect_unique_providers

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = SyncadeConfig(
            reviewers=[ReviewerConfig(name="a", provider="anthropic", model="m", auth="api")],
            producer=ProducerConfig(provider="anthropic", model="claude-sonnet-4-6"),
        )
        assert _collect_unique_providers(cfg), "auth-check tracebacked instead of reporting"


class TestProbeRefusesToClassifyAFailedStatus:
    """`probe_codex_state` classified `codex login status` output by substring WITHOUT
    checking the return code. A FAILED probe whose stderr incidentally contains "api key"
    ("could not read API key file: permission denied") was read as a confident `api`, so
    the gate ANNOUNCED API billing on a state it never verified.

    The guardrail: an unverified state is `unknown`, and `unknown` + a non-auto declaration
    is refused. Only a clean exit may resolve to a real state."""

    def test_nonzero_exit_is_unknown_even_with_api_key_in_stderr(self, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        class Failed:
            returncode = 1
            stdout = ""
            stderr = "error: could not read API key file: permission denied"

        monkeypatch.setattr(ap, "run_subprocess", lambda *a, **k: Failed())
        state, raw = ap.probe_codex_state()
        assert state == "unknown", "a failed probe was classified as a confident credential"

    def test_logged_out_is_none_even_at_the_real_rc1(self, monkeypatch) -> None:
        """codex's logged-out state is rc=1 + stdout "Not logged in" — a real answer, not a
        probe failure. It must resolve to `none` (actionable "run codex login"), not the
        opaque `unknown` a blanket nonzero-is-unknown rule produced."""
        import syncade.auth_preflight as ap

        class LoggedOut:
            returncode = 1
            stdout = "Not logged in"
            stderr = ""

        monkeypatch.setattr(ap, "run_subprocess", lambda *a, **k: LoggedOut())
        assert ap.probe_codex_state()[0] == "none"

    @pytest.mark.parametrize(
        ("out", "expected"),
        [
            ("Logged in using ChatGPT", "subscription"),
            ("Logged in using an API key - sk-x", "api"),
            ("Not logged in", "none"),
        ],
    )
    def test_clean_exit_still_resolves_real_states(self, out, expected, monkeypatch) -> None:
        import syncade.auth_preflight as ap

        class Ok:
            returncode = 0
            stdout = out
            stderr = ""

        monkeypatch.setattr(ap, "run_subprocess", lambda *a, **k: Ok())
        assert ap.probe_codex_state()[0] == expected
