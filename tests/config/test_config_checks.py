"""Schema-level tests for :mod:`syncade.config` (loop timeout, reviewer
name collisions, test_command / test_timeout, deprecated spec_audit,
and ``[[checks]]`` config)."""

import pytest
from pydantic import ValidationError

from syncade.config import SyncadeConfig


def test_loop_max_rounds_four_rejected_above_cap():
    """PR-8: PRD Appendix C caps max_rounds at 3. Values > 3 are
    rejected at config-load (``le=3``) — exit 50 at the CLI."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"max_rounds": 4})
    assert "max_rounds" in str(exc_info.value)


def test_loop_max_rounds_five_rejected():
    """Belt-and-braces: even further past the cap is also rejected."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"max_rounds": 5})
    assert "max_rounds" in str(exc_info.value)


def test_loop_timeout_seconds_accepts_override():
    cfg = SyncadeConfig(loop={"timeout_seconds": 3600})
    assert cfg.loop.timeout_seconds == 3600


def test_loop_timeout_seconds_accepts_sub_second_float():
    # gt=0 admits any positive float — useful for tests that drive a
    # real timeout without waiting a full second.
    cfg = SyncadeConfig(loop={"timeout_seconds": 0.5})
    assert cfg.loop.timeout_seconds == 0.5


def test_loop_timeout_seconds_zero_raises():
    # gt=0 (not ge=0): zero would SIGKILL every reviewer instantly.
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"timeout_seconds": 0})
    assert "timeout_seconds" in str(exc_info.value)


def test_loop_timeout_seconds_negative_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"timeout_seconds": -1})
    assert "timeout_seconds" in str(exc_info.value)


def test_loop_timeout_seconds_nan_raises():
    """T1.5: NaN passes pydantic's ``gt=0`` admit unaided (nan
    comparisons return False, which some pydantic versions
    interpret as "didn't violate"). The field_validator catches it.
    Without this, the dispatcher's
    ``subprocess.communicate(timeout=nan)`` would have undefined
    behavior."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"timeout_seconds": float("nan")})
    assert "timeout_seconds" in str(exc_info.value)


def test_loop_timeout_seconds_inf_raises():
    """T1.5: ``inf > 0`` is True so ``gt=0`` admits infinity unaided.
    The field_validator rejects it — without this guard an
    operator config could produce a reviewer subprocess that
    blocks forever ('wait forever' is the timeout=inf semantic)."""
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(loop={"timeout_seconds": float("inf")})
    assert "timeout_seconds" in str(exc_info.value)


# ---------------------------------------------------------------------------
# T1.4: reviewer named "tests" collides with the reserved test-leg
# worktree basename when test_command is configured.
# ---------------------------------------------------------------------------


class TestReviewerNameTestsCollision:
    """When ``[loop] test_command`` is set, the orchestrator
    provisions a worktree named ``"tests"`` at
    ``/tmp/syncade/<run-id>/tests/``. A reviewer named ``"tests"``
    would try to create the same path. Catching this at
    config-load avoids paying reviewer+synth cost only to fail
    on collision in the test phase."""

    def test_collision_rejected_when_test_command_set(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                reviewers=[
                    {"name": "tests", "provider": "anthropic", "model": "x"},
                    {"name": "ok", "provider": "openai", "model": "y"},
                ],
                loop={"test_command": "pytest"},
            )
        msg = str(exc_info.value)
        assert "'tests'" in msg or "tests" in msg
        assert "test_command" in msg or "test re-run" in msg or "reserved" in msg

    def test_no_collision_when_test_command_unset(self):
        """A reviewer named 'tests' is harmless when the test leg
        is disabled — no worktree collision possible. Operators
        who want this naming can keep using it pre-PR-7.5 style."""
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "tests", "provider": "anthropic", "model": "x"},
                {"name": "ok", "provider": "openai", "model": "y"},
            ],
        )
        assert cfg.reviewers[0].name == "tests"
        assert cfg.loop.test_command is None

    def test_no_collision_when_no_reviewer_named_tests(self):
        """Sanity: typical configs with test_command set + no
        offending reviewer name pass cleanly."""
        cfg = SyncadeConfig(
            reviewers=[
                {"name": "claude-reviewer", "provider": "anthropic", "model": "x"},
                {"name": "codex-reviewer", "provider": "openai", "model": "y"},
            ],
            loop={"test_command": "pytest -q"},
        )
        assert cfg.loop.test_command == "pytest -q"

    def test_uppercase_tests_collision_rejected(self):
        """R2.T1.4: case-insensitive comparison catches 'Tests' on
        case-insensitive filesystems (macOS APFS/HFS+ default,
        Windows). Both names resolve to the same on-disk path."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                reviewers=[
                    {"name": "Tests", "provider": "anthropic", "model": "x"},
                    {"name": "ok", "provider": "openai", "model": "y"},
                ],
                loop={"test_command": "pytest"},
            )
        assert "Tests" in str(exc_info.value)

    def test_all_caps_TESTS_collision_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                reviewers=[
                    {"name": "TESTS", "provider": "anthropic", "model": "x"},
                    {"name": "ok", "provider": "openai", "model": "y"},
                ],
                loop={"test_command": "pytest"},
            )
        assert "TESTS" in str(exc_info.value)

    def test_mixed_case_TeStS_collision_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                reviewers=[
                    {"name": "TeStS", "provider": "anthropic", "model": "x"},
                    {"name": "ok", "provider": "openai", "model": "y"},
                ],
                loop={"test_command": "pytest"},
            )
        assert "TeStS" in str(exc_info.value)

    def test_lower_tests_collision_still_rejected(self):
        """Sanity: the original lowercase case still works after
        the casefold refactor."""
        with pytest.raises(ValidationError):
            SyncadeConfig(
                reviewers=[
                    {"name": "tests", "provider": "anthropic", "model": "x"},
                    {"name": "ok", "provider": "openai", "model": "y"},
                ],
                loop={"test_command": "pytest"},
            )


