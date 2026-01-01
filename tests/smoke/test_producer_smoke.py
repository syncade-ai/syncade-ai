"""End-to-end producer smoke against real ``claude`` + ``codex`` (PR-8).

Exercises :func:`syncade.producer.run_producer` against real LLM
subprocesses to confirm the producer adapter argv, parse path, and
stall-detection signals all compose against the actual CLIs.

Gated behind ``@pytest.mark.smoke``. Default ``pytest`` runs deselect
via ``addopts = "-m 'not smoke'"``. Tests skip cleanly when the
required CLI is missing.

Each test runs ONE real LLM subprocess + a small git working tree.
Test runtime is typically 10-40 seconds per real-adapter test
(haiku / gpt-5.5 at low thinking); the per-producer-round timeout
is the CLI default of 1800s but real runs land far below it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from syncade.adapters.producer_anthropic import AnthropicProducerAdapter
from syncade.adapters.producer_openai import OpenAIProducerAdapter
from syncade.config import ProducerConfig
from syncade.producer import run_producer


def _skip_if_missing(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        pytest.skip(f"required CLIs not on PATH: {', '.join(missing)}")


def _seed_repo_with_bug(tmp_path: Path) -> tuple[str, Path, Path]:
    """Init a tiny git working tree at ``tmp_path`` with a
    deliberate null-pointer bug in ``foo.py``. Returns
    ``(starting_sha, pr_doc_path, findings_md_path)``.

    The bug is small and self-evident so the producer doesn't
    need much spec context to fix it:

    .. code-block:: python

       def add_one(x):
           return x + 1  # crashes if x is None

    The PR doc + findings.md both describe the fix (handle None
    gracefully — return None when x is None).
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.com"],
        ["config", "user.name", "Smoke"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)

    (tmp_path / "foo.py").write_text(
        "def add_one(x):\n    # crashes if x is None — should handle that case\n    return x + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed: introduce null-pointer bug"],
        cwd=tmp_path,
        check=True,
    )
    starting_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text(
        "# Synthetic PR — null-pointer fix\n\n"
        "**Goal:** Make `foo.add_one(x)` handle `x is None` "
        "gracefully (return None when given None; otherwise return "
        "x + 1).\n",
        encoding="utf-8",
    )

    findings_dir = tmp_path / ".syncade" / "runs" / "smoke" / "round-0"
    findings_dir.mkdir(parents=True)
    findings_md = findings_dir / "findings.md"
    findings_md.write_text(
        "# Findings — null-pointer fix\n\n"
        "**Verdict:** NO-SHIP\n\n"
        "## Findings\n\n"
        "### [blocker] foo.add_one crashes on None input\n\n"
        "**File:** `foo.py`  \n"
        "**Synthesizer severity:** blocker\n\n"
        "The function signature implies `x` is always a numeric "
        "value; but the PR spec asks for graceful None handling. "
        "Add a `if x is None: return None` guard at the top.\n",
        encoding="utf-8",
    )
    return starting_sha, pr_doc, findings_md


@pytest.mark.smoke
def test_anthropic_producer_commits_a_fix(tmp_path):
    """Real ``claude`` producer + a real null-pointer bug. The
    producer should make file edits, commit them, and report a
    moved HEAD. The bug in foo.py is small enough that haiku at
    low thinking can fix it in 10-30 seconds."""
    _skip_if_missing("claude")

    starting_sha, pr_doc, findings_md = _seed_repo_with_bug(tmp_path)

    result = run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings_md,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(
            provider="anthropic",
            # Use the "sonnet" alias which the local ``claude`` CLI
            # resolves to whichever Sonnet model is currently
            # default. Haiku at low thinking sometimes decides
            # the trivial bug is fine and stalls; Sonnet is more
            # reliable for the committed-outcome assertion.
            model="sonnet",
            thinking="low",
            # Default producer permissions are yolo because real
            # headless claude needs bypassPermissions for the bash
            # `git commit` step; acceptEdits only approves file edits.
            permissions="yolo",
        ),
        timeout_seconds=180.0,
        round_number=0,
        max_rounds=2,
        repo_root=tmp_path,
        adapter=AnthropicProducerAdapter(),
    )

    assert result.outcome == "committed", (
        f"producer did not commit a fix; outcome={result.outcome!r}, "
        f"error={type(result.error).__name__ if result.error else 'None'}"
    )
    assert result.starting_sha == starting_sha
    assert result.ending_sha != starting_sha
    # The fix changed foo.py
    foo_after = (tmp_path / "foo.py").read_text()
    assert "None" in foo_after, (
        f"producer's commit didn't reference None in foo.py; got:\n{foo_after}"
    )


@pytest.mark.smoke
def test_openai_producer_commits_a_fix(tmp_path):
    """Real ``codex`` producer + the same null-pointer bug. Asserts
    the same shape — commit + moved HEAD + None-aware foo.py."""
    _skip_if_missing("codex")

    starting_sha, pr_doc, findings_md = _seed_repo_with_bug(tmp_path)

    result = run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings_md,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(
            provider="openai",
            model="gpt-5.5",
            thinking="low",
            # Real codex with ``-s workspace-write`` (the trusted
            # mapping) blocks writes to ``.git/`` — verified live:
            # the producer
            # successfully edits ``foo.py`` but ``git commit``
            # fails with ``fatal: Unable to create
            # '.git/index.lock': Operation not permitted`` because
            # the sandbox protects ``.git`` from writes even when
            # the workspace itself is writable. Headless committed-
            # outcome therefore requires ``--dangerously-bypass-
            # approvals-and-sandbox`` (``yolo``) for the codex
            # producer too, same as the anthropic case.
            # The ProducerConfig default is therefore yolo for
            # unattended producer commits.
            permissions="yolo",
        ),
        timeout_seconds=180.0,
        round_number=0,
        max_rounds=2,
        repo_root=tmp_path,
        adapter=OpenAIProducerAdapter(),
    )

    assert result.outcome == "committed", (
        f"producer did not commit a fix; outcome={result.outcome!r}, "
        f"error={type(result.error).__name__ if result.error else 'None'}"
    )
    assert result.starting_sha == starting_sha
    assert result.ending_sha != starting_sha
    foo_after = (tmp_path / "foo.py").read_text()
    assert "None" in foo_after


_STALL_FORCING_PRODUCER_TEMPLATE = """SMOKE TEST FIXTURE — producer stall test.

The findings.md at {findings_md_path} describes a finding for
which NO COMMIT should be made under any circumstance. The code
is already correct.

Your job:
1. Emit exactly this narrative: "smoke stall fixture — code is
   already correct, no commit will be made"
2. DO NOT modify any files.
3. DO NOT run `git add`, `git commit`, or any other git write
   command.
4. Return immediately after the narrative.

Inputs (ignored):
- PR: {pr_doc_path}
- Findings: {findings_md_path}
- Test: {test_run_stdout_path}
- Worktree: {worktree_path}
- Round: {round_number} of {max_rounds}
"""


@pytest.mark.smoke
def test_anthropic_producer_stalls_when_template_forbids_commit(tmp_path):
    """PR-8 R2.T8 strict stall: when the producer template
    explicitly forbids making a commit, the producer MUST stall
    (HEAD doesn't move) deterministically. Pre-R2.T8 the smoke
    accepted ``committed`` OR ``stalled`` because model behavior
    was non-deterministic against a "this code is already
    correct" findings.md — the model might decide to commit a
    stylistic refactor anyway. R2.T8 uses a per-repo producer
    template override that's unambiguous, which gives
    deterministic stall behavior.
    """
    _skip_if_missing("claude")

    # Build a repo where foo.py is already None-aware.
    tmp_path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["init", "-q"],
        ["config", "user.email", "smoke@example.com"],
        ["config", "user.name", "Smoke"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)
    (tmp_path / "foo.py").write_text(
        "def add_one(x):\n    if x is None:\n        return None\n    return x + 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "seed: already-fixed foo.py"],
        cwd=tmp_path,
        check=True,
    )
    starting_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    # Install per-repo producer template that pins the stall.
    template_dir = tmp_path / ".syncade" / "templates"
    template_dir.mkdir(parents=True)
    (template_dir / "producer.md").write_text(_STALL_FORCING_PRODUCER_TEMPLATE, encoding="utf-8")

    pr_doc = tmp_path / "pr.md"
    pr_doc.write_text("# PR — content ignored by stall-fixture template\n")
    findings_dir = tmp_path / ".syncade" / "runs" / "smoke" / "round-0"
    findings_dir.mkdir(parents=True)
    findings_md = findings_dir / "findings.md"
    findings_md.write_text("# Findings\n\nSee producer template for instructions.\n")

    result = run_producer(
        worktree_path=tmp_path,
        starting_sha=starting_sha,
        pr_doc_path=pr_doc,
        findings_md_path=findings_md,
        test_run_stdout_path=None,
        producer_config=ProducerConfig(
            provider="anthropic",
            model="sonnet",
            thinking="low",
            permissions="yolo",
        ),
        timeout_seconds=180.0,
        round_number=0,
        max_rounds=2,
        repo_root=tmp_path,
        adapter=AnthropicProducerAdapter(),
    )

    # R2.T8 strict assertion: outcome MUST be "stalled" because
    # the template explicitly forbids committing.
    assert result.outcome == "stalled", (
        f"R2.T8 stall smoke: expected outcome='stalled' (template "
        f"forbids commit); got outcome={result.outcome!r}, "
        f"error={result.error}. If the model committed anyway, "
        f"the template-forbid-commit fixture is not reliably "
        f"deterministic — investigate or strengthen the prompt."
    )
    assert result.ending_sha == starting_sha
