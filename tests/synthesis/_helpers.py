"""Shared test helpers for :mod:`syncade.synthesis` tests (PR-R3 split)."""

from __future__ import annotations


def _provenance(
    *,
    reviewer_name: str = "claude-reviewer",
    original_severity: str = "blocker",
    original_index: int = 0,
    original_description: str = "Missing nullability on user.email",
) -> dict:
    return {
        "reviewer_name": reviewer_name,
        "original_severity": original_severity,
        "original_index": original_index,
        "original_description": original_description,
    }


def _finding(
    *,
    description: str = "user.email column lacks NOT NULL constraint",
    file: str | None = "src/db/schema.sql",
    severity: str = "blocker",
    provenance: list[dict] | None = None,
    dismissed: bool = False,
    dismissal_rationale: str | None = None,
    severity_change_rationale: str | None = None,
) -> dict:
    if provenance is None:
        provenance = [_provenance()]
    out: dict = {
        "description": description,
        "file": file,
        "severity": severity,
        "provenance": provenance,
        "dismissed": dismissed,
    }
    if dismissal_rationale is not None:
        out["dismissal_rationale"] = dismissal_rationale
    if severity_change_rationale is not None:
        out["severity_change_rationale"] = severity_change_rationale
    return out
