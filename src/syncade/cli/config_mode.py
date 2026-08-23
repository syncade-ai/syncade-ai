"""``syncade --config`` — inspect and edit the config layers (pr-v2-30)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args, get_origin

from syncade import config_loader
from syncade.cli import config_keys
from syncade.config import SyncadeConfig
from syncade.config_loader import (
    CONFIG_RELATIVE_PATH,
    ConfigError,
    _read_toml,
    load_config,
)
from syncade.snapshot import SnapshotError, discover_repo_root

_VERBS = ("list", "get", "set")

# Default model per provider, mirroring config_cold._COLD_MODELS. Used when a provider
# edit re-derives the paired model for a reviewer (which has no schema-level default).
_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.5",
}


def _resolve_repo_root(hint: str | None) -> tuple[Path, bool]:
    """The repo whose ``.syncade/config.toml`` is the repo layer, and whether we are actually inside
    a git repo. ``--config`` is mostly about the global file, so being outside a repo is fine — and
    ``--config`` inspects the CURRENT state, so with no git repo there is no repo layer *yet* and
    callers pass ``include_repo=False`` (nothing is labelled repo). NB a *review* run would ``git
    init`` here and THEN read a ``cwd/.syncade/config.toml`` — that divergence is surfaced by
    :func:`_ignored_repo_config_note`, not by pretending the file cannot exist."""
    base = Path(hint) if hint else Path.cwd()
    try:
        return discover_repo_root(base), True
    except SnapshotError:
        return base.resolve(), False


def _get(config, key: str):
    """Resolve a dotted ``key`` against the effective config, restricted to pydantic model fields.
    Rejects non-schema attributes (``model_dump``, ``__dict__``) and negative list indices."""
    obj = config
    for part in key.split("."):
        if isinstance(obj, list):
            idx = int(part)  # ValueError → caught by _cmd_get
            if idx < 0:
                raise IndexError("negative list index not allowed")
            obj = obj[idx]
        else:
            fields = getattr(type(obj), "model_fields", None)
            if fields is None or part not in fields:
                raise AttributeError(f"unknown field: {part!r}")
            obj = getattr(obj, part)
    return obj


def _layer_of(global_raw: dict, repo_raw: dict, section: str, subkey: str | None) -> str:
    """Which layer set this value: highest layer that defines the section (or, for ``[loop]`` scalar
    knobs, the specific subkey); else ``default``. Mirrors the loader's merge rules."""
    for label, raw in (("repo", repo_raw), ("global", global_raw)):
        sec = raw.get(section)
        if sec is None:
            continue
        if subkey is None or (isinstance(sec, dict) and subkey in sec):
            return label
    return "default"


def _settings(config):
    """The surfaced settings (pr-v2-30 D6) as ``(key, label, section, subkey)``; reviewer rows
    expand to the resolved roster so N reviewers each get a row."""
    rows = [("producer.model", "Producer model", "producer", None)]
    for i in range(len(config.reviewers)):
        rows.append((f"reviewers.{i}.model", f"Reviewer {i + 1} model", "reviewers", None))
    rows += [
        ("synthesizer.model", "Judge model", "synthesizer", None),
        ("loop.max_rounds", "Rounds (max)", "loop", "max_rounds"),
        ("loop.timeout_seconds", "Time per subprocess (s)", "loop", "timeout_seconds"),
        ("loop.budget_usd", "Cost cap (USD)", "loop", "budget_usd"),
    ]
    return rows


def _shown(config, key: str) -> str:
    """A model row shows ``provider / model`` for context; a scalar shows its value or ``none``.
    Returns the RAW value (control chars intact) so the curated ``--config list`` / ``get`` stay
    byte-compatible; the ``--all`` renderer single-lines it via :func:`_single_line`."""
    if key.endswith(".model"):
        provider = _get(config, key.rsplit(".", 1)[0] + ".provider")
        return f"{provider} / {_get(config, key)}"
    value = _get(config, key)
    return "— none —" if value is None else str(value)


def _single_line(text: str) -> str:
    """Escape control characters so a value can never split or overwrite its physical row in the
    ``--all`` dump (a machine-readable-enough one-row-per-key surface). ``\\n``/``\\r``/``\\t`` get
    readable escapes; any other C0 control char becomes ``\\xNN``."""
    named = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return "".join(named.get(c) or (f"\\x{ord(c):02x}" if ord(c) < 0x20 else c) for c in text)


