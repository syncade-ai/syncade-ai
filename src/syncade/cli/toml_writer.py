"""Minimal TOML *writer* for ``.syncade/config.toml`` (pr-v2-30).

stdlib ``tomllib`` reads TOML but cannot write it, and syncade adds no runtime dependency. This
writes the subset syncade produces — nested tables, arrays-of-tables, inline scalar arrays, and
str/int/float/bool scalars — and is round-trip-correct (``tomllib.loads(dumps(d)) == d``), proven
in tests. ``None`` is omitted (TOML has no null); an unserializable value raises ``ValueError``.
"""

from __future__ import annotations

import re
import tomllib

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def dumps(data: dict) -> str:
    lines: list[str] = []
    _emit(data, (), lines)
    text = "\n".join(lines).strip("\n")
    return text + "\n" if text else ""


def _emit(table: dict, path: tuple[str, ...], lines: list[str]) -> None:
    # A table's own scalars MUST precede its sub-tables in TOML, so emit them in two passes.
    for key, value in table.items():
        if value is None:
            continue
        if isinstance(value, list) and not (value and isinstance(value[0], dict)):
            # a scalar array is an INLINE value of this table — emitting it in the second pass
            # would place it after a sub-table header, silently reparenting it into that sub-table
            lines.append(f"{_fmt_key(key)} = [{', '.join(_fmt_scalar(v) for v in value)}]")
        elif not isinstance(value, (dict, list)):
            lines.append(f"{_fmt_key(key)} = {_fmt_scalar(value)}")
    for key, value in table.items():
        if isinstance(value, dict):
            lines.append("")
            lines.append(f"[{_fmt_path(path + (key,))}]")
            _emit(value, path + (key,), lines)
        elif isinstance(value, list):
            if value and isinstance(value[0], dict):
                # array-of-tables ([[key]]) — each element must be a dict
                for item in value:
                    if not isinstance(item, dict):
                        raise ValueError(f"cannot serialize a mixed list at {key!r}")
                    lines.append("")
                    lines.append(f"[[{_fmt_path(path + (key,))}]]")
                    _emit(item, path + (key,), lines)


def _fmt_path(path: tuple[str, ...]) -> str:
    return ".".join(_fmt_key(p) for p in path)


def _fmt_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _fmt_scalar(key)


def _fmt_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\b", "\\b")
            .replace("\t", "\\t")
            .replace("\n", "\\n")
            .replace("\f", "\\f")
            .replace("\r", "\\r")
        )
        # TOML forbids U+0000-U+0008, U+000B, U+000E-U+001F, U+007F raw in basic strings
        escaped = re.sub(
            r"[\x00-\x07\x0b\x0e-\x1f\x7f]", lambda m: f"\\u{ord(m.group()):04X}", escaped
        )
        return f'"{escaped}"'
    raise ValueError(f"cannot serialize value of type {type(value).__name__}: {value!r}")


