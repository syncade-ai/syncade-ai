"""Auth-mode declaration + load-time key enforcement (PR-v2-24, issue 1).

The bug being closed: syncade copies ``dict(os.environ)`` into every subprocess, and
the two CLIs resolve auth OPPOSITELY (verified live — claude lets ANTHROPIC_API_KEY
beat its OAuth login; codex lets its stored ChatGPT login beat OPENAI_API_KEY). So one
run can bill your subscription for one provider and your API account for the other,
silently. Step one is letting the user DECLARE which they want, and refusing a
declaration we cannot honour.
"""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from syncade.config import SyncadeConfig
from syncade.config_auth import api_key_problems, authed_actors, default_api_key_env
from syncade.config_loader import ConfigError, load_config

_NO_KEYS = {"PATH": "/usr/bin"}


def _cfg(toml: str) -> SyncadeConfig:
    return SyncadeConfig.model_validate(tomllib.loads(toml))


def _write(tmp_path, toml: str):
    (tmp_path / ".syncade").mkdir(exist_ok=True)
    (tmp_path / ".syncade" / "config.toml").write_text(toml)
    return tmp_path


class TestEveryActorCarriesTheDeclaration:
    def test_all_five_actor_types_have_auth(self) -> None:
        """Reviewers, producer, judge, drafter, auditor. A declaration that covers four
        of the five is not a guarantee — the fifth is where the money leaks."""
        cfg = SyncadeConfig()
        labels = [label for label, _ in authed_actors(cfg)]
        assert any("reviewers" in label for label in labels)
        for block in ("[producer]", "[synthesizer]", "[drafter]", "[auditor]"):
            assert block in labels
        assert all(hasattr(actor, "auth") for _, actor in authed_actors(cfg))

    def test_zero_config_is_auto_everywhere(self) -> None:
        """auto = "whatever the CLI would have done anyway", so adding the field changes
        nobody's behaviour. Stripping keys by default would break users who deliberately
        run on API keys with no subscription."""
        cfg = SyncadeConfig()
        assert {actor.auth for _, actor in authed_actors(cfg)} == {"auto"}
        assert {actor.api_key_env for _, actor in authed_actors(cfg)} == {None}


class TestKeyVarResolution:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"), ("mystery", None)],
    )
    def test_default_key_var_per_provider(self, provider, expected) -> None:
        assert default_api_key_env(provider) == expected

    def test_explicit_api_key_env_overrides_the_default(self) -> None:
        """A user with a work key separate from a personal one names their own var.

        On ANTHROPIC: claude reads the env, so the mapping is honourable. (openai is
        refused outright — codex ignores the env entirely; see
        TestApiKeyEnvOnOpenAiIsRefused.)"""
        cfg = _cfg('[producer]\nprovider = "anthropic"\nauth = "api"\napi_key_env = "WORK_KEY"\n')
        assert cfg.producer.key_var() == "WORK_KEY"


class TestApiModeRequiresAKeyAtConfigLoad:
    """The load-time guarantee, for the providers where a key in the ENV is what auths.

    ANTHROPIC only: `claude` reads the env, so a missing key there IS a config error. codex
    never reads the env (its key lives in CODEX_HOME), so demanding OPENAI_API_KEY was a
    false requirement — see TestOpenAiApiModeIsVerifiedByTheCodexProbeNotTheEnv.

    Left to runtime this surfaces as a 401 AFTER every reviewer has run and billed. That is
    the failure this PR exists to delete, so it must fail before any subprocess starts."""

    _ANTH = '[producer]\nprovider = "anthropic"\nauth = "api"\n'

    def test_api_without_key_is_config_error(self, tmp_path) -> None:
        repo = _write(tmp_path, self._ANTH)
        with pytest.raises(ConfigError) as exc:
            load_config(repo, env=_NO_KEYS)
        msg = str(exc.value)
        assert "[producer]" in msg
        assert "ANTHROPIC_API_KEY" in msg, "the error must name the var the user has to set"

    def test_api_with_key_present_loads(self, tmp_path) -> None:
        repo = _write(tmp_path, self._ANTH)
        cfg = load_config(repo, env={**_NO_KEYS, "ANTHROPIC_API_KEY": "sk-test"})
        assert cfg.producer.auth == "api"

    def test_empty_string_key_counts_as_missing(self, tmp_path) -> None:
        """`export ANTHROPIC_API_KEY=` is set-but-empty. The CLI would 401 on it, so it is
        not a key."""
        repo = _write(tmp_path, self._ANTH)
        with pytest.raises(ConfigError):
            load_config(repo, env={**_NO_KEYS, "ANTHROPIC_API_KEY": ""})

    def test_every_offending_actor_is_reported_at_once(self, tmp_path) -> None:
        """Not one per re-run. A user fixing their config should see the whole list."""
        repo = _write(
            tmp_path,
            '[producer]\nprovider = "anthropic"\nauth = "api"\n'
            '[synthesizer]\nprovider = "anthropic"\nauth = "api"\n'
            '[drafter]\nprovider = "anthropic"\nauth = "api"\n',
        )
        with pytest.raises(ConfigError) as exc:
            load_config(repo, env=_NO_KEYS)
        msg = str(exc.value)
        for block in ("[producer]", "[synthesizer]", "[drafter]"):
            assert block in msg

    def test_custom_api_key_env_is_the_var_checked(self, tmp_path) -> None:
        """The check must follow api_key_env, not the provider default — otherwise a
        user with a correctly-set custom var is refused for no reason. (anthropic: the
        only provider where a custom var CAN be honoured.)"""
        toml = '[producer]\nprovider = "anthropic"\nauth = "api"\napi_key_env = "WORK_KEY"\n'
        repo = _write(tmp_path, toml)
        # the DEFAULT var being set must not satisfy a custom declaration
        with pytest.raises(ConfigError) as exc:
            load_config(repo, env={**_NO_KEYS, "ANTHROPIC_API_KEY": "sk-wrong"})
        assert "WORK_KEY" in str(exc.value)
        # and the custom var being set must satisfy it
        cfg = load_config(repo, env={**_NO_KEYS, "WORK_KEY": "sk-right"})
        assert cfg.producer.key_var() == "WORK_KEY"

    def test_unknown_provider_in_api_mode_demands_an_explicit_var(self) -> None:
        cfg = _cfg('[[reviewers]]\nname = "r"\nprovider = "openai"\nmodel = "m"\n')
        # simulate a provider with no conventional var by overriding after validation
        cfg.reviewers[0].__dict__["provider"] = "mystery"
        cfg.reviewers[0].__dict__["auth"] = "api"
        problems = api_key_problems(cfg, env=_NO_KEYS)
        assert any("api_key_env" in p for p in problems)


