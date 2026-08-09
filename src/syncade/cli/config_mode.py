"""``syncade --config`` — inspect and edit the config layers (pr-v2-30)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import get_args, get_origin

from pydantic import ValidationError

from syncade import config_loader
from syncade.cli import config_keys
from syncade.cli.toml_writer import render
from syncade.config import SyncadeConfig
from syncade.config_loader import (
    _PAIRED_SECTIONS,
    CONFIG_RELATIVE_PATH,
    ConfigError,
    _deep_merge,
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
        print(f"[syncade] config error:\n{exc}", file=sys.stderr)
        return 50

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
_LIST_SECTIONS = frozenset(
    name for name, f in SyncadeConfig.model_fields.items() if get_origin(f.annotation) is list
)


# Models each provider is recognizably the owner of (the curated picker lists + defaults + common
# aliases). A model owned by the OTHER provider is a mismatch; a genuinely off-map custom string is
# allowed (the adapter validates it at dispatch). Prefix rules catch off-list variants (o3-mini, …).
_OPENAI_MODELS = {"gpt-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "o3", "o4-mini"}
_ANTHROPIC_MODELS = {
    "claude-sonnet-4-6",
    "claude-opus-4-8",
    "claude-haiku-4-5",
    "opus",
    "haiku",
    "sonnet",
}


def _model_provider_hint(model: str) -> str | None:
    """Best-effort: which provider a model name obviously belongs to (known names + prefixes), or
    None for a genuinely off-map custom string."""
    if model in _OPENAI_MODELS or model.startswith(("gpt-", "o3", "o4")):
        return "openai"
    if model in _ANTHROPIC_MODELS or model.startswith("claude-"):
        return "anthropic"
    return None


def _cross_provider_error(provider: str, model: str) -> str | None:
    """A fix-it message when the model is recognizably the OTHER known provider's; else None.
    Catches off-prefix models (``o3``, ``o4-mini``, ``opus``), not just ``gpt-*``/``claude-*``."""
    hint = _model_provider_hint(model)
    if hint is not None and provider in ("anthropic", "openai") and hint != provider:
        appropriate = "claude-*" if provider == "anthropic" else "gpt-*"
        return (
            f"model {model!r} looks like {hint}'s but provider is {provider!r}; "
            f"set provider={hint!r} first, or pick a {appropriate} model"
        )
    return None


def _unknown_reviewer_provider_error(provider: str) -> str | None:
    """Return an error message if ``provider`` is not a registered reviewer adapter, else None."""
    from syncade.adapters.registry import known_providers

    known = known_providers()
    if provider not in known:
        return f"unknown reviewer provider {provider!r}; known: {', '.join(known)}"
    return None


def _rederive_model(section: str, provider: str) -> str:
    """The default model for ``(section, provider)`` so a provider edit keeps the pair valid in the
    SAME write. For paired actors (producer/synthesizer/drafter/auditor) pydantic auto-fills the
    model; for reviewers (no schema default) we fall back to ``_PROVIDER_DEFAULT_MODELS``."""
    if section == "reviewers":
        return _PROVIDER_DEFAULT_MODELS.get(provider, "gpt-5.5")
    resolved = SyncadeConfig.model_validate({section: {"provider": provider}})
    return getattr(resolved, section).model


def _keep_int_if_equal(existing, value):
    """When coercing a float from CLI/menu, preserve an existing int token that is semantically
    equal so that ``timeout_seconds = 1800`` is not rewritten as ``1800.0`` on a no-op edit.
    Guards booleans, which satisfy ``int(True) == 1`` but must never silently substitute."""
    if (
        isinstance(value, float)
        and isinstance(existing, int)
        and not isinstance(existing, bool)
        and float(existing) == value
    ):
        return existing
    return value


def _apply(target_raw: dict, key: str, value, config) -> None:
    """Set the dotted ``key`` to ``value`` in ``target_raw`` (the target layer's raw dict).

    A wholesale-replace section absent from the target is MATERIALIZED from ``config`` first, so
    editing one field can't drop the pair-consistent rest: the paired actors (``_PAIRED_SECTIONS``)
    and the list rosters (``_LIST_SECTIONS``) dump their whole section/list; a key-by-key section
    (loop, review, retry, gc, pricing) sets just the (possibly nested) key. Setting an actor
    ``.provider`` re-derives its paired ``model`` in the same write."""
    parts = key.split(".")
    if len(parts) == 1:  # top-level scalar (e.g. worktree_base)
        target_raw[parts[0]] = value
        return
    section = parts[0]
    if section in _LIST_SECTIONS:  # reviewers/checks: [section, index, field] — materialize roster
        index, field = int(parts[1]), parts[2]
        if section not in target_raw:
            target_raw[section] = [m.model_dump() for m in getattr(config, section)]
        existing = target_raw[section][index].get(field)
        target_raw[section][index][field] = _keep_int_if_equal(existing, value)
        if field == "provider":
            target_raw[section][index]["model"] = _rederive_model(section, value)
        return
    if section in _PAIRED_SECTIONS:  # producer/synthesizer/drafter/auditor — materialize section
        field = parts[1]
        if section not in target_raw:
            target_raw[section] = getattr(config, section).model_dump()
        existing = target_raw[section].get(field)
        target_raw[section][field] = _keep_int_if_equal(existing, value)
        if field == "provider":
            target_raw[section]["model"] = _rederive_model(section, value)
        return
    # key-by-key merge section: set just the (possibly nested) key, creating sub-tables as needed.
    node = target_raw.setdefault(section, {})
    for part in parts[1:-1]:
        node = node.setdefault(part, {})
    # Guard: a malformed node (non-dict) surfaces its existing TypeError on the assignment below,
    # not here — so apply_edit's exception handler can catch it.
    existing = node.get(parts[-1]) if isinstance(node, dict) else None
    node[parts[-1]] = _keep_int_if_equal(existing, value)


def _existing_text(path: Path) -> str:
    """The file's current text, or "" when absent/unreadable — what :func:`render` preserves
    comments from. A read problem must degrade to a full rewrite, never block the write.

    ``newline=""`` disables Python's universal-newline translation so CRLF files reach
    :func:`render` with their ``\\r\\n`` intact; :func:`render` detects CRLF and normalises
    any inserted lines to match."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # newline="" writes the string verbatim — CRLF produced by render() reaches the file as CRLF
    # rather than being re-translated on Windows (and is a no-op on Unix).
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    tmp.replace(path)


def _cmd_set(
    config, rest: list[str], *, repo_root: Path, global_path: Path, target_repo: bool
) -> int:
    if len(rest) != 2:
        print("[syncade] --config set: expects <key> <value>", file=sys.stderr)
        return 2
    key, raw_value = rest
    try:
        annotation = config_keys.resolve_annotation(key)
    except config_keys.UnknownKey as exc:
        print(f"[syncade] --config set: {exc}", file=sys.stderr)
        return 2  # a bad KEY is a usage error (exit 2); a bad VALUE is a config error (exit 50)
    parts = key.split(".")
    section, field = parts[0], parts[-1]

    if target_repo:
        # A per-repo config only matters inside a git repo — a real run resolves the repo via a HARD
        # discover, so writing one in a non-git dir fabricates a file no run will ever read.
        try:
            discover_repo_root(repo_root)
        except SnapshotError:
            print(
                f"[syncade] --config set --repo: {repo_root} is not inside a git repository; "
                "a per-repo config there would never be read. Use the global config (omit --repo).",
                file=sys.stderr,
            )
            return 60

    repo_path = repo_root / CONFIG_RELATIVE_PATH
    global_raw, repo_raw = _read_toml(global_path), _read_toml(repo_path)
    target_raw = repo_raw if target_repo else global_raw
    target_path = repo_path if target_repo else global_path

    # Materializing an absent section (so one field-edit can't reset the pair-consistent whole) must
    # pull from the layer the TARGET inherits, not the full effective config. A repo edit inherits
    # defaults+global (== effective when the section is absent from repo), so `config` is right. A
    # GLOBAL edit inherits defaults+global only — using `config` would bake the current repo's
    # producer/reviewer choices into ~/.syncade. Rebuild global-only for that case.
    if target_repo:
        mat_config = config
    else:
        try:
            mat_config = SyncadeConfig.model_validate(global_raw)
        except ValidationError:
            mat_config = config  # a global file invalid on its own; effective is the safe fallback

    if section in _LIST_SECTIONS and len(parts) >= 2 and parts[1].isdigit():
        index = int(parts[1])
        existing = target_raw.get(section)
        roster = len(existing) if existing is not None else len(getattr(mat_config, section))
        if index >= roster:
            print(
                f"[syncade] --config set: {section} {index} out of range (has {roster})",
                file=sys.stderr,
            )
            return 2
    try:
        value = config_keys.coerce(annotation, raw_value)
    except config_keys.InvalidValue:
        # A bad VALUE is a config error (exit 50), matching the schema-invalid path below and the
        # exit-50 contract in docs/skills — distinct from a bad KEY (exit 2, a usage error).
        print(
            f"[syncade] --config set: {raw_value!r} is not a valid value for {key}; file unchanged",
            file=sys.stderr,
        )
        return 50

    if field == "provider" and section == "reviewers":
        err = _unknown_reviewer_provider_error(str(value))
        if err:
            print(f"[syncade] --config set: {err}; file unchanged", file=sys.stderr)
            return 50

    try:
        _apply(target_raw, key, value, mat_config)
        # After materializing the section, check for an obvious cross-provider model mismatch
        # before pydantic validation (the schema accepts arbitrary model strings).
        if field == "model":
            actor_raw = (
                target_raw[section][int(parts[1])]
                if section in _LIST_SECTIONS
                else target_raw.get(section, {})
            )
            pair_err = _cross_provider_error(actor_raw.get("provider", ""), str(value))
            if pair_err:
                print(f"[syncade] --config set: {pair_err}", file=sys.stderr)
                return 50
        # Validate the FILE BEING WRITTEN on its own (schema only), before touching disk. In
        # isolation, not merged: a bad global value could be masked by a repo section-replace, but
        # the file must stand on its own — else it breaks the moment it's used without that repo.
        SyncadeConfig.model_validate(target_raw)
        # Also validate the merged effective config (global + repo) to catch cross-layer conflicts
        # such as duplicate reviewer/check names split across layers. global_raw and repo_raw each
        # point to the post-_apply dict for their respective target, so the merge is always current.
        SyncadeConfig.model_validate(_deep_merge(copy.deepcopy(global_raw), repo_raw))
    except (ValidationError, KeyError, TypeError, IndexError, AttributeError) as exc:
        if isinstance(exc, ValidationError):
            print(
                "[syncade] --config set: rejected — the result would be invalid, file unchanged:\n"
                f"{config_loader._format_validation_errors(exc)}",
                file=sys.stderr,
            )
        else:
            print(
                f"[syncade] --config set: malformed config section, file unchanged: {exc}",
                file=sys.stderr,
            )
        return 50

    try:
        _atomic_write(target_path, render(target_raw, _existing_text(target_path)))
    except OSError as exc:
        print(f"[syncade] --config set: cannot write {target_path}: {exc}", file=sys.stderr)
        return 60
    print(f"[syncade] set {key} = {value}  ({'repo' if target_repo else 'global'}: {target_path})")
    return 0
