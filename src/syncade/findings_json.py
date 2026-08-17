"""Verdict-block selection for cold-actor output parsing.

Every cold actor (reviewer, synthesizer, auditor, drafter) answers with prose
plus a JSON verdict. This module owns the single question *which bytes ARE the
verdict* — and answers it with exactly one candidate, never a search for
whichever block happens to validate.

The rule (2026-07-27 audit rank 1 / PR-h-01):

1. **Mask code samples.** The content of every fence with a non-empty,
   non-``json`` label (```` ```python ````, ```` ```js ````, ```` ```json5 ````)
   is blanked out. A labeled fence is an illustration by definition, so it is
   excluded from *all* candidate discovery — not merely from the fence scan.
2. **A ``json``-labeled fence is authoritative.** When one exists, the LAST one
   is the verdict and nothing else in the response is considered — not bare
   objects, not unlabeled fences. (Multiple are NOT an error: the anthropic
   adapter joins the text of every result turn, so a normal multi-turn claude
   response legitimately carries more than one.)
3. **Only when there is no ``json`` fence** do unlabeled fences and bare
   top-level objects compete, by latest document position.
4. **Otherwise the whole masked response.**
5. A **duplicate JSON key** in the selected block fails rather than resolving
   last-wins (see :func:`_reject_duplicate_keys`).
6. If the selected block does not decode, the parse **fails** (exit 70). There
   is deliberately no fallback to an earlier block that validates.

Every clause above is the scar of a reproduced false SHIP, and each is argued at
the code that enforces it rather than re-argued here. The shape of the history:
the original parser returned the first of many candidates that *validated*, so
an invalid intended verdict silently fell back to an earlier example. Fixing
that by POSITION alone then let three separate things written AFTER the verdict
replace it. Position cannot distinguish a verdict from an afterthought; a label
can, which is why rule 2 exists. None of these needed an adversarial model, and
several punished a reviewer for being helpful.

The bare-object scan is *kept* (rule 3) because it is load-bearing for a real
recorded case:
``tests/fixtures/pr-5.6-parser-regression/claude-reviewer-prose-with-jsx.stdout``
is genuine claude output with no fences at all — narrative containing
``style={{ color: 'var(--mm-amber)' }}`` followed by a bare verdict object.

Two accepted residuals, both requiring the actor to violate its template, and
both pinned as ``KNOWN_RESIDUAL`` tests rather than papered over:

- an illustration in a TRAILING ``json`` fence still replaces the verdict.
  Failing closed on multiple ``json`` fences was implemented and then reverted
  on evidence: a recorded 2026-05-30 run shows a normal claude response whose
  joined result turns carry two differing ``json`` fences, so refusing them
  would burn real rounds. Every reviewer template now warns against trailing
  illustrations explicitly.
- an actor that labels an EXAMPLE ``json`` and leaves its real verdict
  unlabeled or bare loses to the example.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from typing import TypeVar

from pydantic import ValidationError

_log = logging.getLogger(__name__)

# Triple-backtick fence: opening ```, an optional language label, optional
# trailing spaces, newline, the content (non-greedy), then a closing ``` THAT
# MUST BE PRECEDED BY A NEWLINE.
#
# The label class is deliberately `[^\s`]*` rather than `[a-zA-Z]*`. With the
# alphabetic-only class, `json5`, `my-language`, and `c++` matched NO fence at
# all — so their contents were invisible to the mask and leaked back in through
# the bare-object scan. That made the rule arbitrary: a verdict mislabeled
# ```python failed closed, while the same verdict mislabeled ```json5 was
# silently accepted, and a trailing ```json5 illustration could still override a
# real verdict. Every labeled fence is now a code sample; only an empty label or
# exactly `json` marks a verdict fence.
#
# The required newline before the closing ``` is load-bearing, not cosmetic.
# Without it, a reviewer whose own finding text mentions ``` closed the fence
# early — mid-string — and the truncated block failed to parse, losing a real
# verdict to exit 70. Requiring the newline makes that impossible rather than
# unlikely: JSON forbids a raw newline inside a string value (it must be the
# two-character `\n` escape), so every newline in the content is necessarily
# OUTSIDE a string, and therefore a ``` appearing inside a string value can
# never terminate the fence.
_FENCE_RE = re.compile(r"```([^\s`]*)[ \t]*\r?\n(.*?)\r?\n```", re.DOTALL)
# Matches only the opening line of a fence (label + newline), without requiring
# a closing fence. Used to detect unclosed openers that _FENCE_RE never matches.
_FENCE_OPEN_RE = re.compile(r"```([^\s`]*)[ \t]*\r?\n")
_NON_NEWLINE_RE = re.compile(r"[^\n]")
_JSON_DECODER = json.JSONDecoder()

_SNIPPET_LEN = 200


class VerdictBlockError(Exception):
    """No decodable verdict block in a cold actor's response.

    Carries a human-readable reason (which block was selected, why it failed).
    Each parser catches this and re-raises its own phase-named error type so
    exit 70 can say *which* actor to debug, while the reason text stays
    identical across all four.
    """


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """``object_pairs_hook`` that refuses a repeated key instead of last-wins.

    PR-h-01 increment B. ``json.loads`` silently resolves
    ``{"verdict": "NO-SHIP", "verdict": "SHIP"}`` to the LAST value, so a
    duplicated key was a one-token path from a blocker verdict to a false
    SHIP — and it survives every downstream schema check, because by the time
    pydantic sees the dict there is only one ``verdict``.

    A model that emits the same key twice has already lost the property we
    need, so this fails the parse rather than picking a winner.
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise VerdictBlockError(
                f"duplicate JSON key {key!r} in the verdict block. json.loads "
                f"would silently keep the last value, so a repeated key could "
                f"flip a field (a duplicated 'verdict' turns NO-SHIP into SHIP). "
                f"Emit each key exactly once."
            )
        seen.add(key)
    return dict(pairs)


