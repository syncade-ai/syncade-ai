"""Global config layer (``~/.syncade/config.toml``) — precedence, section-replace, back-compat.

pr-v2-30 Issue 1. The load chain is defaults < preset < global < repo < CLI. Paired sections
([producer]/[synthesizer]/[drafter]/[auditor]) replace wholesale so a provider/model pair can never
split across layers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.config import SyncadeConfig
from syncade.config_loader import ConfigError, _deep_merge, load_config


def _repo(tmp_path: Path, toml: str | None = None) -> Path:
    repo = tmp_path / "repo"
    if toml is None:
        repo.mkdir()
    else:
        (repo / ".syncade").mkdir(parents=True)
        (repo / ".syncade" / "config.toml").write_text(toml)
    return repo


def _global(tmp_path: Path, toml: str) -> Path:
    p = tmp_path / "global-config.toml"
    p.write_text(toml)
    return p


def test_no_global_no_repo_is_byte_identical_to_zero_config(tmp_path):
    """The global layer is purely additive: absent it, load_config == SyncadeConfig()."""
    cfg = load_config(_repo(tmp_path), global_config_path=tmp_path / "absent.toml")
    assert cfg.model_dump() == SyncadeConfig().model_dump()


def test_global_applies_when_no_repo_config(tmp_path):
    g = _global(tmp_path, "[loop]\nmax_rounds = 1\n")
    cfg = load_config(_repo(tmp_path), global_config_path=g)
    assert cfg.loop.max_rounds == 1


def test_repo_overrides_global_but_inherits_unset_loop_keys(tmp_path):
    g = _global(tmp_path, "[loop]\nmax_rounds = 2\nbudget_usd = 5.0\n")
    repo = _repo(tmp_path, "[loop]\nmax_rounds = 1\n")
    cfg = load_config(repo, global_config_path=g)
    assert cfg.loop.max_rounds == 1  # repo wins
    assert cfg.loop.budget_usd == 5.0  # unset in repo -> inherited from global (key-merge)


def test_paired_section_replace_prevents_split_pairing(tmp_path):
    """The load-bearing rule: a repo overriding only [producer].provider must NOT inherit global's
    model — the pair re-derives for the new provider, never openai + a claude model."""
    g = _global(tmp_path, '[producer]\nprovider = "anthropic"\nmodel = "claude-sonnet-4-6"\n')
    repo = _repo(tmp_path, '[producer]\nprovider = "openai"\n')
    cfg = load_config(repo, global_config_path=g)
    assert cfg.producer.provider == "openai"
    assert cfg.producer.model != "claude-sonnet-4-6"  # claude model did NOT leak across layers
    expected = SyncadeConfig.model_validate({"producer": {"provider": "openai"}}).producer.model
    assert cfg.producer.model == expected  # a consistent, re-derived openai pair


def test_paired_name_replaces_only_at_top_level(tmp_path):
    """D2's wholesale-replace is TOP-LEVEL only: a nested table that merely SHARES a paired name
    (e.g. pricing.models.producer, for a model literally named 'producer') must still key-merge, not
    be wholesale-replaced — else a partial nested override drops sibling keys."""
    base = {
        "producer": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "pricing": {"models": {"producer": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}},
    }
    override = {
        "producer": {"provider": "openai"},  # top-level paired -> REPLACE
        "pricing": {"models": {"producer": {"output_per_mtok": 9.0}}},  # nested -> KEY-MERGE
    }
    merged = _deep_merge(base, override)
    assert merged["producer"] == {"provider": "openai"}  # replaced whole (no inherited model)
    # nested 'producer' key-merged: input inherited, output overridden (pre-fix dropped input)
    nested = merged["pricing"]["models"]["producer"]
    assert nested == {"input_per_mtok": 1.0, "output_per_mtok": 9.0}


def test_global_producer_pair_applies_whole(tmp_path):
    """Global can pin a full producer pair; with no repo override it applies verbatim."""
    g = _global(tmp_path, '[producer]\nprovider = "anthropic"\nmodel = "claude-opus-4-6"\n')
    cfg = load_config(_repo(tmp_path), global_config_path=g)
    assert (cfg.producer.provider, cfg.producer.model) == ("anthropic", "claude-opus-4-6")


def test_bad_global_toml_names_the_global_path(tmp_path):
    g = _global(tmp_path, "[loop]\nmax_rounds = = 1\n")  # malformed TOML
    with pytest.raises(ConfigError) as ei:
        load_config(_repo(tmp_path), global_config_path=g)
    assert str(g) in str(ei.value)  # the GLOBAL file is named, not the repo


def test_bad_global_schema_is_a_config_error(tmp_path):
    g = _global(tmp_path, "[loop]\nbogus_key = 1\n")  # unknown field -> strict-schema reject
    with pytest.raises(ConfigError):
        load_config(_repo(tmp_path), global_config_path=g)


def test_global_overrides_preset(tmp_path):
    """The global layer sits ABOVE --preset in precedence (defaults < preset < global < repo)."""
    g = _global(tmp_path, "[loop]\nmax_rounds = 2\n")
    cfg = load_config(_repo(tmp_path), preset="cheap", global_config_path=g)
    assert cfg.loop.max_rounds == 2  # global(2) wins over cheap's max_rounds = 1


def test_production_wiring_uses_default_path_when_unset(tmp_path, monkeypatch):
    """Production path: load_config() with NO global_config_path reads whatever
    _default_global_config_path() returns (i.e. ~/.syncade/config.toml). Proves the feature works
    for real callers, not only tests that pass an explicit path."""
    g = tmp_path / "default-global.toml"
    g.write_text("[loop]\nmax_rounds = 1\n")
    monkeypatch.setattr("syncade.config_loader._default_global_config_path", lambda: g)
    cfg = load_config(_repo(tmp_path))  # no global_config_path -> resolver is consulted
    assert cfg.loop.max_rounds == 1
