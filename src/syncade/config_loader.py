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


# Top-level tables whose provider/model form a PAIR (config.py re-derives model from provider). A
# higher layer that names one of these replaces it WHOLESALE, so the pair can never split across
# layers (global sets anthropic/claude, repo overrides provider=openai -> a consistent openai pair,
# never openai + a claude model). Everything else merges key-by-key; lists/scalars replace.
_PAIRED_SECTIONS = frozenset({"producer", "synthesizer", "drafter", "auditor"})


def _deep_merge(base: dict, override: dict, *, _top_level: bool = True) -> dict:
    """Merge ``override`` onto ``base`` (override wins). Tables merge key-by-key EXCEPT the
    TOP-LEVEL paired sections (:data:`_PAIRED_SECTIONS`), which replace wholesale; lists and scalars
    replace. Layers the chain: defaults < ``--preset`` < global (``~/.syncade``) < repo
    (``.syncade``). The paired-section rule is TOP-LEVEL ONLY (D2): ``_top_level=False`` on recurse
    so a nested table that merely SHARES a paired name (e.g. a ``[pricing.models.producer]`` for a
    model named ``producer``) still key-merges instead of being wholesale-replaced."""
    out = dict(base)
    for key, value in override.items():
        if _top_level and key in _PAIRED_SECTIONS:
            out[key] = value
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value, _top_level=False)
        else:
            out[key] = value
    return out


def _default_global_config_path() -> Path:
    """``~/.syncade/config.toml`` — the global config layer. A function (not a constant) so tests
    can monkeypatch it to an isolated path without touching ``$HOME``."""
    return Path.home() / ".syncade" / "config.toml"


def _read_toml(path: Path) -> dict:
    """Parse ``path`` to a dict; ``{}`` if it is absent. Read/parse failures raise
    :class:`ConfigError` naming ``path``, so a bad global vs repo file is unambiguous."""
    try:
        if not path.is_file():
            return {}
    except OSError as exc:
        raise ConfigError(f"Failed to inspect {path}: {exc}") from exc
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Failed to read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc


def load_config(
    repo_root: Path,
    *,
    preset: str | None = None,
    check_api_keys: bool = True,
    deprecation_callback=None,
    env: dict[str, str] | None = None,
    global_config_path: Path | None = None,
    include_repo: bool = True,
) -> SyncadeConfig:
    """Load and validate ``<repo_root>/.syncade/config.toml``.

    Behaviour:

    - If the file is missing, returns :class:`SyncadeConfig` with all PRD
      defaults applied (or the ``preset`` base, if given). No warning is
      emitted — zero-config is a supported and expected mode.
    - Precedence is defaults < ``preset`` < ``~/.syncade/config.toml`` (global) <
      the repo's ``.syncade/config.toml`` < CLI flags. Each layer deep-merges onto
      the one below (higher wins), except the paired sections replace wholesale
      (see :func:`_deep_merge`). A missing layer is skipped; with no global and no
      repo file, the result is byte-identical to zero-config today.
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
        global_config_path: The global layer (defaults to ``~/.syncade/config.toml``). Tests pass
            an explicit path to exercise or isolate it; production uses the default.
        include_repo: When ``True`` (default), the repo layer (``<repo_root>/.syncade/config.toml``)
            participates. ``False`` drops it — used by ``--config`` when the cwd is NOT inside a git
            repo. ``--config`` inspects the CURRENT state (no repo → no repo layer), so a stray
            ``cwd/.syncade/config.toml`` is not read as the repo layer. (A *review* run with
            ``--allow-auto-init`` would ``git init`` the dir first and THEN read that file; without
            it, a non-empty dir is refused. ``--config`` surfaces the divergence as a note rather
            than silently adopting or ignoring the file.)
    """
    global_path = (
        global_config_path if global_config_path is not None else _default_global_config_path()
    )
    repo_path = repo_root / CONFIG_RELATIVE_PATH

    # defaults(< preset) < global < repo. A missing layer contributes {} and is not blamed on error.
    raw = load_preset(preset) if preset else {}
    sources: list[Path] = []
    layer_paths = (global_path, repo_path) if include_repo else (global_path,)
    for path in layer_paths:
        layer = _read_toml(path)
        if layer:
            raw = _deep_merge(raw, layer)
            sources.append(path)
    where = " + ".join(str(p) for p in sources) or "configuration"

    try:
        config = SyncadeConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid configuration in {where}:\n{_format_validation_errors(exc)}"
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
                    where, "\n".join(f"  - {p}" for p in problems)
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
