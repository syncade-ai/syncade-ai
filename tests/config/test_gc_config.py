"""``[gc]`` config block (PR-v2-9): run-artifact retention as config.

Covers the schema surface (defaults reproduce the runtime defaults, round-trips, valid range,
extra-key rejection) and the user path — loading from ``.syncade/config.toml``. That ``[gc]`` feeds
BOTH the ``--gc`` CLI path and the per-loop auto-prune (C3) is proven in ``tests/cli/test_gc.py``
and ``tests/gc/test_autoprune.py``."""

import pytest
from pydantic import ValidationError

from syncade import gc as gc_mod
from syncade.config import SyncadeConfig
from syncade.config_gc import GcConfig
from syncade.config_loader import load_config


def test_defaults_reproduce_the_runtime_defaults():
    """Drift guard: ``GcConfig`` defaults ARE ``gc.DEFAULT_KEEP`` / ``DEFAULT_MAX_AGE_DAYS``, so a
    zero-config run prunes byte-identically and the two can never silently diverge."""
    assert GcConfig().keep == gc_mod.DEFAULT_KEEP
    assert GcConfig().max_age_days == gc_mod.DEFAULT_MAX_AGE_DAYS
    assert SyncadeConfig().gc.keep == gc_mod.DEFAULT_KEEP
    assert SyncadeConfig().gc.max_age_days == gc_mod.DEFAULT_MAX_AGE_DAYS


def test_round_trips_losslessly():
    cfg = SyncadeConfig.model_validate({"gc": {"keep": 5, "max_age_days": 3}})
    assert (cfg.gc.keep, cfg.gc.max_age_days) == (5, 3)
    rt = SyncadeConfig.model_validate(cfg.model_dump())
    assert (rt.gc.keep, rt.gc.max_age_days) == (5, 3)


def test_zero_keep_and_zero_age_are_allowed():
    """keep=0 (prune all beyond-protection) and max_age_days=0 (age floor off) are valid."""
    g = GcConfig(keep=0, max_age_days=0)
    assert (g.keep, g.max_age_days) == (0, 0)


@pytest.mark.parametrize("kw", [{"keep": -1}, {"max_age_days": -1}])
def test_negative_is_rejected(kw):
    with pytest.raises(ValidationError):
        GcConfig(**kw)


def test_extra_key_is_forbidden():
    with pytest.raises(ValidationError):
        GcConfig(keep=20, nonsense=1)


def test_gc_section_loads_from_toml(tmp_path):
    """The user path: ``[gc] keep = N / max_age_days = M`` in the TOML reaches the config."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        "[gc]\nkeep = 5\nmax_age_days = 3\n", encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    assert (cfg.gc.keep, cfg.gc.max_age_days) == (5, 3)


@pytest.mark.parametrize(
    "kw",
    [{"keep": True}, {"keep": False}, {"max_age_days": True}, {"max_age_days": False}],
)
def test_boolean_is_rejected(kw):
    """TOML booleans coerce silently to int in pydantic — reject them explicitly.
    gc.keep=false→0 would prune all transcripts; always a config mistake."""
    with pytest.raises(ValidationError, match="boolean"):
        GcConfig(**kw)


@pytest.mark.parametrize(
    "toml_text",
    ["[gc]\nkeep = false\n", "[gc]\nmax_age_days = true\n"],
)
def test_boolean_from_toml_is_rejected(toml_text, tmp_path):
    """``[gc] keep = false`` in TOML must raise, not silently set keep=0."""
    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(toml_text, encoding="utf-8")
    with pytest.raises(Exception, match="boolean|ValidationError"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "kw", [{"keep": "0"}, {"keep": 1.0}, {"max_age_days": "3"}, {"max_age_days": 2.5}]
)
def test_non_integer_is_rejected(kw):
    """Strict numeric (dogfood R4): a quoted number or a float is REJECTED, not coerced —
    ``gc.keep = "0"`` / ``= 1.0`` would silently prune differently than intended; fail exit 50."""
    with pytest.raises(ValidationError):
        GcConfig(**kw)
