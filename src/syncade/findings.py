"""Pydantic v2 models for reviewer-emitted findings.

Schema mirrors PRD Appendix B exactly: a top-level ``ReviewerOutput`` with
a ``SHIP``/``NO-SHIP`` ``verdict`` and a list of ``Finding`` records
grouped by ``severity``. Any field not listed here is rejected
(``extra="forbid"``) so reviewer drift surfaces as a parse error instead
of being silently absorbed.

``parse_reviewer_output`` is the main entry point. It accepts bare JSON,
markdown-fenced JSON, or JSON embedded in prose. The candidate-extraction
strategy lives in the shared helper :func:`_extract_json_candidates`,
which is also used by :func:`syncade.synthesis.parse_synthesizer_output` so the
parser hardening is not duplicated across parsers. The strategy:

1. **Collect all candidates with their document positions.** Every
   ``json``-labeled or unlabeled ` ``` ` fence and every top-level JSON
   object decoded by :meth:`json.JSONDecoder.raw_decode` becomes a
   ``(start_pos, content)`` candidate.
2. **Try in REVERSE document position order.** The latest candidate
   wins — preserving the "real verdict at the end" property whether
   it's a fence or a bare JSON block. A fenced *example* earlier in
   the response cannot mask a real bare-JSON verdict at the end.
3. **Each candidate must pass BOTH** ``json.loads`` AND
   :meth:`ReviewerOutput.model_validate`. Fragments that look JSON-shaped but
   aren't real verdicts (JSX object-literal syntax, schema illustrations,
   pseudocode) fail one or both checks and the parser keeps scanning.
4. **Whole-string fallback** as a final attempt for the no-narrative
   case; kept defensively even though raw decoding normally finds this shape.

The raw decoder scan is a find-then-parse-or-skip loop. When a ``{`` does not
start valid JSON, the scanner advances past that ``{`` and keeps looking — so
an unmatched ``{`` in prose ("``if (x) { do something``") doesn't swallow the
rest of the document and starve the parser of later candidates.

Fence regex tolerates CRLF line endings (``\\r?\\n``); fences with
non-empty, non-``json`` labels (``json5``, ``python``,
``my-language``, etc.) don't match the regex and can still be found by the raw
decoder when their contents are valid JSON objects.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# the pure JSON-candidate scanners live in findings_json; _extract_json_candidates
# is re-exported here so syncade.findings._extract_json_candidates is unchanged
# (shared by the reviewer/synth/spec-audit/spec-draft parsers).
from syncade.findings_json import _extract_json_candidates

Severity = Literal["blocker", "minor", "nit"]
"""Per-finding severity classification, per PRD Appendix B."""

Verdict = Literal["SHIP", "NO-SHIP"]
"""Top-level reviewer verdict, per PRD Appendix B."""


class ReviewerOutputError(Exception):
    """Raised when reviewer stdout can't be parsed as a :class:`ReviewerOutput`.

    The message includes the count of candidate blocks attempted, a
    truncated snippet of the first attempted block, and a hint pointing
    at the reviewer's ``.stdout`` artifact so the CLI can surface a
    useful error via exit code 70 (``REVIEWER_OUTPUT_UNPARSEABLE``)
    without further introspection.
    """


class Finding(BaseModel):
    """A single reviewer-emitted finding, per PRD Appendix B.

    ``file`` is optional: ``None`` means the finding is repo-wide
    rather than tied to a specific file (e.g., a commit-message
    issue, a hygiene problem about which files are committed,
    repository-level configuration). Rejecting repo-wide findings on schema
    would discard legitimate blocker-level concerns.

    ``line`` is also optional: ``None`` means the finding is
    file-level rather than line-specific.

    ``evidence_cmd`` and ``evidence_output`` are optional —
    reviewers should populate them when they ran a concrete repro
    command, but bare assertions (e.g., a doc-level concern) don't
    need them.
    """

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    file: str | None = None
    line: int | None = None
    spec_clause: str
    finding: str
    evidence_cmd: str | None = None
    evidence_output: str | None = None


class ReviewerOutput(BaseModel):
    """A complete reviewer-run output: a verdict, a list of findings, and
    narrative-surface fields that capture the reviewer's summary,
    prioritization, coverage gaps, and dismissed concerns.

    Reviewers MAY return ``verdict="SHIP"`` with a non-empty findings list
    (e.g., minor / nit findings that don't block ship). The orchestrator
    decides what to do; the schema doesn't enforce verdict↔findings
    consistency.

    ``summary``, ``priority_order``, ``coverage_gaps``, and
    ``dismissed_concerns`` are required so the reviewer must consciously assert
    each answer rather than silently omit it. ``coverage_gaps`` and
    ``dismissed_concerns`` may be ``[]`` (explicit "nothing to flag").
    ``priority_order`` is constrained to be a complete permutation of
    ``range(len(findings))`` by :meth:`_validate_priority_order`.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)

    summary: str = Field(
        ...,
        min_length=1,
        description=(
            "Reviewer's headline narrative: what was verified, what stood out, "
            "why this verdict. Must contain non-whitespace content even when "
            "verdict=SHIP and findings is empty — a SHIP without verification "
            "narrative is not useful to the operator or to the synthesizer. "
            "This structured field is the verification summary. ``min_length=1`` "
            "catches pure-empty strings; :meth:`_validate_summary_nonblank` "
            "catches whitespace-only strings (``'   '``, ``'\\n\\t '``, etc.)."
        ),
    )

    priority_order: list[int] = Field(
        ...,
        description=(
            "Indices into ``findings`` in priority order — most urgent first. "
            "Must be a complete permutation of range(len(findings)): every "
            "finding gets exactly one priority position. The orchestrator and "
            "synthesizer use this to rank within-severity-tier; a list of 3 "
            "blockers without explicit ordering is much less actionable than "
            "the same 3 with the reviewer's judgment about which to fix first. "
            "Empty list iff ``findings`` is empty."
        ),
    )

    coverage_gaps: list[str] = Field(
        ...,
        description=(
            "What the reviewer did NOT verify, and why. Surfaces honest "
            "operational limits ('I couldn't reach the staging DB', 'I "
            "didn't test mobile viewports', 'I trusted the producer's "
            "claim about backend tests'). Empty list means 'I verified "
            "everything the spec asked for' — forces the reviewer to "
            "consciously assert that rather than silently omit."
        ),
    )

    dismissed_concerns: list[str] = Field(
        ...,
        description=(
            "Issues the reviewer noticed but ruled out as non-issues, with "
            "rationale. Surfaces the rigor of the review — a NO-SHIP with "
            "zero dismissed concerns is suspicious; a SHIP with several "
            "dismissed concerns suggests the reviewer actively looked for "
            "issues rather than pattern-matching against the spec."
        ),
    )

    @field_validator("summary")
    @classmethod
    def _validate_summary_nonblank(cls, v: str) -> str:
        """``summary`` must contain at least one non-whitespace character.

        ``Field(min_length=1)`` rejects empty strings but accepts
        ``'   '`` / ``'\\n\\t '`` / other pure-whitespace values, which
        carry no narrative information and defeat the field's whole
        purpose (forcing the reviewer to assert what they actually
        verified). Brief explicitly calls this out — whitespace-only
        summary must fail validation.
        """
        if not v.strip():
            raise ValueError(
                "summary must contain non-whitespace content; got an "
                "all-whitespace value which provides no verification "
                "narrative to the operator or the synthesizer"
            )
        return v

    @model_validator(mode="after")
    def _validate_priority_order(self) -> ReviewerOutput:
        """``priority_order`` must be a complete permutation of
        ``range(len(findings))``.

        Strict on purpose. A partial ordering is harder to consume
        downstream and easier to get wrong silently. Forcing reviewers to
        permute all findings means they consciously rank each one.

        - ``len(priority_order)`` must equal ``len(findings)``
        - ``sorted(priority_order)`` must equal ``list(range(len(findings)))``
        - no duplicates, no out-of-range indices, no missing positions
        - empty list iff findings is empty
        """
        expected = list(range(len(self.findings)))
        if sorted(self.priority_order) != expected:
            raise ValueError(
                f"priority_order must be a complete permutation of "
                f"range({len(self.findings)}); got {self.priority_order}"
            )
        return self


