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
from .resolve import _current_branch, _resolve_openspec_pr_doc, _resolve_scope_base

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

    # Default-branch refusal (PR-v2-26), BEFORE auth_gate — which probes `codex login
    # status` (a subprocess) for any OpenAI actor. So a committing run on the default branch
    # fails fast with NO subprocess and no auth output. run_review re-checks the same guard
    # for direct library callers.
    # --max-rounds is already folded into config.loop.max_rounds by apply_cli_overrides above.
    effective_rounds = config.loop.max_rounds
    # A syncade-auto-created repo (fresh dir) is exempt — see repo_preexisted above.
    allow_default = args.allow_default_branch or not repo_preexisted
    try:
        guard_default_branch(
            repo_root,
            current_branch_name(repo_root),
            allow=allow_default,
            will_commit=effective_rounds > 1,
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

    # ``--scope`` derives the diff base; an unresolvable scope stops the run
    # before the loop. Otherwise the explicit ``--base`` value is used.
    base_ref = args.base
    if args.scope is not None:
        base_ref = _resolve_scope_base(repo_root, args.scope, logger)
        if base_ref is None:
            return WORKTREE_ERROR

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
    parser = build_parser()
    args = parser.parse_args(argv)

    # --- command-shape validation (no filesystem work yet) --------------
    if args.resume and args.pr_doc:
        print(
            "[syncade] error: --resume cannot be combined with a PR_DOC "
            "positional argument; pass one or the other",
            file=sys.stderr,
        )
        return 2
    # --selfcheck is mutually exclusive with PR_DOC and other review modes
    # because it is a one-shot producer smoke.
    # Argparse-style mutual exclusion via post-parse check so each flag
    # keeps its standalone help text.
    if args.selfcheck and args.pr_doc:
        print(
            "[syncade] error: --selfcheck cannot be combined with a PR_DOC "
            "positional argument; pass one or the other",
            file=sys.stderr,
        )
        return 2
    if args.selfcheck and args.resume:
        print(
            "[syncade] error: --selfcheck cannot be combined with --resume",
            file=sys.stderr,
        )
        return 2
    if args.selfcheck and args.spec_audit:
        print(
            "[syncade] error: --selfcheck cannot be combined with --spec-audit",
            file=sys.stderr,
        )
        return 2
    # --auth-check is mutually exclusive with PR_DOC and every other one-shot
    # mode. Keep the post-parse checks so each flag has standalone help text.
    if args.auth_check and args.pr_doc:
        print(
            "[syncade] error: --auth-check cannot be combined with a PR_DOC "
            "positional argument; pass one or the other",
            file=sys.stderr,
        )
        return 2
    if args.auth_check and args.resume:
        print(
            "[syncade] error: --auth-check cannot be combined with --resume",
            file=sys.stderr,
        )
        return 2
    if args.auth_check and args.selfcheck:
        print(
            "[syncade] error: --auth-check cannot be combined with --selfcheck",
            file=sys.stderr,
        )
        return 2
    if args.auth_check and args.spec_audit:
        print(
            "[syncade] error: --auth-check cannot be combined with --spec-audit",
            file=sys.stderr,
        )
        return 2
    # --spec-audit takes its own PR_DOC path and is mutually exclusive with
    # the positional PR_DOC and the other one-shot modes.
    if args.spec_audit and args.pr_doc:
        print(
            "[syncade] error: --spec-audit cannot be combined with a PR_DOC "
            "positional argument; pass one or the other",
            file=sys.stderr,
        )
        return 2
    # --resume reuses the original run's base_ref from run-init.json.
    if args.resume and args.base is not None:
        print(
            "[syncade] error: --resume cannot be combined with --base; "
            "the original run's base_ref is read from run-init.json",
            file=sys.stderr,
        )
        return 2
    # --scope derives the base and cannot combine with an explicit --base or
    # with resume, whose base is read from run-init.json.
    if args.scope is not None and args.base is not None:
        print(
            "[syncade] error: --scope cannot be combined with --base; pass "
            "one or the other (a scope derives the base for you)",
            file=sys.stderr,
        )
        return 2
    if args.scope is not None and args.resume:
        print(
            "[syncade] error: --scope cannot be combined with --resume; the "
            "resumed run's base is read from run-init.json",
            file=sys.stderr,
        )
        return 2
    # --force-drift only has meaning during resume.
    if args.force_drift and not args.resume:
        print(
            "[syncade] error: --force-drift requires --resume; it "
            "controls tree-drift behavior during a resumed run and "
            "has no meaning outside that context",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    if args.spec_audit and args.resume:
        print(
            "[syncade] error: --spec-audit cannot be combined with --resume",
            file=sys.stderr,
        )
        return 2
    # --openspec supplies the spec from an OpenSpec proposal folder, so it
    # replaces the positional PR_DOC and cannot combine with diagnostic modes.
    # It remains compatible with --base/--scope because those set the diff base.
    if args.openspec is not None and args.pr_doc:
        print(
            "[syncade] error: --openspec cannot be combined with a PR_DOC "
            "positional argument; pass one or the other (both supply the spec)",
            file=sys.stderr,
        )
        return 2
    for _flag, _used in (
        ("--resume", bool(args.resume)),
        ("--selfcheck", args.selfcheck),
        ("--auth-check", args.auth_check),
        ("--spec-audit", bool(args.spec_audit)),
    ):
        if args.openspec is not None and _used:
            print(
                f"[syncade] error: --openspec cannot be combined with {_flag}",
                file=sys.stderr,
            )
            return 2
    # --draft-spec is its own one-shot mode. It manufactures a spec from a
    # transcript and is mutually exclusive with PR_DOC and every other mode.
    if args.draft_spec:
        for _flag, _used in (
            ("a PR_DOC positional argument", bool(args.pr_doc)),
            ("--resume", bool(args.resume)),
            ("--selfcheck", args.selfcheck),
            ("--auth-check", args.auth_check),
            ("--spec-audit", bool(args.spec_audit)),
            ("--openspec", args.openspec is not None),
        ):
            if _used:
                print(
                    f"[syncade] error: --draft-spec cannot be combined with {_flag}",
                    file=sys.stderr,
                )
                return 2
    # --transcript only has meaning with --draft-spec.
    if args.transcript is not None and not args.draft_spec:
        print(
            "[syncade] error: --transcript requires --draft-spec; it names the "
            "session transcript the cold drafter reads",
            file=sys.stderr,
        )
        return 2
    # --gc is its own one-shot maintenance mode and is mutually exclusive with
    # PR_DOC and every other mode.
    if args.gc:
        for _flag, _used in (
            ("a PR_DOC positional argument", bool(args.pr_doc)),
            ("--resume", bool(args.resume)),
            ("--selfcheck", args.selfcheck),
            ("--auth-check", args.auth_check),
            ("--spec-audit", bool(args.spec_audit)),
            ("--draft-spec", args.draft_spec),
            ("--openspec", args.openspec is not None),
        ):
            if _used:
                print(
                    f"[syncade] error: --gc cannot be combined with {_flag}",
                    file=sys.stderr,
                )
                return 2
    else:
        # --gc-keep/--gc-max-age-days/--gc-dry-run are meaningful ONLY with --gc.
        # They are NOT harmless without it: argparse still parses them, and the
        # invocation would otherwise fall through to the normal review path
        # (silently ignoring the knob — and, with a PR_DOC, even provisioning
        # reviewers). Reject them up front.
        for _flag, _used in (
            ("--gc-keep", args.gc_keep is not None),
            ("--gc-max-age-days", args.gc_max_age_days is not None),
            ("--gc-dry-run", args.gc_dry_run),
        ):
            if _used:
                print(
                    f"[syncade] error: {_flag} is meaningful only with --gc",
                    file=sys.stderr,
                )
                return 2
    # --budget-tokens/--budget-usd are loop-only knobs; reject them for every one-shot mode
    # that does not run the review loop (metrics, doctor, gc, install-skill, auth-check,
    # selfcheck, spec-audit, draft-spec). Silently ignoring them would mislead the operator
    # into thinking a budget was enforced when it was not.
    _is_loop_mode = bool(args.pr_doc) or (args.openspec is not None) or bool(args.resume)
    _budget_used = args.budget_tokens is not None or args.budget_usd is not None
    if _budget_used and not _is_loop_mode:
        _which = "--budget-tokens" if args.budget_tokens is not None else "--budget-usd"
        print(
            f"[syncade] error: {_which} is meaningful only with a review loop "
            f"(a PR_DOC, --openspec, or --resume). Omit it for one-shot modes "
            f"(--metrics, --doctor, --gc, --auth-check, --selfcheck, --spec-audit, "
            f"--draft-spec, --install-skill).",
            file=sys.stderr,
        )
        return 2

    # --reviewer-model/thinking/timeout override the FRESH review loop's roster; --resume reuses the
    # roster from the current .syncade/config.toml (per-invocation CLI overrides are not rehydrated
    # from the run snapshot — see run_init), and the one-shot modes run no reviewers. Reject rather
    # than silently ignore — the same "meaningful only in context" discipline as --budget-* above.
    _reviewer_used = bool(args.reviewer_model or args.reviewer_thinking or args.reviewer_timeout)
    _reviewer_applies = bool(args.pr_doc) or (args.openspec is not None)
    if _reviewer_used and not _reviewer_applies:
        print(
            "[syncade] error: --reviewer-model / --reviewer-thinking / --reviewer-timeout are "
            "meaningful only for a fresh review loop (a PR_DOC or --openspec). Omit them for "
            "--resume (it reuses the roster from the current .syncade/config.toml; put a panel "
            "override there to keep it across a resume) and the one-shot modes.",
            file=sys.stderr,
        )
        return 2
    # --metrics is a read-only maintenance/report mode; mutually exclusive with
    # PR_DOC and every other one-shot mode.
    if args.metrics:
        for _flag, _used in (
            ("a PR_DOC positional argument", bool(args.pr_doc)),
            ("--resume", bool(args.resume)),
            ("--selfcheck", args.selfcheck),
            ("--auth-check", args.auth_check),
            ("--spec-audit", bool(args.spec_audit)),
            ("--draft-spec", args.draft_spec),
            ("--openspec", args.openspec is not None),
            ("--gc", args.gc),
        ):
            if _used:
                print(
                    f"[syncade] error: --metrics cannot be combined with {_flag}",
                    file=sys.stderr,
                )
                return 2
    elif args.metrics_last is not None:
        print(
            "[syncade] error: --metrics-last is meaningful only with --metrics",
            file=sys.stderr,
        )
        return 2
    # --doctor is a read-only one-shot preflight; mutually exclusive with PR_DOC and every
    # other mode. NOT with --base/--scope: doctor previews the diff those select.
    if args.doctor:
        for _flag, _used in (
            ("a PR_DOC positional argument", bool(args.pr_doc)),
            ("--resume", bool(args.resume)),
            ("--selfcheck", args.selfcheck),
            ("--auth-check", args.auth_check),
            ("--spec-audit", bool(args.spec_audit)),
            ("--draft-spec", args.draft_spec),
            ("--openspec", args.openspec is not None),
            ("--gc", args.gc),
            ("--metrics", args.metrics),
            ("--install-skill", args.install_skill is not None),
        ):
            if _used:
                print(
                    f"[syncade] error: --doctor cannot be combined with {_flag}",
                    file=sys.stderr,
                )
                return 2
    elif args.quick:
        print(
            "[syncade] error: --quick is meaningful only with --doctor",
            file=sys.stderr,
        )
        return 2
    if (
        not args.pr_doc
        and args.openspec is None
        and not args.draft_spec
        and not args.resume
        and not args.selfcheck
        and not args.auth_check
        and not args.spec_audit
        and not args.gc
        and not args.metrics
        and not args.doctor
        and args.install_skill is None
    ):
        parser.print_help(sys.stderr)
        return 2

    # --- dispatch -------------------------------------------------------
    # --install-skill is a standalone local operation — copies bundled skill files into the
    # harness dirs. Dispatched after command-shape validation so --doctor/--quick combinations
    # are rejected before the filesystem mutation.
    if args.install_skill is not None:
        from syncade.cli.install_skill import install_skill

        return install_skill(args.install_skill)
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