def run_config(config_args: list[str], *, args) -> int:
    verb = config_args[0] if config_args else None
    if verb is not None and verb not in _VERBS:
        print(
            f"[syncade] --config: unknown verb {verb!r} (use: list | get <key> | set <key> <val>)",
            file=sys.stderr,
        )
        return 2

    repo_root, in_git = _resolve_repo_root(getattr(args, "repo_root", None))
    # Resolve the global path ONCE (module-qualified so tests can isolate it), and thread the SAME
    # path into load_config so the effective config and the provenance read agree on the layer.
    # Outside a git repo, drop the repo layer entirely (include_repo=in_git): --config reflects the
    # current state (no repo → no repo layer). A present non-git config is noted, not read.
    global_path = config_loader._default_global_config_path()
    try:
        config = load_config(
            repo_root,
            check_api_keys=False,
            global_config_path=global_path,
            include_repo=in_git,
        )
    except ConfigError as exc:
        if verb != "set":
            print(f"[syncade] config error:\n{exc}", file=sys.stderr)
            return 50
        # `set` IS the repair tool. Refusing to run it because the config is invalid is
        # the one failure mode that leaves an operator with no way out but a text editor
        # — and a schema change that retires a released value (PR-h-05's producer
        # `yolo` -> `confined`) lands exactly there. Fall back to schema defaults, which
        # `_cmd_set` uses ONLY to materialize an absent section; it still validates the
        # file it writes and the merged result, so a broken config cannot get worse here.
        print(
            f"[syncade] config error — running `set` anyway so it can repair this:\n{exc}",
            file=sys.stderr,
        )
        config = SyncadeConfig.model_validate({})

    if verb == "list":
        if len(config_args) > 1:
            print(
                f"[syncade] --config list: takes no arguments (got: {config_args[1:]!r}); "
                "did you mean `syncade <PR_DOC>` for a review run?",
                file=sys.stderr,
            )
            return 2
        return _cmd_list(
            config, global_path, repo_root, in_git, show_all=bool(getattr(args, "all", False))
        )
    if verb == "get":
        return _cmd_get(config, config_args[1:], repo_root=repo_root, in_git=in_git)
    if verb == "set":
        return _cmd_set(
            config,
            config_args[1:],
            repo_root=repo_root,
            global_path=global_path,
            target_repo=bool(getattr(args, "repo", False)),
        )
    # no verb -> the interactive arrow-menu (Issue 3)
    from syncade.cli.config_tui import run as run_tui

    return run_tui(global_path=global_path, repo_root=repo_root, in_git=in_git)


def _ignored_repo_config_note(repo_root: Path, in_git: bool) -> str | None:
    """`--config` inspects the CURRENT state, so outside a git repo it does not read
    ``cwd/.syncade/config.toml`` (no repo → no repo layer). A REVIEW run with
    ``--allow-auto-init`` would ``git init`` the dir and THEN consume that file; without
    ``--allow-auto-init``, a non-empty dir is refused. Either way there is a divergence between
    ``--config`` inspection and a run — surfaced as a note (on stderr, so ``get`` stdout stays
    clean)."""
    if in_git:
        return None
    repo_cfg = repo_root / CONFIG_RELATIVE_PATH
    if not repo_cfg.is_file():
        return None
    return (
        f"[syncade] note: {repo_cfg} exists but this directory is not a git repo, so --config does "
        "not read it. Run `git init` to manage it as the repo layer."
    )


def _explicitly_set(raw: dict, key: str) -> bool:
    """Does the raw layer dict explicitly set this dotted ``key`` (vs inheriting a default)?"""
    node = raw
    for part in key.split("."):
        if isinstance(node, list):
            idx = int(part)
            if idx >= len(node):
                return False
            node = node[idx]
        elif isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        else:
            return False
    return True


def _raw_get(raw: dict, key: str):
    """Navigate a raw nested dict/list by dotted key (no pydantic field validation)."""
    node = raw
    for part in key.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def _default_provider_for(prefix: str) -> str | None:
    """Schema-default provider for an actor key prefix (e.g. ``'producer'``, ``'synthesizer'``),
    used when the global raw layer sets ``.model`` but omits ``.provider``. Returns ``None`` when
    the actor's ``provider`` is REQUIRED (no schema default — a ``[[reviewers]]`` entry), so the
    masked-value display never FABRICATES a provider the global layer did not set."""
    cls = SyncadeConfig
    for part in prefix.split("."):
        if get_origin(cls) is list:
            cls = get_args(cls)[0]  # element type; `part` is the index
            continue
        cls = cls.model_fields[part].annotation
    field = getattr(cls, "model_fields", {}).get("provider")
    if field is not None and field.is_required():
        return None  # reviewers: provider is required, there is no default to honestly show
    try:
        return _get(SyncadeConfig.model_validate({}), prefix + ".provider")
    except (AttributeError, IndexError, ValueError, KeyError, TypeError):
        return "openai"  # safe fallback for a defaulted-provider actor


