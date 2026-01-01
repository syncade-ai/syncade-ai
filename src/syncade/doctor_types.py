"""Shared types for ``syncade --doctor`` (PR-v2-12).

A LEAF module so the check *engine* (:mod:`syncade.doctor`) and the run-plan + cost *preview*
(:mod:`syncade.doctor_preview`) can both build :class:`DoctorCheck` rows without an import
cycle (doctor imports doctor_preview for the preview checks; both import this).
"""

from __future__ import annotations

from dataclasses import dataclass

_OK = "ok"
_RED = "red"
_SKIP = "skip"

_STATUS_GLYPH = {_OK: "✓", _RED: "✗", _SKIP: "–"}  # ✓ ✗ –


@dataclass(frozen=True)
class DoctorCheck:
    """One preflight check's outcome. ``status`` is one of ``ok`` / ``red`` / ``skip``;
    only ``red`` fails doctor's exit code (a ``skip`` means doctor could not run the check,
    not that it passed). ``fix`` is an operator-facing remediation shown under a red row."""

    name: str
    status: str
    detail: str
    fix: str | None = None