class TestSubscriptionAndAutoNeedNoKey:
    @pytest.mark.parametrize("mode", ["subscription", "auto"])
    def test_no_key_required(self, tmp_path, mode) -> None:
        repo = _write(tmp_path, f'[synthesizer]\nauth = "{mode}"\n')
        assert load_config(repo, env=_NO_KEYS).synthesizer.auth == mode


class TestApiKeyEnvWithoutApiModeIsRefused:
    """Naming a key var plainly means you intend to use it. Accepting the field and
    ignoring it is the same class of silent-wrong-mode bug this PR exists to kill."""

    @pytest.mark.parametrize("mode", ["subscription", "auto"])
    def test_rejected(self, mode) -> None:
        with pytest.raises(ValidationError) as exc:
            _cfg(f'[synthesizer]\nauth = "{mode}"\napi_key_env = "MY_KEY"\n')
        assert "api_key_env" in str(exc.value)


class TestBadValuesAreRefused:
    def test_unknown_auth_mode(self) -> None:
        with pytest.raises(ValidationError):
            _cfg('[synthesizer]\nauth = "freeloading"\n')

    def test_auth_on_a_reviewer(self) -> None:
        cfg = _cfg(
            '[[reviewers]]\nname = "r"\nprovider = "anthropic"\nmodel = "m"\n'
            'auth = "subscription"\n'
        )
        assert cfg.reviewers[0].auth == "subscription"
        assert cfg.reviewers[0].key_var() == "ANTHROPIC_API_KEY"


class TestApiKeyEnvOnOpenAiIsRefused:
    """`codex` does not read the environment — its key comes from CODEX_HOME. So
    `api_key_env = "WORK_KEY"` on an openai actor was ACCEPTED while codex quietly used
    whatever key its stored login held: the user believes their WORK_KEY is paying, and a
    DIFFERENT key actually is.

    That is the exact deceit this PR exists to delete, so it is refused rather than
    accepted-and-ignored. Caught by syncade's own panel."""

    def test_openai_api_key_env_is_a_config_error(self) -> None:
        with pytest.raises(ValidationError) as exc:
            _cfg('[synthesizer]\nauth = "api"\napi_key_env = "WORK_KEY"\n')
        msg = str(exc.value)
        assert "cannot be honoured" in msg
        assert "codex login --with-api-key" in msg, "must name the only thing that DOES work"

    def test_anthropic_api_key_env_is_still_fine(self) -> None:
        """claude DOES read the env, so the mapping is honourable there."""
        cfg = _cfg('[producer]\nprovider = "anthropic"\nauth = "api"\napi_key_env = "WORK_KEY"\n')
        assert cfg.producer.key_var() == "WORK_KEY"


class TestOpenAiApiModeIsVerifiedByTheCodexProbeNotTheEnv:
    """`codex` NEVER reads the environment — its key lives in CODEX_HOME. Demanding
    OPENAI_API_KEY at config load was a false requirement, and a DEAD END: we told the user
    to run `codex login --with-api-key` (the only thing that works) and then refused to
    load anyway unless they ALSO exported a var codex ignores.

    The openai `api` declaration is verified where it actually lives — the
    `codex login status` probe. That is the real check; the env one was theatre."""

    def test_openai_api_mode_loads_without_the_env_var(self, tmp_path) -> None:
        repo = _write(tmp_path, '[synthesizer]\nauth = "api"\n')
        cfg = load_config(repo, env=_NO_KEYS)  # OPENAI_API_KEY deliberately absent
        assert cfg.synthesizer.auth == "api", "the documented remediation path was a dead end"

    def test_anthropic_api_mode_STILL_requires_its_key(self, tmp_path) -> None:
        """claude DOES read the env, so there the requirement is real. Removing the openai
        check must not weaken the one that matters."""
        repo = _write(tmp_path, '[producer]\nprovider = "anthropic"\nauth = "api"\n')
        with pytest.raises(ConfigError):
            load_config(repo, env=_NO_KEYS)
