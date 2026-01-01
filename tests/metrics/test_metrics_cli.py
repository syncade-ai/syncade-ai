"""`--metrics` CLI wiring (PR-v2-03 Task 3): mutex + no-diff guard.

These exercise the arg-shape validation only (no repo/filesystem work), so they
run without a git repo — every case returns before dispatch reaches the sink.
"""

from __future__ import annotations

from syncade.cli import main


def test_metrics_rejects_pr_doc(capsys):
    assert main(["--metrics", "docs/x.md"]) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_metrics_rejects_gc(capsys):
    assert main(["--metrics", "--gc"]) == 2
    assert "cannot be combined with --gc" in capsys.readouterr().err


def test_metrics_rejects_selfcheck(capsys):
    assert main(["--metrics", "--selfcheck"]) == 2


def test_metrics_rejects_base_ref_via_no_diff_guard(capsys):
    # --metrics renders no reviewer diff, so --base is rejected by the same N13
    # guard --gc uses, before any repo discovery happens.
    assert main(["--metrics", "--base", "main"]) == 2


def test_metrics_last_requires_metrics(capsys):
    assert main(["--metrics-last", "1"]) == 2
    assert "meaningful only with --metrics" in capsys.readouterr().err
    assert main(["--metrics-last", "1", "docs/x.md"]) == 2
    assert "meaningful only with --metrics" in capsys.readouterr().err