def _mask_fence_interiors(raw: str, *, labeled_only: bool) -> str:
    """Blank the CONTENT of fences, preserving length and line structure so
    positions and snippets stay meaningful.

    ``labeled_only=True`` masks only non-``json``-labeled fences
    (```` ```python ````, ```` ```js ````, ```` ```json5 ````): those are
    illustrations, so nothing inside them may become a verdict candidate.
    Masking rather than skipping at fence level is what stops the bare-object
    scan from reaching back into a code sample — the exact hole that let a
    ```` ```python ````-wrapped example be parsed as a verdict.

    ``labeled_only=False`` masks every fence, and is used to scope the
    bare-object scan to text OUTSIDE any fence, so a fence's own contents are
    counted once (as a fence candidate) rather than twice.
    """
    parts: list[str] = []
    last = 0
    # Record both the opener start AND the closer start of every matched fence.
    # A closer is the ``` that ends the match; it starts at match.end()-3.
    # The unclosed-opener pass below uses this set to skip those lines so that
    # a closer ``` (which looks like an unlabeled opener) is never mistaken for
    # a new unclosed opener.
    matched_positions: set[int] = set()
    for match in _FENCE_RE.finditer(raw):
        matched_positions.add(match.start())  # opener
        matched_positions.add(match.end() - 3)  # closer (last 3 chars are ```)
        label = match.group(1).lower()
        if labeled_only and (not label or label == "json"):
            continue
        parts.append(raw[last : match.start(2)])
        parts.append(_NON_NEWLINE_RE.sub(" ", match.group(2)))
        last = match.end(2)
    parts.append(raw[last:])
    result = "".join(parts)

    # Mask unclosed fence openers: _FENCE_RE requires a closing ``` line so it
    # never matches a truncated/unclosed fence. Without this pass the interior
    # of an unclosed ```python opener leaks into the bare-object scan.
    # Scan `result` (not `raw`): content inside matched fences is already masked
    # to spaces there, so it cannot produce false inner-fence opener matches.
    # `matched_positions` covers opener AND closer lines of every matched fence.
    for m in _FENCE_OPEN_RE.finditer(result):
        if m.start() in matched_positions:
            continue
        label = m.group(1).lower()
        if labeled_only and (not label or label == "json"):
            continue
        cs = m.end()
        if cs < len(result):
            result = result[:cs] + _NON_NEWLINE_RE.sub(" ", result[cs:])
    return result


