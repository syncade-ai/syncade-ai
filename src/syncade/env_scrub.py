"""Environment helpers that remove repo-root path references."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

PATH_LIST_ENV_KEYS = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "NODE_PATH",
        "CDPATH",
        "MANPATH",
        "INFOPATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "PKG_CONFIG_PATH",
    }
)

_VALUE_TOKEN_SPLIT = re.compile(r"[\s'\"=" + re.escape(os.pathsep) + r"]+")


def path_is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is within ``parent`` after resolution."""
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def value_references_repo_path(value: str, resolved_repo_root: Path) -> bool:
    """Detect repo-root path references without sibling-prefix false positives."""
    root_text = str(resolved_repo_root)
    start = value.find(root_text)
    while start != -1:
        following = value[start + len(root_text) : start + len(root_text) + 1]
        if not following or following in "/\\'\"=:,;)]}" or following.isspace():
            return True
        start = value.find(root_text, start + 1)
    for token in _VALUE_TOKEN_SPLIT.split(value):
        if not token:
            continue
        candidate = Path(token.removeprefix("file://")).expanduser()
        if candidate.is_absolute() and path_is_relative_to(candidate, resolved_repo_root):
            return True
    return False


def scrub_env_for_repo_paths(
    env: dict[str, str],
    repo_root: Path,
    *,
    preserve_keys: Iterable[str] = (),
) -> dict[str, str]:
    """Drop env entries whose values expose paths inside ``repo_root``."""
    resolved_repo_root = repo_root.resolve(strict=False)
    preserved = frozenset(preserve_keys)
    scrubbed: dict[str, str] = {}
    for key, value in env.items():
        if key in {"PWD", "OLDPWD"}:
            continue
        if key in preserved:
            scrubbed[key] = value
            continue
        if value_references_repo_path(key, resolved_repo_root):
            continue
        if key in PATH_LIST_ENV_KEYS:
            kept_segments = [
                segment
                for segment in value.split(os.pathsep)
                if not segment
                or not Path(segment).expanduser().is_absolute()
                or not path_is_relative_to(Path(segment).expanduser(), resolved_repo_root)
            ]
            if kept_segments:
                scrubbed[key] = os.pathsep.join(kept_segments)
            continue
        if value_references_repo_path(value, resolved_repo_root):
            continue
        scrubbed[key] = value
    return scrubbed
