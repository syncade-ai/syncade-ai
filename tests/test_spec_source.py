"""Tests for :mod:`syncade.spec_source` (PR-C — OpenSpec tier-B consumption).

Real temp ``openspec/changes/<id>/`` fixtures, no mocks. The module reads
markdown files directly (consume-don't-depend) — it never invokes the ``openspec``
binary — so the tests just build folders on disk and assert on the assembled
markdown.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncade.spec_source import (
    SpecSourceError,
    assemble_openspec_spec,
    find_openspec_changes,
    resolve_openspec_change,
)


def _make_change(
    root: Path,
    change_id: str,
    *,
    proposal: str | None = "## Why\n\nBecause the thing.\n\n## What Changes\n\n- a\n",
    deltas: dict[str, str] | None = None,
) -> Path:
    """Build ``<root>/openspec/changes/<change_id>/`` with a proposal.md and
    optional per-capability spec deltas (``{capability: spec_text}``)."""
    d = root / "openspec" / "changes" / change_id
    d.mkdir(parents=True, exist_ok=True)
    if proposal is not None:
        (d / "proposal.md").write_text(proposal, encoding="utf-8")
    for cap, text in (deltas or {}).items():
        sp = d / "specs" / cap
        sp.mkdir(parents=True, exist_ok=True)
        (sp / "spec.md").write_text(text, encoding="utf-8")
    return d


class TestFindOpenspecChanges:
    def test_no_openspec_dir_returns_empty(self, tmp_path: Path):
        assert find_openspec_changes(tmp_path) == []

    def test_finds_changes_with_proposal_sorted(self, tmp_path: Path):
        _make_change(tmp_path, "b-second")
        _make_change(tmp_path, "a-first")
        assert find_openspec_changes(tmp_path) == ["a-first", "b-second"]

    def test_excludes_dir_without_proposal(self, tmp_path: Path):
        _make_change(tmp_path, "good")
        # a change dir with no proposal.md is not an active change
        _make_change(tmp_path, "incomplete", proposal=None)
        assert find_openspec_changes(tmp_path) == ["good"]

    def test_excludes_archive(self, tmp_path: Path):
        _make_change(tmp_path, "live")
        # OpenSpec archives completed changes under changes/archive/
        (tmp_path / "openspec" / "changes" / "archive").mkdir(parents=True)
        (tmp_path / "openspec" / "changes" / "archive" / "proposal.md").write_text("x")
        assert find_openspec_changes(tmp_path) == ["live"]


class TestResolveOpenspecChange:
    def test_explicit_present_returns_it(self, tmp_path: Path):
        _make_change(tmp_path, "add-auth")
        assert resolve_openspec_change(tmp_path, "add-auth") == "add-auth"

    def test_explicit_absent_raises(self, tmp_path: Path):
        _make_change(tmp_path, "add-auth")
        with pytest.raises(SpecSourceError) as exc:
            resolve_openspec_change(tmp_path, "nope")
        assert "nope" in str(exc.value)

    def test_none_single_autoresolves(self, tmp_path: Path):
        _make_change(tmp_path, "only-one")
        assert resolve_openspec_change(tmp_path, None) == "only-one"

    def test_none_zero_raises(self, tmp_path: Path):
        with pytest.raises(SpecSourceError):
            resolve_openspec_change(tmp_path, None)

    def test_none_multiple_raises_and_lists(self, tmp_path: Path):
        _make_change(tmp_path, "one")
        _make_change(tmp_path, "two")
        with pytest.raises(SpecSourceError) as exc:
            resolve_openspec_change(tmp_path, None)
        msg = str(exc.value)
        assert "one" in msg and "two" in msg


class TestAssembleOpenspecSpec:
    def test_includes_proposal_and_deltas(self, tmp_path: Path):
        _make_change(
            tmp_path,
            "add-auth",
            proposal="## Why\n\nUsers need login.\n",
            deltas={"auth": "## ADDED Requirements\n### Requirement: Login\n#### Scenario: ok\n"},
        )
        out = assemble_openspec_spec(tmp_path, "add-auth")
        assert "Users need login." in out
        assert "### Requirement: Login" in out
        assert "ADDED Requirements" in out

    def test_multi_capability_sorted_deterministic(self, tmp_path: Path):
        _make_change(
            tmp_path,
            "multi",
            deltas={
                "zebra": "## ADDED Requirements\n### Requirement: Z\n",
                "alpha": "## ADDED Requirements\n### Requirement: A\n",
            },
        )
        out = assemble_openspec_spec(tmp_path, "multi")
        # capabilities assembled in sorted order → alpha before zebra
        assert out.index("Requirement: A") < out.index("Requirement: Z")

    def test_self_contained_names_change_id(self, tmp_path: Path):
        _make_change(tmp_path, "add-auth")
        out = assemble_openspec_spec(tmp_path, "add-auth")
        assert "add-auth" in out

    def test_missing_proposal_raises(self, tmp_path: Path):
        _make_change(tmp_path, "broken", proposal=None)
        with pytest.raises(SpecSourceError):
            assemble_openspec_spec(tmp_path, "broken")

    def test_proposal_only_no_deltas_still_assembles(self, tmp_path: Path):
        _make_change(tmp_path, "doc-only", proposal="## Why\n\nJust docs.\n", deltas=None)
        out = assemble_openspec_spec(tmp_path, "doc-only")
        assert "Just docs." in out
