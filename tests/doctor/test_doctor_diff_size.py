"""`--doctor` predicts the `diff_too_large` refusal — PR-h-field-01 item 4.

The brief's closing thread: *"syncade's preflights verify what is cheap to verify —
credentials, and a producer commit against a synthetic repo — and neither touches the
inputs that vary per run. A safety check that cannot fail for the reasons a run actually
fails is a green light with no lamp behind it."*

`--selfcheck` is structurally the wrong home: it runs in a throwaway tmp workspace and
REJECTS `--base`/`--scope`, so it cannot see the operator's diff at all. `--doctor` is the
one mode that accepts them, and `CLAUDE.md` is explicit that doctor is **not** a deferral
site — it has no `run_review` downstream to be authoritative for it, so its row IS the
prediction and a false green sends the operator on to spend the live auth and
producer-commit legs.

Measured before this landed: doctor reported `doctor OK` (exit 0) for a repo whose run
`run_review` refuses at exit 60.
"""

from __future__ import annotations

import subprocess

import pytest

from syncade.config import SyncadeConfig
from syncade.doctor_preview import based_diff_classify, check_plan, reviewer_facing_bytes
from syncade.doctor_types import _RED

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="git binary not found on PATH",
)


def _config(cap: int) -> SyncadeConfig:
    return SyncadeConfig(
        reviewers=[
            {"name": "rv1", "provider": "fake1", "model": "x"},
            {"name": "rv2", "provider": "fake2", "model": "y"},
        ],
        loop={"max_diff_bytes": cap},
        review={"strip_repo_context_files": []},
    )


def _repo(tmp_path, payload: str):
    repo = tmp_path / "r"
    repo.mkdir()
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *argv], cwd=repo, capture_output=True, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, capture_output=True, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    (repo / "bulk.py").write_text(payload)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "bulk"], cwd=repo, capture_output=True, check=True)
    return repo, base


def test_doctor_reds_the_plan_row_for_a_diff_the_run_will_refuse(tmp_path):
    repo, base = _repo(tmp_path, "x = 1\n" * 5_000)

    check = check_plan(repo, _config(2_000), base_ref=base, scope=None, two_dot=False, max_rounds=3)

    assert check.status is _RED, "doctor stayed green for a run that exits 60"
    assert "diff_too_large" in check.detail, "the predicted exit is not named"
    assert "max_diff_bytes" in check.detail
    assert check.fix and "--base" in check.fix, "no actionable fix offered"


def test_doctor_stays_green_under_the_cap_and_shows_the_headroom(tmp_path):
    """The control. Without it, the red test could pass because doctor reds everything."""
    repo, base = _repo(tmp_path, "x = 1\n" * 5_000)

    check = check_plan(
        repo, _config(1_000_000), base_ref=base, scope=None, two_dot=False, max_rounds=3
    )

    assert check.status is not _RED
    assert "reviewed of" in check.detail and "allowed" in check.detail, (
        "the operator cannot see how close this run is to the ceiling"
    )


def test_doctors_prediction_equals_what_the_round_measures(tmp_path):
    """The property that makes the row trustworthy, asserted rather than assumed.

    Doctor and the round must agree on what "the diff" is, or one of them is lying. Both
    call `reviewer_facing_bytes`, so this pins that they keep sharing it: a second, drifting
    computation is exactly the weakness `pr-h-03.5` recorded about `diff_bytes_reviewed`.
    """
    from syncade.diff_filter import elide_binary_hunks, filter_diff_for_reviewer
    from syncade.snapshot import take_snapshot

    repo, base = _repo(tmp_path, "x = 1\n" * 5_000)
    config = _config(1_000_000)
    snap = take_snapshot(repo, base_ref=base)

    predicted = reviewer_facing_bytes(snap.diff_text, config)
    # What round.py computes, spelled out independently here so a change to either side
    # of the pair breaks this test rather than silently diverging.
    as_the_round_does, _ = elide_binary_hunks(
        filter_diff_for_reviewer(snap.diff_text, config.review.strip_repo_context_files)
    )
    assert predicted == len(as_the_round_does.encode("utf-8"))


def test_the_classifier_reports_a_too_large_diff_as_non_dispatching(tmp_path):
    """A refused run makes no commit, so the commit-safety guards must not double-fire.

    Same reasoning `no_changes` and `malformed` already carry: refusing a run for being on
    the default branch, when it was never going to commit, is a wrong refusal.
    """
    repo, base = _repo(tmp_path, "x = 1\n" * 5_000)

    assert (
        based_diff_classify(repo, _config(2_000), base_ref=base, scope=None, two_dot=False)
        == "too_large"
    )
    assert (
        based_diff_classify(repo, _config(1_000_000), base_ref=base, scope=None, two_dot=False)
        == "dispatch"
    )


def test_binary_heavy_repos_are_not_falsely_predicted_oversize(tmp_path):
    """Doctor must measure the reviewer-facing diff, not the raw one.

    A repo with committed screenshot baselines is the exact shape PR-h-field-01 exists to make
    reviewable. Predicting a refusal there would send the operator to narrow a `--base`
    that was never the problem.
    """
    repo, base = _repo(tmp_path, "x = 1\n")
    (repo / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 400)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, capture_output=True, check=True)

    config = _config(20_000)
    check = check_plan(repo, config, base_ref=base, scope=None, two_dot=False, max_rounds=3)

    assert check.status is not _RED, (
        "doctor predicted a refusal on binary bytes the reviewer would never be sent"
    )


# ── The assembled-prompt lower bound ────────────────────────────────────────
#
# Added because a calibration found NOTHING covered doctor's prompt-size check: removing
# the comparison left the whole suite green. It was built across three dogfood rounds and
# never had a test.


def test_doctor_reds_when_even_the_LOWER_BOUND_prompt_exceeds_the_ceiling(tmp_path):
    """doctor renders with a placeholder PR-doc ref, so its prompt size is a lower bound.

    That makes a RED always a true positive — if the lower bound is over the provider
    ceiling, the real prompt certainly is. The converse is deliberately not claimed.
    """
    from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING

    # Over the provider ceiling but UNDER max_diff_bytes, so the diff cap cannot fire first
    # and this test can only pass because of the prompt check.
    payload = "x = 1  # padding\n" * ((_CODEX_CHAR_CEILING // 17) + 20_000)
    repo, base = _repo(tmp_path, payload)
    config = SyncadeConfig(
        reviewers=[{"name": "rv1", "provider": "openai", "model": "gpt-5.5"}],
        loop={"max_diff_bytes": 50_000_000},
        review={"strip_repo_context_files": []},
    )

    check = check_plan(repo, config, base_ref=base, scope=None, two_dot=False, max_rounds=3)

    assert check.status is _RED, "doctor stayed green for a prompt the provider will refuse"
    assert "prompt_too_large" in check.detail, "the predicted exit is not named"


def test_doctor_does_not_red_on_prompt_size_for_an_ordinary_diff(tmp_path):
    """The control. Without it the assertion above could pass because doctor reds every run."""
    repo, base = _repo(tmp_path, "x = 1\n" * 100)
    config = SyncadeConfig(
        reviewers=[{"name": "rv1", "provider": "openai", "model": "gpt-5.5"}],
        loop={"max_diff_bytes": 50_000_000},
        review={"strip_repo_context_files": []},
    )

    check = check_plan(repo, config, base_ref=base, scope=None, two_dot=False, max_rounds=3)

    assert check.status is not _RED
