"""Tests for :mod:`syncade.spec_audit` (PR-10 Task 1).

Uses :class:`FakeAuditorAdapter` exclusively via the ``adapter=``
injection so the unit tests never shell out to a real codex CLI.
The smoke test path would cover the real codex path end-to-end.

The ``_init_workspace_git`` helper called by :func:`run_spec_audit`
does run real git (same as the synthesizer path does in orchestrator
tests) — git must be on PATH in the test environment.

Coverage points per the brief's T1 acceptance:

1. Happy path: FakeAuditorAdapter returning canned READY →
   ``outcome="ready"``
2. Blockers path: FakeAuditorAdapter returning canned output with a
   blocker-severity finding → ``outcome="needs_clarification"``
3. Subprocess-error path: FakeAuditorAdapter raising
   ReviewerInvocationError → ``outcome="subprocess_error"``, error
   preserved
4. Parse-failure path: FakeAuditorAdapter raising SpecAuditOutputError
   → ``outcome="subprocess_error"``, ``isinstance(error,
   SpecAuditOutputError)`` True
5. ``SpecAuditResult.__post_init__`` consistency enforcement — the
   exactly-one-of-three contract analogous to other result classes
"""

from __future__ import annotations

import pytest

from syncade.adapters.base import ReviewerInvocationError
from syncade.adapters.fake import FakeAuditorAdapter
from syncade.spec_audit import (
    AUDITOR_MODEL,
    AUDITOR_NAME,
    AUDITOR_PERMISSIONS,
    AUDITOR_THINKING,
    SpecAuditFinding,
    SpecAuditOutput,
    SpecAuditOutputError,
    SpecAuditResult,
    _classify_outcome,
    get_spec_audit_schema_string,
    parse_spec_audit_output,
    run_spec_audit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ready_output() -> SpecAuditOutput:
    """Minimal valid READY output with no findings."""
    return SpecAuditOutput(
        verdict="READY",
        findings=[],
        summary="audit complete: brief is ready",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _output_with_blocker() -> SpecAuditOutput:
    """Output with one blocker-severity finding."""
    return SpecAuditOutput(
        verdict="NEEDS-CLARIFICATION",
        findings=[
            SpecAuditFinding(
                severity="blocker",
                section="Tasks",
                line=42,
                issue_class="unverified_claim",
                finding="The brief asserts X without citing a verification source.",
                suggested_remediation="Add 'verified <date> against <version>'.",
            )
        ],
        summary="audit: one blocker finding — unverified claim in Tasks section",
        priority_order=[0],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


_STUB_TEMPLATE = (
    "You are auditing the PR brief at {pr_doc_path} for spec-level issues.\n"
    "Output JSON matching:\n{json_schema}\n"
)
"""Minimal stub template seeded into the per-repo override dir for tests
that call run_spec_audit.  Task 2 provides the real template; these tests
must not depend on the packaged template being present."""


def _pr_doc(tmp_path):
    """Write a stub PR doc and return its path."""
    doc = tmp_path / "pr-test.md"
    doc.write_text("# PR stub\n\nThis is a test PR brief.\n", encoding="utf-8")
    return doc


def _seed_template(repo_root):
    """Write the stub spec_audit.md template into the per-repo override dir."""
    tpl_dir = repo_root / ".syncade" / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / "spec_audit.md").write_text(_STUB_TEMPLATE, encoding="utf-8")


# ---------------------------------------------------------------------------
# SpecAuditResult.__post_init__ consistency enforcement
# ---------------------------------------------------------------------------


class TestSpecAuditResultConsistency:
    def test_ready_requires_output_non_none(self):
        with pytest.raises(ValueError, match="requires output to be non-None"):
            SpecAuditResult(
                outcome="ready",
                output=None,
                error=None,
                duration_seconds=1.0,
            )

    def test_ready_requires_error_none(self):
        with pytest.raises(ValueError, match="requires error=None"):
            SpecAuditResult(
                outcome="ready",
                output=_ready_output(),
                error=RuntimeError("oops"),
                duration_seconds=1.0,
            )

    def test_needs_clarification_requires_output_non_none(self):
        with pytest.raises(ValueError, match="requires output to be non-None"):
            SpecAuditResult(
                outcome="needs_clarification",
                output=None,
                error=None,
                duration_seconds=1.0,
            )

    def test_subprocess_error_requires_error_non_none(self):
        with pytest.raises(ValueError, match="requires error to be non-None"):
            SpecAuditResult(
                outcome="subprocess_error",
                output=None,
                error=None,
                duration_seconds=1.0,
            )

    def test_subprocess_error_requires_output_none(self):
        with pytest.raises(ValueError, match="requires output=None"):
            SpecAuditResult(
                outcome="subprocess_error",
                output=_ready_output(),
                error=RuntimeError("fail"),
                duration_seconds=1.0,
            )

    def test_valid_ready_constructs(self):
        result = SpecAuditResult(
            outcome="ready",
            output=_ready_output(),
            error=None,
            duration_seconds=1.5,
        )
        assert result.outcome == "ready"
        assert result.error is None

    def test_valid_subprocess_error_constructs(self):
        exc = RuntimeError("subprocess died")
        result = SpecAuditResult(
            outcome="subprocess_error",
            output=None,
            error=exc,
            duration_seconds=0.1,
        )
        assert result.outcome == "subprocess_error"
        assert result.error is exc


# ---------------------------------------------------------------------------
# _classify_outcome
# ---------------------------------------------------------------------------


class TestClassifyOutcome:
    def test_no_findings_is_ready(self):
        assert _classify_outcome(_ready_output()) == "ready"

    def test_blocker_finding_is_needs_clarification(self):
        assert _classify_outcome(_output_with_blocker()) == "needs_clarification"

    def test_minor_only_is_ready(self):
        output = SpecAuditOutput(
            verdict="READY",
            findings=[
                SpecAuditFinding(
                    severity="minor",
                    section="Goal",
                    line=None,
                    issue_class="ambiguous_acceptance_criteria",
                    finding="Minor wording ambiguity.",
                    suggested_remediation=None,
                )
            ],
            summary="minor issue only",
            priority_order=[0],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        assert _classify_outcome(output) == "ready"

    def test_nit_only_is_ready(self):
        output = SpecAuditOutput(
            verdict="READY",
            findings=[
                SpecAuditFinding(
                    severity="nit",
                    section="Out of scope",
                    line=None,
                    issue_class="missing_structural_sections",
                    finding="Nit-level formatting suggestion.",
                    suggested_remediation=None,
                )
            ],
            summary="nit only",
            priority_order=[0],
            coverage_gaps=[],
            dismissed_concerns=[],
        )
        assert _classify_outcome(output) == "ready"


# ---------------------------------------------------------------------------
# parse_spec_audit_output
# ---------------------------------------------------------------------------


class TestParseSpecAuditOutput:
    def test_parses_fenced_json_block(self):
        raw = _ready_output().model_dump_json()
        fenced = f"```json\n{raw}\n```"
        parsed = parse_spec_audit_output(fenced)
        assert parsed.verdict == "READY"
        assert parsed.findings == []

    def test_parses_bare_json(self):
        raw = _ready_output().model_dump_json()
        parsed = parse_spec_audit_output(raw)
        assert parsed.verdict == "READY"

    def test_parses_json_with_blocker_finding(self):
        raw = _output_with_blocker().model_dump_json()
        parsed = parse_spec_audit_output(raw)
        assert parsed.verdict == "NEEDS-CLARIFICATION"
        assert len(parsed.findings) == 1
        assert parsed.findings[0].severity == "blocker"
        assert parsed.findings[0].section == "Tasks"

    def test_raises_on_malformed_json(self):
        with pytest.raises(SpecAuditOutputError, match="no parseable SpecAuditOutput JSON"):
            parse_spec_audit_output("not json at all")

    def test_raises_on_wrong_schema(self):
        """A genuinely wrong shape — a finding using "file" where the schema says "section".

        The payload now matches what this test has always CLAIMED to check. It previously used
        a bare `extra_field`, which since PR-h-field-05 is repaired rather than rejected (an
        advisory key must not discard a whole audit). A RENAMED field is the case that must
        still raise, and it does so by construction: it emits `missing` for `section` beside
        `extra_forbidden` for `file`, and mixed error types are never repaired.
        """
        bad = (
            '{"verdict": "READY", "summary": "x", '
            '"priority_order": [], "coverage_gaps": [], "dismissed_concerns": [], '
            '"findings": [{"severity": "blocker", "file": "S1", '
            '"issue_class": "ambiguity", "finding": "y"}]}'
        )
        with pytest.raises(SpecAuditOutputError):
            parse_spec_audit_output(bad)

    def test_an_advisory_extra_key_no_longer_discards_the_audit(self):
        """The other half of the contract change, pinned beside its counterpart."""
        ok = (
            '{"verdict": "READY", "findings": [], "summary": "x", '
            '"priority_order": [], "coverage_gaps": [], "dismissed_concerns": [], '
            '"extra_field": "advisory"}'
        )
        assert parse_spec_audit_output(ok).verdict == "READY"

    def test_raises_on_empty_string(self):
        with pytest.raises(SpecAuditOutputError):
            parse_spec_audit_output("")


# ---------------------------------------------------------------------------
# get_spec_audit_schema_string
# ---------------------------------------------------------------------------


def test_schema_string_mentions_section_and_line():
    schema = get_spec_audit_schema_string()
    assert '"section"' in schema
    assert '"line"' in schema


def test_schema_string_forbids_aliases():
    schema = get_spec_audit_schema_string()
    # The schema doc must warn about forbidden aliases
    assert "location" in schema or "SCHEMA FIELD NAMES ARE EXACT" in schema


# ---------------------------------------------------------------------------
# run_spec_audit — via FakeAuditorAdapter
# ---------------------------------------------------------------------------


class TestRunSpecAudit:
    def test_happy_path_returns_ready(self, tmp_path):
        """Fake returning READY with no blockers → outcome='ready'."""
        _seed_template(tmp_path)
        pr_doc = _pr_doc(tmp_path)
        adapter = FakeAuditorAdapter(canned_output=_ready_output())

        result = run_spec_audit(
            pr_doc_path=pr_doc,
            repo_root=tmp_path,
            timeout_seconds=10.0,
            adapter=adapter,
        )

        assert result.outcome == "ready"
        assert result.output is not None
        assert result.output.verdict == "READY"
        assert result.error is None
        assert result.duration_seconds >= 0.0

    def test_blockers_returns_needs_clarification(self, tmp_path):
        """Fake returning a blocker-severity finding → outcome='needs_clarification'."""
        _seed_template(tmp_path)
        pr_doc = _pr_doc(tmp_path)
        adapter = FakeAuditorAdapter(canned_output=_output_with_blocker())

        result = run_spec_audit(
            pr_doc_path=pr_doc,
            repo_root=tmp_path,
            timeout_seconds=10.0,
            adapter=adapter,
        )

        assert result.outcome == "needs_clarification"
        assert result.output is not None
        assert result.output.verdict == "NEEDS-CLARIFICATION"
        assert len(result.output.findings) == 1
        assert result.output.findings[0].severity == "blocker"
        assert result.error is None

    def test_subprocess_error_maps_to_subprocess_error(self, tmp_path):
        """Fake raising ReviewerInvocationError → outcome='subprocess_error'."""
        _seed_template(tmp_path)
        pr_doc = _pr_doc(tmp_path)
        exc = ReviewerInvocationError(
            "codex returned non-zero rc", returncode=1, stdout="", stderr=""
        )
        adapter = FakeAuditorAdapter(canned_exception=exc)

        result = run_spec_audit(
            pr_doc_path=pr_doc,
            repo_root=tmp_path,
            timeout_seconds=10.0,
            adapter=adapter,
        )

        assert result.outcome == "subprocess_error"
        assert result.output is None
        assert result.error is exc

    def test_parse_failure_maps_to_subprocess_error_with_spec_audit_output_error(self, tmp_path):
        """Fake raising SpecAuditOutputError → outcome='subprocess_error',
        error is a SpecAuditOutputError (CLI can route to exit 70)."""
        _seed_template(tmp_path)
        pr_doc = _pr_doc(tmp_path)
        exc = SpecAuditOutputError("could not parse auditor output")
        adapter = FakeAuditorAdapter(canned_exception=exc)

        result = run_spec_audit(
            pr_doc_path=pr_doc,
            repo_root=tmp_path,
            timeout_seconds=10.0,
            adapter=adapter,
        )

        assert result.outcome == "subprocess_error"
        assert result.output is None
        assert isinstance(result.error, SpecAuditOutputError)

    def test_missing_pr_doc_returns_subprocess_error(self, tmp_path):
        """A pr_doc_path that doesn't exist causes a copy failure → outcome='subprocess_error'."""
        nonexistent = tmp_path / "no-such-brief.md"
        adapter = FakeAuditorAdapter()

        result = run_spec_audit(
            pr_doc_path=nonexistent,
            repo_root=tmp_path,
            timeout_seconds=10.0,
            adapter=adapter,
        )

        assert result.outcome == "subprocess_error"
        assert result.output is None
        assert result.error is not None

    def test_adapter_receives_auditor_config(self, tmp_path):
        """The ReviewerConfig built internally uses the AUDITOR_* constants."""
        _seed_template(tmp_path)
        pr_doc = _pr_doc(tmp_path)
        adapter = FakeAuditorAdapter()

        run_spec_audit(
            pr_doc_path=pr_doc,
            repo_root=tmp_path,
            timeout_seconds=10.0,
            adapter=adapter,
        )

        assert len(adapter.invocations) == 1
        reviewer_config, worktree_path, prompt = adapter.invocations[0]
        assert reviewer_config.name == AUDITOR_NAME
        assert reviewer_config.model == AUDITOR_MODEL
        assert reviewer_config.thinking == AUDITOR_THINKING
        assert reviewer_config.permissions == AUDITOR_PERMISSIONS
        # Workspace is a tempdir, not the repo_root
        assert worktree_path != tmp_path
        # Prompt must reference the PR doc filename
        assert pr_doc.name in prompt

    def test_invalid_adapter_fails_at_missing_method(self, tmp_path):
        pr_doc = _pr_doc(tmp_path)

        with pytest.raises(AttributeError, match="build_invocation"):
            run_spec_audit(
                pr_doc_path=pr_doc,
                repo_root=tmp_path,
                timeout_seconds=10.0,
                adapter=object(),  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# FakeAuditorAdapter — unit tests on the test double itself
# ---------------------------------------------------------------------------


class TestFakeAuditorAdapter:
    def test_default_output_is_ready(self):
        adapter = FakeAuditorAdapter()
        assert adapter.canned_output.verdict == "READY"
        assert adapter.canned_output.findings == []

    def test_custom_output_used(self):
        output = _output_with_blocker()
        adapter = FakeAuditorAdapter(canned_output=output)
        assert adapter.canned_output.verdict == "NEEDS-CLARIFICATION"

    def test_extract_returns_model_dump_json_roundtrip(self, tmp_path):
        """extract_final_text produces JSON parseable by
        parse_spec_audit_output — ensures the real parser path is exercised
        in tests that use the fake."""
        from syncade.process import SubprocessResult

        adapter = FakeAuditorAdapter(canned_output=_ready_output())
        fake_result = SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)
        text = adapter.extract_final_text(
            fake_result,
            empty_output_exception_class=SpecAuditOutputError,
        )
        parsed = parse_spec_audit_output(text)
        assert parsed.verdict == "READY"
        assert adapter.extract_calls == 1

    def test_canned_exception_propagates(self, tmp_path):
        from syncade.process import SubprocessResult

        exc = SpecAuditOutputError("bad output")
        adapter = FakeAuditorAdapter(canned_exception=exc)
        fake_result = SubprocessResult(returncode=0, stdout="", stderr="", duration_seconds=0.0)
        with pytest.raises(SpecAuditOutputError):
            adapter.extract_final_text(
                fake_result,
                empty_output_exception_class=SpecAuditOutputError,
            )
