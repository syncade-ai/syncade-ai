"""Regression test against the first real-world syncade run.

The artifacts under ``tests/fixtures/pr-5-real-world-timeout-run/`` are
the verbatim output of the Acme field run that surfaced the three
bugs PR-5.5 fixes — both reviewers SIGKILL'd at the old 600s timeout
floor, exit 40, no useful output. See that directory's README.md.

This test loads the frozen fixture and pins the on-disk layout: the
``manifest.json`` schema and the per-reviewer file naming. If a future
PR renames ``manifest.json`` or changes the ``<name>.{stdout,stderr,
error.txt}`` convention, the fixture stops matching and this fails —
catching the drift the way the field run caught the original bugs.
"""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr-5-real-world-timeout-run"
_ROUND_DIR = _FIXTURE_DIR / "round-0"


class TestAcmeTimeoutRegression:
    def test_fixture_manifest_matches_documented_schema(self):
        manifest = json.loads((_ROUND_DIR / "manifest.json").read_text())
        for key in (
            "syncade_version",
            "run_id",
            "round",
            "started_at_utc",
            "snapshot",
            "reviewers",
            "exit_code",
        ):
            assert key in manifest, f"manifest missing {key!r}"
        # The snapshot sub-object carries the documented fields.
        for key in ("commit_sha", "branch", "base_ref", "diff_present"):
            assert key in manifest["snapshot"], f"snapshot missing {key!r}"
        # The run that surfaced the bugs: both reviewers timed out -> 40.
        assert manifest["exit_code"] == 40
        assert manifest["round"] == 0
        assert len(manifest["reviewers"]) == 2
        for entry in manifest["reviewers"]:
            assert entry["outcome"] == "failure"
            assert entry["error_type"] == "SubprocessTimeoutError"
            assert entry["verdict"] is None
            assert entry["finding_count"] is None

    def test_fixture_per_reviewer_layout_matches_persistence(self):
        """Every reviewer named in the manifest has the .stdout / .stderr
        / .error.txt files persist_reviewer_result writes on the failure
        path. The fixture's .stdout / .stderr are 0 bytes — that empty
        content IS the bug PR-5.5 task 3 fixed; the *set* of files is
        unchanged, and that set is what this test pins."""
        manifest = json.loads((_ROUND_DIR / "manifest.json").read_text())
        for entry in manifest["reviewers"]:
            name = entry["name"]
            assert (_ROUND_DIR / f"{name}.stdout").is_file()
            assert (_ROUND_DIR / f"{name}.stderr").is_file()
            error_path = _ROUND_DIR / f"{name}.error.txt"
            assert error_path.is_file()
            # The error file names the timeout exception class — the
            # same string downstream tooling buckets failures by.
            assert "SubprocessTimeoutError" in error_path.read_text()

    def test_fixture_readme_documents_the_failure(self):
        """The fixture ships with a README explaining what the failed
        run demonstrated (per the PR-5.5 brief, Task 6b)."""
        readme = (_FIXTURE_DIR / "README.md").read_text()
        assert "timeout" in readme.lower()
        assert "600" in readme  # the old hardcoded floor
