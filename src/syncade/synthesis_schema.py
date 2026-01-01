"""Synthesizer JSON schema string.

``get_synthesizer_schema_string`` — the synthesizer prompt template's
``{json_schema}`` source. Pulls the root-cause-cluster fragment from
``synthesis_clusters`` (no dependency on ``synthesis`` itself), so it extracts
without a circular import. Re-exported from ``synthesis`` so the
``syncade.synthesis.get_synthesizer_schema_string`` import path is unchanged.
"""

from __future__ import annotations

from syncade.synthesis_clusters import cluster_schema_fragment


def get_synthesizer_schema_string() -> str:
    """Return the JSON schema string for :class:`SynthesizerOutput`,
    formatted for inclusion in the synthesizer prompt template's
    ``{json_schema}`` placeholder.

    Single source of truth — the synthesizer subprocess
    pulls from here rather than hand-rolling the schema. Parallel to
    :func:`syncade.findings.get_findings_schema_string`.

    Output is a JSON-ish skeleton with inline ``//`` comments calling
    out the contracts the prompt relies on (which fields are required,
    which can be ``[]``, the cannot-invent and cannot-deactivate-unanimous-
    blocker rules). NOT a strict JSON Schema document — it's prompt
    input, intended for the model to read, not a validator.
    """
    return (
        "{\n"
        '  "consolidated_findings": [\n'
        "    {\n"
        '      "description": "string (required, non-empty)",\n'
        '      "file": "path"|null,\n'
        '      "severity": "blocker"|"minor"|"nit",  '
        "// synthesizer's final severity\n"
        "      // Do NOT include reviewer-only fields here: no line, "
        "spec_clause, finding, priority_order, coverage_gaps, or "
        "dismissed_concerns\n"
        '      "provenance": [  // required, min length 1; '
        "cannot invent findings\n"
        "        {\n"
        '          "reviewer_name": "string",\n'
        '          "original_severity": "blocker"|"minor"|"nit",\n'
        '          "original_index": int,  // index into that '
        "reviewer's original findings list\n"
        '          "original_description": "string"  // reviewer\'s '
        "verbatim wording\n"
        "        }\n"
        "      ],\n"
        '      "dismissed": bool,  // default false\n'
        '      "dismissal_rationale": "string"|null,  '
        "// required iff dismissed=true; cannot dismiss OR downgrade "
        "a finding two distinct reviewers flagged at blocker "
        "(unanimous-blocker rule, schema-enforced)\n"
        '      "severity_change_rationale": "string"|null  '
        "// required iff severity differs from EVERY reviewer's "
        "original_severity\n"
        "    }\n"
        "  ],\n" + cluster_schema_fragment() + ",\n"
        '  "synthesis_summary": "string (required, non-empty narrative '
        'about how the consolidation went)"\n'
        "}"
    )
