"""Unit tests for :mod:`syncade.cli.config_overrides` — the single owner of the CLI→config
precedence (loop-level + per-reviewer), extracted so it is testable without an end-to-end run
(PR-v2-9)."""

from types import SimpleNamespace

import pytest

from syncade.cli.config_overrides import OverrideError, apply_cli_overrides
from syncade.config import SyncadeConfig


def _args(**kw):
    base = dict(
        max_rounds=None,
        budget_tokens=None,
        budget_usd=None,
        reviewer_model=None,
        reviewer_thinking=None,
        reviewer_timeout=None,
        worktree_base=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _cfg():
    return SyncadeConfig()  # codex-reviewer + codex-reviewer-adv, both gpt-5.5 @ high


def test_reviewer_model_override_targets_exactly_the_named_reviewer():
    """C1: ``--reviewer-model NAME=X`` moves only that reviewer's model; others are untouched."""
    out = apply_cli_overrides(_cfg(), _args(reviewer_model=[("codex-reviewer", "gpt-5.6-sol")]))
    assert out.reviewers[0].model == "gpt-5.6-sol"
    assert out.reviewers[1].model == "gpt-5.5"  # unchanged


def test_reviewer_thinking_and_timeout_override():
    out = apply_cli_overrides(
        _cfg(),
        _args(
            reviewer_thinking=[("codex-reviewer-adv", "xhigh")],
            reviewer_timeout=[("codex-reviewer", "600")],
        ),
    )
    assert out.reviewers[1].thinking == "xhigh"
    assert out.reviewers[0].timeout_seconds == 600.0  # str coerced to float by the schema


def test_unknown_reviewer_name_is_override_error():
    with pytest.raises(OverrideError) as exc:
        apply_cli_overrides(_cfg(), _args(reviewer_model=[("nope", "x")]))
    assert "unknown reviewer" in str(exc.value)
    assert "configured reviewers" in str(exc.value)  # names them so the operator can fix it


@pytest.mark.parametrize(
    "kw",
    [
        dict(reviewer_thinking=[("codex-reviewer", "ludicrous")]),
        dict(reviewer_timeout=[("codex-reviewer", "-5")]),
        dict(reviewer_timeout=[("codex-reviewer", "abc")]),
        dict(reviewer_timeout=[("codex-reviewer", "0")]),
    ],
)
def test_bad_reviewer_value_is_override_error(kw):
    """A per-reviewer value the schema rejects (bad tier, non-numeric / <=0 timeout) →
    OverrideError, reusing the SAME validators as the TOML rather than a second copy of them."""
    with pytest.raises(OverrideError):
        apply_cli_overrides(_cfg(), _args(**kw))


def test_no_overrides_is_byte_identical():
    """C6: with no override flags set, the config is returned unchanged."""
    cfg = _cfg()
    assert apply_cli_overrides(cfg, _args()) == cfg


def test_worktree_base_override_beats_config():
    """--worktree-base overrides config.worktree_base (CLI beats config); applied by
    apply_cli_overrides on the review path."""
    from pathlib import Path

    out = apply_cli_overrides(_cfg(), _args(worktree_base="/fast/disk/syncade"))
    assert out.worktree_base == Path("/fast/disk/syncade")


def test_worktree_base_unset_leaves_config_default():
    from syncade.worktree import DEFAULT_WORKTREE_BASE

    assert apply_cli_overrides(_cfg(), _args()).worktree_base == DEFAULT_WORKTREE_BASE


def test_worktree_base_expands_user():
    """A ``~``-relative --worktree-base is expanded (mirrors --repo-root's expanduser)."""
    from pathlib import Path

    from syncade.cli.config_overrides import apply_worktree_base_override

    out = apply_worktree_base_override(_cfg(), _args(worktree_base="~/wt"))
    assert out.worktree_base == Path("~/wt").expanduser()
    assert "~" not in str(out.worktree_base)


def test_loop_overrides_still_apply():
    """The loop-level merge extracted from cli/__init__ (max_rounds / budget) still works."""
    out = apply_cli_overrides(_cfg(), _args(max_rounds=1, budget_tokens=5000))
    assert out.loop.max_rounds == 1
    assert out.loop.budget_tokens == 5000


def test_repeated_flags_override_multiple_reviewers_independently():
    out = apply_cli_overrides(
        _cfg(),
        _args(reviewer_model=[("codex-reviewer", "m1"), ("codex-reviewer-adv", "m2")]),
    )
    assert (out.reviewers[0].model, out.reviewers[1].model) == ("m1", "m2")


def test_reviewer_override_type_parses_name_value():
    from syncade.cli.parser import _reviewer_override

    assert _reviewer_override("rv=gpt-5.5") == ("rv", "gpt-5.5")
    assert _reviewer_override("rv=a=b") == (
        "rv",
        "a=b",
    )  # splits on FIRST '=' — value may contain =


def test_reviewer_override_type_rejects_malformed():
    import argparse

    from syncade.cli.parser import _reviewer_override

    for bad in ("noequals", "=noname", "rv="):  # rv= has no value → an empty model would slip in
        with pytest.raises(argparse.ArgumentTypeError):
            _reviewer_override(bad)


@pytest.mark.parametrize("mode_argv", [["--resume"], ["--metrics"], ["--doctor"], ["--gc"]])
def test_reviewer_flags_rejected_outside_a_fresh_review_loop(mode_argv, capsys):
    """--reviewer-* overrides the FRESH review loop's roster; on --resume (reuses the roster from
    the current config.toml) / the one-shot modes (no reviewers) they are REJECTED (exit 2)."""
    from syncade.cli import main

    rc = main([*mode_argv, "--reviewer-model", "rv=x"])
    assert rc == 2
    assert "meaningful only for a fresh review loop" in capsys.readouterr().err


def test_parser_wires_reviewer_flags_to_args():
    """The other half of the wiring (parser → args): the three flags are repeatable and each
    parses to a ``(name, value)`` tuple ``apply_cli_overrides`` consumes. The application half is
    covered above; the end-to-end exit-50 path is exercised by a live-CLI check in the PR proof."""
    from syncade.cli.parser import build_parser

    args = build_parser().parse_args(
        [
            "--reviewer-model",
            "rv1=gpt-5.6-sol",
            "--reviewer-model",
            "rv2=m2",
            "--reviewer-thinking",
            "rv1=xhigh",
            "--reviewer-timeout",
            "rv2=600",
            "pr.md",
        ]
    )
    assert args.reviewer_model == [("rv1", "gpt-5.6-sol"), ("rv2", "m2")]
    assert args.reviewer_thinking == [("rv1", "xhigh")]
    assert args.reviewer_timeout == [("rv2", "600")]


def test_parser_wires_worktree_base_flag():
    """--worktree-base parses to args.worktree_base (a plain string; expanded on apply)."""
    from syncade.cli.parser import build_parser

    args = build_parser().parse_args(["--worktree-base", "/mnt/fast/syncade", "pr.md"])
    assert args.worktree_base == "/mnt/fast/syncade"
    assert build_parser().parse_args(["pr.md"]).worktree_base is None  # default: unset


def test_parser_wires_preset_flag():
    """--preset parses to args.preset; argparse `choices` rejects a bad name (exit 2)."""
    from syncade.cli.parser import build_parser

    assert build_parser().parse_args(["--preset", "cheap", "pr.md"]).preset == "cheap"
    assert build_parser().parse_args(["pr.md"]).preset is None  # default: unset
    with pytest.raises(SystemExit):  # argparse choices → exit 2
        build_parser().parse_args(["--preset", "bogus", "pr.md"])