def test_empty_reviewers_list_raises():
    with pytest.raises(ValidationError) as exc_info:
        SyncadeConfig(reviewers=[])
    assert "reviewers" in str(exc_info.value)


def test_single_reviewer_is_valid():
    # one reviewer is the schema floor; cross-model diversity is an
    # orchestrator-level concern, not enforced here
    cfg = SyncadeConfig(
        reviewers=[{"name": "solo", "provider": "anthropic", "model": "claude-opus-4-7"}]
    )
    assert len(cfg.reviewers) == 1


# ---------------------------------------------------------------------------
# PR-7.5: opt-in test re-run leg (LoopConfig.test_command + test_timeout_seconds)
# ---------------------------------------------------------------------------


class TestLoopTestCommand:
    """``test_command`` is the opt-in switch for PR-7.5's third
    convergence leg. ``None`` (default) keeps the pre-PR-7.5
    behavior — exit 0 reflects synth-clean only."""

    def test_unset_test_command_is_none_by_default(self):
        """The opt-in default: no inferred test command."""
        cfg = SyncadeConfig()
        assert cfg.loop.test_command is None

    def test_test_command_round_trips(self):
        cfg = SyncadeConfig(loop={"test_command": "pytest -q"})
        assert cfg.loop.test_command == "pytest -q"

    def test_test_command_accepts_multi_command_shell_string(self):
        """The string is passed verbatim to ``sh -c`` — multi-command
        sequences are intentional (operator's free-form config)."""
        cmd = "npm test && playwright test"
        cfg = SyncadeConfig(loop={"test_command": cmd})
        assert cfg.loop.test_command == cmd

    def test_empty_test_command_rejected(self):
        """``min_length=1``: an empty string would silently SIGKILL
        every run when passed to ``sh -c``."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(loop={"test_command": ""})
        assert "test_command" in str(exc_info.value)

    def test_whitespace_only_test_command_rejected(self):
        """field_validator: ``"   "`` passes min_length but defeats the
        field's purpose — same PR-6 fix #1 lesson reapplied."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(loop={"test_command": "   \n\t  "})
        assert "test_command" in str(exc_info.value)


