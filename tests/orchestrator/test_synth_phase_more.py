"""Orchestrator synth-phase tests — part 2 of 2.

The heavyweight production-cold-invariant methods of ``TestSynthesizerPhase``
(split out of the giant single class). These exercise the real cold-synth
env-scrub / isolated-tempdir path with the real ``CodexAdapter`` and patch
``syncade.synthesizer`` internals. Same class name as ``test_synth_phase.py``
(intentional — distinct node paths).
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from syncade.dispatcher import ReviewerRunResult
from tests.orchestrator._helpers import FakeAdapter, _no_ship, _ship

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


class TestSynthesizerPhase:
    """PR-7 task 5: the cold Codex synthesizer phase fires after the
    reviewer dispatch succeeds, is skipped when any reviewer fails,
    and its own failure modes map to 70 (parse) / 40 (subprocess) per
    the decision table."""

    def test_production_synth_invocation_does_not_expose_repo_root(
        self, repo_with_pr_doc, monkeypatch
    ):
        """R2.1 cold-isolation regression: the production-shape synth
        invocation does NOT expose ``repo_root`` in argv, cwd, prompt,
        OR env.

        This pins the four layers of the cold-isolation invariant:

        1. ``argv`` — codex's ``-C`` / ``--add-dir`` flags must
           reference the workspace tempdir, NOT repo_root.
        2. ``cwd`` — the subprocess's working directory must be the
           workspace, NOT repo_root.
        3. ``prompt`` — the ``pr_doc_path`` placeholder in the
           rendered prompt must reference the workspace copy of the
           PR doc, NOT the original repo-side path.
        4. ``env`` — no path inside ``repo_root`` may leak. PWD,
           OLDPWD, repo-local scalar vars, and repo-local PATH
           segments are scrubbed via ``_scrub_env_for_cold_synth``.

        Approach: monkeypatch ``syncade.synthesizer.driver.run_subprocess``
        to capture what got passed (instead of actually shelling
        out), then run with the REAL ``CodexAdapter`` so the
        production flag/env construction is exercised end-to-end.
        Also fake ``_init_workspace_git`` so we don't depend on git
        being on PATH for this specific check.
        """
        import syncade.synthesizer.driver as synth_driver
        from syncade.adapters.openai import CodexAdapter
        from syncade.process import SubprocessResult

        captured: dict = {}

        def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
            captured["argv"] = list(argv)
            captured["cwd"] = cwd
            captured["env"] = dict(env)
            captured["input_text"] = input_text
            # Minimal valid codex JSONL stream so the parser path
            # runs through cleanly (even if we don't care about the
            # result here).
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "t"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "i0",
                                "type": "agent_message",
                                "text": json.dumps(
                                    {
                                        "consolidated_findings": [],
                                        "synthesis_summary": ("nothing to consolidate"),
                                    }
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        }
                    ),
                ]
            )
            return SubprocessResult(
                returncode=0,
                stdout=stdout,
                stderr="",
                duration_seconds=0.1,
            )

        def fake_init_workspace_git(workspace):
            # No-op: git isn't required for the env/argv/cwd/prompt
            # assertions below.
            del workspace

        monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
        monkeypatch.setattr(synth_driver, "_init_workspace_git", fake_init_workspace_git)

        # Plant a sentinel env var whose value contains repo_root
        # so we can assert env-scrub actually fires on real values.
        repo, pr_doc = repo_with_pr_doc
        repo_str = str(repo.resolve())
        monkeypatch.setenv("SYNCADE_SCRUB_SENTINEL", f"{repo_str}/some/path")
        # Also explicitly set PWD to mimic the shell — env-scrub
        # drops it unconditionally regardless of value.
        monkeypatch.setenv("PWD", repo_str)
        # Regression: sibling paths like "<repo>-dev" contain the repo
        # string as a prefix but are NOT inside the repo. They must not
        # cause the entire PATH to be dropped.
        sibling_bin = repo.parent / f"{repo.name}-dev" / "bin"
        repo_local_bin = repo / ".venv" / "bin"
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join([str(sibling_bin), str(repo_local_bin), "/usr/local/bin"]),
        )

        from syncade.synthesizer import SynthesizerResult, run_synthesizer

        adapters = [
            FakeAdapter(canned_output=_no_ship()),  # 1 finding
            FakeAdapter(canned_output=_ship()),
        ]
        # Use a real CodexAdapter — that's the whole point. The
        # monkeypatched run_subprocess + git-init mean we don't
        # actually shell out.
        result = run_synthesizer(
            reviewer_results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_no_ship(),
                    error=None,
                    duration_seconds=1.0,
                ),
            ],
            repo_root=repo,
            pr_doc_path=pr_doc,
            timeout_seconds=60.0,
            adapter=CodexAdapter(),
        )
        # Suppress unused-variable lint while still surfacing the
        # synth result type for future-readers debugging.
        del adapters
        assert isinstance(result, SynthesizerResult)

        # 1. argv (codex flags) — none may contain repo_root.
        for arg in captured["argv"]:
            assert repo_str not in arg, f"R2.1 regression: argv contains repo_root: {arg!r}"

        # 2. cwd — must NOT be repo_root.
        cwd_str = str(captured["cwd"])
        assert repo_str not in cwd_str, f"R2.1 regression: cwd contains repo_root: {cwd_str!r}"
        assert "syncade-synth-" in cwd_str, (
            f"R2.1 regression: cwd is not a syncade-synth workspace: {cwd_str!r}"
        )

        # 3. prompt on stdin (NOT argv, PR-h-field-01 item 1) — must not reference the
        # repo-side pr_doc path.
        prompt = captured["input_text"]
        original_pr_doc_str = str(pr_doc.resolve())
        # The prompt should reference the workspace copy of the PR
        # doc, not the original. The original-path string should
        # not appear in the prompt at all.
        assert original_pr_doc_str not in prompt, (
            f"R2.1 regression: prompt references original PR doc path: "
            f"{original_pr_doc_str!r} found in prompt"
        )
        # Defensive: the prompt should reference SOMETHING under
        # the workspace tempdir.
        assert cwd_str in prompt, (
            "R2.1 regression: prompt's pr_doc_path placeholder doesn't reference the workspace"
        )

        # 4. env — no path inside repo_root may leak.
        for key, value in captured["env"].items():
            if key == "PATH":
                for segment in value.split(os.pathsep):
                    assert segment != repo_str
                    assert not segment.startswith(repo_str + os.sep), (
                        f"R2.1 regression: env[{key!r}] segment {segment!r} "
                        f"leaks repo_root {repo_str!r}"
                    )
                continue
            assert repo_str not in value, (
                f"R2.1 regression: env[{key!r}] = {value!r} leaks repo_root {repo_str!r}"
            )
        # PWD / OLDPWD must be absent (scrubbed unconditionally).
        assert "PWD" not in captured["env"]
        assert "OLDPWD" not in captured["env"]
        # The planted sentinel containing repo_root must be gone too.
        assert "SYNCADE_SCRUB_SENTINEL" not in captured["env"]
        path_segments = captured["env"]["PATH"].split(os.pathsep)
        assert str(sibling_bin) in path_segments
        assert "/usr/local/bin" in path_segments
        assert str(repo_local_bin) not in path_segments

    def test_pr_doc_and_master_plan_basename_collision_kept_separate(
        self, repo_with_pr_doc, monkeypatch, tmp_path
    ):
        """R2.7: when the PR doc and the master plan have IDENTICAL
        basenames (e.g. ``acme/pr-7/spec.md`` and
        ``acme/master/spec.md`` — common in real repos),
        ``shutil.copy2`` to the same workspace path would silently
        clobber the first with the second. Fix: each input lives in
        its own subdir (``workspace/pr-doc/`` and
        ``workspace/master-plan/``); both prompt paths must be
        distinct AND contents preserved.
        """
        import json as _json

        import syncade.synthesizer.driver as synth_driver
        from syncade.process import SubprocessResult

        # Plant a second file with the SAME basename as the PR doc,
        # but in a different directory and with different contents.
        repo, pr_doc = repo_with_pr_doc
        pr_doc_content = "PR DOC CONTENT — must survive\n"
        pr_doc.write_text(pr_doc_content)

        master_plan_dir = tmp_path / "master-side"
        master_plan_dir.mkdir()
        # Same basename, different content.
        master_plan_path = master_plan_dir / pr_doc.name
        master_plan_content = "MASTER PLAN CONTENT — must also survive\n"
        master_plan_path.write_text(master_plan_content)
        assert pr_doc.name == master_plan_path.name  # collision setup

        captured: dict = {}

        def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
            captured["argv"] = list(argv)
            captured["cwd"] = cwd
            # Capture file contents BEFORE the with-block cleanup
            # destroys the workspace. The synth subprocess sees the
            # workspace files; we read them here to confirm both
            # survived the basename collision.
            pr_doc_workspace = cwd / "pr-doc" / pr_doc.name
            master_workspace = cwd / "master-plan" / master_plan_path.name
            captured["pr_doc_workspace_content"] = pr_doc_workspace.read_text()
            captured["master_workspace_content"] = master_workspace.read_text()
            captured["input_text"] = input_text
            del env, timeout
            stdout = "\n".join(
                [
                    _json.dumps({"type": "thread.started", "thread_id": "t"}),
                    _json.dumps({"type": "turn.started"}),
                    _json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "i0",
                                "type": "agent_message",
                                "text": _json.dumps(
                                    {
                                        "consolidated_findings": [],
                                        "synthesis_summary": "ok",
                                    }
                                ),
                            },
                        }
                    ),
                    _json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        }
                    ),
                ]
            )
            return SubprocessResult(
                returncode=0,
                stdout=stdout,
                stderr="",
                duration_seconds=0.1,
            )

        monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
        monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

        from syncade.adapters.openai import CodexAdapter
        from syncade.synthesizer import run_synthesizer

        run_synthesizer(
            reviewer_results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_no_ship(),
                    error=None,
                    duration_seconds=1.0,
                ),
            ],
            repo_root=repo,
            pr_doc_path=pr_doc,
            timeout_seconds=60.0,
            master_plan_path=master_plan_path,
            adapter=CodexAdapter(),
        )

        # Both files survived the copy — basename collision did NOT
        # cause one to clobber the other.
        assert captured["pr_doc_workspace_content"] == pr_doc_content
        assert captured["master_workspace_content"] == master_plan_content

        # The prompt's pr_doc_path and master_plan_path placeholders
        # reference DISTINCT paths (different subdirs in the workspace).
        prompt = captured["input_text"]
        cwd_str = str(captured["cwd"])
        # Both subdirs appear in the rendered prompt
        assert f"{cwd_str}/pr-doc/{pr_doc.name}" in prompt
        assert f"{cwd_str}/master-plan/{master_plan_path.name}" in prompt

    def test_production_synth_uses_trusted_execute_permissions_not_yolo(
        self, repo_with_pr_doc, monkeypatch
    ):
        """R2.1: SYNTHESIZER_PERMISSIONS is now ``trusted-execute`` so the
        codex sandbox is ACTIVE (scoped to the workspace) instead
        of bypassed via ``--dangerously-bypass-approvals-and-sandbox``.
        Pin the argv shape — if a future refactor flips back to
        yolo, this surfaces it."""
        import syncade.synthesizer.driver as synth_driver
        from syncade.adapters.openai import CodexAdapter
        from syncade.process import SubprocessResult

        captured: dict = {}

        def fake_run_subprocess(argv, *, cwd, env, timeout, input_text):
            captured["argv"] = list(argv)
            del cwd, env, timeout, input_text
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "t"}),
                    json.dumps({"type": "turn.started"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "i0",
                                "type": "agent_message",
                                "text": json.dumps(
                                    {
                                        "consolidated_findings": [],
                                        "synthesis_summary": "ok",
                                    }
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        }
                    ),
                ]
            )
            return SubprocessResult(
                returncode=0,
                stdout=stdout,
                stderr="",
                duration_seconds=0.1,
            )

        monkeypatch.setattr(synth_driver, "run_subprocess", fake_run_subprocess)
        monkeypatch.setattr(synth_driver, "_init_workspace_git", lambda workspace: None)

        repo, pr_doc = repo_with_pr_doc
        from syncade.synthesizer import run_synthesizer

        run_synthesizer(
            reviewer_results=[
                ReviewerRunResult(
                    reviewer_name="rv1",
                    provider="anthropic",
                    output=_no_ship(),
                    error=None,
                    duration_seconds=1.0,
                ),
            ],
            repo_root=repo,
            pr_doc_path=pr_doc,
            timeout_seconds=60.0,
            adapter=CodexAdapter(),
        )

        # Trusted-execute mode uses `-s workspace-write -c approval_policy=never`
        # — NOT the combined yolo bypass flag.
        assert "--dangerously-bypass-approvals-and-sandbox" not in captured["argv"], (
            "R2.1 regression: synth is back on yolo permissions, bypassing the codex sandbox"
        )
        assert "-s" in captured["argv"]
        sandbox_idx = captured["argv"].index("-s")
        assert captured["argv"][sandbox_idx + 1] == "workspace-write"
        assert "approval_policy=never" in captured["argv"]
