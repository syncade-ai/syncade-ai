"""Tests for :mod:`syncade.orchestrator`.

Uses :class:`FakeAdapter` exclusively via the ``adapter_factory``
parameter — no real CLI calls. Each test sets up an ephemeral git
repo in ``tmp_path`` so the snapshot + worktree provisioning steps
exercise real git, then injects fakes for the reviewer dispatch.

Total runtime under 5 seconds (the brief's bound).
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from syncade.adapters.fake import FakeAdapter
from syncade.orchestrator import run_review
from tests.orchestrator._helpers import (
    _factory_returning,
    _ship,
    _two_reviewer_config,
)

# Skip everything in this module if git isn't on PATH.
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestLookupSiteMonkeyPatches:
    def test_run_review_resolves_dispatch_reviewers_via_round_module(
        self, repo_with_pr_doc, monkeypatch
    ):
        import syncade.orchestrator as orch
        import syncade.orchestrator.round as round_module

        invocations: list[int] = []
        wrong_site_calls: list[int] = []
        real = round_module.dispatch_reviewers

        def recording(*args, **kwargs):
            invocations.append(len(invocations))
            return real(*args, **kwargs)

        def wrong_site(*args, **kwargs):
            del args, kwargs
            wrong_site_calls.append(len(wrong_site_calls))
            raise AssertionError("package-level dispatch patch should not be used")

        monkeypatch.setattr(orch, "dispatch_reviewers", wrong_site, raising=False)
        monkeypatch.setattr(round_module, "dispatch_reviewers", recording)

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        assert result.exit_code == 0
        assert len(invocations) == 1
        assert wrong_site_calls == []

    def test_run_review_resolves_datetime_via_loop_module(self, repo_with_pr_doc, monkeypatch):
        import syncade.orchestrator as orch
        import syncade.orchestrator.loop as loop_module

        right_time = datetime(2030, 7, 1, 12, 0, 0, tzinfo=UTC)
        wrong_time = datetime(1999, 1, 1, 0, 0, 0, tzinfo=UTC)

        class _RightDatetime:
            @staticmethod
            def now(tz=None):
                del tz
                return right_time

        class _WrongDatetime:
            @staticmethod
            def now(tz=None):
                del tz
                return wrong_time

        monkeypatch.setattr(orch, "datetime", _WrongDatetime, raising=False)
        monkeypatch.setattr(loop_module, "datetime", _RightDatetime)

        repo, pr_doc = repo_with_pr_doc
        adapters = [FakeAdapter(canned_output=_ship()) for _ in range(2)]
        result = run_review(
            repo_root=repo,
            pr_doc_path=pr_doc,
            config=_two_reviewer_config(),
            adapter_factory=_factory_returning(*adapters),
        )

        manifest = json.loads(result.artifacts.manifest_path.read_text())
        assert manifest["started_at_utc"] == "2030-07-01T12:00:00Z"

    def test_run_round_step_uses_loop_round_step_module_globals(self):
        import syncade.orchestrator as orch
        import syncade.orchestrator.loop_round_step as step_module

        assert orch._run_round_step.__globals__ is vars(step_module)
