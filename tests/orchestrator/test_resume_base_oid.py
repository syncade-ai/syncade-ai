"""Tests for resume base-OID stability (PR-h-02).

Verifies that plan_resume reads base_oid from run-init.json and rejects
malformed values, so a resumed run cannot re-resolve a moved symbolic ref.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from syncade.orchestrator.resume import ResumeError, plan_resume
from tests.orchestrator._resume_fixtures import _prepare_aborted_run

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestResumeBaseOid:
    """PR-h-02: on resume, the stable base OID from run-init.json must be
    passed as base_ref so the resumed run cannot re-resolve a moved
    symbolic ref."""

    def test_plan_resume_populates_base_oid_from_run_init(self, repo_with_pr_doc):
        """plan_resume reads base_oid from run-init.json and stores it on
        ResumePlan.base_oid, so the resume caller can pass it as base_ref
        instead of the potentially-moved symbolic base_ref."""
        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)

        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )

        run_init_path = run_dir / "run-init.json"
        run_init = json.loads(run_init_path.read_text())
        known_oid = "a" * 40
        run_init["base_ref"] = "main"
        run_init["base_oid"] = known_oid
        run_init_path.write_text(json.dumps(run_init))

        plan = plan_resume(repo_root, run_dir)

        assert plan.base_oid == known_oid, (
            f"plan_resume did not read base_oid from run-init.json; got {plan.base_oid!r}"
        )
        assert plan.base_ref == "main"

    def test_plan_resume_legacy_run_with_base_ref_but_no_base_oid_is_refused(
        self, repo_with_pr_doc
    ):
        """plan_resume raises ResumeError when run-init.json has base_ref but no
        base_oid. Resuming such a legacy run would re-resolve the symbolic ref
        under three-dot semantics, producing a different diff identity than the
        earlier rounds. Operators must start a fresh run instead."""
        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)

        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )
        # Inject a legacy run-init: base_ref present, base_oid absent.
        run_init_path = run_dir / "run-init.json"
        run_init = json.loads(run_init_path.read_text())
        run_init["base_ref"] = "main"
        run_init.pop("base_oid", None)
        run_init_path.write_text(json.dumps(run_init))

        with pytest.raises(ResumeError, match="no base_oid"):
            plan_resume(repo_root, run_dir)

    def test_plan_resume_base_oid_none_when_both_absent(self, repo_with_pr_doc):
        """When run-init.json has neither base_ref nor base_oid (no-diff run),
        plan_resume succeeds and ResumePlan.base_oid is None."""
        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "main"], cwd=repo_root, check=False)

        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )
        # No base_ref, no base_oid: a no-diff run. Should succeed.
        run_init_path = run_dir / "run-init.json"
        run_init = json.loads(run_init_path.read_text())
        run_init.pop("base_ref", None)
        run_init.pop("base_oid", None)
        run_init_path.write_text(json.dumps(run_init))

        plan = plan_resume(repo_root, run_dir)
        assert plan.base_oid is None

    def test_plan_resume_raises_on_malformed_base_oid(self, repo_with_pr_doc):
        """plan_resume raises ResumeError when run-init.json has a non-null but
        invalid base_oid (e.g. empty string). Falsy values must not silently
        fall through to the symbolic base_ref."""
        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "feature"], cwd=repo_root, check=False)
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )
        run_init_path = run_dir / "run-init.json"
        run_init = json.loads(run_init_path.read_text())
        run_init["base_oid"] = ""  # non-null but not a valid OID
        run_init_path.write_text(json.dumps(run_init))

        with pytest.raises(ResumeError, match="malformed base_oid"):
            plan_resume(repo_root, run_dir)

    def test_plan_resume_raises_on_non_string_base_oid(self, repo_with_pr_doc):
        """plan_resume raises ResumeError (not TypeError) when run-init.json
        has a non-string base_oid (e.g. a JSON number)."""
        repo_root, pr_doc = repo_with_pr_doc
        subprocess.run(["git", "branch", "-m", "feature"], cwd=repo_root, check=False)
        run_dir, _ = _prepare_aborted_run(
            repo_root,
            pr_doc,
            completed_round_count=1,
            max_rounds=2,
            aborted_round_partial=True,
            aborted_exit_code=40,
        )
        run_init_path = run_dir / "run-init.json"
        run_init = json.loads(run_init_path.read_text())
        run_init["base_oid"] = 12345  # non-string JSON value
        run_init_path.write_text(json.dumps(run_init))

        with pytest.raises(ResumeError, match="malformed base_oid"):
            plan_resume(repo_root, run_dir)
