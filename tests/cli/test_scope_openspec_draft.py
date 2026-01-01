"""CLI-surface tests: ``--scope`` (PR-B), ``--openspec`` (PR-C), and
``--draft-spec`` / ``--transcript`` (PR-D).

Split out of the monolithic ``tests/test_cli.py`` (PR-R3).
"""

import json
import subprocess
from pathlib import Path

import pytest

from syncade.cli import main
from syncade.config import SyncadeConfig
from tests.cli._helpers import _init_git_repo

# ---------------------------------------------------------------------------
# PR-B: --scope flag (base/scope resolution)
# ---------------------------------------------------------------------------


def _rev(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name + "\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True)


def test_scope_base_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--scope", "everything", "--base", "main", "x.md"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_scope_resume_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--scope", "everything", "--resume", "r"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_invalid_scope_value_exits_2(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["--repo-root", str(tmp_path), "--scope", "bogus", "x.md"])
    assert exc.value.code == 2


def test_resolve_scope_base_everything(tmp_path):
    from syncade.cli import _resolve_scope_base
    from syncade.logging import Logger

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    branch_point = _rev(repo)  # default-branch tip before the feature branch
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    _commit(repo, "f.txt")
    assert _resolve_scope_base(repo, "everything", Logger(level="quiet")) == branch_point


def test_resolve_scope_base_since_last_review_reads_record(tmp_path):
    from syncade.cli import _resolve_scope_base
    from syncade.logging import Logger
    from syncade.persistence import persist_last_reviewed

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    recorded = _rev(repo)
    _commit(repo, "g.txt")
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    persist_last_reviewed(repo, branch=branch, sha=recorded, run_id="r", recorded_at_utc="t")
    assert _resolve_scope_base(repo, "since-last-review", Logger(level="quiet")) == recorded


def test_resolve_scope_base_unresolvable_returns_none(tmp_path, capsys):
    from syncade.cli import _resolve_scope_base
    from syncade.logging import Logger

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "wip", str(repo)], check=True)
    for cmd in (["config", "user.email", "t@e.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    _commit(repo, "a.txt")  # no main/master branch exists
    assert _resolve_scope_base(repo, "everything", Logger(level="quiet")) is None


def test_resolve_scope_base_note_emitted_in_quiet_mode(tmp_path, capsys):
    """Scope fallback notes must reach stderr even when --quiet suppresses logger.warning."""
    from syncade.cli import _resolve_scope_base
    from syncade.logging import Logger

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    branch_point = _rev(repo)
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    _commit(repo, "f.txt")

    # since-last-review with no record → fallback note; must appear in stderr in quiet mode
    result = _resolve_scope_base(repo, "since-last-review", Logger(level="quiet"))
    assert result == branch_point
    captured = capsys.readouterr()
    assert "scope:" in captured.err and "branch point" in captured.err


# --- PR-C: --openspec (OpenSpec tier-B consumption) -------------------------


def _make_openspec_change(repo: Path, change_id: str, *, proposal: str, delta: str | None = None):
    d = repo / "openspec" / "changes" / change_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "proposal.md").write_text(proposal, encoding="utf-8")
    if delta is not None:
        sp = d / "specs" / "cap"
        sp.mkdir(parents=True, exist_ok=True)
        (sp / "spec.md").write_text(delta, encoding="utf-8")


def test_openspec_pr_doc_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--openspec", "add-x", "brief.md"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_openspec_resume_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--openspec", "add-x", "--resume", "r"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_openspec_selfcheck_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--openspec", "add-x", "--selfcheck"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_openspec_auth_check_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--openspec", "add-x", "--auth-check"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_openspec_spec_audit_mutex_returns_2(tmp_path, capsys):
    # Dogfood datapoint-3 finding (run 2026-06-02T10-59-49): the 5th mutex combo
    # (--openspec + --spec-audit) was implemented but untested. Close the gap.
    rc = main(["--repo-root", str(tmp_path), "--openspec", "add-x", "--spec-audit", "some.md"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_resolve_openspec_pr_doc_assembles_explicit(tmp_path):
    from syncade.cli import _resolve_openspec_pr_doc
    from syncade.logging import Logger

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_openspec_change(
        repo,
        "add-auth",
        proposal="## Why\n\nUsers need login.\n",
        delta="## ADDED Requirements\n### Requirement: Login\n#### Scenario: ok\n",
    )
    path = _resolve_openspec_pr_doc(repo, "add-auth", Logger(level="quiet"))
    assert path is not None and path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "Users need login." in content
    assert "### Requirement: Login" in content
    assert "add-auth" in content


def test_resolve_openspec_pr_doc_auto_single(tmp_path):
    from syncade.cli import _resolve_openspec_pr_doc
    from syncade.logging import Logger

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_openspec_change(repo, "only-change", proposal="## Why\n\nSolo.\n")
    # change_id=None (bare --openspec) auto-resolves the single active change
    path = _resolve_openspec_pr_doc(repo, None, Logger(level="quiet"))
    assert path is not None and "Solo." in path.read_text(encoding="utf-8")


def test_resolve_openspec_pr_doc_unresolvable_returns_none(tmp_path, capsys):
    from syncade.cli import _resolve_openspec_pr_doc
    from syncade.logging import Logger

    repo = tmp_path / "repo"
    repo.mkdir()
    # no openspec/ folder at all → SpecSourceError → None + message
    result = _resolve_openspec_pr_doc(repo, None, Logger(level="quiet"))
    assert result is None
    assert "openspec error" in capsys.readouterr().err


def test_openspec_temp_pr_doc_removed_after_run_and_artifact_name_passed(tmp_path, monkeypatch):
    import types

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _make_openspec_change(repo, "add-auth", proposal="## Why\n\nUsers need login.\n")
    consumed: dict[str, Path | str] = {}

    def fake_run_review(**kwargs):
        pr_doc_path = kwargs["pr_doc_path"]
        consumed["path"] = pr_doc_path
        consumed["body"] = pr_doc_path.read_text(encoding="utf-8")
        consumed["artifact_name"] = kwargs["pr_doc_artifact_name"]
        assert pr_doc_path.is_file()
        return types.SimpleNamespace(
            exit_code=0,
            artifacts=types.SimpleNamespace(run_dir=repo / ".syncade" / "runs" / "x"),
            dispatch_result=types.SimpleNamespace(results=[]),
        )

    monkeypatch.setattr("syncade.cli.run_review", fake_run_review)

    rc = main(["--repo-root", str(repo), "--openspec", "add-auth"])

    assert rc == 0
    temp_path = consumed["path"]
    assert isinstance(temp_path, Path)
    assert "Users need login." in consumed["body"]
    assert consumed["artifact_name"] == temp_path.name
    assert not temp_path.exists()


# --- PR-D: --draft-spec (cold-drafter front door) ---------------------------


def _write_transcript(repo: Path, name: str = "sess.jsonl") -> Path:
    entries = [
        {
            "type": "user",
            "isSidechain": False,
            "message": {"role": "user", "content": "Add a --greet flag."},
        },
        {
            "type": "assistant",
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I'll add --greet."}],
            },
        },
    ]
    p = repo / name
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return p


def test_draft_spec_requires_transcript_returns_2(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    rc = main(["--repo-root", str(repo), "--draft-spec"])
    assert rc == 2
    assert "requires --transcript" in capsys.readouterr().err


def test_draft_spec_pr_doc_mutex_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--draft-spec", "--transcript", "x.jsonl", "foo.md"])
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_draft_spec_openspec_mutex_returns_2(tmp_path, capsys):
    rc = main(
        [
            "--repo-root",
            str(tmp_path),
            "--draft-spec",
            "--transcript",
            "x.jsonl",
            "--openspec",
            "ch",
        ]
    )
    assert rc == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_transcript_requires_draft_spec_returns_2(tmp_path, capsys):
    rc = main(["--repo-root", str(tmp_path), "--transcript", "x.jsonl", "foo.md"])
    assert rc == 2
    assert "requires --draft-spec" in capsys.readouterr().err


def test_draft_spec_missing_transcript_file_returns_60(tmp_path, capsys):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    rc = main(["--repo-root", str(repo), "--draft-spec", "--transcript", str(repo / "nope.jsonl")])
    assert rc == 60
    assert "transcript error" in capsys.readouterr().err


def test_draft_spec_end_to_end_writes_ratifiable_file(tmp_path, capsys, monkeypatch):
    import syncade.spec_draft as sd

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    transcript = _write_transcript(repo)
    canned = sd.SpecDraftOutput(
        proposal="Add a --greet flag that prints hello.",
        acceptance_criteria=[
            sd.Criterion(text="--greet prints hello", origin="transcribed"),
            sd.Criterion(text="exit code is 0", origin="inferred"),
        ],
        assumptions=["Assumed it writes to stdout."],
    )

    def _fake_run(*, dialogue, diff, repo_root, timeout_seconds, config=None):
        # the parsed transcript dialogue must reach the drafter
        assert "Add a --greet flag." in dialogue
        # PR-v2-23: the CLI must hand the drafter its [drafter] config block, not
        # None — otherwise run_spec_draft silently falls back to its own defaults
        # and a user's `[drafter] provider = "anthropic"` would be ignored.
        assert config is not None
        assert config == SyncadeConfig().drafter
        return sd.SpecDraftResult(
            outcome="drafted", output=canned, error=None, duration_seconds=0.1
        )

    monkeypatch.setattr(sd, "run_spec_draft", _fake_run)
    rc = main(["--repo-root", str(repo), "--draft-spec", "--transcript", str(transcript)])
    assert rc == 0
    spec = repo / ".syncade" / f"draft-spec-{transcript.stem}.md"
    assert spec.is_file()
    text = spec.read_text(encoding="utf-8")
    assert "Assumptions to confirm" in text
    assert "exit code is 0" in text  # the inferred criterion is surfaced
    assert "Assumed it writes to stdout." in text


def test_draft_spec_transcript_relative_path_resolves_from_repo_root(tmp_path, capsys, monkeypatch):
    import syncade.spec_draft as sd

    repo = tmp_path / "repo"
    other_cwd = tmp_path / "elsewhere"
    _init_git_repo(repo)
    other_cwd.mkdir()
    transcripts_dir = repo / "transcripts"
    transcripts_dir.mkdir()
    transcript = _write_transcript(transcripts_dir)
    canned = sd.SpecDraftOutput(
        proposal="Add --greet flag.",
        acceptance_criteria=[sd.Criterion(text="--greet prints hello", origin="transcribed")],
    )
    called_with: dict = {}

    def _fake_run(*, dialogue, diff, repo_root, timeout_seconds, config=None):
        called_with["dialogue"] = dialogue
        called_with["repo_root"] = repo_root
        return sd.SpecDraftResult(
            outcome="drafted", output=canned, error=None, duration_seconds=0.1
        )

    monkeypatch.setattr(sd, "run_spec_draft", _fake_run)
    monkeypatch.chdir(other_cwd)

    rc = main(["--repo-root", str(repo), "--draft-spec", "--transcript", "transcripts/sess.jsonl"])

    assert rc == 0
    assert "Add a --greet flag." in called_with["dialogue"]
    assert called_with["repo_root"] == repo
    assert (repo / ".syncade" / f"draft-spec-{transcript.stem}.md").is_file()


def test_draft_spec_existing_output_is_preserved_and_unique_path_is_written(
    tmp_path, capsys, monkeypatch
):
    import syncade.spec_draft as sd

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    transcript = _write_transcript(repo)
    out_dir = repo / ".syncade"
    out_dir.mkdir()
    existing = out_dir / f"draft-spec-{transcript.stem}.md"
    existing.write_text("keep me\n", encoding="utf-8")
    canned = sd.SpecDraftOutput(
        proposal="Add --greet flag.",
        acceptance_criteria=[sd.Criterion(text="--greet prints hello", origin="transcribed")],
    )

    def _fake_run(*, dialogue, diff, repo_root, timeout_seconds, config=None):
        return sd.SpecDraftResult(
            outcome="drafted", output=canned, error=None, duration_seconds=0.1
        )

    monkeypatch.setattr(sd, "run_spec_draft", _fake_run)

    rc = main(["--repo-root", str(repo), "--draft-spec", "--transcript", str(transcript)])

    assert rc == 0
    assert existing.read_text(encoding="utf-8") == "keep me\n"
    written = sorted(out_dir.glob(f"draft-spec-{transcript.stem}*.md"))
    assert len(written) == 2
    assert any(path != existing and "--greet prints hello" in path.read_text() for path in written)


def test_draft_spec_with_base_carries_base_into_ratification_instruction(
    tmp_path, capsys, monkeypatch
):
    """When --base is given, the rendered .md and CLI print both include --base <ref>."""
    import syncade.spec_draft as sd

    repo = tmp_path / "repo"
    _init_git_repo(repo)
    transcript = _write_transcript(repo)
    canned = sd.SpecDraftOutput(
        proposal="Add --greet flag.",
        acceptance_criteria=[sd.Criterion(text="--greet prints hello", origin="transcribed")],
    )

    # Patch take_snapshot so --base doesn't need a real git history
    from syncade import snapshot as snap_mod

    def _fake_snapshot(repo_root, *, base_ref):
        from syncade.snapshot import Snapshot

        return Snapshot(
            repo_root=repo_root,
            commit_sha="abc1234",
            branch="main",
            base_ref=base_ref,
            diff_text=f"diff --git a/cli.py b/cli.py\n+--base {base_ref}\n",
            dirty_state=None,
        )

    monkeypatch.setattr(snap_mod, "take_snapshot", _fake_snapshot)

    def _fake_run(*, dialogue, diff, repo_root, timeout_seconds, config=None):
        return sd.SpecDraftResult(
            outcome="drafted", output=canned, error=None, duration_seconds=0.1
        )

    monkeypatch.setattr(sd, "run_spec_draft", _fake_run)

    rc = main(
        [
            "--repo-root",
            str(repo),
            "--draft-spec",
            "--transcript",
            str(transcript),
            "--base",
            "HEAD~1",
        ]
    )
    assert rc == 0

    spec = repo / ".syncade" / f"draft-spec-{transcript.stem}.md"
    text = spec.read_text(encoding="utf-8")
    # The rendered markdown must include the --base flag in the ratification instruction
    assert "--base HEAD~1" in text

    out = capsys.readouterr().out
    # The CLI print must include --base HEAD~1 in the "then run:" line
    assert "--base HEAD~1" in out
