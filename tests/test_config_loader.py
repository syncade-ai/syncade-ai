"""Loader-level tests for :mod:`syncade.config_loader`."""

import pytest

from syncade.config_loader import ConfigError, load_config


def _write_config(tmp_path, body: str):
    """Helper: drop ``body`` into ``<tmp_path>/.syncade/config.toml`` and
    return the tmp_path for chaining."""
    (tmp_path / ".syncade").mkdir(exist_ok=True)
    (tmp_path / ".syncade" / "config.toml").write_text(body)
    return tmp_path


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.loop.max_rounds == 3
    assert cfg.reviewers[0].name == "codex-reviewer"
    assert cfg.reviewers[1].name == "codex-reviewer-adv"
    assert cfg.reviewers[1].template == "reviewer_adversarial.md"
    assert all(r.provider == "openai" for r in cfg.reviewers)


def test_missing_directory_also_returns_defaults(tmp_path):
    # no .syncade/ subdir at all
    cfg = load_config(tmp_path)
    assert cfg.loop.max_rounds == 3


def test_empty_file_returns_all_defaults(tmp_path):
    _write_config(tmp_path, "")
    cfg = load_config(tmp_path)
    assert cfg.loop.max_rounds == 3
    # ProducerConfig default follows the invoking harness; the conftest
    # neutral-harness fixture clears both markers → Codex shape.
    assert cfg.producer.model == "gpt-5.6-terra"


