"""Bundled config presets (PR-v2-9): ``--preset cheap|balanced|thorough``.

Loads a packaged TOML preset as a plain ``dict`` — stdlib + importlib only, NO ``config`` import, so
:func:`syncade.config_loader.load_config` can deep-merge it without a cycle. The preset is the run's
BASE config; the user's ``.syncade/config.toml`` layers on top (user wins).

Presets vary ONLY safe loop dimensions (rounds / timeout) and NEVER the reviewer model or effort
tier — a cheaper panel that audits leniently is the pr-29 hazard. ``tests/config/test_presets.py``
pins that: every preset yields the SAME reviewer roster as the shipped defaults, and ``balanced`` is
behaviourally the defaults.
"""

from __future__ import annotations

import tomllib
from importlib.resources import files

# The bundled presets. Explicit (not directory-discovered) so ``--preset``'s argparse choices and
# this tuple cannot drift, and an unrelated ``.toml`` dropped in the dir can't become a preset.
PRESET_NAMES: tuple[str, ...] = ("cheap", "balanced", "thorough")


class PresetError(Exception):
    """An unknown ``--preset`` name (the CLI's argparse ``choices`` normally catches this first)."""


def load_preset(name: str) -> dict:
    """Return the bundled preset ``name`` as a parsed TOML dict.

    Read via :func:`importlib.resources.files` so it resolves in both a source checkout and an
    installed wheel. Raises :class:`PresetError` for an unknown name.
    """
    if name not in PRESET_NAMES:
        raise PresetError(f"unknown preset {name!r}; choose from {list(PRESET_NAMES)}")
    text = (files("syncade") / "templates" / "presets" / f"{name}.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)
