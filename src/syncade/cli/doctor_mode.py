"""``syncade --doctor`` dispatch — read-only run preflight (PR-v2-12).

Resolves the git repo root + loads ``.syncade/config.toml`` like every other one-shot
mode, then hands off to :func:`syncade.doctor.run_doctor`. Error mapping mirrors the
siblings in :mod:`syncade.cli.modes`: repo-discovery failures → exit 60, config errors →
exit 50; doctor's own exit codes (0 / 60) pass through verbatim.

Unlike ``--auth-check`` / ``--gc`` / ``--metrics``, doctor does **not** reject
``--base`` / ``--scope`` — it is the one preflight that previews the diff those flags
select (the run-plan preview resolves that base and reports its diff size). So there is no
``_reject_diff_base_flags`` guard here, deliberately; ``--base`` / ``--scope`` (and the
other run-shaping flags) are threaded into ``run_doctor`` below.
"""

from __future__ import annotations

import sys
from pathlib import Path

from syncade.config_loader import ConfigError, load_config
from syncade.exit_codes import CONFIG_ERROR, WORKTREE_ERROR
from syncade.snapshot import SnapshotError, discover_repo_root

from .config_overrides import apply_worktree_base_override


def _run_doctor(args) -> int:
    """Dispatch ``syncade --doctor``."""
    # Local import keeps the doctor engine out of the default-import cost.
    from syncade.doctor import run_doctor

    repo_root_hint = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    try:
        repo_root = discover_repo_root(repo_root_hint)
    except SnapshotError as exc:
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    def _emit_deprecation(message: str) -> None:
        print(f"[syncade] {message}", file=sys.stderr)

    try:
        config = load_config(repo_root, preset=args.preset, deprecation_callback=_emit_deprecation)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    # --worktree-base overrides config.worktree_base so the writability preview probes the base the
    # real run would use (doctor does not route through apply_cli_overrides — see config_overrides).
    config = apply_worktree_base_override(config, args)

    # Mirror the CLI's own run resolution so the branch preview matches what a real
    # `syncade <pr-doc>` would do for the same flags (C1). repo always pre-exists here
    # (discover_repo_root succeeded), so the auto-init default-branch exemption never applies.
    # NOTE: there is deliberately no PR-doc here. `--doctor` is a one-shot mode and the CLI
    # rejects `--doctor` with a PR_DOC positional (cli/validate.py), so any `args.pr_doc`
    # plumbing would be unreachable — a dogfood round shipped exactly that and a blind panel
    # caught it. The prompt-size preview renders with a placeholder ref instead, which makes
    # its number a LOWER BOUND; see doctor_preview.check_plan for why that is still useful.
    return run_doctor(
        config,
        repo_root,
        quick=args.quick,
        max_rounds=args.max_rounds,
        allow_default_branch=args.allow_default_branch,
        force_dirty=args.force_dirty,
        base_ref=args.base,
        scope=args.scope,
        two_dot=getattr(args, "two_dot", False),
        quiet=args.quiet,
        timeout_seconds=args.timeout,
    )
