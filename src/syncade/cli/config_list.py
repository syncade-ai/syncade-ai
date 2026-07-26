"""Enumerate the FULL settable config surface for ``syncade --config list --all`` (pr-v2-32).

A leaf module. It walks the ``SyncadeConfig`` schema with the SAME settability rule as
``--config set`` (``config_keys._settable_scalar``), so the ``--all`` surface can never drift from
what ``set`` accepts. It decides only WHAT fields exist and how they group; ``config_mode`` renders
each row's value, layer, and ``overrides`` note.
"""

from __future__ import annotations

from typing import get_args, get_origin

from syncade.cli import config_keys
from syncade.config import SyncadeConfig
from syncade.config_loader import _PAIRED_SECTIONS

# Sections whose TOML table replaces WHOLESALE (layer is section-granular → subkey=None); others
# merge key-by-key (layer is per-field). Mirrors config_loader / config_menu_rows.
_WHOLESALE = _PAIRED_SECTIONS | {"reviewers", "checks"}

# Display order + section headers. Sections not listed here are appended after, under a `[<name>]`
# fallback header — a completeness test asserts every settable field is emitted, so none vanish.
_HEADERS = {
    "producer": "[producer]",
    "reviewers": "[[reviewers]]",
    "synthesizer": "[synthesizer]  (judge)",
    "loop": "[loop]",
    "review": "[review]",
    "retry": "[advanced.retry]",
    "gc": "[advanced.gc]",
    "checks": "[advanced.checks]",
    "drafter": "[advanced.drafter]",
    "auditor": "[advanced.auditor]",
    "worktree_base": "[advanced]",
}


def header_for(section: str) -> str:
    return _HEADERS.get(section, f"[{section}]")


def _subkey(section: str, parts: list[str]) -> str | None:
    """Layer granularity for ``_layer_of``: per-field for a key-merge section (a model section not
    replaced wholesale, e.g. loop/review/retry/gc); section-granular (None) for wholesale sections
    and top-level scalars."""
    if len(parts) == 1 or section in _WHOLESALE:
        return None
    return parts[-1]


def _walk(value, annotation, prefix: str):
    """Yield the dotted key of every settable scalar under ``value`` (a config instance/section)."""
    if config_keys._settable_scalar(annotation):
        yield prefix
        return
    if get_origin(annotation) is list:
        elems = [a for a in get_args(annotation) if a is not type(None)]
        if elems and config_keys._is_model(elems[0]):
            for i, item in enumerate(value):
                yield from _walk(item, elems[0], f"{prefix}.{i}")
        return
    if config_keys._is_model(annotation):
        for name, field in annotation.model_fields.items():
            yield from _walk(getattr(value, name), field.annotation, f"{prefix}.{name}")


def all_rows(config) -> list[tuple[str, str, str, str | None]]:
    """``(key, label, section, subkey)`` for every settable scalar field, in display order.

    ``label`` is the key minus its section prefix (unambiguous per section); ``config`` is the
    effective config, whose roster sizes drive the reviewer/check indices."""
    fields = SyncadeConfig.model_fields
    sections = list(_HEADERS) + [s for s in fields if s not in _HEADERS]
    rows = []
    for section in sections:
        if section not in fields:
            continue
        for key in _walk(getattr(config, section), fields[section].annotation, section):
            parts = key.split(".")
            label = key[len(section) + 1 :] if "." in key else key
            rows.append((key, label, section, _subkey(section, parts)))
    return rows
