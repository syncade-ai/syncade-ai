"""``permissions = "yolo"`` must be ANNOUNCED on every run that can dispatch a producer.

``yolo`` is allowed to exist for the same reason ``auth = "auto"`` is: the resolved truth is
stated out loud. What it grants is not recoverable from the run's own output — a producer that
moves an operator ref directly leaves its OWN ``HEAD`` unchanged, so syncade reports the round as
STALLED and nothing ever contradicts that. An operator who cannot see the grant cannot audit the
consequence.

Scoped to ``blocks``, not to config alone: ``--spec-audit`` and ``--draft-spec`` never dispatch a
producer, and warning about authority a command cannot grant is the false alarm that teaches
operators to ignore the real one. That is the same wrong-axis mistake ``cli/auth_gate.py``'s own
docstring records (all actor types checked, all entry points not).
"""

import pytest

from syncade.cli.auth_gate import auth_gate
from syncade.config import ProducerConfig, ReviewerConfig, SyncadeConfig
from syncade.config_auth import AUDIT_BLOCKS, DRAFT_BLOCKS, REVIEW_BLOCKS, SELFCHECK_BLOCKS


@pytest.fixture(autouse=True)
def _codex_is_on_a_subscription(monkeypatch):
    import syncade.auth_preflight as ap

    monkeypatch.setattr(ap, "probe_codex_state", lambda *a, **k: ("subscription", ""))


def _config(permissions: str, *, max_rounds: int = 5) -> SyncadeConfig:
    return SyncadeConfig(
        loop={"max_rounds": max_rounds},
        reviewers=[ReviewerConfig(name="rv", provider="openai", model="gpt-5.5")],
        producer=ProducerConfig(
            provider="anthropic", model="claude-sonnet-4-6", permissions=permissions
        ),
    )


@pytest.mark.parametrize("blocks", [REVIEW_BLOCKS, SELFCHECK_BLOCKS])
def test_yolo_is_announced_wherever_a_producer_can_run(blocks, capsys):
    assert auth_gate(_config("yolo"), blocks) is None
    err = capsys.readouterr().err
    assert "producer sandbox: OFF" in err
    assert "unsandboxed shell" in err
    assert "syncade --config set producer.permissions confined" in err, "must name the repair"


@pytest.mark.parametrize("blocks", [AUDIT_BLOCKS, DRAFT_BLOCKS])
def test_silent_where_no_producer_can_run(blocks, capsys):
    assert auth_gate(_config("yolo"), blocks) is None
    assert "producer sandbox" not in capsys.readouterr().err


def test_confined_says_nothing(capsys):
    assert auth_gate(_config("confined"), REVIEW_BLOCKS) is None
    assert "producer sandbox" not in capsys.readouterr().err


def test_the_notice_survives_quiet(capsys):
    """It goes to stderr from ``auth_gate``, which has no verbosity switch at all.

    That is the design, not an accident: ``Logger``'s ``--quiet`` filter is for PROGRESS, and
    ``Logger.safety`` is explicitly scoped to the disposition of committed work — neither is the
    right home for a statement about which authority a model was handed before it runs.
    """
    import inspect

    from syncade.cli.auth_gate import _announce_unconfined_producer

    source = inspect.getsource(_announce_unconfined_producer)
    assert "logger" not in source.lower(), (
        "the notice must not route through a suppressible channel"
    )
    assert "input(" not in source, "a NOTICE, never a prompt: an unattended run must not stop"


def test_single_pass_review_does_not_warn_about_a_producer_it_cannot_dispatch(capsys):
    """`max_rounds=1` commits nothing, so the notice would be a false alarm.

    Flagged in three consecutive dogfood rounds and never fixed by a producer, because it was a
    minor and producers work blockers first.
    """
    assert auth_gate(_config("yolo", max_rounds=1), REVIEW_BLOCKS) is None
    assert "producer sandbox" not in capsys.readouterr().err


def test_selfcheck_still_warns_at_max_rounds_one(capsys):
    """`--selfcheck` dispatches a real producer regardless of `max_rounds`.

    The obvious fix — gate the notice on `max_rounds > 1` — would have silenced the one command
    whose entire job is running the producer for real.
    """
    assert auth_gate(_config("yolo", max_rounds=1), SELFCHECK_BLOCKS) is None
    assert "producer sandbox: OFF" in capsys.readouterr().err


def test_resume_mode_applies_effective_max_rounds_before_auth_gate(capsys):
    """A resumed run must not suppress the yolo notice when the raw config drifted to max_rounds=1.

    resume_mode.py computes effective_max_rounds = max(config.loop.max_rounds, plan.max_rounds).
    If it passes the RAW config (max_rounds=1) to auth_gate, the suppression condition fires and
    the notice is silenced — even though the resumed run can dispatch a producer at effective
    max_rounds=2. The fix: apply effective_max_rounds to config before calling auth_gate.

    This test pins the functional contract at the auth_gate level: a config with max_rounds>1
    must announce the yolo notice regardless of whether max_rounds was set by the raw config or
    by applying the effective cap from the resume plan.
    """
    from syncade.cli.auth_gate import _announce_unconfined_producer

    raw_config = _config("yolo", max_rounds=1)
    _announce_unconfined_producer(raw_config, REVIEW_BLOCKS)
    assert "producer sandbox" not in capsys.readouterr().err, (
        "prerequisite: raw max_rounds=1 suppresses the notice (no regression on the fix itself)"
    )

    effective_config = _config("yolo", max_rounds=2)
    _announce_unconfined_producer(effective_config, REVIEW_BLOCKS)
    assert "producer sandbox: OFF" in capsys.readouterr().err, (
        "after resume_mode.py applies effective_max_rounds=2, the notice must fire"
    )