class TestLoopTestTimeoutSeconds:
    """``test_timeout_seconds`` is the per-test-run wall-clock cap.
    ``None`` (default) means 'reuse :attr:`timeout_seconds`'; the
    orchestrator does that resolution, not the schema."""

    def test_unset_test_timeout_seconds_is_none_by_default(self):
        cfg = SyncadeConfig()
        assert cfg.loop.test_timeout_seconds is None

    def test_test_timeout_seconds_round_trips(self):
        cfg = SyncadeConfig(loop={"test_timeout_seconds": 300.0})
        assert cfg.loop.test_timeout_seconds == 300.0

    def test_test_timeout_seconds_accepts_sub_second_float(self):
        """Useful for tests that drive a real timeout without burning
        a full second of clock time."""
        cfg = SyncadeConfig(loop={"test_timeout_seconds": 0.5})
        assert cfg.loop.test_timeout_seconds == 0.5

    def test_test_timeout_seconds_zero_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(loop={"test_timeout_seconds": 0})
        assert "test_timeout_seconds" in str(exc_info.value)

    def test_test_timeout_seconds_negative_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(loop={"test_timeout_seconds": -5})
        assert "test_timeout_seconds" in str(exc_info.value)

    def test_test_timeout_seconds_nan_rejected(self):
        """``math.isfinite`` guard from PR-5.5's pattern: NaN is
        comparison-defined to be neither >, <, nor == anything, so
        ``gt=0`` alone can let it through depending on pydantic
        version. The validator hardens against that."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(loop={"test_timeout_seconds": float("nan")})
        assert "test_timeout_seconds" in str(exc_info.value)

    def test_test_timeout_seconds_inf_rejected(self):
        """``float('inf') > 0`` is True so ``gt=0`` admits infinity
        unaided. The validator rejects it so an operator config
        can't produce an unkillable test-run subprocess."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(loop={"test_timeout_seconds": float("inf")})
        assert "test_timeout_seconds" in str(exc_info.value)


# ---------------------------------------------------------------------------
# SpecAuditConfig removal + stale config rejection.
#
# The PRD documents [spec_audit] as intentionally NOT a v1 config block
# (cleaned in PR-10.5 T3). PR-10.6 removes the dataclass and parent field
# from the schema. Stale `.syncade/config.toml` files that still define
# [spec_audit] now fail as ordinary unknown config instead of being stripped.
# ---------------------------------------------------------------------------


def test_spec_audit_config_not_in_syncade_config():
    """PR-10.6 T1: ``SyncadeConfig`` no longer has a ``spec_audit``
    attribute. The dataclass was removed because v1 spec-audit is
    hardcoded (module constants in :mod:`syncade.spec_audit`); the
    PRD explicitly reserves the table name for a future PR."""
    cfg = SyncadeConfig()
    assert not hasattr(cfg, "spec_audit")
    assert "spec_audit" not in SyncadeConfig.model_fields


def test_removed_spec_audit_toml_is_rejected(tmp_path):
    """A config that still defines ``[spec_audit]`` is rejected."""

    from syncade.config_loader import ConfigError, load_config

    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[spec_audit]\non_ambiguity = "pause-and-ask"\n\n[loop]\nmax_rounds = 2\n',
        encoding="utf-8",
    )
    warnings: list[str] = []
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path, deprecation_callback=warnings.append)

    assert "spec_audit" in str(exc_info.value)
    assert warnings == []


def test_removed_notifications_toml_is_rejected(tmp_path):
    """The one-value ``[notifications]`` config block was removed."""

    from syncade.config_loader import ConfigError, load_config

    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[notifications]\nmode = "inline"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)

    assert "notifications" in str(exc_info.value)


def test_removed_pushback_policy_toml_is_rejected(tmp_path):
    """The one-value ``loop.pushback_policy`` field was removed."""

    from syncade.config_loader import ConfigError, load_config

    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        '[loop]\npushback_policy = "trust-with-escalation"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)

    assert "loop.pushback_policy" in str(exc_info.value)


def test_removed_require_unanimous_ship_toml_is_rejected(tmp_path):
    """The ignored ``loop.require_unanimous_ship`` field was removed."""

    from syncade.config_loader import ConfigError, load_config

    (tmp_path / ".syncade").mkdir()
    (tmp_path / ".syncade" / "config.toml").write_text(
        "[loop]\nrequire_unanimous_ship = true\n",
        encoding="utf-8",
    )
    warnings: list[str] = []
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path, deprecation_callback=warnings.append)

    assert "loop.require_unanimous_ship" in str(exc_info.value)
    assert warnings == []


