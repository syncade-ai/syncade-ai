"""Bundled config presets (PR-v2-9): ``--preset cheap|balanced|thorough`` (C4).

The load-bearing safety property is reviewer PARITY: no preset may lower the reviewer model or
effort tier (the pr-29 leniency hazard). ``balanced`` must be behaviourally the shipped defaults,
every preset must parse/load, and the user's ``.syncade/config.toml`` must layer on top."""

import pytest

from syncade.config import SyncadeConfig
from syncade.config_loader import load_config
from syncade.presets import PRESET_NAMES, PresetError, load_preset


def _cfg_with_preset(tmp_path, name, toml_body=None):
    if toml_body is not None:
        (tmp_path / ".syncade").mkdir(exist_ok=True)
        (tmp_path / ".syncade" / "config.toml").write_text(toml_body, encoding="utf-8")
    return load_config(tmp_path, preset=name)


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_every_preset_parses_and_loads(tmp_path, name):
    """C4 'a preset that fails to parse/load': each bundled preset is valid TOML + valid schema."""
    cfg = load_config(tmp_path, preset=name)
    assert isinstance(cfg, SyncadeConfig)


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_no_preset_changes_the_reviewer_panel(tmp_path, name):
    """THE pr-29 guard (C4): a preset may vary rounds/timeout but must NEVER touch the reviewer
    model or effort tier. Every preset yields the exact default reviewer roster."""
    default = SyncadeConfig().reviewers
    got = load_config(tmp_path, preset=name).reviewers
    assert [(r.name, r.provider, r.model, r.thinking) for r in got] == [
        (r.name, r.provider, r.model, r.thinking) for r in default
    ]


def test_balanced_is_behaviourally_the_defaults(tmp_path):
    """C4: ``balanced`` == a zero-config run. Pins balanced.toml to the shipped defaults so it can
    never quietly diverge from what a plain ``syncade <pr-doc>`` does."""
    assert load_config(tmp_path, preset="balanced") == SyncadeConfig()


def test_cheap_is_single_pass(tmp_path):
    cfg = load_config(tmp_path, preset="cheap")
    assert cfg.loop.max_rounds == 1  # no producer loop
    assert cfg.loop.timeout_seconds == SyncadeConfig().loop.timeout_seconds  # timeout unchanged


def test_thorough_doubles_the_timeout_keeps_full_rounds(tmp_path):
    cfg = load_config(tmp_path, preset="thorough")
    assert cfg.loop.max_rounds == 3
    assert cfg.loop.timeout_seconds == 3600.0


def test_user_config_layers_on_top_of_the_preset(tmp_path):
    """Q2 / precedence: preset is the BASE, ``.syncade/config.toml`` wins per-key, and a preset knob
    the user did NOT set survives the merge (deep merge, not wholesale replace)."""
    cfg = _cfg_with_preset(tmp_path, "thorough", "[loop]\nmax_rounds = 1\n")
    assert cfg.loop.max_rounds == 1  # user file beat the preset's 3
    assert cfg.loop.timeout_seconds == 3600.0  # preset's timeout survived (user didn't set it)


def test_no_preset_and_no_file_is_byte_identical(tmp_path):
    """C6: no ``--preset`` and no config file → exactly ``SyncadeConfig()``."""
    assert load_config(tmp_path) == SyncadeConfig()


def test_unknown_preset_raises(tmp_path):
    with pytest.raises(PresetError):
        load_preset("nonexistent")
    with pytest.raises(PresetError):
        load_config(tmp_path, preset="nonexistent")


def test_preset_names_match_bundled_files():
    """Every name in PRESET_NAMES resolves to a bundled file, and vice-versa — no orphan .toml can
    silently exist and no advertised name can 404."""
    from importlib.resources import files

    present = {
        p.name.removesuffix(".toml")
        for p in (files("syncade") / "templates" / "presets").iterdir()
        if p.name.endswith(".toml")
    }
    assert present == set(PRESET_NAMES)