def _find_fenced_json_candidates(raw: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return ``(json_labeled, unlabeled)`` fences as ``(start_pos, content)``
    lists, each in document order. Labeled-but-not-``json`` fences are code
    samples and appear in neither.
    """
    labeled: list[tuple[int, str]] = []
    unlabeled: list[tuple[int, str]] = []
    for match in _FENCE_RE.finditer(raw):
        label = match.group(1).lower()
        if label and label != "json":
            continue
        content = match.group(2).rstrip("\r\n")
        (labeled if label == "json" else unlabeled).append((match.start(2), content))
    return labeled, unlabeled


def _find_last_json_object(raw: str) -> tuple[int, str] | None:
    """Return ``(start_pos, source_text)`` of the LAST top-level JSON object in
    ``raw``, or ``None``.

    Find-then-parse-or-skip scan: when a ``{`` does not start valid JSON the
    scanner advances one character and keeps looking, so an unmatched brace in
    prose (``if (x) { do something``) cannot swallow the rest of the document.
    Only the last object is returned — the caller gets one candidate, never a
    chain to fall back through.
    """
    found: tuple[int, str] | None = None
    pos, n = 0, len(raw)
    while pos < n:
        start = raw.find("{", pos)
        if start == -1:
            break
        try:
            parsed, end = _JSON_DECODER.raw_decode(raw, start)
        except (json.JSONDecodeError, RecursionError):
            # RecursionError: deeply-nested junk in prose is not a verdict; skip
            # it like any other non-JSON rather than aborting the scan.
            pos = start + 1
            continue
        if isinstance(parsed, dict):
            found = (start, raw[start:end])
        pos = max(end, start + 1)
    return found


def _select_verdict_block(raw: str) -> tuple[str | None, str]:
    """Return ``(block, description)`` for the one block that IS the verdict.

    A ```` ```json ````-labeled fence is AUTHORITATIVE: every template tells the
    actor to put its verdict in exactly one, so when one exists nothing else in
    the response is a candidate. Multiple are NOT an error — the last one wins
    (see the comment at the return site). Only when there is no ``json`` fence at
    all do unlabeled fences and bare objects compete, by latest document position.

    That precedence — rather than position across all kinds — is what closes
    three separately-reproduced false SHIPs, each of which let something the
    reviewer wrote AFTER its verdict replace the verdict: a trailing bare object,
    a trailing unlabeled fence, and an ordinary JSON snippet in closing prose
    (``Fix: add {"strict": true} to tsconfig.json``), which additionally burned a
    whole round at exit 70. Position alone cannot tell a verdict from a
    afterthought; a label can.

    The cost is a residual in the mirror direction: an actor that labels an
    EXAMPLE ``json`` and leaves its real verdict unlabeled or bare loses to the
    example. That inverts every template instruction, and unlike the cases above
    it cannot happen to an actor that simply follows the contract.
    """
    masked = _mask_fence_interiors(raw, labeled_only=True)
    labeled, unlabeled = _find_fenced_json_candidates(masked)

    # Detect unclosed fence openers (```json or unlabeled ```) that appear after
    # the last matched fence of the same kind. An unclosed opener is the intended
    # final verdict block; since it has no closing ```, it cannot be decoded, and
    # the parse must fail rather than fall back to an earlier block.
    # Scan `masked` rather than `raw`: content inside non-json labeled fences is
    # already blanked there, so a ```json comment inside a ```python block cannot
    # produce a false unclosed-opener trigger.
    _matched_positions: set[int] = set()
    _last_json_end = -1
    _last_unlabeled_end = -1
    for _m in _FENCE_RE.finditer(raw):
        _matched_positions.add(_m.start())
        _matched_positions.add(_m.end() - 3)
        _lbl = _m.group(1).lower()
        if _lbl == "json":
            _last_json_end = _m.end()
        elif not _lbl:
            _last_unlabeled_end = _m.end()
    for _m in _FENCE_OPEN_RE.finditer(masked):
        if _m.start() in _matched_positions:
            continue
        _lbl = _m.group(1).lower()
        if _lbl == "json" and _m.start() > _last_json_end:
            return None, "the last ```json fence (unclosed — no closing ``` found)"
        # Only fail closed on an unclosed unlabeled opener when there are no
        # closed json fences — if a json fence exists, it wins regardless.
        if not _lbl and not labeled and _m.start() > _last_unlabeled_end:
            return None, "the last unlabeled fence (unclosed — no closing ``` found)"

    if labeled:
        # LAST ```json fence wins, and multiple are NOT treated as ambiguous.
        # Failing closed on two of them was tried and reverted on evidence: the
        # anthropic adapter deliberately joins the text of EVERY result turn
        # (claude emits spurious epilogue turns after delivering a verdict, and
        # taking only the terminal turn lost real verdicts — see
        # `adapters/anthropic.py`), so a normal multi-turn claude response
        # legitimately contains two DIFFERING ```json fences whose last one is
        # the real verdict. A recorded 2026-05-30 run has exactly that shape;
        # refusing it would burn real rounds to close a residual the templates
        # already forbid.
        return labeled[-1][1].strip() or None, "the last ```json fence"

    # Unlabeled fences beat bare objects. A bare object appearing AFTER the
    # verdict fence must not override it — position alone cannot distinguish
    # a verdict from an afterthought in closing prose. An unlabeled fence is
    # a deliberate structural choice; bare objects in prose are not.
    if unlabeled:
        return unlabeled[-1][1].strip() or None, "the last unlabeled fence"

    # Bare objects are scanned OUTSIDE every fence so a fence's contents compete
    # once, as a fence, rather than twice.
    bare = _find_last_json_object(_mask_fence_interiors(raw, labeled_only=False))
    if bare is not None:
        return bare[1].strip() or None, "the last bare JSON object"

    return masked.strip() or None, "the whole response (no ```json fence or JSON object found)"


def _decode_verdict_object(raw: str) -> object:
    """Select the verdict block and JSON-decode it.

    Raises :class:`VerdictBlockError` with an operator-readable reason when
    there is no block, the block is not valid JSON, or it decodes to something
    other than a JSON object.
    """
    block, which = _select_verdict_block(raw)
    if block is None:
        raise VerdictBlockError(
            f"{which} is empty (first {_SNIPPET_LEN} chars of the response: {raw[:_SNIPPET_LEN]!r})"
        )
    try:
        parsed = json.loads(block, object_pairs_hook=_reject_duplicate_keys)
    except RecursionError as exc:
        # json's C/py decoders recurse per nesting level. Left uncaught this
        # escapes as a bare RecursionError, which the dispatcher's broad handler
        # turns into exit 40 ("subprocess failed") — the wrong story for output
        # that arrived fine and simply cannot be parsed.
        raise VerdictBlockError(
            f"{which} nests too deeply to decode ({exc}); treated as unparseable output"
        ) from exc
    except json.JSONDecodeError as exc:
        raise VerdictBlockError(
            f"{which} is not valid JSON ({exc}); no earlier block is considered. "
            f"Block: {block[:_SNIPPET_LEN]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise VerdictBlockError(
            f"{which} decoded to {type(parsed).__name__}, not a JSON object. "
            f"Block: {block[:_SNIPPET_LEN]!r}"
        )
    return parsed


_T = TypeVar("_T")


def decode_and_validate(
    raw: str,
    *,
    validate: Callable[[object], _T],
    error: type[Exception],
    label: str,
    model_name: str,
    artifact: str,
) -> _T:
    """Select the verdict block, decode it, and validate it — for all four
    cold-actor parsers.

    Every parser needs the same two-step failure story, differing only in which
    exception type names the phase and where the raw response was persisted.
    Copying it produced four independently-editable copies of
    ``"no earlier block is considered"``, a string five tests assert on, which
    is exactly the drift the shared selector exists to prevent.

    ``validate`` raises :class:`pydantic.ValidationError` on rejection; the
    synthesizer passes a wrapper that tries its known-deviation repair first.

    Raises ``error`` — the caller's phase-named type — so exit 70 still says
    which actor to debug.
    """
    try:
        parsed = _decode_verdict_object(raw)
    except VerdictBlockError as exc:
        raise error(
            f"{label} output had no parseable {model_name} JSON: {exc}; "
            f"the raw response is preserved at {artifact}."
        ) from exc
    try:
        return validate(parsed)
    except ValidationError as exc:
        raise error(
            f"the {label}'s verdict block is not a valid {model_name} "
            f"({exc.error_count()} schema error(s)): {exc}; no earlier block is "
            f"considered. The raw response is preserved at {artifact}."
        ) from exc


def _drop_key_at(payload: object, loc: tuple[object, ...]) -> bool:
    """Delete the key/index named by a pydantic error ``loc``. False if it is not there."""
    for step in loc[:-1]:
        if isinstance(payload, dict) and step in payload:
            payload = payload[step]
        elif isinstance(payload, list) and isinstance(step, int) and step < len(payload):
            payload = payload[step]
        else:
            return False
    last = loc[-1]
    if isinstance(payload, dict) and last in payload:
        del payload[last]
        return True
    return False


def validate_dropping_forbidden_extras(
    payload: object, validate: Callable[[object], _T], *, label: str
) -> _T:
    """Validate strictly; if the ONLY defect is forbidden extra keys, drop them and retry.

    A model that returns a complete, correct verdict and adds one advisory key should not cost
    the run. Measured (PR-h-field-05): a single ``recommended_fix`` on one finding rejected a
    review worth 1,325,087 tokens, and took the other reviewer's 936,113 with it because the
    judge is skipped when any reviewer fails.

    **Eligibility is the SHAPE OF THE FAILURE, never a list of key names** — the rule
    :mod:`syncade.synthesis_repair` states for its own repairs, because a name list rots and an
    enumeration of what a model might invent is unbounded. Every error must be
    ``extra_forbidden``; one error of any other type and the whole payload still raises.

    That single condition is what keeps this narrow, and it excludes the dangerous case BY
    CONSTRUCTION rather than by remembering to: a model that RENAMES a required field produces
    a ``missing`` error beside the extra one, so mixed types never repair. Wrong types,
    out-of-range values and blank required strings are untouched.

    Dropping is a real loss and is therefore WARNED, naming every key. Unlike the synthesizer's
    repairs — a duplicated coordinate, a cluster that groups nothing — an extra key may carry
    content. The verdict cannot depend on it (severity, evidence and description are all
    required fields that survive), so the trade is right; it is still a trade, and a silent one
    would be a schema quietly bent.
    """
    try:
        return validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        if not errors or any(e.get("type") != "extra_forbidden" for e in errors):
            raise
        repaired = copy.deepcopy(payload)
        dropped = [
            ".".join(str(p) for p in e["loc"])
            for e in errors
            if _drop_key_at(repaired, tuple(e["loc"]))
        ]
        if not dropped:
            raise
        _log.warning(
            "%s: dropped %d key(s) the schema forbids, so the verdict could be used: %s",
            label,
            len(dropped),
            ", ".join(dropped),
        )
        return validate(repaired)
