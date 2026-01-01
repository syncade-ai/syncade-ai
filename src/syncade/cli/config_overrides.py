"""Apply per-invocation CLI overrides to a loaded :class:`SyncadeConfig` (PR-v2-9).

The CLI can override config for a single run: loop-level (``--max-rounds`` / ``--budget-tokens`` /
``--budget-usd``), per-reviewer (``--reviewer-model`` / ``--reviewer-thinking`` /
``--reviewer-timeout`` ``NAME=VALUE``, name-qualified + repeatable), and the worktree base
(``--worktree-base``). This module is the ONE place that owns that precedence — a CLI override beats
the config-file value — extracted from ``cli/__init__`` so the merge is unit-testable in isolation
rather than only end-to-end.

An override that cannot be applied — a NAME that is not a configured reviewer, or a VALUE the
reviewer schema rejects — raises :class:`OverrideError`, which the caller maps to exit 50 so a typo
in a flag reads the same as a typo in the file. (``--timeout`` is deliberately NOT here: it is
threaded to ``run_review`` as a separate arg, not patched into the config object — tests pin that
"the CLI doesn't pre-resolve". One consequence, by design: the ``run-init.json`` config snapshot
records the config + the loop overrides folded in HERE, but NOT ``--timeout``; read the CLI
invocation for the effective per-reviewer timeout on a ``--timeout`` run.)
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from syncade.config import ReviewerConfig, SyncadeConfig


class OverrideError(Exception):
    """A CLI override that can't be applied (unknown reviewer name / schema-rejected value)."""


def apply_cli_overrides(config: SyncadeConfig, args) -> SyncadeConfig:
    """Return ``config`` with every per-invocation CLI override applied (CLI beats config).

    Raises :class:`OverrideError` on an unknown reviewer name or a bad per-reviewer value.
    """
    config = _apply_loop_overrides(config, args)
    config = _apply_reviewer_overrides(config, args)
    config = apply_worktree_base_override(config, args)
    return config


def apply_worktree_base_override(config: SyncadeConfig, args) -> SyncadeConfig:
    """Return ``config`` with ``--worktree-base`` applied (CLI beats config); a no-op when unset.

    Standalone rather than folded into the reviewer/loop merge because BOTH a review run AND
    ``--doctor`` honor it, and ``--doctor`` does not route through :func:`apply_cli_overrides` —
    ``doctor_mode`` calls this directly so the writability preview matches the run it previews."""
    raw = getattr(args, "worktree_base", None)
    if raw is None:
        return config
    return config.model_copy(update={"worktree_base": Path(raw).expanduser()})


def _apply_loop_overrides(config: SyncadeConfig, args) -> SyncadeConfig:
    # --max-rounds / --budget-* patch [loop], only-set-if-given so an unset flag leaves the config
    # value (which may itself be None = no ceiling). One resolved value reaches the orchestrator.
    updates = {
        field: value
        for field, value in (
            ("max_rounds", args.max_rounds),
            ("budget_tokens", args.budget_tokens),
            ("budget_usd", args.budget_usd),
        )
        if value is not None
    }
    if not updates:
        return config
    return config.model_copy(update={"loop": config.loop.model_copy(update=updates)})


def _apply_reviewer_overrides(config: SyncadeConfig, args) -> SyncadeConfig:
    # Collect NAME -> {field: value} from the three repeatable name-qualified flags. A later flag
    # for the same NAME+field wins (argparse append order), which is the only sane last-wins rule.
    overrides: dict[str, dict[str, object]] = {}
    for attr, field in (
        ("reviewer_model", "model"),
        ("reviewer_thinking", "thinking"),
        ("reviewer_timeout", "timeout_seconds"),
    ):
        for name, value in getattr(args, attr, None) or []:
            if field == "timeout_seconds":
                # The schema is strict (rejects a TOML string), so parse the CLI string to a float
                # HERE — a non-numeric value is an OverrideError (exit 50), like a bad TOML value.
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise OverrideError(
                        f"--reviewer-timeout {name}={value!r}: not a number"
                    ) from exc
            overrides.setdefault(name, {})[field] = value
    if not overrides:
        return config

    configured = {reviewer.name for reviewer in config.reviewers}
    unknown = sorted(name for name in overrides if name not in configured)
    if unknown:
        raise OverrideError(
            f"--reviewer-* override names unknown reviewer(s) {unknown}; "
            f"configured reviewers: {sorted(configured)}"
        )

    new_reviewers = []
    for reviewer in config.reviewers:
        update = overrides.get(reviewer.name)
        if update:
            # Re-VALIDATE through the schema (a bare model_copy skips validators) so a bad tier or a
            # non-numeric / <=0 / non-finite timeout is rejected here, by the SAME rules as TOML.
            try:
                reviewer = ReviewerConfig.model_validate({**reviewer.model_dump(), **update})
            except ValidationError as exc:
                raise OverrideError(
                    f"invalid --reviewer-* override for {reviewer.name!r}: {_first_error(exc)}"
                ) from exc
        new_reviewers.append(reviewer)
    return config.model_copy(update={"reviewers": new_reviewers})


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(part) for part in err["loc"]) or "<value>"
    return f"{loc}: {err['msg']}"
