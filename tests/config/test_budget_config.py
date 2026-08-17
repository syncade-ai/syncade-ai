"""Schema tests for the `[loop]` budget ceilings.

Split from ``test_config_schema.py`` in PR-h-field-06, which sits at the 500-LOC cap — the same
reason ``test_reviewer_config.py`` exists, and its docstring says so. The budget knobs became a
cohesive group once they gained a DEFAULT and a `0 = no ceiling` opt-out, so they get a home
rather than another exemption.
"""

import pytest
from pydantic import ValidationError

from syncade import config_loader
from syncade.cli import main
from syncade.config import SyncadeConfig


def test_budget_defaults_to_a_token_ceiling_and_no_dollar_ceiling():
    """PR-h-field-06: tokens default to a ceiling, dollars stay unset.

    Deliberately asymmetric. `cost_usd` is an API-EQUIVALENT VALUATION and most traffic is
    subscription, so a default dollar ceiling would interrupt a run over money nobody spent;
    tokens are real work whoever pays.
    """
    cfg = SyncadeConfig()
    assert cfg.loop.budget_tokens == 50_000_000
    assert cfg.loop.budget_usd is None


def test_budget_explicit_round_trips():
    cfg = SyncadeConfig(loop={"budget_tokens": 500_000, "budget_usd": 2.5})
    assert cfg.loop.budget_tokens == 500_000
    assert cfg.loop.budget_usd == 2.5


@pytest.mark.parametrize("value", [-1])
def test_budget_tokens_negative_rejected(value):
    """0 is no longer rejected — it is the explicit no-ceiling opt-out (PR-h-field-06),
    the only way to say "unlimited" once an omitted key means the default."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"budget_tokens": value})
    assert "budget_tokens" in str(exc_info.value)


@pytest.mark.parametrize("bad", [False, True, 0.0, 1.0, "0", "50000000"])
def test_budget_tokens_wrong_type_rejected(bad):
    """Wrong-type TOML values must be refused rather than silently coerced.

    pydantic's lax int coercion turns `budget_tokens = false` -> 0 (the opt-out sentinel),
    `budget_tokens = true` -> 1 (a 1-token ceiling), and `budget_tokens = 0.0` or `"0"`
    -> 0. Any of these disables or distorts the default ceiling instead of surfacing a
    config error. The field_validator rejects them so that a TOML typo does not silently
    compromise the safety guard (fails loudly at config load, exit 50).
    """
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"budget_tokens": bad})
    assert "budget_tokens" in str(exc_info.value)


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_budget_usd_invalid_rejected(value):
    # ge=0 rejects negatives; the field_validator rejects NaN/inf (which the bound admits and
    # would silently mean "no ceiling"). 0 is NOT invalid — it is the explicit no-ceiling
    # opt-out, symmetric with budget_tokens (PR-h-field-06).
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"budget_usd": value})
    assert "budget_usd" in str(exc_info.value)


@pytest.mark.parametrize("bad", [False, True, "0", "1.5"])
def test_budget_usd_wrong_type_rejected(bad):
    """Wrong-type TOML values for budget_usd must fail rather than silently coerce.

    pydantic's lax mode coerces `budget_usd = false` -> 0.0 (the no-ceiling opt-out sentinel),
    `budget_usd = true` -> 1.0 ($1 ceiling), and quoted strings like "1.5" -> 1.5. Because
    explicit 0 lands in model_fields_set, a TOML typo of `budget_usd = false` can silently
    disable an inherited dollar ceiling instead of surfacing a config error. The mode="before"
    validator rejects all non-numeric forms (fails loudly at config load, exit 50).
    """
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"budget_usd": bad})
    assert "budget_usd" in str(exc_info.value)


@pytest.mark.parametrize("ok", [0, 0.0, 1, 2.5, None])
def test_budget_usd_valid_forms_accepted(ok):
    """Plain int/float (including 0.0 opt-out) and None (unset) must all pass validation."""
    cfg = SyncadeConfig(loop={"budget_usd": ok})
    assert cfg.loop.budget_usd == ok


@pytest.mark.parametrize(
    "key,toml",
    [
        ("loop.budget_tokens", "[loop]\nbudget_tokens = 0\n"),
        ("loop.budget_usd", "[loop]\nbudget_usd = 9.99\n"),
    ],
)
def test_set_empty_budget_field_rejected(tmp_path, monkeypatch, key, toml):
    """--config set with an empty value must exit 50 and leave the file unchanged.

    Clearing via empty omits the TOML key: budget_tokens then resolves the 50M default
    and budget_usd is re-inherited on --resume. The correct opt-out is explicit 0.
    """
    g = tmp_path / "global.toml"
    g.write_text(toml)
    monkeypatch.setattr(config_loader, "_default_global_config_path", lambda: g)
    repo = tmp_path / "repo"
    repo.mkdir()
    rc = main(["--repo-root", str(repo), "--config", "set", key, ""])
    assert rc == 50, f"empty {key!r} must exit 50, got {rc}"
    assert g.read_text() == toml
