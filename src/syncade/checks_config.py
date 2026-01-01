"""User-defined mechanical-check config.

:class:`CheckConfig` models one ``[[checks]]`` block — a mechanical gate the
orchestrator runs itself (a shell command + exit code), tagged ``blocking`` or
``advisory``. It lives in its OWN module because ``config.py`` is already over
the project's 400–500 LOC discipline; :class:`SyncadeConfig` imports this model
plus :func:`validate_check_names` and adds only the ``checks`` field and a thin
delegating validator. Empty ``checks`` list = today's loop, byte-identical.

Two-lane wall: a check is a command-with-an-exit-code ONLY. Anything needing
LLM judgment stays in ``reviewer.md`` — never a mechanical check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from syncade.worktree import TEST_WORKTREE_NAME

CheckSeverity = Literal["blocking", "advisory"]
"""``blocking`` → failure ORs into NO-SHIP exactly like a failing test leg (and
the producer, which runs on NO-SHIP, can fix it). ``advisory`` → failure is
surfaced but NEVER gates the verdict. Default is ``advisory`` — fail-safe, so a
forgotten tag cannot silently block a ship."""


class CheckConfig(BaseModel):
    """One mechanical check: a named shell command + a severity."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        description="Stable identifier used in run artifacts and as the "
        "check's worktree basename. Must be unique and must not collide with a "
        "reviewer name or the reserved 'tests' worktree basename.",
    )
    command: str = Field(
        min_length=1,
        description="Shell command run verbatim via ``sh -c`` in a fresh "
        "stripped worktree — the same mechanism as ``[loop] test_command``. "
        "Non-zero exit = the check failed.",
    )
    severity: CheckSeverity = Field(
        default="advisory",
        description="``blocking`` folds a failure into the mechanical verdict "
        "(NO-SHIP, like a failing test); ``advisory`` surfaces it without "
        "gating. Defaults to ``advisory`` so a forgotten tag never gates.",
    )

    @field_validator("name", "command")
    @classmethod
    def _not_whitespace(cls, value: str) -> str:
        """Reject ``"   "`` / ``"\\n\\t"`` — ``min_length`` passes them through
        (length is non-zero), but a whitespace command would silently SIGKILL
        the check leg and a whitespace name would make an unusable worktree
        basename. Mirrors ``LoopConfig._test_command_not_whitespace``."""
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only (got a blank string)")
        return value

    @field_validator("name")
    @classmethod
    def _name_plain_basename(cls, value: str) -> str:
        """The name is used as a worktree basename AND an artifact filename, so
        reject anything that isn't a plain basename — a check name must never be
        able to escape the round directory. Mirrors
        ``WorktreeManager._validate_reviewer_name``."""
        if value in (".", "..") or "/" in value or "\\" in value or Path(value).is_absolute():
            raise ValueError(
                f"check name {value!r} must be a plain basename "
                f"(no '/', '\\', '.', '..', or absolute paths)"
            )
        return value


def validate_check_names(reviewer_names: list[str], check_names: list[str]) -> None:
    """Raise ``ValueError`` if any check name collides on the per-round worktree
    path. Each configured check provisions a worktree at ``round-N/<name>/``, so
    a name must be unique among checks and must not equal (case-insensitively)
    any reviewer name or the reserved ``tests`` worktree basename.

    UNCONDITIONAL (unlike ``SyncadeConfig``'s reviewer-vs-``tests`` validator,
    which is gated on ``test_command``): checks always provision worktrees when
    configured, so reserving ``tests`` here also forecloses the latent collision
    the moment ``test_command`` is set. ``casefold()`` matches the existing
    validator — case-insensitive filesystems resolve ``Tests``/``tests`` to one
    path.
    """
    if not check_names:
        return
    seen: set[str] = set()
    reviewer_fold = {n.casefold() for n in reviewer_names}
    reserved = TEST_WORKTREE_NAME.casefold()
    for name in check_names:
        fold = name.casefold()
        if fold in seen:
            raise ValueError(f"duplicate check name {name!r} (check names must be unique)")
        seen.add(fold)
        if fold in reviewer_fold:
            raise ValueError(
                f"check name {name!r} collides with a reviewer name "
                f"(both provision a worktree at round-N/{name}/); rename the check"
            )
        if fold == reserved:
            raise ValueError(
                f"check name {name!r} collides with the reserved "
                f"{TEST_WORKTREE_NAME!r} test-re-run worktree basename "
                f"(case-insensitive); rename the check"
            )
