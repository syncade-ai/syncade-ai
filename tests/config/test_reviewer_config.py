"""Schema tests for :class:`syncade.config.ReviewerConfig` fields that are
per-reviewer (PR-v2-9). Split out of ``test_config_schema.py`` (which sits at the
500-LOC cap) as the home for reviewer-entry validation."""

import pytest
from pydantic import ValidationError

from syncade.config import ReviewerConfig, SyncadeConfig


def test_reviewer_timeout_seconds_unset_is_none():
    """An unset per-reviewer timeout reuses the loop/CLI timeout (resolved at dispatch), so the
    nullable field distinguishes 'use the loop default' from 'explicitly set to that same value'."""
    cfg = SyncadeConfig(reviewers=[{"name": "rv", "provider": "openai", "model": "m"}])
    assert cfg.reviewers[0].timeout_seconds is None


def test_reviewer_timeout_seconds_explicit_round_trips():
    cfg = SyncadeConfig(
        reviewers=[{"name": "rv", "provider": "openai", "model": "m", "timeout_seconds": 600.0}]
    )
    assert cfg.reviewers[0].timeout_seconds == 600.0


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_reviewer_timeout_seconds_invalid_rejected(bad):
    """``gt=0`` rejects 0 / negative; the ``isfinite`` validator rejects NaN / inf (an unkillable
    reviewer subprocess would hang the whole parallel dispatch)."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(
            reviewers=[{"name": "rv", "provider": "openai", "model": "m", "timeout_seconds": bad}]
        )
    assert "timeout_seconds" in str(exc_info.value)


@pytest.mark.parametrize("bad", [True, False])
def test_reviewer_timeout_seconds_boolean_rejected(bad):
    """TOML booleans coerce silently to float in pydantic — reject them explicitly.
    timeout_seconds=true→1.0 would give a 1-second timeout, always a config mistake."""
    with pytest.raises(ValidationError, match="boolean"):
        SyncadeConfig(
            reviewers=[{"name": "rv", "provider": "openai", "model": "m", "timeout_seconds": bad}]
        )


def test_reviewer_timeout_seconds_toml_string_rejected():
    """Strict numeric (dogfood R4): a TOML string ``timeout_seconds = "1"`` is rejected, not
    coerced to 1.0. The CLI ``--reviewer-timeout NAME=1`` still works — config_overrides parses it
    to a float BEFORE validation (see tests/cli/test_config_overrides.py)."""
    with pytest.raises(ValidationError):
        SyncadeConfig(
            reviewers=[{"name": "rv", "provider": "openai", "model": "m", "timeout_seconds": "1"}]
        )


def test_reviewer_timeout_seconds_integer_is_accepted():
    """A plain TOML int is a valid timeout (widened to float) — strictness rejects strings/bools,
    not a legitimate ``timeout_seconds = 600``."""
    cfg = SyncadeConfig(
        reviewers=[{"name": "rv", "provider": "openai", "model": "m", "timeout_seconds": 600}]
    )
    assert cfg.reviewers[0].timeout_seconds == 600.0


def test_reviewer_name_with_equals_rejected():
    """A reviewer name containing '=' must be rejected at config load.

    The NAME=VALUE CLI override grammar (--reviewer-model / --reviewer-thinking /
    --reviewer-timeout) splits on the first '=', so a name that contains '='
    cannot be unambiguously targeted.  Without the validator this is silently
    accepted but then permanently unreachable via --reviewer-* overrides."""
    with pytest.raises(ValidationError, match="="):
        SyncadeConfig(reviewers=[{"name": "rv=bad", "provider": "openai", "model": "m"}])


def test_reviewer_name_without_equals_accepted():
    """A name that does NOT contain '=' must remain valid — the validator must
    not over-restrict plain names like 'codex-reviewer' or 'rv.1'."""
    cfg = SyncadeConfig(reviewers=[{"name": "codex-reviewer", "provider": "openai", "model": "m"}])
    assert cfg.reviewers[0].name == "codex-reviewer"


def test_reviewer_bug_class_sweep_defaults_false_and_accepts_true():
    """Per-reviewer directed bug-class sweep, OPT-IN like ``adversarial_lens``.

    Contributed default-ON; held opt-in until an ablation measures whether the
    checklist raises recall or narrows the search. Per-reviewer is what makes
    that ablation a config change, so BOTH directions are pinned.
    """
    rc = ReviewerConfig(name="r", provider="anthropic", model="m")
    assert rc.bug_class_sweep is False
    rc_on = ReviewerConfig(name="r", provider="anthropic", model="m", bug_class_sweep=True)
    assert rc_on.bug_class_sweep is True
