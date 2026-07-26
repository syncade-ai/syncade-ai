"""Schema-driven dotted-key resolution + coercion for ``--config set`` (pr-v2-31 Increment 2).

Shared by ``cli.config_mode`` (the ``--config set`` verb) and ``cli.config_tui`` (the menu), so
both surfaces resolve and coerce identically. Resolves a dotted key against ``SyncadeConfig``'s
pydantic schema to the leaf field's declared type, and coerces a raw CLI string to that type.

A path that does not resolve to a settable SCALAR leaf (an unknown field, or a whole
section/roster) is an :class:`UnknownKey`; a value that does not fit the leaf's type is an
:class:`InvalidValue`.
"""

from __future__ import annotations

from types import UnionType
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel

from syncade.config import SyncadeConfig

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


class UnknownKey(Exception):
    """A dotted key that does not resolve to a settable scalar field."""


class InvalidValue(Exception):
    """A raw string that cannot be coerced to the field's declared type."""


def coerce(annotation, raw: str):
    """Coerce ``raw`` (a CLI string) to ``annotation``'s Python type.

    Raises :class:`InvalidValue` when the string does not fit. An optional field (``X | None``)
    clears to ``None`` on empty input; ``str``/``Path`` and other scalars keep the raw string
    (pydantic coerces on load, and TOML has no ``Path`` type).
    """
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Optional (X | None): empty input clears; otherwise coerce the non-None arm.
    if origin in (Union, UnionType) and type(None) in args:
        if raw == "":
            return None
        (inner,) = [a for a in args if a is not type(None)]
        return coerce(inner, raw)

    if origin is Literal:
        if raw not in args:
            raise InvalidValue(f"{raw!r} is not one of {list(args)}")
        return raw

    if origin is list:
        return [s.strip() for s in raw.split(",") if s.strip()]

    if annotation is bool:
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise InvalidValue(f"{raw!r} is not a boolean (use true/false)")

    if annotation is int:
        try:
            return int(raw)
        except ValueError:
            raise InvalidValue(f"{raw!r} is not an integer") from None

    if annotation is float:
        try:
            return float(raw)
        except ValueError:
            raise InvalidValue(f"{raw!r} is not a number") from None

    return raw


def resolve_annotation(key: str):
    """Walk ``SyncadeConfig``'s schema along the dotted ``key`` to the leaf field's declared
    annotation.

    Raises :class:`UnknownKey` for an unknown field, a non-index where a list index is expected,
    or a leaf that is a whole section/roster (a pydantic model, or a list of them) rather than a
    settable scalar.
    """
    current = SyncadeConfig
    for part in key.split("."):
        if get_origin(current) is list:  # walking into a list element
            elem = (get_args(current) or (str,))[0]
            if not _is_model(elem):  # a list-of-values (list[str]) is set WHOLE (csv), not indexed
                raise UnknownKey(f"{key!r}: {part!r} indexes a list set as a whole, not by element")
            if not part.isdigit():
                raise UnknownKey(f"{key!r}: {part!r} is not a list index")
            current = elem
            continue
        fields = getattr(current, "model_fields", None)
        if fields is None or part not in fields:
            raise UnknownKey(f"{key!r}: unknown field {part!r}")
        current = fields[part].annotation
    if not _settable_scalar(current):
        raise UnknownKey(f"{key!r} is a section or roster, not a settable field")
    return current


def _settable_scalar(annotation) -> bool:
    """A leaf is settable iff it is not a pydantic model and not a list of models (``list[str]``
    is fine; ``list[ReviewerConfig]`` is not — you set elements by index, not the whole roster)."""
    origin = get_origin(annotation)
    if origin is dict:  # a dict-shaped roster (e.g. pricing.models) is not a settable scalar
        return False
    if origin is list:
        elem = (get_args(annotation) or (str,))[0]
        return not _is_model(elem)
    if origin in (Union, UnionType):
        return all(_settable_scalar(a) for a in get_args(annotation) if a is not type(None))
    return not _is_model(annotation)


def _is_model(annotation) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _empty_clears(annotation) -> bool:
    """True for Optional (X | None) and list fields — empty input clears instead of no-op."""
    origin = get_origin(annotation)
    return origin is list or (origin in (Union, UnionType) and type(None) in get_args(annotation))
