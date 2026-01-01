"""Shared helpers for the ``tests/producer/`` split — small git
worktrees built per test, not shared."""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _git_required():
    """Skip the test when git isn't on PATH (unusual but possible
    in stripped CI environments)."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")


def _seed_repo(tmp_path):
    """Create a minimal repo at ``tmp_path`` with one seed commit.

    Returns the seed commit's full object ID. The producer
    worktree subdir is created by the caller (via _make_worktree
    below) so each test can drive its own worktree at the seed
    SHA.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _read_head(path):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _make_pr_doc(tmp_path):
    """Drop a PR doc in tmp_path so the renderer's placeholder
    substitution has a real path to point at."""
    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR\n\nSpec body.\n", encoding="utf-8")
    return pr_doc


def _make_findings_md(round_dir):
    """Drop a stub findings.md in the round_dir so the producer
    template's {findings_md_path} resolves to a real file."""
    round_dir.mkdir(parents=True, exist_ok=True)
    findings = round_dir / "findings.md"
    findings.write_text("# Findings\n\nblocker: do the thing\n", encoding="utf-8")
    return findings
