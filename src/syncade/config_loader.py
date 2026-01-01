"""Load and validate ``.syncade/config.toml``.

The loader is intentionally tolerant of a missing config file (returns PRD
defaults silently) but strict about invalid content: TOML parse errors and
pydantic validation failures are both wrapped in :class:`ConfigError` with
a message that names the offending field(s) when possible.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from syncade.config import SyncadeConfig
from syncade.config_auth import api_key_problems
from syncade.presets import load_preset

CONFIG_RELATIVE_PATH: Path = Path(".syncade") / "config.toml"
"""Path of the config file relative to a repo root."""


class ConfigError(Exception):
    """Raised when ``.syncade/config.toml`` cannot be parsed or fails schema
    validation.

    The exception message names the offending field(s) when possible so the
    CLI can surface a self-explanatory error to the user without further
    introspection of the underlying cause.
    """


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto ``base`` (override wins); nested tables merge key-by-key,
    scalars and lists replace wholesale. Used to layer ``.syncade/config.toml`` over a ``--preset``
    base so a user who sets one loop knob keeps the preset's other knobs."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    repo_root: Path,
    *,
    preset: str | None = None,
    check_api_keys: bool = True,
    deprecation_callback=None,
    env: dict[str, str] | None = None,
) -> SyncadeConfig:
    """Load and validate ``<repo_root>/.syncade/config.toml``.

    Behaviour:

    - If the file is missing, returns :class:`SyncadeConfig` with all PRD
      defaults applied (or the ``preset`` base, if given). No warning is
      emitted — zero-config is a supported and expected mode.
    - ``preset`` (``--preset``) supplies a bundled BASE config; the user's
      ``.syncade/config.toml`` deep-merges ON TOP of it (user wins), so the
      precedence is defaults < preset < user file < CLI flags.
    - If the file exists but is syntactically invalid TOML, raises
      :class:`ConfigError` referencing the parse error.
    - If the file parses but fails schema validation (unknown field,
      invalid enum value, wrong type, etc.), raises :class:`ConfigError`
      whose message names the offending dotted field path(s).

    Args:
        repo_root: The git repo root to load config from.
        preset: Optional bundled preset name; its TOML is the base config the
            user file deep-merges onto (see :func:`syncade.presets.load_preset`).
        check_api_keys: When ``True`` (default), an ``auth = "api"`` actor with no
            key in the env fails as a config error. Set ``False`` for actor-less
            maintenance modes (``--gc``) that must not require credentials for
            actors they never spawn — malformed TOML / bad schema still fail.
        deprecation_callback: Retained for the CLI call shape. There are no
            current load-time deprecations; stale fields now fail validation.
        env: Environment consulted for the ANTHROPIC ``auth = "api"`` key check (openai
            is exempt -- codex reads no env key). Defaults to ``os.environ``. An explicit
            parameter rather than a global read, so tests can exercise the check without
            mutating process state — and so the env-dependence of config loading is
            visible in the signature.
    """
    base = load_preset(preset) if preset else {}

    config_path = repo_root / CONFIG_RELATIVE_PATH
    try:
        config_exists = config_path.is_file()
    except OSError as exc:
        raise ConfigError(f"Failed to inspect {config_path}: {exc}") from exc
    if not config_exists:
        # No user file: the preset (or {}) is the whole config. model_validate({}) reproduces
        # SyncadeConfig() exactly, so zero-config + no preset stays byte-identical.
        return SyncadeConfig.model_validate(base)

    try:
        with config_path.open("rb") as f:
            raw = tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Failed to read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse {config_path}: {exc}") from exc

    raw = _deep_merge(base, raw)  # preset is the base; the user file wins per-key.

    try:
        config = SyncadeConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid configuration in {config_path}:\n{_format_validation_errors(exc)}"
        ) from exc

    # auth = "api" with no key in the env is a CONFIG error, not a runtime one. Left to
    # runtime it surfaces as a provider 401 — after every reviewer has already run and
    # billed. Every offending actor is reported at once so one re-run fixes them all.
    # ``check_api_keys=False`` for actor-less maintenance modes (e.g. ``--gc``): they still
    # reject malformed TOML / bad schema above, but must not require credentials for actors
    # they will never spawn (dogfood R2-B2).
    if check_api_keys:
        problems = api_key_problems(config, env=env)
        if problems:
            raise ConfigError(
                "Invalid configuration in {}:\n{}".format(
                    config_path, "\n".join(f"  - {p}" for p in problems)
                )
            )
    return config


def _format_validation_errors(exc: ValidationError) -> str:
    """Render a :class:`pydantic.ValidationError` as a human-readable
    bulleted list of ``<dotted.field>: <message>`` lines."""
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