def get_findings_schema_string() -> str:
    """Return the JSON schema string for :class:`ReviewerOutput`,
    formatted for inclusion in a reviewer prompt.

    Single source of truth — the orchestrator and any future
    prompt-rendering caller pulls from here rather than hand-rolling
    the schema. If :class:`Finding` or :class:`ReviewerOutput` evolves
    shape, this function changes once and every caller picks up the
    update.

    Output is a JSON-ish skeleton with type annotations as comments,
    NOT a strict JSON Schema document — it's prompt input, intended
    for the model to read, not a validator. The shape mirrors
    :class:`ReviewerOutput` and :class:`Finding`, which are the canonical
    pydantic models. Inline ``//`` comments call out the
    contracts the reviewer prompt relies on (which fields are
    required, which can be ``[]``, what permutation rule
    ``priority_order`` must satisfy).
    """
    return (
        "{\n"
        '  "verdict": "SHIP" | "NO-SHIP",\n'
        '  "findings": [\n'
        '    {"severity": "blocker"|"minor"|"nit", '
        '"file": "path"|null, "line": int|null, '
        '"spec_clause": "string", "finding": "string", '
        '"evidence_cmd": "string"|null, '
        '"evidence_output": "string"|null}\n'
        "  ],\n"
        '  "summary": "string (required, non-empty narrative)",\n'
        '  "priority_order": [int],  // indices into findings, '
        "complete permutation; [] when findings is []\n"
        '  "coverage_gaps": [string],     // required; [] if none\n'
        '  "dismissed_concerns": [string] // required; [] if none\n'
        "}"
    )


