"""The `--config set` write path: coerce, apply, validate, write.

Split out of ``config_mode`` (PR-h-05) when the merged-config non-regression check pushed that
module over the blocking 500-code-LOC gate. The seam is read-vs-write: ``config_mode`` renders
``list``/``get`` and owns verb dispatch; everything that can touch a file on disk lives here.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import get_origin

from pydantic import ValidationError

from syncade import config_loader
from syncade.cli import config_keys
from syncade.cli.config_mode import _PROVIDER_DEFAULT_MODELS
from syncade.cli.toml_writer import render
from syncade.config import SyncadeConfig
from syncade.config_loader import (
    _PAIRED_SECTIONS,
    CONFIG_RELATIVE_PATH,
    _deep_merge,
    _read_toml,
)
from syncade.snapshot import SnapshotError, discover_repo_root

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


def _merge_errors(global_raw: dict, repo_raw: dict) -> dict[tuple[str, ...], str]:
    """Cross-layer validation errors of the merged config, keyed by field location."""
    try:
        SyncadeConfig.model_validate(_deep_merge(copy.deepcopy(global_raw), repo_raw))
    except ValidationError as exc:
        return {tuple(str(part) for part in e["loc"]): e["msg"] for e in exc.errors()}
    return {}


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
    # Snapshot the cross-layer errors BEFORE `_apply` mutates a raw layer in place, so the
    # merged check below can blame this edit for what it ADDS and nothing else.
    errors_before = _merge_errors(global_raw, repo_raw)
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
    if raw_value == "" and key in config_keys.BUDGET_KEYS:
        print(
            f"[syncade] --config set: set {key!r} to 0 to disable the ceiling;"
            " clearing to empty is not supported (empty would omit the key,"
            " reactivating the default); file unchanged",
            file=sys.stderr,
        )
        return 50
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
        # Also check the merged effective config (global + repo) for cross-layer conflicts such as
        # duplicate reviewer/check names split across layers. global_raw and repo_raw each point to
        # the post-_apply dict for their respective target, so the merge is always current.
        #
        # NON-REGRESSION, not absolute: an error the merge ALREADY had is not this edit's fault,
        # and blaming it deadlocks repair — with the same retired value in both layers, `set` and
        # `set --repo` each get refused because of the other, and neither layer can ever be fixed.
        # The written file was validated ON ITS OWN above, so it still stands alone either way.
        errors_after = _merge_errors(global_raw, repo_raw)
        introduced = {
            loc: msg for loc, msg in errors_after.items() if errors_before.get(loc) != msg
        }
        if introduced:
            rendered = "\n".join(
                f"  - {'.'.join(loc) or '<root>'}: {msg}" for loc, msg in introduced.items()
            )
            print(
                "[syncade] --config set: rejected — this edit would break the merged "
                f"global+repo config, file unchanged:\n{rendered}",
                file=sys.stderr,
            )
            return 50
        remaining = {loc: msg for loc, msg in errors_after.items() if loc not in introduced}
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
    if remaining:
        rendered = "\n".join(
            f"  - {'.'.join(loc) or '<root>'}: {msg}" for loc, msg in remaining.items()
        )
        print(
            "[syncade] note: the merged global+repo config is still invalid elsewhere; "
            f"a review run will refuse until these are repaired too:\n{rendered}",
            file=sys.stderr,
        )
    return 0
