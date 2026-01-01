"""Shared helpers for the dispatcher test subdir.

All tests use :class:`FakeAdapter` (or trivial subclasses) via the
``adapter_factory`` injection point. No real CLIs spawned; only the
no-op subprocess ``/bin/true`` (whatever ``FakeAdapter._noop_argv()``
returns) actually executes, except for the timeout test which uses
``sleep``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from syncade.adapters.base import (
    ReviewerAdapter,
)
from syncade.config import ReviewerConfig
from syncade.findings import Finding, ReviewerOutput


def _config(
    name: str,
    provider: str = "fake",
    *,
    model: str = "test-model",
    permissions: str = "yolo",
) -> ReviewerConfig:
    return ReviewerConfig(
        name=name,
        provider=provider,
        model=model,
        permissions=permissions,  # type: ignore[arg-type]
    )


def _factory_returning(*adapters: ReviewerAdapter) -> Callable[[str], ReviewerAdapter]:
    """Return an adapter_factory whose lookups consume the supplied
    adapters in order. One adapter per dispatcher call — the factory
    is invoked once per ReviewerConfig at lookup time.

    The iterator consumption is lock-guarded. The current dispatcher
    runs Phase 1 (adapter lookup) serially, so the lock is uncontended
    today — but it keeps the test correct under a future refactor that
    parallelizes lookup. Cheap defensiveness.
    """
    iterator = iter(adapters)
    lock = threading.Lock()

    def factory(_provider: str) -> ReviewerAdapter:
        with lock:
            try:
                return next(iterator)
            except StopIteration as exc:
                raise RuntimeError(
                    "factory exhausted — test supplied fewer adapters than configs"
                ) from exc

    return factory


def _worktree_paths(*names: str, tmp_path: Path) -> dict[str, Path]:
    """Create one subdirectory per reviewer name and return the map."""
    out: dict[str, Path] = {}
    for name in names:
        wt = tmp_path / name
        wt.mkdir()
        out[name] = wt
    return out


def _ship() -> ReviewerOutput:
    return ReviewerOutput(
        verdict="SHIP",
        findings=[],
        summary="dispatcher test SHIP",
        priority_order=[],
        coverage_gaps=[],
        dismissed_concerns=[],
    )


def _no_ship_with_finding() -> ReviewerOutput:
    return ReviewerOutput(
        verdict="NO-SHIP",
        findings=[
            Finding(
                severity="blocker",
                file="src/x.py",
                spec_clause="G1",
                finding="problem",
            )
        ],
        summary="dispatcher test NO-SHIP with one blocker",
        priority_order=[0],
        coverage_gaps=[],
        dismissed_concerns=[],
    )
