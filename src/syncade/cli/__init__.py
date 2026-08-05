"""Argparse-based CLI entry point for the syncade orchestrator.

The parser, one-shot mode handlers, and diff-base/spec-source resolvers live in
the package siblings. The main review path (``_run``) and ``main`` stay here so
the ``run_review`` import lives in the same namespace ``_run`` reads it from —
that keeps the ``syncade.cli.run_review`` monkeypatch working without any
rebind. Public CLI helpers are re-exported here so supported ``syncade.cli.X``
import paths remain stable.
"""

from __future__ import annotations

import sys
from pathlib import Path

from syncade import run_status
from syncade.cli.auth_gate import auth_gate
from syncade.config_auth import REVIEW_BLOCKS
from syncade.config_loader import ConfigError, load_config
from syncade.exit_codes import (
    CLI_USAGE_ERROR,
    CONFIG_ERROR,
    WORKTREE_ERROR,
)
from syncade.git_preconditions import GitUnavailableError, ensure_repo_initialized
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.branch_guard import current_branch_name, guard_default_branch
from syncade.process import SubprocessError
from syncade.snapshot import SnapshotError, discover_repo_root
from syncade.worktree import WorktreeError

from .config_overrides import OverrideError, apply_cli_overrides
from .modes import (
    _DRAFT_DIALOGUE_SOFT_CAP,
    _extract_config_operands,
    _run_auth_check,
    _run_doctor,
    _run_draft_spec,
    _run_gc,
    _run_metrics,
    _run_resume,
    _run_selfcheck,
    _run_spec_audit,
)
from .parser import _max_rounds, _positive_float, build_parser
from .paths import resolve_repo_relative_input_path
from .resolve import (
    _cli_proves_commit,
    _current_branch,
    _resolve_openspec_pr_doc,
    _resolve_scope_base,
)
from .validate import validate_command_shape

__all__ = [
    "main",
    "build_parser",
    "run_review",
    "_run",
    "_positive_float",
    "_max_rounds",
    "_current_branch",
    "_resolve_scope_base",
    "_resolve_openspec_pr_doc",
    "_run_selfcheck",
    "_run_auth_check",
    "_run_doctor",
    "_run_spec_audit",
    "_run_draft_spec",
    "_run_resume",
    "_run_gc",
    "_run_metrics",
    "_DRAFT_DIALOGUE_SOFT_CAP",
    "resolve_repo_relative_input_path",
]


