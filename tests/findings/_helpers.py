"""Shared helpers for the split ``tests/findings/`` package.

``_verdict_json`` is used by both ``test_parser_resilience.py`` and
``test_parser_and_fields.py`` (the parser-facing test files), so it is
hoisted here verbatim from the original ``tests/test_findings.py``.
"""

from __future__ import annotations

import json


def _verdict_json(
    verdict: str = "SHIP",
    findings: list[dict] | None = None,
    *,
    summary: str = "test ok",
    coverage_gaps: list[str] | None = None,
    dismissed_concerns: list[str] | None = None,
    priority_order: list[int] | None = None,
    extras: dict | None = None,
) -> str:
    """Build a minimal valid :class:`ReviewerOutput`-shaped JSON string.

    Centralizes the PR-6 narrative-surface fields (``summary``,
    ``priority_order``, ``coverage_gaps``, ``dismissed_concerns``) so
    the dozens of parser tests don't each have to spell them out.
    ``priority_order`` defaults to the identity permutation
    (``range(len(findings))``) which is the simplest valid value.

    ``extras`` can be merged in to inject extra top-level keys for the
    "unknown field at top level" rejection tests.
    """
    findings = findings or []
    body: dict = {
        "verdict": verdict,
        "findings": findings,
        "summary": summary,
        "priority_order": (
            priority_order if priority_order is not None else list(range(len(findings)))
        ),
        "coverage_gaps": coverage_gaps if coverage_gaps is not None else [],
        "dismissed_concerns": dismissed_concerns if dismissed_concerns is not None else [],
    }
    if extras:
        body.update(extras)
    return json.dumps(body)
