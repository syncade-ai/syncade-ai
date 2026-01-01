"""``[retry]`` config block (PR-v2-9): the shared transient-retry bound exposed as config.

Covers the schema surface (default reproduces the runtime bound, round-trips, valid range,
extra-key rejection) and the actual user path — loading it from ``.syncade/config.toml``. The
threading into the reviewer / synth / producer legs is proven in :mod:`tests.test_retry`."""

import pytest
from pydantic import ValidationError

from syncade import retry
from syncade.config import SyncadeConfig
from syncade.config_loader import load_config
from syncade.config_retry import RetryConfig


def test_default_reproduces_the_runtime_bound_exactly():
    """C6 / drift guard: the config default IS ``retry.MAX_RETRIES``, so a zero-config run is
    byte-identical and the two can never silently diverge."""
    assert RetryConfig().max_retries == retry.MAX_RETRIES
    assert SyncadeConfig().retry.max_retries == retry.MAX_RETRIES


def test_round_trips_losslessly():
    cfg = SyncadeConfig.model_validate({"retry": {"max_retries": 5}})
    assert cfg.retry.max_retries == 5
    assert SyncadeConfig.model_validate(cfg.model_dump()).retry.max_retries == 5


def test_zero_is_allowed_fail_fast():
    """0 disables retries (fail fast) — a valid, deliberate setting, not out of range."""
    assert RetryConfig(max_retries=0).max_retries == 0


@pytest.mark.parametrize("bad", [-1, -5])
def test_negative_is_rejected(bad):
    with pytest.raises(ValidationError):
        RetryConfig(max_retries=bad)


def test_extra_key_is_forbidden():
    with pytest.raises(ValidationError):
        RetryConfig(max_retries=2, nonsense=1)


def test_retry_section_absent_yields_the_runtime_default(tmp_path):
    """An absent ``[retry]`` section leaves the bound at ``retry.MAX_RETRIES``."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text("[loop]\nmax_rounds = 2\n", encoding="utf-8")
    assert load_config(tmp_path).retry.max_retries == retry.MAX_RETRIES


def test_retry_section_loads_from_toml(tmp_path):
    """The user path: ``[retry] max_retries = N`` in the TOML reaches the config."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        "[retry]\nmax_retries = 5\n", encoding="utf-8"
    )
    assert load_config(tmp_path).retry.max_retries == 5


@pytest.mark.parametrize("bad", [True, False])
def test_boolean_is_rejected(bad):
    """TOML booleans coerce silently to int in pydantic — reject them explicitly."""
    with pytest.raises(ValidationError, match="boolean"):
        RetryConfig(max_retries=bad)


def test_boolean_from_toml_is_rejected(tmp_path):
    """``[retry] max_retries = true`` in TOML must exit with config error, not silently become 1."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        "[retry]\nmax_retries = true\n", encoding="utf-8"
    )
    with pytest.raises(Exception, match="boolean|ValidationError"):
        load_config(tmp_path)


@pytest.mark.parametrize("bad", ["5", 5.0, "abc", 2.5])
def test_non_integer_is_rejected(bad):
    """Strict numeric (dogfood R4): a quoted number, a float, or junk is REJECTED, not coerced —
    ``max_retries = "5"`` / ``= 5.0`` is a TOML typo that must fail exit 50, not silently change
    retry behavior."""
    with pytest.raises(ValidationError):
        RetryConfig(max_retries=bad)