def _toml_equal(a, b) -> bool:
    """Type-strict TOML value equality: bool and int are distinct even though ``True == 1``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_toml_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_toml_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def render(data: dict, old_text: str) -> str:
    """The text to write for ``data``, preserving ``old_text``'s comments/formatting where possible.

    A config file is meant to be hand-edited, so regenerating it from parsed data (:func:`dumps`)
    silently destroys the operator's comments, key order, and layout. This instead patches only the
    lines whose values changed — and keeps that patch ONLY if it re-parses to ``data`` after
    normalizing away ``None`` leaves and empty tables (which TOML cannot represent).
    Anything the patcher cannot place safely falls back to :func:`dumps`, so the worst case is the
    old behaviour: a write can lose comments, but can never write a value that wasn't intended.
    """
    if not old_text.strip():
        return dumps(data)
    # `dumps` omits None (TOML has no null), so the FILE's data is `data` minus its None leaves.
    # Patch and verify against that: otherwise any None — every materialized optional field, and the
    # documented "clear an optional" gesture — makes the check unsatisfiable and silently forces the
    # comment-destroying rewrite. (A key whose new value is None correctly becomes a removal.)
    wanted = _strip_none(data)
    try:
        patched = _patch(old_text, wanted)
        # Strip empty tables from both sides: a header-only [section] in old_text parses to {}
        # but _strip_none(data) already dropped it, so a naive compare would always mismatch and
        # fall back to the comment-destroying dumps(). Stripping both sides aligns them on content.
        if patched is not None and _toml_equal(_strip_none(tomllib.loads(patched)), wanted):
            return patched
    except (tomllib.TOMLDecodeError, ValueError, KeyError, IndexError, TypeError):
        pass  # any patcher failure degrades to the full rewrite, never to a wrong file
    return dumps(data)


def _patch(old_text: str, new_data: dict) -> str | None:
    """``old_text`` with only the CHANGED/ADDED/REMOVED leaves applied, or None when a delta cannot
    be placed safely (the caller then falls back to a full rewrite)."""
    old_data = tomllib.loads(old_text)
    if _toml_equal(old_data, new_data):
        return old_text  # a no-op write must not reformat the file
    old_flat, new_flat = _flatten(old_data), _flatten(new_data)
    changed = [p for p in new_flat if p in old_flat and not _toml_equal(old_flat[p], new_flat[p])]
    added = [p for p in new_flat if p not in old_flat]
    removed = [p for p in old_flat if p not in new_flat]
    # Work on ENDING-LESS text lines with a PARALLEL per-line ending list, so each existing line
    # keeps its own ending (mixed files stay mixed) while a file with no trailing newline can never
    # leave a bare CR/LF at a boundary. Inserted lines take the dominant ending.
    lines, endings = _split_lines(old_text)
    had_final_nl = bool(lines) and endings[-1] != ""
    dominant = "\r\n" if "\r\n" in old_text else "\n"
    key_at, table_last, table_header = _scan(lines)

    for path in changed:  # replace the value in place, keeping key text + trailing comment
        line_no = key_at.get(path)
        if line_no is None:
            return None
        replaced = _replace_value(lines[line_no], new_flat[path])
        if replaced is None:
            return None
        lines[line_no] = replaced

    drop = set()
    for path in removed:
        line_no = key_at.get(path)
        if line_no is None:
            return None
        drop.add(line_no)

    after: dict[int, list[str]] = {}  # insert AFTER this line
    before: dict[int, list[str]] = {}  # insert BEFORE this line (root keys precede any table)
    tail: list[str] = []  # appended blocks (a leading "" is a blank separator line)
    by_table: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for path in added:
        by_table.setdefault(path[:-1], []).append(path)
    aot_new: dict[tuple[str, ...], list[tuple[int, list[str]]]] = {}
    for table, paths in by_table.items():
        rendered = [f"{_fmt_key(p[-1])} = {_render_value(new_flat[p])}" for p in paths]
        anchor = table_last.get(table, table_header.get(table))
        if anchor is not None:  # the table exists — append to its key block
            after.setdefault(anchor, []).extend(rendered)
        elif table == ():  # a root key must precede every table header
            first_header = next(
                (i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), None
            )
            if first_header is None:
                tail.extend(rendered)
            else:
                before.setdefault(first_header, []).extend(rendered)
        elif any(part.isdigit() for part in table):
            # New AOT element: accumulate by base array name for ordered tail emission.
            ridx = max(i for i, p in enumerate(table) if p.isdigit())
            aot_new.setdefault(table[:ridx], []).append((int(table[ridx]), rendered))
        else:  # a brand-new table — append a block, leaving existing text untouched
            tail.extend(["", f"[{_fmt_path(table)}]", *rendered])
    for base, elements in aot_new.items():
        for _, elem_rendered in sorted(elements):
            tail.extend(["", f"[[{_fmt_path(base)}]]", *elem_rendered])

    out: list[tuple[str, str]] = []  # (text, ending); inserted lines take the dominant ending
    for i, line in enumerate(lines):
        out.extend((nl, dominant) for nl in before.get(i, []))
        if i not in drop:
            out.append((line, endings[i]))
        out.extend((nl, dominant) for nl in after.get(i, []))
    if tail and out and out[-1][0] == "" and tail[0] == "":
        tail = tail[1:]  # file ends in a blank line; a new block starts with one — don't double it
    out.extend((nl, dominant) for nl in tail)
    if not out:
        return ""
    # Every line but the last carries a real ending (a mid-file "" — the original trailing-less line
    # now followed by appends — takes the dominant ending). The final line keeps its OWN ending, but
    # the file's trailing-newline PROPERTY is byte-preserved: dropped entirely if the original had
    # one, so an appended (dominant-ended) final line loses its newline to match.
    body = "".join(text + (ending or dominant) for text, ending in out[:-1])
    last_text, last_ending = out[-1]
    return body + last_text + (last_ending if had_final_nl else "")


def _strip_none(data: dict) -> dict:
    """``data`` without its ``None`` leaves or empty sub-tables — what the written file contains."""
    out: dict = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            stripped = _strip_none(value)
            if stripped:  # drop tables that become empty after stripping None leaves
                out[key] = stripped
        elif isinstance(value, list):
            out[key] = [_strip_none(i) if isinstance(i, dict) else i for i in value]
        else:
            out[key] = value
    return out


def _split_lines(text: str) -> tuple[list[str], list[str]]:
    """Ending-less logical lines + a PARALLEL list of each line's ending (CRLF, LF, or
    ``""`` for a final line with no newline). Only ``\\n`` / ``\\r\\n`` are breaks (unlike
    :meth:`str.splitlines`, which also breaks on other C0 controls a TOML value can hold)."""
    lines: list[str] = []
    endings: list[str] = []
    start = i = 0
    n = len(text)
    while i < n:
        if text[i] == "\r" and i + 1 < n and text[i + 1] == "\n":
            lines.append(text[start:i])
            endings.append("\r\n")
            i += 2
            start = i
        elif text[i] == "\n":
            lines.append(text[start:i])
            endings.append("\n")
            i += 1
            start = i
        else:
            i += 1
    if start < n or not lines:  # a trailing chunk with no newline is the final (ending-less) line
        lines.append(text[start:])
        endings.append("")
    return lines, endings


def _flatten(data: dict, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    """Every scalar leaf as ``dotted-path -> value``. A list of tables recurses (indexed); a scalar
    array (including an empty one) is itself a leaf."""
    out: dict[tuple[str, ...], object] = {}
    for key, value in data.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        elif isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
            for i, item in enumerate(value):
                out.update(_flatten(item, path + (str(i),)))
        else:
            out[path] = value
    return out


def _scan(lines: list[str]) -> tuple[dict[tuple[str, ...], int], dict[tuple[str, ...], int]]:
    """Map each ``key = value`` line to its full dotted path, tracking `[table]` / `[[array]]`
    headers (arrays get a per-path element index, matching :func:`_flatten`)."""
    key_at: dict[tuple[str, ...], int] = {}
    table_last: dict[tuple[str, ...], int] = {}
    table_header: dict[tuple[str, ...], int] = {}
    table: tuple[str, ...] = ()
    seen_aot: dict[tuple[str, ...], int] = {}
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            end = line.find("]]")
            if end < 0:
                continue
            base = _key_path(line[2:end].strip())
            index = seen_aot.get(base, 0)
            seen_aot[base] = index + 1
            table = base + (str(index),)
            table_header.setdefault(table, i)
            continue
        if line.startswith("["):
            end = line.find("]")
            if end < 0:
                continue
            table = _key_path(line[1:end].strip())
            table_header.setdefault(table, i)
            continue
        eq = _eq_index(raw)
        if eq is None:
            continue
        key_at[table + _key_path(raw[:eq].strip())] = i
        table_last[table] = i
    return key_at, table_last, table_header


def _key_path(text: str) -> tuple[str, ...]:
    """Split a (possibly dotted, possibly quoted) key or table name into its path parts."""
    parts: list[str] = []
    current = ""
    quote = ""
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ".":
            parts.append(_unquote(current.strip()))
            current = ""
        else:
            current += ch
    parts.append(_unquote(current.strip()))
    return tuple(parts)


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _eq_index(line: str) -> int | None:
    """Index of the ``=`` separating key from value, ignoring any inside a quoted key."""
    quote = ""
    escaped = False
    for i, ch in enumerate(line):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':  # only a BASIC string honours backslash escapes
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "=":
            return i
        elif ch == "#":
            return None
    return None


def _replace_value(line: str, value) -> str | None:
    """``line`` (no line ending) with its value replaced, keeping the key text, the operator's exact
    spacing around ``=``, and the trailing comment at its original column."""
    eq = _eq_index(line)
    if eq is None:
        return None
    rest = line[eq + 1 :]
    rendered = _render_value(value)
    if rendered is None:
        return None
    comment = _trailing_comment(rest)
    # Preserve the EXACT whitespace the operator put between `=` and the value (byte-perfect: never
    # collapse `=   3` to `= 3`, never pad `=3` to `= 3`).
    lead_ws = rest[: len(rest) - len(rest.lstrip(" \t"))]
    prefix = f"{line[: eq + 1]}{lead_ws}{rendered}"
    if not comment:
        trail_ws = rest[len(rest.rstrip(" \t")) :]  # keep any whitespace that trailed the value
        return f"{prefix}{trail_ws}"
    # Preserve the COLUMN of # by padding to it, not by copying the old whitespace run (which was
    # sized for the old value; a different-width replacement would otherwise shift the #).
    comment_text = comment.lstrip()
    hash_col = len(line) - len(comment_text)
    pad = max(1, hash_col - len(prefix))
    return f"{prefix}{' ' * pad}{comment_text}"


def _render_value(value) -> str | None:
    """A scalar or inline scalar-array as TOML text; None for anything not representable inline."""
    if isinstance(value, list):
        if any(isinstance(v, (dict, list)) for v in value):
            return None
        return f"[{', '.join(_fmt_scalar(v) for v in value)}]"
    return _fmt_scalar(value)


def _trailing_comment(rest: str) -> str:
    """A value's trailing ``  # …``, including the whitespace that preceded the ``#``. "" when
    the value carries no comment. Quotes are respected so a ``#`` inside a string is not mistaken
    for a comment. The caller strips the whitespace and recomputes padding from the ``#`` column."""
    quote = ""
    escaped = False
    for i, ch in enumerate(rest):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':  # only a BASIC string honours backslash escapes
                escaped = True
            elif ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            start = i
            while start > 0 and rest[start - 1] in " \t":
                start -= 1
            return rest[start:] if start < i else "  " + rest[i:]
    return ""
