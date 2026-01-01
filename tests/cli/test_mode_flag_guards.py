"""CLI guard regression tests for the asymmetric-mutex nits N11-N13
(the design notes).

These cover *correct behavior, previously-missing/misleading guards*:

N11 — ``--selfcheck`` help text must list every exclusion ``main()`` actually
      enforces (it understated to "PR_DOC, --draft-spec").
N13 — ``--base`` / ``--scope`` select the reviewer diff base; the diagnostic /
      maintenance modes (``--selfcheck`` / ``--auth-check`` / ``--spec-audit`` /
      ``--gc``) render no reviewer diff, so the combination is now a hard
      exit-2 usage error instead of being silently accepted-and-ignored.
N12 — ``--force-dirty`` + ``--resume`` stays a *permitted* combination at the
      CLI mutex layer: ``--force-dirty`` is the dirty-tree escape hatch during
      resume, and the handler threads ``force_dirty`` through unchanged. (The
      behavioral "ignored during resume" half is the orchestrator/loop.py
      dirty-refusal fix — audit-H3 — not a CLI concern, so no CLI mutex is
      added that would defeat the escape hatch.)
"""

import shutil

import pytest

from syncade.cli import build_parser, main
from tests.cli._helpers import _init_git_repo

# ---------------------------------------------------------------------------
# N11 — --selfcheck help advertises its full exclusion set
# ---------------------------------------------------------------------------


def test_selfcheck_help_lists_every_enforced_exclusion():
    """The ``--selfcheck`` help must name all seven exclusions ``main()``
    enforces, not just the original "PR_DOC, --draft-spec"."""
    normalized = " ".join(build_parser().format_help().split())
    assert (
        "Mutually exclusive with PR_DOC, --auth-check, --spec-audit, "
        "--draft-spec, --resume, --openspec, --gc." in normalized
    )


# ---------------------------------------------------------------------------
# N13 — --base / --scope rejected by the diff-less one-shot modes
# ---------------------------------------------------------------------------

_DIFF_BASE_FLAGS = (("--base", ["--base", "main"]), ("--scope", ["--scope", "local"]))
_DIFFLESS_MODES = (
    ("--selfcheck", ["--selfcheck"]),
    ("--auth-check", ["--auth-check"]),
    ("--spec-audit", ["--spec-audit", "pr.md"]),
    ("--gc", ["--gc"]),
)


@pytest.mark.parametrize(
    "mode_flag,mode_argv", _DIFFLESS_MODES, ids=[m[0] for m in _DIFFLESS_MODES]
)
@pytest.mark.parametrize(
    "diff_flag,diff_argv", _DIFF_BASE_FLAGS, ids=[d[0] for d in _DIFF_BASE_FLAGS]
)
def test_diff_base_flags_rejected_by_diffless_modes(
    tmp_path, capsys, mode_flag, mode_argv, diff_flag, diff_argv
):
    """N13: each diagnostic/maintenance mode rejects ``--base``/``--scope`` with
    a hard exit-2 usage error and a clear message. The guard fires before any
    repo discovery or provider work, so a non-repo ``tmp_path`` still yields
    exit 2 — not the snapshot-error 60 the unguarded handler would have
    returned (which is exactly why the unguarded combination was a silent
    no-op rather than a usage error)."""
    rc = main(["--repo-root", str(tmp_path), *mode_argv, *diff_argv])
    assert rc == 2
    err = capsys.readouterr().err
    assert f"{diff_flag} cannot be combined with {mode_flag}" in err
    assert "renders no reviewer diff" in err


# ---------------------------------------------------------------------------
# N12 — --force-dirty stays compatible with --resume (escape hatch)
# ---------------------------------------------------------------------------


def test_force_dirty_with_resume_is_permitted_not_a_mutex_error(tmp_path, capsys):
    """N12: ``--force-dirty`` is the resume dirty-tree escape hatch, so the CLI
    must NOT reject ``--force-dirty --resume`` as an exit-2 mutex. Here the pair
    is accepted and proceeds into the resume handler, failing only on the
    unknown run-id (exit 60) — proving ``--force-dirty`` did not turn a resume
    into a usage error. (The behavioral "force-dirty ignored during resume"
    remediation is orchestrator/loop.py / audit-H3, not the CLI.)"""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    rc = main(["--repo-root", str(repo), "--force-dirty", "--resume", "no-such-run"])
    assert rc == 60  # resume-not-found, NOT a force-dirty/resume exit-2 mutex
    err = capsys.readouterr().err.lower()
    assert "resume error" in err
    assert "not found" in err


def test_force_dirty_help_documents_resume_escape():
    """N12: ``--force-dirty``'s help must state it also governs — and is the
    only escape from — the dirty-tree refusal on a resumed loop-mode run,
    mirroring ``--force-drift``'s explicit ``--resume`` framing. This closes the
    "docs silent on the force-dirty/resume interaction" asymmetry the finding
    names; the behavior itself is the loop.py H3 dirty-refusal fix."""
    normalized = " ".join(build_parser().format_help().split())
    assert "resumed loop-mode run (--resume)" in normalized
    assert "--force-dirty is likewise the only escape" in normalized