def test_valid_file_applies_overrides(tmp_path):
    _write_config(
        tmp_path,
        """
[producer]
model = "claude-opus-4-7"

[loop]
max_rounds = 2
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.producer.model == "claude-opus-4-7"
    # max_rounds=2 is an in-range override; this test exercises override
    # flow, not the bound.
    assert cfg.loop.max_rounds == 2
    # untouched defaults survive
    assert cfg.producer.thinking == "medium"


def test_producer_provider_override_in_toml(tmp_path):
    """PR-8: operators wanting codex as producer write
    ``[producer] provider = "openai"`` in their config. The empty
    section / absent section path is covered in
    test_producer_section_absent_yields_defaults."""
    _write_config(
        tmp_path,
        """
[producer]
provider = "openai"
model = "gpt-5-codex"
""",
    )
    cfg = load_config(tmp_path)
    assert cfg.producer.provider == "openai"
    assert cfg.producer.model == "gpt-5-codex"
    # other defaults survive
    assert cfg.producer.thinking == "medium"
    assert cfg.producer.permissions == "yolo"


def test_max_rounds_above_cap_rejected_at_config_load(tmp_path):
    """PR-v2-31: the ceiling is 10 (``le=10``, raised from 3); an
    above-cap value is rejected at config-load with a clean ConfigError
    that maps to exit 50."""
    _write_config(
        tmp_path,
        """
[loop]
max_rounds = 11
""",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)
    assert "max_rounds" in str(exc_info.value)


def test_invalid_toml_raises_config_error(tmp_path):
    _write_config(tmp_path, "this is not = valid = toml syntax")
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)
    assert "Failed to parse" in str(exc_info.value)


def test_unreadable_config_raises_config_error_without_traceback(tmp_path, monkeypatch):
    config_dir = tmp_path / ".syncade"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text("[loop]\nmax_rounds = 1\n", encoding="utf-8")
    original_open = type(config_path).open

    def deny_config_open(self, *args, **kwargs):
        if self == config_path:
            raise PermissionError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(type(config_path), "open", deny_config_open)

    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)

    message = str(exc_info.value)
    assert "Failed to read" in message
    assert "PermissionError" not in message


def test_unreadable_config_parent_probe_raises_config_error(tmp_path, monkeypatch):
    config_dir = tmp_path / ".syncade"
    config_dir.mkdir()
    config_path = config_dir / "config.toml"
    config_path.write_text("[loop]\nmax_rounds = 1\n", encoding="utf-8")
    original_is_file = type(config_path).is_file

    def deny_config_probe(self):
        if self == config_path:
            raise PermissionError("permission denied")
        return original_is_file(self)

    monkeypatch.setattr(type(config_path), "is_file", deny_config_probe)

    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)

    message = str(exc_info.value)
    assert "Failed to inspect" in message
    assert str(config_path) in message


def test_unknown_field_raises_config_error_naming_field(tmp_path):
    _write_config(
        tmp_path,
        """
[producer]
model = "x"
typo_field = "oops"
""",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)
    assert "typo_field" in str(exc_info.value)


def test_invalid_enum_raises_config_error_naming_path(tmp_path):
    _write_config(
        tmp_path,
        """
[loop]
pushback_policy = "wrong-policy"
""",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)
    msg = str(exc_info.value)
    assert "loop.pushback_policy" in msg


def test_reviewers_array_in_toml_validates(tmp_path):
    _write_config(
        tmp_path,
        """
[[reviewers]]
name = "claude-reviewer"
provider = "anthropic"
model = "claude-opus-4-7"

[[reviewers]]
name = "gemini-reviewer"
provider = "google"
model = "gemini-2-pro"
""",
    )
    cfg = load_config(tmp_path)
    assert len(cfg.reviewers) == 2
    assert cfg.reviewers[1].name == "gemini-reviewer"
    assert cfg.reviewers[1].provider == "google"


def test_zero_max_rounds_in_toml_raises_config_error(tmp_path):
    _write_config(tmp_path, "[loop]\nmax_rounds = 0\n")
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path)
    assert "max_rounds" in str(exc_info.value)


# QA fix #18 (P1.13): deprecation-callback test class
# --------------------------------------------------------------


class TestRemovedConfigFields:
    """Removed config fields fail as ordinary schema errors."""

    def test_require_unanimous_ship_false_rejected(self, tmp_path):
        _write_config(tmp_path, "[loop]\nrequire_unanimous_ship = false\n")
        warnings: list[str] = []
        with pytest.raises(ConfigError) as exc_info:
            load_config(tmp_path, deprecation_callback=warnings.append)
        assert "loop.require_unanimous_ship" in str(exc_info.value)
        assert warnings == []

    def test_require_unanimous_ship_true_rejected(self, tmp_path):
        _write_config(tmp_path, "[loop]\nrequire_unanimous_ship = true\n")
        warnings: list[str] = []
        with pytest.raises(ConfigError) as exc_info:
            load_config(tmp_path, deprecation_callback=warnings.append)
        assert "loop.require_unanimous_ship" in str(exc_info.value)
        assert warnings == []

    def test_callback_not_fired_when_removed_fields_omitted(self, tmp_path):
        _write_config(
            tmp_path,
            # max_rounds=2 is an in-range loop key (this test checks the
            # deprecation callback, not the bound).
            "[loop]\nmax_rounds = 2\n",
        )
        warnings: list[str] = []
        load_config(tmp_path, deprecation_callback=warnings.append)
        assert warnings == []

    def test_callback_not_fired_for_default_config(self, tmp_path):
        """Empty config emits no deprecation callback."""
        _write_config(tmp_path, "")
        warnings: list[str] = []
        load_config(tmp_path, deprecation_callback=warnings.append)
        assert warnings == []

    def test_callback_not_fired_when_no_file(self, tmp_path):
        """No config file at all → load_config short-circuits to
        defaults before the TOML parse path → no callback invocation."""
        warnings: list[str] = []
        load_config(tmp_path, deprecation_callback=warnings.append)
        assert warnings == []

    def test_partial_path_does_not_trigger(self, tmp_path):
        """``[loop]`` section without removed keys does not warn."""
        # max_rounds=3 is an in-range loop key (this test checks the
        # depth-correctness of the deprecation path, not the bound).
        _write_config(tmp_path, "[loop]\nmax_rounds = 3\n")
        warnings: list[str] = []
        load_config(tmp_path, deprecation_callback=warnings.append)
        assert warnings == []


class TestTrustedPermissionsRemoved:
    def test_reviewer_trusted_rejected(self, tmp_path):
        _write_config(
            tmp_path,
            '[[reviewers]]\nname = "r"\nprovider = "anthropic"\n'
            'model = "m"\npermissions = "trusted"\n',
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(tmp_path)
        assert "reviewers.0.permissions" in str(exc_info.value)

    def test_producer_trusted_rejected(self, tmp_path):
        _write_config(
            tmp_path,
            '[producer]\nprovider = "anthropic"\n'
            'model = "claude-sonnet-4-6"\npermissions = "trusted"\n',
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(tmp_path)
        assert "producer.permissions" in str(exc_info.value)

    def test_reviewer_trusted_execute_still_loads(self, tmp_path):
        _write_config(
            tmp_path,
            '[[reviewers]]\nname = "r"\nprovider = "anthropic"\n'
            'model = "m"\npermissions = "trusted-execute"\n',
        )
        warnings: list[str] = []
        cfg = load_config(tmp_path, deprecation_callback=warnings.append)
        assert cfg.reviewers[0].permissions == "trusted-execute"
        assert warnings == []


class TestReviewerNameTestsCollisionAtLoad:
    """T1.4: the model_validator on SyncadeConfig surfaces through
    config_loader as a ConfigError naming the field path. End-to-
    end check that a TOML file with the collision is rejected at
    load time, not at orchestrator dispatch."""

    def test_collision_surfaces_as_config_error(self, tmp_path):
        _write_config(
            tmp_path,
            """
[loop]
test_command = "pytest -q"

[[reviewers]]
name = "tests"
provider = "anthropic"
model = "claude-opus-4-7"

[[reviewers]]
name = "ok"
provider = "openai"
model = "gpt-5.5"
""",
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(tmp_path)
        # Message must name BOTH the offending field AND mention
        # the conflict cause so the operator can act on it.
        msg = str(exc_info.value)
        assert "tests" in msg
        # Reviewer-level test_command-related guidance must surface
        assert "test_command" in msg or "test re-run" in msg or "reserved" in msg

    def test_no_collision_passes_loader(self, tmp_path):
        _write_config(
            tmp_path,
            """
[loop]
test_command = "pytest -q"

[[reviewers]]
name = "claude-reviewer"
provider = "anthropic"
model = "claude-opus-4-7"

[[reviewers]]
name = "codex-reviewer"
provider = "openai"
model = "gpt-5.5"
""",
        )
        cfg = load_config(tmp_path)
        assert cfg.loop.test_command == "pytest -q"
        assert cfg.reviewers[0].name == "claude-reviewer"
