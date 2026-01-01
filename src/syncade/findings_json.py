"""JSON-candidate extraction for reviewer/synth output parsing."""

from __future__ import annotations

import json
import re

# Triple-backtick fence: opening ```, an optional alphabetic language
# label, optional CR before LF, the content (non-greedy), then a
# closing ```. Only fences with no label or a `json` label are treated
# as verdict candidates — `python`, `js`, `json5`, hyphenated labels,
# etc. don't match this regex and fall through to the raw decoder scan. CRLF
# tolerated via `\r?`.
_FENCE_RE = re.compile(r"```([a-zA-Z]*)\r?\n(.*?)```", re.DOTALL)
_JSON_DECODER = json.JSONDecoder()


def _find_fenced_json_candidates(raw: str) -> list[tuple[int, str]]:
    """Return ``(start_pos, content)`` for every json-or-unlabeled fence
    in ``raw``, in document order.

    Fences with a non-empty, non-``json`` language label are skipped because
    they are code samples, not verdicts. Their contents may still be picked up
    by the raw decoder scan if they contain valid JSON.

    ``start_pos`` is the start of the captured inner content (just
    after the opening fence's newline) so the caller can sort
    candidates by document position together with brace blocks.
    Trailing newlines and CR characters are stripped so an attempted
    ``json.loads`` doesn't have to tolerate them.
    """
    candidates: list[tuple[int, str]] = []
    for match in _FENCE_RE.finditer(raw):
        label = match.group(1).lower()
        if label and label != "json":
            continue
        content = match.group(2).rstrip("\r\n")
        candidates.append((match.start(2), content))
    return candidates


def _find_json_object_candidates(raw: str) -> list[tuple[int, str]]:
    """Return ``(start_pos, content)`` for each raw-decoded JSON object."""
    candidates: list[tuple[int, str]] = []
    pos = 0
    n = len(raw)
    while pos < n:
        start = raw.find("{", pos)
        if start == -1:
            break
        try:
            parsed, end = _JSON_DECODER.raw_decode(raw, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(parsed, dict):
            candidates.append((start, raw[start:end]))
        pos = max(end, start + 1)
    return candidates


def _extract_json_candidates(raw: str) -> list[tuple[int, str]]:
    """Return every JSON-candidate block in ``raw``, sorted by start
    position **descending** (latest in document first).

    Combines fenced blocks with JSON objects found by
    :meth:`json.JSONDecoder.raw_decode`. The position-sorted-descending ordering
    keeps the "real verdict at the end wins" property: the parser tries the
    latest candidate first regardless of whether it's a fence or a bare object.

    When a fence and a brace block start at the same position (the
    fence content IS a brace block), the order between them is
    irrelevant — both validate to the same parsed structure or both
    fail. Stable sort means insertion order breaks ties; harmless.
    """
    fenced = _find_fenced_json_candidates(raw)
    objects = _find_json_object_candidates(raw)
    candidates = fenced + objects
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates
