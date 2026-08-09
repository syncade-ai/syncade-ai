"""Cheap, certain validation of a run's inputs — one predicate, several call sites.

A LEAF (imports only ``pathlib``), and deliberately so. The CLI must run this BEFORE
anything mutates the operator's directory (PR-h-04 item A), and making the CLI import
``orchestrator.loop`` just to check whether a file exists would drag the whole loop in for
a `Path.exists()`. It also keeps ``loop.py`` under the file-length cap.

The value is having ONE implementation: ``run_review`` calls it and so does the CLI, so a
pre-flight cannot refuse what the library would accept. A private copy in the CLI is
precisely the drift PR-h-02d.5 spent four rounds on — and reintroducing one here silently
dropped the "is it a file?" half, which is why a directory passed as the brief slipped
through in calibration.
"""

from __future__ import annotations

from pathlib import Path


def validate_run_inputs(repo_root: Path, pr_doc_path: Path) -> None:
    """Cheap, certain input checks. Raises ``NotADirectoryError`` / ``FileNotFoundError``.

    Lifted out of :func:`run_review` so the CLI can run it BEFORE anything mutates the
    operator's directory (PR-h-04 item A). Previously a mistyped brief path reached this
    point only after ``ensure_repo_initialized`` had created a repo and a baseline commit,
    and in an existing repo the default-branch guard refused FIRST — so a typo was reported
    as a branch problem while the real mistake was a filename.

    It lives here, in the module that owns the authoritative check, and the CLI calls this
    same function: one predicate, two call sites, so the pre-flight cannot drift from what
    ``run_review`` accepts. That drift is the defect PR-h-02d.5 spent four rounds on.
    """
    if not repo_root.exists():
        raise NotADirectoryError(f"repo_root does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise NotADirectoryError(f"repo_root is not a directory: {repo_root}")
    if not pr_doc_path.exists():
        raise FileNotFoundError(f"pr_doc_path does not exist: {pr_doc_path}")
    if not pr_doc_path.is_file():
        raise FileNotFoundError(f"pr_doc_path is not a file: {pr_doc_path}")
