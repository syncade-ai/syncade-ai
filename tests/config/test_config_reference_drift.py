"""C5: ``docs/config-reference.md`` documents every config field and no ghost fields.

A bidirectional, auto-discovering drift guard (Q7 — hand-written doc plus a test, not generated).
It walks every ``BaseModel`` reachable from :class:`SyncadeConfig` and asserts the doc's per-model
table — demarcated by ``<!-- config-fields: ModelName -->`` — lists EXACTLY that model's fields.
Adding a field, renaming one, or adding a whole new nested config model all fail this test until the
doc is updated (the attack in C5: "add a field, the doc test still passes" must not hold).
The marker-locked-table pattern mirrors SKILL.md's ``SYNCADE-SHARED`` span."""

import re
import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

from syncade.config import SyncadeConfig

_DOC = (Path(__file__).resolve().parents[2] / "docs" / "config-reference.md").read_text(
    encoding="utf-8"
)


def _nested_models(annotation):
    """Yield every BaseModel subclass inside a type annotation (handles ``list[X]``, ``dict[k, V]``,
    ``X | None``, etc. by recursing into ``typing.get_args``)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for arg in typing.get_args(annotation):
        yield from _nested_models(arg)


def _reachable_models():
    seen: set[type] = set()
    stack: list[type] = [SyncadeConfig]
    while stack:
        model = stack.pop()
        if model in seen:
            continue
        seen.add(model)
        for field in model.model_fields.values():
            stack.extend(_nested_models(field.annotation))
    return seen


def _table_rows(model_name: str) -> dict[str, list[str]]:
    """Return ``{field_name: [cell, cell, ...]}`` for the model's marked table (first-column
    backticked field name → its row's cells)."""
    marker = f"<!-- config-fields: {model_name} -->"
    assert marker in _DOC, (
        f"docs/config-reference.md has no field table for {model_name}. Add a section with "
        f"`{marker}` above a table whose first column backticks each field name."
    )
    rows: dict[str, list[str]] = {}
    in_table = False
    for line in _DOC.split(marker, 1)[1].splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            in_table = True
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            match = re.fullmatch(r"`([a-z_][a-z0-9_]*)`", cells[0])
            if match:
                rows[match.group(1)] = cells
        elif in_table:
            break  # the table ended
    return rows


def _documented_fields(model_name: str) -> set[str]:
    return set(_table_rows(model_name))


def _literal_values(annotation) -> set[str] | None:
    """The value set of a ``Literal[...]`` field (unwrapping ``X | None`` etc.), else ``None``."""
    for candidate in (annotation, *typing.get_args(annotation)):
        if typing.get_origin(candidate) is typing.Literal:
            return set(typing.get_args(candidate))
    return None


_REACHABLE = sorted(_reachable_models(), key=lambda m: m.__name__)

# Every Literal-typed field, so the doc's enum values are checked against the schema (dogfood B3:
# the earlier drift test checked field NAMES only, so the doc drifted on the `thinking` enum).
_LITERAL_FIELDS = [
    (model, field, values)
    for model in _REACHABLE
    for field, info in model.model_fields.items()
    if (values := _literal_values(info.annotation)) is not None
]


@pytest.mark.parametrize("model", _REACHABLE, ids=lambda m: m.__name__)
def test_reference_table_matches_schema_exactly(model):
    documented = _documented_fields(model.__name__)
    actual = set(model.model_fields)
    assert not (actual - documented), (
        f"{model.__name__}: undocumented field(s) {sorted(actual - documented)} — "
        "add a row to its table in docs/config-reference.md"
    )
    assert not (documented - actual), (
        f"{model.__name__}: table documents field(s) not in the schema "
        f"{sorted(documented - actual)} — remove/rename the row in docs/config-reference.md"
    )


@pytest.mark.parametrize(
    "model,field,values",
    _LITERAL_FIELDS,
    ids=[f"{m.__name__}.{f}" for m, f, _ in _LITERAL_FIELDS],
)
def test_reference_documents_enum_values_exactly(model, field, values):
    """B3: for every ``Literal`` field, the doc's Type column lists EXACTLY the schema's accepted
    values — no invalid value (the `minimal` bug), no missing one (the omitted `max`)."""
    type_cell = _table_rows(model.__name__)[field][1]  # the Type column
    documented = set(re.findall(r"`([a-z0-9_-]+)`", type_cell))
    assert documented == values, (
        f"{model.__name__}.{field}: the doc's Type column lists {sorted(documented)} but the "
        f"schema accepts {sorted(values)} — fix docs/config-reference.md"
    )


def test_reference_names_every_preset_and_states_precedence():
    """Lighter guard: the doc names each bundled preset and states the override precedence."""
    from syncade.presets import PRESET_NAMES

    for name in PRESET_NAMES:
        assert name in _DOC, f"config-reference.md never mentions the {name!r} preset"
    assert "precedence" in _DOC.lower()
