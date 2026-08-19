"""``[gc]`` config block (PR-v2-9): run-artifact retention shared by ``--gc`` AND auto-prune.

A LEAF module — pydantic only. Like :mod:`syncade.config_retry`, it deliberately does NOT import
:mod:`syncade.gc` (heavy: gc → gc_protection → orchestrator → config → back here), so the defaults
are LITERAL mirrors of ``gc.DEFAULT_KEEP`` / ``gc.DEFAULT_MAX_AGE_DAYS``, drift-guarded by a test
(``tests/config/test_gc_config.py::test_defaults_reproduce_the_runtime_defaults``).

These govern transcript pruning: the newest ``keep`` runs are always retained, and ``max_age_days``
(0 = disabled) is an ADDITIONAL floor — a beyond-keep run is pruned only if it is ALSO older. The
same policy feeds the auto-prune at each fresh loop AND the explicit ``syncade --gc`` pass, so the
two can never diverge. Run directories are NEVER deleted; only bulky subprocess transcripts are.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Drift-guarded literal mirrors of syncade.gc.DEFAULT_KEEP / DEFAULT_MAX_AGE_DAYS (see docstring).
_DEFAULT_KEEP = 20
_DEFAULT_MAX_AGE_DAYS = 0
#: Calibrated on 421 runs / 65 resume-protected: 14d releases 86% while keeping 9.
#: 7d buys 6% more for half the margin; 30d leaves 19 runs at ~130 MB/round accruing.
_DEFAULT_WORKTREE_MAX_AGE_DAYS = 14  # mirrors gc.DEFAULT_WORKTREE_MAX_AGE_DAYS


class GcConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep: int = Field(
        default=_DEFAULT_KEEP,
        ge=0,
        description=(
            "Newest N runs whose transcripts are ALWAYS kept when pruning (default "
            f"{_DEFAULT_KEEP}). Run directories are never deleted — only bulky subprocess "
            "transcripts. Governs BOTH the per-loop auto-prune and an explicit ``syncade --gc``."
        ),
    )
    max_age_days: int = Field(
        default=_DEFAULT_MAX_AGE_DAYS,
        ge=0,
        description=(
            "Additional age floor: a beyond-``keep`` run is pruned only if ALSO older than this "
            f"many days. {_DEFAULT_MAX_AGE_DAYS} (default) disables the age floor."
        ),
    )

    worktree_max_age_days: int = Field(
        default=_DEFAULT_WORKTREE_MAX_AGE_DAYS,
        ge=0,
        description=(
            "Days after which a run's WORKTREE is removed even though the run is still "
            f"resume-eligible (default {_DEFAULT_WORKTREE_MAX_AGE_DAYS}; 0 disables the bound "
            "and restores the previous never-ending protection). Tier 3 of the retention "
            "policy: a worktree is reconstructible from the SHA the run already records, so "
            "removing one costs a `git worktree add` and never history. Distinct from "
            "`max_age_days`, which gates TRANSCRIPTS only — one key cannot be non-zero for "
            "worktrees and zero for transcripts at once."
        ),
    )

    @field_validator("keep", "max_age_days", "worktree_max_age_days", mode="before")
    @classmethod
    def _strict_int(cls, value: object) -> object:
        # Pydantic's lax mode coerces a quoted number ("0"→0), an exact float (1.0→1), and a boolean
        # (bool subclasses int: false→0) silently. gc.keep=false/"0" → keep=0 would prune EVERY
        # transcript; a float/string is a typo. Reject all three (exit 50), accept only a plain int.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"gc integer fields must be plain integers (got {value!r}); quoted numbers, "
                "floats, and booleans are rejected"
            )
        return value
