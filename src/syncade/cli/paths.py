from __future__ import annotations

import sys
from pathlib import Path


def resolve_repo_relative_input_path(raw_path: str, *, repo_root: Path, label: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path

    repo_path = repo_root / path
    if repo_path.exists():
        return repo_path

    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        print(
            f"[syncade] {label}: {path} was not found under repo root; "
            f"using cwd-relative path {cwd_path}",
            file=sys.stderr,
        )
        return cwd_path

    return repo_path


def unique_markdown_path(directory: Path, stem: str) -> Path:
    path = directory / f"{stem}.md"
    if not path.exists():
        return path

    suffix = 2
    while True:
        candidate = directory / f"{stem}-{suffix}.md"
        if not candidate.exists():
            return candidate
        suffix += 1