def _run(args, repo_root: Path) -> int:
    """Execute a ``syncade <PR_DOC>`` invocation.

    Wraps :func:`syncade.orchestrator.run_review` with CLI-friendly error
    handling; expected exceptions map to exit codes from
    :mod:`syncade.exit_codes` with a single-line stderr message.

    ``repo_root`` arrives as the user-supplied *hint* (cwd or
    ``--repo-root``). ``_run`` resolves it to the actual git repo root
    via :func:`~syncade.snapshot.discover_repo_root` **before** loading
    config, so a user invoking ``syncade`` from a subdirectory still
    picks up ``<repo-root>/.syncade/config.toml`` — not a (usually
    absent) config under the subdir. A hint that isn't inside a git
    repo is initialized with a baseline commit and the run proceeds.
    Only a missing ``git`` *binary* fails here with exit 60.

    The end-of-run summary is printed by ``run_review`` itself via the
    :class:`~syncade.logging.Logger` constructed here from ``--quiet``; the CLI
    no longer formats its own summary line.
    """
    # A repo syncade itself auto-initializes (a fresh non-git dir) is EXEMPT from the
    # default-branch guard below: there is no pre-existing integration branch to protect and
    # the operator explicitly ran syncade here. Detect it BEFORE ensure_repo_initialized
    # creates the repo.
    try:
        discover_repo_root(repo_root)
        repo_preexisted = True
    except SnapshotError:
        repo_preexisted = False

    # In a non-repo directory, initialize a conservative baseline repo so the
    # snapshot below has a tree to work with. Diagnostic modes still use hard
    # repo discovery and never mutate the caller's directory.
    try:
        ensure_repo_initialized(repo_root)
    except GitUnavailableError as exc:
        print(f"[syncade] {exc}", file=sys.stderr)
        return WORKTREE_ERROR
    except SubprocessError as exc:
        # A malformed repo_root hint (a path that does not exist or is a
        # file — run_subprocess pre-validates cwd) or a genuine init /
        # baseline-commit failure (read-only target dir, unresolvable git
        # identity, ...) surfaces out of the precondition as a
        # SubprocessError. These are environment/precondition failures →
        # exit 60, mirroring the discover_repo_root/SnapshotError mapping
        # just below.
        print(f"[syncade] git precondition error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR
    except OSError as exc:
        # Filesystem-level precondition failures that are neither a missing
        # git binary nor a git subprocess error: an over-long --repo-root
        # component (OSError(ENAMETOOLONG) from the path stat inside
        # discover_repo_root), or a write that cannot complete (e.g. a
        # read-only target dir when writing .git/info/exclude or the starter
        # The .gitignore write can escape the precondition as a bare OSError; map
        # them to the same environment/precondition exit 60 as above rather
        # than letting them surface as an uncaught traceback (exit 1).
        print(f"[syncade] git precondition error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    # Resolve the hint to the real git repo root first — config and the
    # whole run must be anchored there, not under whatever subdirectory
    # the user happened to invoke syncade from.
    try:
        repo_root = discover_repo_root(repo_root)
    except SnapshotError as exc:
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    logger = Logger("quiet" if args.quiet else "normal")

    # deprecation warnings are emitted directly to stderr
    # (not through `logger.warning` which is suppressed in
    # quiet mode). Raw-TOML key-presence is now the single source
    # of truth for deprecation warnings (the orchestrator-level
    # value-based duplicate was removed). Operators using --quiet
    # still see the warning because deprecated config is
    # actionable regardless of verbosity preference.
    def _emit_deprecation(message: str) -> None:
        print(f"[syncade] {message}", file=sys.stderr)

    try:
        config = load_config(repo_root, preset=args.preset, deprecation_callback=_emit_deprecation)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    # Apply every per-invocation CLI override (loop --max-rounds / --budget-* AND per-reviewer
    # --reviewer-model/thinking/timeout) HERE — right after load, BEFORE the branch guard + the auth
    # probe — so a bad flag fails fast at exit 50 (a config-level error, like a typo in the file)
    # without first exiting 60 on the guard or spawning an auth subprocess. apply_cli_overrides is
    # pure (no I/O); CLI beats config.
    try:
        config = apply_cli_overrides(config, args)
    except OverrideError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    # Default-branch guard (PR-v2-26). Baseless loop runs are refused here before auth_gate
    # probes `codex login status`. Based/scoped runs defer (D1(c), PR-h-02d.5): auth runs
    # first and run_review enforces the guard, so both paths refuse at exit 60.
    # --max-rounds is already folded into config.loop.max_rounds by apply_cli_overrides above.
    effective_rounds = config.loop.max_rounds
    # A syncade-auto-created repo (fresh dir) is exempt — see repo_preexisted above.
    allow_default = args.allow_default_branch or not repo_preexisted

    # Resolve --scope BEFORE the guard: a scope that resolves to HEAD is a known no-change run
    # (no producer fires), so the guard must not refuse it. --base and --scope are mutually
    # exclusive; only one path runs.
    base_ref = args.base
    if args.scope is not None:
        base_ref = _resolve_scope_base(repo_root, args.scope, logger)
        if base_ref is None:
            return WORKTREE_ERROR

    # Refuse ONLY when the CLI can PROVE the run commits (D1(c), PR-h-02d.5).
    #
    # Whether a run commits depends on the FILTERED diff, which is not knowable here:
    # the CLI has not snapshotted or resolved scope. (Config IS loaded, so
    # `strip_repo_context_files` is available — but the filtered diff isn't.) Four
    # earlier attempts substituted a cheaper predicate — base == HEAD, then merge-base,
    # then merge-base plus `--two-dot` — and each was wrong for a different input, because
    # the question simply is not expressible from what the CLI has.
    #
    # So the direction is inverted. With NO diff-shaping flag the reviewer diff is full
    # HEAD, which is non-empty in any repo that has a commit, so a multi-round run will
    # produce a producer commit — provable, and the common case this pre-auth guard exists
    # for. With a base or a scope, defer: `run_review` classifies authoritatively at the
    # run-entry choke, still BEFORE any reviewer/producer subprocess. The cost of deferring
    # is one auth probe; the cost of guessing wrong was refusing valid no-change runs.
    #
    # `--two-dot` needs `--base`/`--scope` (enforced in validate), so it is covered.
    _cli_will_commit = effective_rounds > 1 and _cli_proves_commit(
        repo_root, base_ref, config.review.strip_repo_context_files, two_dot=args.two_dot
    )
    try:
        guard_default_branch(
            repo_root,
            current_branch_name(repo_root),
            allow=allow_default,
            will_commit=_cli_will_commit,
        )
    except WorktreeError as exc:
        print(f"[syncade] worktree error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    # Auth reality check (PR-v2-24). `codex` IGNORES OPENAI_API_KEY entirely — auth
    # comes only from its stored login — so an `auth = "api"` declaration on a ChatGPT
    # login cannot be enforced by anything syncade controls. Refuse rather than run in
    # a mode the user did not ask for: silently billing the wrong account is the whole
    # bug. Checked here, before a single reviewer spawns and bills.
    # The SAME gate every other entry point uses -- one function, not a policy each mode
    # is trusted to remember. See cli/auth_gate.py for why that distinction matters.
    gate = auth_gate(config, REVIEW_BLOCKS)
    if gate is not None:
        return gate

    # ``--openspec`` derives the spec from an OpenSpec proposal folder. An
    # unresolvable proposal stops the run before the loop.
    openspec_tmp_path: Path | None = None
    pr_doc_artifact_name: str | None = None
    if args.openspec is not None:
        pr_doc_path = _resolve_openspec_pr_doc(repo_root, args.openspec or None, logger)
        if pr_doc_path is None:
            return WORKTREE_ERROR
        openspec_tmp_path = pr_doc_path
        pr_doc_artifact_name = pr_doc_path.name
    else:
        pr_doc_path = resolve_repo_relative_input_path(
            args.pr_doc, repo_root=repo_root, label="PR_DOC"
        )

    try:
        with run_status.install_signal_handlers():
            try:
                result = run_review(
                    repo_root=repo_root,
                    pr_doc_path=pr_doc_path,
                    config=config,
                    base_ref=base_ref,
                    timeout_seconds=args.timeout,
                    logger=logger,
                    force_dirty=args.force_dirty,
                    two_dot=args.two_dot,
                    allow_default_branch=allow_default,
                    pr_doc_artifact_name=pr_doc_artifact_name,
                    worktree_base=config.worktree_base,
                )
            except FileNotFoundError as exc:
                print(f"[syncade] error: {exc}", file=sys.stderr)
                # PR_DOC is a CLI-input issue; exit 2 matches argparse's
                # convention for "user-supplied argument problem".
                return 2
            except NotADirectoryError as exc:
                print(f"[syncade] error: {exc}", file=sys.stderr)
                return 2
            except SnapshotError as exc:
                print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
                # A mid-loop SnapshotError is caught HERE, not by the catch-all below,
                # so it must finalize the breadcrumb itself — else status.json stays
                # `running` and a clean exit-60 falsely reads as a hard kill.
                run_status.finalize_active(f"exception:{type(exc).__name__}", WORKTREE_ERROR)
                return WORKTREE_ERROR
            except WorktreeError as exc:
                print(f"[syncade] worktree error: {exc}", file=sys.stderr)
                run_status.finalize_active(f"exception:{type(exc).__name__}", WORKTREE_ERROR)
                return WORKTREE_ERROR
            except KeyboardInterrupt:
                if run_status.received_signal():
                    # Signal-induced KI: finalize with signal:<NAME> + 128+signum.
                    return run_status.finalize_signal()
                # Non-signal KI: the orchestrator guard already finalized status.json
                # as exception:KeyboardInterrupt. Finalize here only if still active
                # (e.g. raised before begin()), then return the conventional 130.
                run_status.finalize_active("exception:KeyboardInterrupt", None)
                return 130
            except Exception as exc:
                # Unexpected mid-run failure: record it before it propagates so the
                # breadcrumb never lies about why the run ended.
                run_status.finalize_active(f"exception:{type(exc).__name__}", None)
                raise
    finally:
        if openspec_tmp_path is not None:
            try:
                openspec_tmp_path.unlink(missing_ok=True)
            except OSError as exc:
                print(
                    f"[syncade] warning: could not remove OpenSpec tempfile: {exc}", file=sys.stderr
                )

    # run_review already printed the summary via Logger.summary.
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    """Entry point used by both ``python -m syncade`` and the installed
    ``syncade`` console script.

    Returns the process exit code; callers (e.g. ``__main__.py``) are
    responsible for passing the return value to :func:`sys.exit`.

    Validation order:

    1. Command-shape checks (mutex, required pairs, no-command) — these
       happen *before* any filesystem work, so a user running
       ``syncade`` with no command sees help, never a config or
       snapshot error from a stale ``.syncade/`` or a non-git cwd.
    2. Dispatch to the right command handler. For a real review, :func:`_run`
       resolves the git repo root and loads ``.syncade/config.toml`` *from that root* — not from the
       user-supplied ``--repo-root``/cwd hint.
    """
    _argv = list(sys.argv[1:]) if argv is None else list(argv)
    # Extract --config operands from raw argv before argparse sees them: argparse's nargs="*"
    # stops at dash-prefixed tokens (treating them as unknown options), so a model string like
    # "-custom-model" would be rejected. We pull the operands manually, strip them from argv, and
    # re-inject after parsing. Also detect the forbidden prefix form (--repo before --config).
    _config_operands, _argv_parsed, _repo_prefix = _extract_config_operands(_argv)
    parser = build_parser()
    args = parser.parse_args(_argv_parsed)
    if _config_operands is not None:
        # Merge any trailing non-flag operands that argparse still captured (e.g. `--config list
        # extra`) with the dash-prefixed ones we extracted before parsing.
        args.config = _config_operands + (args.config or [])
    # Reject the prefix form: --repo before --config set violates the suffix-only contract (D1).
    # Only applies to "set" — for other verbs (list/get) or no verb, _reject_config_mode_conflicts
    # will catch the --repo misuse with the appropriate "meaningful only with --config set" message.
    if _repo_prefix and args.config and args.config[0] == "set":
        print(
            "[syncade] error: --repo must trail the key/value: `--config set <key> <value> --repo`",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR

    rc = validate_command_shape(args, parser)
    if rc is not None:
        return rc

    # --- dispatch -------------------------------------------------------
    # --install-skill is a standalone local operation — copies bundled skill files into the
    # harness dirs. Dispatched after command-shape validation so --doctor/--quick combinations
    # are rejected before the filesystem mutation.
    if args.install_skill is not None:
        from syncade.cli.install_skill import install_skill

        return install_skill(args.install_skill)
    if args.config is not None:
        from syncade.cli.config_mode import run_config

        return run_config(args.config, args=args)
    if args.gc:
        return _run_gc(args)
    if args.metrics:
        return _run_metrics(args)
    if args.resume:
        return _run_resume(args)
    if args.selfcheck:
        return _run_selfcheck(args)
    if args.auth_check:
        return _run_auth_check(args)
    if args.doctor:
        return _run_doctor(args)
    if args.spec_audit:
        return _run_spec_audit(args)
    if args.draft_spec:
        return _run_draft_spec(args)

    # repo_root here is a starting *hint* (cwd or --repo-root); _run
    # resolves it to the actual git repo root before doing anything else.
    repo_root = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    return _run(args, repo_root)
