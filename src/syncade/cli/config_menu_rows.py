"""Screen/row model for the drill-in config menu (pr-v2-31 inc 4).

The menu is a tree of screens: the top screen, one per actor (``producer`` / ``reviewers.N`` /
``synthesizer``), an ``advanced`` screen, and one per advanced section (``retry`` / ``gc`` /
``review`` / ``checks`` and each ``checks.N``). Each screen is a list of :class:`Row`; a row is an
``edit`` (a settable scalar field), a ``drill`` (navigate to a child screen), or an ``info`` (an
inert placeholder, e.g. an empty ``checks`` screen). Pure — no curses; ``ConfigMenu`` drives it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args, get_origin

from syncade.cli import config_keys
from syncade.config import SyncadeConfig
from syncade.config_loader import _PAIRED_SECTIONS

# Sections whose TOML table replaces WHOLESALE (shadow is section-granular → subkey=None); every
# other section merges key-by-key (shadow is per-key → subkey=field). Mirrors the loader's rule.
_WHOLESALE = _PAIRED_SECTIONS | {"reviewers", "checks"}
_ACTOR_SECTIONS = _PAIRED_SECTIONS | {"reviewers"}

# Actor fields shown in a drill screen, in display order — filtered to the fields the actor actually
# has. ``provider`` is folded into the Model row (edited as ``provider/model``); ``api_key_env`` is
# CLI-only, kept out of the menu to stay scannable.
_ACTOR_FIELD_ORDER = [
    "model",
    "thinking",
    "permissions",
    "auth",
    "timeout_seconds",
    "name",
    "adversarial_lens",
    "template",
]
_SKIP_FIELDS = {"provider", "api_key_env"}

_LABELS = {
    "model": "Model",
    "thinking": "Thinking",
    "permissions": "Permissions",
    "auth": "Auth",
    "timeout_seconds": "Timeout (s)",
    "name": "Name",
    "adversarial_lens": "Adversarial lens",
    "template": "Template",
    "max_retries": "Max retries",
    "keep": "Keep (runs)",
    "max_age_days": "Max age (days)",
    "strip_repo_context_files": "Strip repo-context files",
    "command": "Command",
    "severity": "Severity",
    "test_command": "Test command",
    "test_timeout_seconds": "Test timeout (s)",
    "budget_tokens": "Budget (tokens)",
}


@dataclass(frozen=True)
class Row:
    """One menu row. ``kind``: ``edit`` (``key`` is a dotted field path), ``drill`` (``key`` is the
    child screen id), or ``info`` (inert). ``value_key`` is the dotted key whose value to display
    (``None`` → no value column); ``section``/``subkey`` drive the shadow flag for edit rows."""

    label: str
    kind: str
    key: str
    section: str | None = None
    subkey: str | None = None
    value_key: str | None = None


def _label(field: str) -> str:
    return _LABELS.get(field, field.replace("_", " ").capitalize())


def _model_at(screen: str):
    """The pydantic model class shown by ``screen`` (walks list indices to the element type)."""
    current = SyncadeConfig
    for part in screen.split("."):
        if get_origin(current) is list:
            current = get_args(current)[0]  # element type; `part` is the index
            continue
        current = current.model_fields[part].annotation
    return current


def _settable(model) -> list[str]:
    return [n for n, f in model.model_fields.items() if config_keys._settable_scalar(f.annotation)]


def _field_rows(config, screen: str) -> list[Row]:
    """Edit rows for every settable scalar field of the model at ``screen`` (actor or section)."""
    section = screen.split(".")[0]
    fields = [f for f in _settable(_model_at(screen)) if f not in _SKIP_FIELDS]
    if section in _ACTOR_SECTIONS:
        fields = [f for f in _ACTOR_FIELD_ORDER if f in fields]
    subkey_none = section in _WHOLESALE
    return [
        Row(
            _label(f), "edit", f"{screen}.{f}", section, None if subkey_none else f, f"{screen}.{f}"
        )
        for f in fields
    ]


def _top_rows(config) -> list[Row]:
    rows = [Row("Producer", "drill", "producer", "producer", None, "producer.model")]
    for i in range(len(config.reviewers)):
        rows.append(
            Row(
                f"Reviewer {i + 1}",
                "drill",
                f"reviewers.{i}",
                "reviewers",
                None,
                f"reviewers.{i}.model",
            )
        )
    rows += [
        Row("Judge", "drill", "synthesizer", "synthesizer", None, "synthesizer.model"),
        Row("Rounds (max)", "edit", "loop.max_rounds", "loop", "max_rounds", "loop.max_rounds"),
        Row(
            "Time per subprocess (s)",
            "edit",
            "loop.timeout_seconds",
            "loop",
            "timeout_seconds",
            "loop.timeout_seconds",
        ),
        Row("Cost cap (USD)", "edit", "loop.budget_usd", "loop", "budget_usd", "loop.budget_usd"),
        Row("Advanced…", "drill", "advanced"),
    ]
    return rows


def _advanced_rows(config) -> list[Row]:
    return [
        Row("Retry", "drill", "retry"),
        Row("GC", "drill", "gc"),
        Row("Review", "drill", "review"),
        Row("Checks", "drill", "checks"),
    ]


def _checks_rows(config) -> list[Row]:
    if not config.checks:
        return [Row("(no checks configured — add via config.toml)", "info", "")]
    return [
        Row(f"Check {i + 1}: {c.name}", "drill", f"checks.{i}", "checks", None, f"checks.{i}.name")
        for i, c in enumerate(config.checks)
    ]


def screen_rows(config, screen: str) -> list[Row]:
    """The rows for ``screen`` (``""`` = top). ``config`` is the effective (merged) config."""
    if screen == "":
        return _top_rows(config)
    if screen == "advanced":
        return _advanced_rows(config)
    if screen == "checks":
        return _checks_rows(config)
    return _field_rows(config, screen)