def _try_parse_and_validate(text: str) -> ReviewerOutput | None:
    """Try ``json.loads(text)`` then :meth:`ReviewerOutput.model_validate`.

    Returns the validated :class:`ReviewerOutput` on success, ``None``
    on any failure. The two-step combined check is the discriminator
    used by :func:`parse_reviewer_output` to decide "is this the verdict
    block?" — fragments that look JSON-shaped but aren't real verdicts
    return ``None`` and the parser keeps scanning.

    Validation failure is treated as "not the verdict" rather than
    re-raised: a reviewer that emits a stray ``{"draft": "..."}``
    object in narrative shouldn't blow up the run. The real verdict
    at the end of the response is what the user wants.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return ReviewerOutput.model_validate(parsed)
    except ValidationError:
        return None


def parse_reviewer_output(raw: str) -> ReviewerOutput:
    """Parse a reviewer's raw stdout text into a :class:`ReviewerOutput`.

    Strategy:

    1. Call :func:`_extract_json_candidates` to collect every fenced
       ``json``/unlabeled block AND every raw-decoded top-level JSON object
       as ``(start_pos, content)`` candidates, sorted
       by ``start_pos`` descending — the LATEST candidate in the
       document is tried first. This preserves the "real verdict at
       the end" property whether the verdict is a fence or a bare JSON
       block, and prevents an EARLIER fenced example from masking a
       LATER real verdict . The extractor is shared
       with :func:`syncade.synthesis.parse_synthesizer_output`.
    2. Return the first candidate that passes BOTH ``json.loads`` AND
       :meth:`ReviewerOutput.model_validate`. Fragments that look JSON-shaped
       but aren't real verdicts (``{{ color:
       'var(--mm-amber)' }}``, schema illustrations, pseudocode) fail
       one or both checks and the parser keeps trying earlier
       candidates.
    3. Whole-string fallback (``json.loads(raw.strip())``) for the
       pure-JSON-no-narrative case where nothing else matched.

    The raw-decoder scan recovers from unmatched ``{`` in prose: an unfinished
    ``if (x) { do something`` doesn't swallow the rest of the document; the
    scanner advances past the unmatched ``{`` and keeps looking for later JSON
    objects.

    Raises :class:`ReviewerOutputError` only when no candidate
    validates anywhere. The message includes the count of candidates
    attempted, a truncated snippet of the first attempted candidate
    (the latest-position one — the one most likely intended as the
    verdict), and a pointer at the reviewer's ``.stdout`` file — so
    the user surfacing exit code 70 knows where the raw response is
    and has at least one concrete fragment to inspect.
    """
    candidates = _extract_json_candidates(raw)

    for _, content in candidates:
        result = _try_parse_and_validate(content.strip())
        if result is not None:
            return result

    # Whole-string fallback: covers pure-JSON-no-narrative if anything slipped
    # past candidate extraction.
    stripped = raw.strip()
    if stripped:
        result = _try_parse_and_validate(stripped)
        if result is not None:
            return result

    # Nothing validated — surface a diagnostic message with attempt
    # count, a snippet of the FIRST attempted candidate (i.e. the
    # latest-position candidate, since we sort descending and try in
    # that order — that's the most likely "intended verdict" the
    # reviewer wrote at the end of their response), and a pointer
    # at the .stdout artifact. Falls back to a slice of raw when
    # there were no candidates at all.
    if candidates:
        # candidates is sorted DESC by position, so [0] is the latest
        # (first-attempted) candidate — which is the most likely
        # "intended verdict" the reviewer wrote at the end of their
        # response.
        snippet_source = candidates[0][1]
    else:
        snippet_source = raw
    snippet = snippet_source[:200]
    raise ReviewerOutputError(
        f"reviewer output had no parseable ReviewerOutput JSON "
        f"(attempted {len(candidates)} candidate block(s); first "
        f"attempted snippet: {snippet!r}); the raw response is "
        f"preserved at <reviewer>.stdout in the run directory."
    )
