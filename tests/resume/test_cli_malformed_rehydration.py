from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from syncade.cli import main
from tests.cli._helpers import _init_git_repo
from tests.resume._helpers import _write_loop_manifest, _write_round_manifest, _write_run_init

MalformedCase = Literal[
    "reviewer_missing_name",
    "reviewer_non_string_name",
    "snapshot_diff_present",
]


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_malformed_completed_round(repo: Path, case: MalformedCase) -> str:
    run_id = "2026-05-28T10-00-00"
    pr_doc = repo / "pr.md"
    pr_doc.write_text("# PR\n", encoding="utf-8")
    run_dir = repo / ".syncade" / "runs" / run_id
    current_head = _head(repo)
    _write_run_init(
        run_dir,
        starting_sha=current_head,
        operator_branch=_branch(repo),
        max_rounds=2,
        pr_doc_path="pr.md",
    )
    round_dir = _write_round_manifest(
        run_dir,
        0,
        snapshot_sha=current_head,
        round_exit_code=0,
    )
    manifest_path = round_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    match case:
        case "reviewer_missing_name":
            manifest["reviewers"] = [{"outcome": "success"}]
        case "reviewer_non_string_name":
            manifest["reviewers"] = [
                {
                    "name": 123,
                    "provider": "anthropic",
                    "verdict": "SHIP",
                    "finding_count": 0,
                    "duration_seconds": 5.0,
                    "outcome": "success",
                    "error_type": None,
                }
            ]
            (round_dir / "123.parsed.json").write_text(
                json.dumps(
                    {
                        "verdict": "SHIP",
                        "findings": [],
                        "summary": "clean",
                        "priority_order": [],
                        "coverage_gaps": [],
                        "dismissed_concerns": [],
                    }
                ),
                encoding="utf-8",
            )
        case "snapshot_diff_present":
            manifest["snapshot"]["diff_present"] = "yes"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _write_loop_manifest(run_dir, final_exit_code=60)
    return run_id


@pytest.mark.parametrize(
    ("case", "message_fragment"),
    [
        ("reviewer_missing_name", "reviewers"),
        ("reviewer_non_string_name", "reviewer name"),
        ("snapshot_diff_present", "snapshot.diff_present"),
    ],
)
def test_resume_malformed_completed_round_rehydration_exits_60_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: MalformedCase,
    message_fragment: str,
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    run_id = _write_malformed_completed_round(repo, case)

    rc = main(["--repo-root", str(repo), "--resume", run_id])

    captured = capsys.readouterr()
    assert rc == 60
    assert message_fragment in captured.err
    assert "Traceback" not in captured.err


def test_resume_threads_preset_to_load_config(tmp_path, monkeypatch):
    """R3-B1: ``--resume --preset X`` applies the preset (it was silently ignored — resume called
    ``load_config`` without ``preset=``). Capture the ``preset`` kwarg resume hands to load_config;
    that the preset then changes the config is covered by tests/config/test_presets.py."""
    from types import SimpleNamespace

    import syncade.cli.resume_mode as rm

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    captured: dict = {}

    class _Stop(Exception):
        pass

    def _capture(repo_root, **kwargs):
        captured["preset"] = kwargs.get("preset")
        raise _Stop  # stop before the resume-plan machinery — we only assert the threading

    monkeypatch.setattr(rm, "load_config", _capture)
    with pytest.raises(_Stop):
        rm._run_resume(SimpleNamespace(repo_root=str(repo), preset="thorough"))
    assert captured["preset"] == "thorough"