def _raw_shown(global_raw: dict, key: str) -> str | None:
    """Display string for ``key`` read directly from the raw global layer dict.

    Applies per-field coercion so numeric types (int→float) display the same way ``_shown``
    formats the pydantic-coerced effective value — enabling an accurate ``==`` comparison.
    Falls back to the raw string when the key is invalid or off-schema. Returns ``None`` when
    the key path is absent or broken (caller skips the note).
    """
    try:
        if key.endswith(".model"):
            prefix = key.rsplit(".", 1)[0]
            model = _raw_get(global_raw, key)
            try:
                provider = _raw_get(global_raw, prefix + ".provider")
            except (KeyError, IndexError, TypeError):
                # Global sets .model but omits .provider. For a defaulted-provider actor, show the
                # schema default (accurate). For reviewers (provider REQUIRED, no default) show the
                # bare model — never fabricate a provider the global layer did not set.
                provider = _default_provider_for(prefix)
                if provider is None:
                    return str(model)
            return f"{provider} / {model}"
        value = _raw_get(global_raw, key)
        if value is None:
            return "— none —"
        # Coerce through the schema type so numeric formatting matches _shown() (e.g. int 2400 →
        # float 2400.0). Falls back to the raw value when the field is invalid or off-schema.
        # Skip re-coercion for already-decoded list values: str(list) followed by CSV parsing
        # mangles the value (e.g. list[str] becomes ["['a'", " 'b']"]).
        try:
            annotation = config_keys.resolve_annotation(key)
            if not (get_origin(annotation) is list and isinstance(value, list)):
                value = config_keys.coerce(annotation, str(value))
        except (config_keys.UnknownKey, config_keys.InvalidValue):
            pass
        return str(value)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _overrides_note(config, global_raw: dict, key: str, layer: str) -> str:
    """The ` — overrides global <value>` shadow note: fires only when ``repo`` wins AND ``global``
    explicitly sets a DIFFERENT value for this key. Reads from the raw global dict directly so
    masked invalid sections report their actual values, not schema defaults."""
    if layer != "repo" or not _explicitly_set(global_raw, key):
        return ""
    masked = _raw_shown(global_raw, key)
    if masked is None or masked == _shown(config, key):
        return ""
    return f" — overrides global {_single_line(masked)}"


def _print_all(config, global_raw: dict, repo_raw: dict) -> None:
    from syncade.cli import config_list

    section = None
    for key, label, sec, subkey in config_list.all_rows(config):
        if sec != section:
            section = sec
            print(config_list.header_for(sec))
        layer = _layer_of(global_raw, repo_raw, sec, subkey)
        note = _overrides_note(config, global_raw, key, layer)
        print(f"  {label:<26} {_single_line(_shown(config, key)):<30} ({layer}{note})  [{key}]")


def _cmd_list(
    config, global_path: Path, repo_root: Path, in_git: bool, *, show_all: bool = False
) -> int:
    global_raw = _read_toml(global_path)
    # Outside a git repo there is no repo layer (see _resolve_repo_root); {} so nothing is
    # mis-attributed to `repo`. A present-but-ignored non-git config is surfaced as a note below.
    repo_raw = _read_toml(repo_root / CONFIG_RELATIVE_PATH) if in_git else {}
    print(f"[syncade] config (effective; global: {global_path})")
    if show_all:
        _print_all(config, global_raw, repo_raw)
        note = _ignored_repo_config_note(repo_root, in_git)
        if note:
            print(note, file=sys.stderr)
        return 0
    for key, label, section, subkey in _settings(config):
        layer = _layer_of(global_raw, repo_raw, section, subkey)
        print(f"  {label:<23} {_shown(config, key):<28} ({layer})  [{key}]")
    note = _ignored_repo_config_note(repo_root, in_git)
    if note:
        print(note, file=sys.stderr)
    return 0


def _cmd_get(config, rest: list[str], *, repo_root: Path, in_git: bool) -> int:
    if len(rest) != 1:
        print("[syncade] --config get: expects exactly one <key>", file=sys.stderr)
        return 2
    key = rest[0]
    try:
        value = _get(config, key)
    except (AttributeError, IndexError, ValueError, KeyError, TypeError):
        print(f"[syncade] --config get: unknown key {key!r}", file=sys.stderr)
        return 2
    note = _ignored_repo_config_note(repo_root, in_git)
    if note:
        print(note, file=sys.stderr)
    print("— none —" if value is None else value)
    return 0


# --- set (2b, generalized to the whole schema in pr-v2-31 Increment 2) ---------------------------
# Top-level LIST sections (reviewers, checks) whose TOML *list* replaces wholesale on override — so
# editing one element materializes the whole roster first (like the paired _PAIRED_SECTIONS models).
# Everything else (loop, review, retry, gc, pricing) merges key-by-key. Derived from the schema so a
# new list-of-models section is handled automatically. Key RESOLUTION + type COERCION live in
# ``config_keys`` (shared with the menu); this module owns materialization + the pairing guards.

from syncade.cli.config_set import _cmd_set  # noqa: E402