class TestCheckConfig:
    """PR-21: user-defined mechanical checks. ``[[checks]]`` blocks
    (name + command + severity) mirror ``[[reviewers]]``. The model
    lives in :mod:`syncade.checks_config`; the ``checks`` field + a
    collision validator hang off ``SyncadeConfig``. Empty list
    (default) = today's loop, byte-identical."""

    def test_checks_empty_by_default(self):
        """The cardinal invariant: zero config => no checks."""
        cfg = SyncadeConfig()
        assert cfg.checks == []

    def test_valid_blocking_and_advisory_checks(self):
        cfg = SyncadeConfig(
            checks=[
                {"name": "lint", "command": "ruff check .", "severity": "blocking"},
                {
                    "name": "file-length",
                    "command": "scripts/check-loc.sh 500",
                    "severity": "advisory",
                },
            ],
        )
        assert [c.name for c in cfg.checks] == ["lint", "file-length"]
        assert cfg.checks[0].severity == "blocking"
        assert cfg.checks[1].severity == "advisory"
        assert cfg.checks[0].command == "ruff check ."

    def test_check_severity_defaults_to_advisory(self):
        """Fail-safe default: a check with no explicit severity is
        advisory — a config that forgets the tag can never silently
        gate the verdict."""
        cfg = SyncadeConfig(checks=[{"name": "loc", "command": "scripts/check-loc.sh 500"}])
        assert cfg.checks[0].severity == "advisory"

    def test_invalid_severity_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(checks=[{"name": "x", "command": "y", "severity": "warn"}])
        assert "severity" in str(exc_info.value)

    def test_empty_command_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(checks=[{"name": "x", "command": "", "severity": "advisory"}])
        assert "command" in str(exc_info.value)

    def test_whitespace_only_command_rejected(self):
        """Mirrors ``test_command``: ``"   "`` passes ``min_length`` but
        would SIGKILL the check leg — rejected by a field_validator."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(checks=[{"name": "x", "command": "  \n\t ", "severity": "advisory"}])
        assert "command" in str(exc_info.value)

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(checks=[{"name": "", "command": "ruff check .", "severity": "advisory"}])
        assert "name" in str(exc_info.value)

    def test_whitespace_only_name_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                checks=[{"name": "  ", "command": "ruff check .", "severity": "advisory"}]
            )
        assert "name" in str(exc_info.value)

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                checks=[{"name": "x", "command": "y", "severity": "advisory", "bogus": 1}]
            )
        assert "bogus" in str(exc_info.value)

    def test_duplicate_check_names_rejected(self):
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                checks=[
                    {"name": "lint", "command": "ruff check .", "severity": "advisory"},
                    {
                        "name": "lint",
                        "command": "ruff format --check .",
                        "severity": "advisory",
                    },
                ]
            )
        assert "lint" in str(exc_info.value)

    def test_check_name_colliding_with_reviewer_rejected(self):
        """A check named after a reviewer would collide on the
        per-round worktree path (both at round-N/<name>/).
        Uses codex-reviewer (current default) — claude-reviewer was
        offlined 2026-06-30 and is no longer in the default roster."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                checks=[
                    {
                        "name": "codex-reviewer",
                        "command": "ruff check .",
                        "severity": "advisory",
                    }
                ]
            )
        assert "codex-reviewer" in str(exc_info.value)

    def test_check_name_colliding_with_reserved_tests_rejected(self):
        """``tests`` is the reserved test-re-run worktree basename —
        a check can't take it (case-insensitive). Unlike the
        reviewer-vs-tests rule, this is UNCONDITIONAL: checks always
        provision worktrees when configured, so reserving ``tests``
        avoids a latent collision the moment ``test_command`` is set."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                checks=[{"name": "Tests", "command": "ruff check .", "severity": "advisory"}]
            )
        msg = str(exc_info.value)
        assert "Tests" in msg or "tests" in msg

    def test_tests_collision_is_unconditional(self):
        """No ``test_command`` set, yet a check named ``tests`` is
        still rejected — checks always make worktrees."""
        with pytest.raises(ValidationError):
            SyncadeConfig(
                checks=[{"name": "tests", "command": "ruff check .", "severity": "advisory"}]
            )

    @pytest.mark.parametrize("bad_name", ["a/b", "../escape", ".", "..", "a\\b"])
    def test_check_name_must_be_plain_basename(self, bad_name):
        """The check name is used as a worktree basename AND an artifact
        filename — separators / parent-refs / absolute paths must be
        rejected at config-load, not produce a runtime path escape."""
        with pytest.raises(ValidationError) as exc_info:
            SyncadeConfig(
                checks=[{"name": bad_name, "command": "ruff check .", "severity": "advisory"}]
            )
        assert "name" in str(exc_info.value) or "basename" in str(exc_info.value)
