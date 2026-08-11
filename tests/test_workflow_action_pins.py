"""Structural assertions about GitHub Actions workflows — PR-h-10.

SHA pins (item 5): every action must be pinned to a commit SHA with a human-readable version
comment so the pin cannot float silently.

CI shape (item 3): gates must be independent — one failure must not mask the others. Asserted
by checking that ci.yml uses separate jobs and that gate steps carry `if: !cancelled()`.

Public-snapshot safety (item 1): the private-identifier gate is dev-repo-only and is NOT
asserted here — it needs an untracked token list and the staging script, neither of which
ships, so oss-release.sh is its single enforcement point.

Artifact gating (item 5): the release workflow must build first, then run ALL required gates
(ruff, format, compileall, LOC, internal-refs, whitespace, pytest) after `uv build`, so every
gate covers the exact source that went into the artifact, not just the working-tree checkout.

Release creation guard (item 5): gh release create must be conditional on public repo
visibility so a private-mirror tag cannot publish an unattested artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))

# `uses: <owner>/<repo>@<ref>` with an optional trailing comment. Local (`./...`) and docker
# (`docker://...`) uses have no upstream tag to float, so they are not in scope.
_USES = re.compile(
    r"^\s*uses:\s*(?P<action>[\w.-]+/[\w.-]+(?:/[\w.-]+)*)@(?P<ref>\S+)(?P<rest>.*)$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"#\s*v?\d+\.\d+")


def _uses_lines() -> list[tuple[Path, int, re.Match[str]]]:
    found = []
    for wf in _WORKFLOWS:
        for lineno, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = _USES.match(line)
            if m:
                found.append((wf, lineno, m))
    return found


def test_there_are_workflows_to_check():
    """Guard the guard: a glob that matches nothing would make every test below vacuous."""
    assert _WORKFLOWS, "no workflow files found — this test would pass by checking nothing"
    assert _uses_lines(), "no `uses:` lines found — the regex or the layout changed"


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_sha(wf: Path):
    unpinned = [
        f"{wf.name}:{lineno} {m['action']}@{m['ref']}"
        for f, lineno, m in _uses_lines()
        if f == wf and not _SHA.match(m["ref"])
    ]
    assert not unpinned, "actions must be pinned to a 40-char commit SHA, not a tag:\n" + "\n".join(
        unpinned
    )


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=lambda p: p.name)
def test_every_pin_records_the_human_version(wf: Path):
    missing = [
        f"{wf.name}:{lineno} {m['action']}@{m['ref'][:12]}…"
        for f, lineno, m in _uses_lines()
        if f == wf and _SHA.match(m["ref"]) and not _VERSION_COMMENT.search(m["rest"])
    ]
    assert not missing, "each SHA pin needs a trailing `# vX.Y.Z` so staleness is readable:\n" + (
        "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# CI shape: gate independence (item 3)
# ---------------------------------------------------------------------------


def test_ci_uses_separate_jobs_for_gates():
    """ci.yml must have independent jobs so one failure cannot mask the others.

    The repo-gates job (LOC, internal-refs, whitespace) must be a separate top-level
    job from the tests job, so a test failure still lets every gate report its own
    result.
    """
    ci_wf = next((w for w in _WORKFLOWS if w.name == "ci.yml"), None)
    assert ci_wf is not None, "ci.yml not found"
    text = ci_wf.read_text(encoding="utf-8")
    # Both jobs must be present at the top level.  A simple string search is
    # sufficient here because each name is unique and the YAML structure keeps
    # them at the `jobs:` level.
    assert "repo-gates:" in text, "ci.yml must have a dedicated repo-gates job"
    assert "tests:" in text, "ci.yml must have a dedicated tests job"


def test_ci_gate_steps_use_cancelled_condition():
    """Gate steps inside the repo-gates job must use `if: !cancelled()`.

    This lets a failing step still allow its siblings to report, so a single red
    gate does not silence the rest.
    """
    ci_wf = next((w for w in _WORKFLOWS if w.name == "ci.yml"), None)
    assert ci_wf is not None
    text = ci_wf.read_text(encoding="utf-8")
    # The repo-gates job contains these three gates; each must carry the condition.
    gate_markers = [
        "scripts/check-loc.sh",
        "scripts/check-no-internal-refs.sh",
        "git diff --check",
    ]
    for marker in gate_markers:
        # Find the line with the gate command and check that somewhere in the
        # preceding step block there is a `!cancelled()` condition.
        pos = text.find(marker)
        assert pos != -1, f"ci.yml must contain gate: {marker}"
        # The `if:` for the step appears before its `run:` in the same block.
        # Look at the 300 characters before the marker for the condition.
        context = text[max(0, pos - 300) : pos]
        assert "!cancelled()" in context, (
            f"The ci.yml step containing '{marker}' must have an "
            f"`if: ${{{{ !cancelled() }}}}` condition so one failure does not mask it"
        )


# ---------------------------------------------------------------------------
# Public-snapshot safety: private-identifier step must not break public CI
# ---------------------------------------------------------------------------


def test_release_workflow_gates_artifact_source():
    """release.yml must run all required gates AFTER `uv build` against the artifact source.

    Building first then gating ensures that every gate covers the exact source that went
    into the artifact — not just the working-tree checkout that uv build read from.
    """
    release_wf = next((w for w in _WORKFLOWS if w.name == "release.yml"), None)
    assert release_wf is not None, "release.yml not found"
    text = release_wf.read_text(encoding="utf-8")
    build_pos = text.find("uv build")
    assert build_pos != -1, "release.yml must have a `uv build` step"

    required_gates = [
        ("pytest", "pytest must run after uv build to gate the artifact, not just the source"),
        ("ruff check", "ruff lint must run after uv build against the artifact source"),
        ("ruff format", "ruff format check must run after uv build against the artifact source"),
        ("compileall", "compileall must run after uv build against the artifact source"),
        ("check-loc.sh", "LOC gate must run after uv build"),
        # check-no-private-identifiers.sh is deliberately absent: it is dev-repo-only (it needs
        # oss-stage.sh) and reads an untracked token list, so it cannot run on a runner without
        # a secret — declined, since one identifier is a client's. oss-release.sh runs it on the
        # dev tree, which is the only path to public.
        ("check-no-internal-refs.sh", "internal-refs gate must run after uv build"),
        ("git diff --check", "whitespace gate must run after uv build"),
    ]
    missing = [msg for gate, msg in required_gates if text.find(gate, build_pos) == -1]
    listed = "\n".join(f"  - {m}" for m in missing)
    assert not missing, f"These gates must appear after `uv build` in release.yml:\n{listed}"


# ---------------------------------------------------------------------------
# Release creation guard: private-mirror tags must not publish unattested artifacts
# ---------------------------------------------------------------------------


def test_release_creation_requires_public_repo():
    """The `gh release create` step in release.yml must be conditional on public visibility.

    Private repositories cannot produce sigstore attestations. Publishing a release
    without attestation from a private-mirror tag violates the signed-release invariant.
    The attestation step already carries the visibility guard; the release creation step
    must carry it too so a private tag cannot slip an unattested artifact through.
    """
    release_wf = next((w for w in _WORKFLOWS if w.name == "release.yml"), None)
    assert release_wf is not None, "release.yml not found"
    text = release_wf.read_text(encoding="utf-8")
    assert "gh release create" in text, "release.yml must contain a gh release create step"
    # The condition must appear in the step block that contains the release command.
    # Look at the 200 characters before the command for the step's `if:` line.
    pos = text.find("gh release create")
    context = text[max(0, pos - 200) : pos]
    assert "repository.visibility == 'public'" in context, (
        "The `gh release create` step must be guarded with "
        "`if: github.event.repository.visibility == 'public'` "
        "so private-mirror tags cannot publish unattested artifacts"
    )
