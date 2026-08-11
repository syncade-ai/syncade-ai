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
from syncade.git_preconditions import (
    AutoInitRefusedError,
    GitUnavailableError,
    ensure_repo_initialized,
    undo_auto_init,
)
from syncade.logging import Logger
from syncade.orchestrator import run_review
from syncade.orchestrator.branch_guard import current_branch_name, guard_default_branch
from syncade.process import SubprocessError
from syncade.run_inputs import validate_run_inputs
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
from .preflight_paths import check_write_targets
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
    via :func:`~syncade.snapshot.discover_repo_root`. Config is loaded
    from the discovered root before any mutation so that a bad config
    or bad CLI override is caught before ``ensure_repo_initialized``
    creates ``.git`` and a baseline commit. A hint that isn't inside a
    git repo is initialized with a baseline commit and the run proceeds.
    Only a missing ``git`` *binary* fails here with exit 60.

    The end-of-run summary is printed by ``run_review`` itself via the
    :class:`~syncade.logging.Logger` constructed here from ``--quiet``; the CLI
    no longer formats its own summary line.
    """
    # Reset the process-global run-state before anything reads it. One review per process is
    # the normal case, but tests (and any library caller) invoke main() repeatedly, and
    # `began()` below is deliberately sticky — without this, a previous invocation's run makes
    # this one look like it took ownership.
    run_status.clear_active()

    # A repo syncade itself auto-initializes (a fresh non-git dir) is EXEMPT from the
    # default-branch guard below: there is no pre-existing integration branch to protect and
    # the operator explicitly ran syncade here. Detect it BEFORE ensure_repo_initialized
    # creates the repo.
    #
    # Capture the discovered root here: the pre-flight below needs it for correct path
    # resolution when --repo-root is a subdirectory (PR-h-04 finding).
    try:
        _discovered_root = discover_repo_root(repo_root)
        repo_preexisted = True
    except SnapshotError:
        _discovered_root = None
        repo_preexisted = False

    # VALIDATE BEFORE MUTATE (PR-h-04 item A). Everything below this point can change the
    # operator's directory — `ensure_repo_initialized` creates a repo and a baseline commit,
    # and `guard_default_branch` further down refuses with a message about BRANCHES. A
    # mistyped brief path therefore either left a repo behind (fresh dir) or was reported as
    # a default-branch problem (existing repo), when the real mistake was a filename.
    #
    # `validate_run_inputs` is the SAME function `run_review` calls, so this pre-flight
    # cannot refuse something the library would accept. `--openspec` is validated HERE too:
    # resolving the proposal folder before mutation ensures a bad change-id is caught before
    # auto-init creates .git and a baseline commit.
    #
    # Use the discovered root for path resolution when the repo already exists: a
    # --repo-root hint that is a subdirectory of the real root must not reject PR docs
    # that exist at the repo root. Fall back to the hint for the auto-init case (no root yet).

    # NOTE: an unresolvable `--base` needs no pre-flight here. It used to: a ~45-line
    # allowlist of the refs auto-init creates, plus a loop normalizing `@{0}`, `^{commit}`,
    # `^{}`, `^0` and `~0` suffixes. That allowlist was revised FIVE times across two dogfoods
    # and was still rejecting spellings git would resolve, because enumerating valid revision
    # syntax does not terminate. `take_snapshot` already resolves the ref authoritatively, and
    # undo (see the finally below) makes reaching it harmless — so the enumeration is deleted
    # rather than extended, and git remains the only authority on what a ref means.

    # Logger only depends on --quiet; create it here so the OpenSpec preflight can use it,
    # and so it is available before config is loaded.
    logger = Logger("quiet" if args.quiet else "normal")

    # Initialized before mutation so the outer try-finally can clean up on every exit path.
    openspec_tmp_path: Path | None = None
    pr_doc_artifact_name: str | None = None
    # PR-h-04.6: was this directory EMPTY before syncade touched it? Captured before the
    # first write, so a partial init cannot lose it and no filesystem race can invalidate it.
    # If it was empty, everything present afterwards is syncade's and undo needs no ownership
    # proof; if it was not, syncade does not undo (see --allow-auto-init's help).
    _started_empty = False
    # Undo is decided in ONE place — the `finally` at the bottom — from these two positional
    # facts. Neither classifies anything, which is the point: naming which exceptions or exit
    # codes mean "the run began" is the enumeration this PR exists to delete, and three
    # successive guards each looked correct and each was wrong. `sys.exc_info()` read empty
    # after a caught signal; `run_status.active()` reads None once `run_review` finalizes, so
    # a real mid-run failure looked like a clean refusal; `exit_code != CONFIG_ERROR` called
    # every refusal exit code review work.
    _result = None  # set IFF run_review returned — it is then the authority on what it did
    _abnormal = False  # set IFF a signal or an unexplained exception ended the run (L4, L6)

    _preflight_root = _discovered_root if _discovered_root is not None else repo_root

    # VALIDATE BEFORE MUTATE — config and CLI overrides (PR-h-04 item A, extended).
    # load_config and apply_cli_overrides are PURE (no filesystem mutations); running them
    # BEFORE the OpenSpec tempfile is created ensures a bad .syncade/config.toml or an
    # unknown --reviewer-* name is caught before any tempfile or .git exists. An early
    # config failure would otherwise return CONFIG_ERROR after the OpenSpec tempfile was
    # written but before the outer try-finally's cleanup started, leaking private content
    # in the system temp directory. Uses _preflight_root (discovered root when the repo
    # already exists, raw hint otherwise) — the same anchor used for PR_DOC validation.
    #
    # deprecation warnings are emitted directly to stderr (not through `logger.warning`
    # which is suppressed in quiet mode). Operators using --quiet still see the warning
    # because deprecated config is actionable regardless of verbosity preference.
    def _emit_deprecation(message: str) -> None:
        print(f"[syncade] {message}", file=sys.stderr)

    try:
        config = load_config(
            _preflight_root, preset=args.preset, deprecation_callback=_emit_deprecation
        )
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    # Apply CLI overrides before mutation — same reasoning: a bad --reviewer-model name
    # or an invalid timeout must refuse before .git is created. apply_cli_overrides is
    # pure (no I/O); CLI beats config.
    try:
        config = apply_cli_overrides(config, args)
    except OverrideError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    if args.openspec is not None:
        # Resolve/validate the OpenSpec proposal BEFORE any mutation so that a bad
        # change-id is caught here, not after ensure_repo_initialized runs.
        # Config is loaded first so a bad config fails before the tempfile is created.
        _early_path = _resolve_openspec_pr_doc(_preflight_root, args.openspec or None, logger)
        if _early_path is None:
            return WORKTREE_ERROR
        openspec_tmp_path = _early_path
        pr_doc_artifact_name = _early_path.name
    else:
        try:
            _pr_doc_path = resolve_repo_relative_input_path(
                args.pr_doc, repo_root=_preflight_root, label="PR_DOC"
            )
            validate_run_inputs(_preflight_root, _pr_doc_path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            print(f"[syncade] error: {exc}", file=sys.stderr)
            return 2

    # Outer try-finally ensures openspec_tmp_path is cleaned up on every exit path —
    # including error returns from ensure_repo_initialized and post-mutation setup that
    # would otherwise leak the staged tempfile.
    try:
        # Auth reality check (PR-v2-24). Moved BEFORE ensure_repo_initialized so that an
        # auth mismatch refuses without creating .git in a fresh directory. `codex` IGNORES
        # OPENAI_API_KEY entirely — auth comes only from its stored login — so an
        # `auth = "api"` declaration on a ChatGPT login cannot be enforced by anything
        # syncade controls. Refusing here, before any mutation, keeps the fresh directory
        # byte-inert on auth failure.
        # The SAME gate every other entry point uses -- one function, not a policy each mode
        # is trusted to remember. See cli/auth_gate.py for why that distinction matters.
        gate = auth_gate(config, REVIEW_BLOCKS)
        if gate is not None:
            return gate

        # Refuse unusable write targets before auto-init mutates anything. Both dirs are
        # created later (worktree_base by provisioning, .syncade/runs by the run-dir mkdir),
        # so without this a refused run would create a repository on its way to failing.
        _bad_target = check_write_targets(config.worktree_base, _preflight_root)
        if _bad_target is not None:
            return _bad_target

        # In a non-repo directory, initialize a conservative baseline repo so the
        # snapshot below has a tree to work with. Diagnostic modes still use hard
        # repo discovery and never mutate the caller's directory.
        # Capture emptiness BEFORE the call: if `git init` fails halfway, the bit is already
        # recorded and undo still removes the repository. A receipt written by the callee
        # cannot do that — round 0 of the dogfood proved exactly this.
        if not repo_preexisted:
            try:
                _started_empty = not any(repo_root.iterdir())
            except OSError:
                _started_empty = False
        try:
            ensure_repo_initialized(repo_root, allow_populated=args.allow_auto_init)
        except AutoInitRefusedError as exc:
            print(f"[syncade] {exc}", file=sys.stderr)
            return WORKTREE_ERROR
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

        # Resolve the hint to the real git repo root — the whole run must be
        # anchored there, not under whatever subdirectory the user invoked from.
        try:
            repo_root = discover_repo_root(repo_root)
        except SnapshotError as exc:
            print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
            return WORKTREE_ERROR

        # Default-branch guard (PR-v2-26). auth_gate already ran above; this guard runs after
        # repo discovery so it can read the current branch name. Based/scoped runs defer
        # (D1(c), PR-h-02d.5): run_review enforces the guard, so both paths refuse at exit 60.
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

        # ``--openspec`` spec was already resolved in the preflight above; use the staged
        # tempfile directly. For the normal path, resolve the PR_DOC relative to the
        # (now-confirmed) repo root.
        if args.openspec is not None:
            pr_doc_path = openspec_tmp_path  # type: ignore[assignment]
        else:
            pr_doc_path = resolve_repo_relative_input_path(
                args.pr_doc, repo_root=repo_root, label="PR_DOC"
            )

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
                # L4: an interrupted run is never a clean refusal. This handler RETURNS, so
                # the outer catch-all below never sees it and it must record that itself.
                _abnormal = True
                if run_status.received_signal():
                    # Signal-induced KI: finalize with signal:<NAME> + 128+signum.
                    return run_status.finalize_signal()
                # Non-signal KI: the orchestrator guard already finalized status.json
                # as exception:KeyboardInterrupt. Finalize here only if still active
                # (e.g. raised before begin()), then return the conventional 130.
                run_status.finalize_active("exception:KeyboardInterrupt", None)
                return 130
            except BaseException as exc:
                # Unexpected mid-run failure: record it before it propagates so the
                # breadcrumb never lies about why the run ended. It re-raises, so the outer
                # catch-all marks it abnormal — an unexplained failure is not a refusal (L6).
                if isinstance(exc, Exception):
                    run_status.finalize_active(f"exception:{type(exc).__name__}", None)
                raise

        # run_review already printed the summary via Logger.summary.
        _result = result
        return result.exit_code
    except KeyboardInterrupt:
        # Signal arriving in the narrow window BEFORE run_status.install_signal_handlers()
        # is entered (between ensure_repo_initialized success and the with-block).  The
        # inner handler covers signals during run_review; this catches the gap.  L4: not a
        # clean refusal — signals must not trigger undo.
        _abnormal = True
        if run_status.received_signal():
            return run_status.finalize_signal()
        run_status.finalize_active("exception:KeyboardInterrupt", None)
        return 130
    except BaseException:
        # Unexpected exception, here or re-raised from the run itself. Not a refusal (L6).
        _abnormal = True
        raise
    finally:
        # UNDO (PR-h-04.6), decided HERE and nowhere else. Any return between auto-init and
        # the review — present or added later — lands in this `finally`, so no list of refusal
        # sites exists to fall out of date. That list is what two dogfoods could not complete.
        #
        # `_started_empty` is what makes deletion safe at all: the directory was empty before
        # the first write, so everything in it now is syncade's (item 1). The rest decides
        # whether this invocation is a refusal, from facts rather than judgements:
        #
        #   run_review RETURNED  → it is the authority on what it did. A run is a refusal
        #     when no reviewer subprocess was started across any round. That covers
        #     `no_changes_to_review` (exit 0), `diff_malformed` (exit 60), and adapter-
        #     lookup / auth-preflight failures — all of which have no dispatcher Phase 3.
        #     `DispatchResult.reviewer_subprocess_started` is the explicit fact;
        #     `dispatch_result.results` is NOT (adapter lookup failures return non-empty
        #     results before any subprocess runs, and `producer_emptied_diff` has an empty
        #     final result after real reviewers ran in prior rounds).
        #   run_review RAISED    → `began()` says whether it had taken ownership. Unlike
        #     `active()` it survives the finalization `run_review` performs before
        #     re-raising, which is exactly what the previous guard got wrong.
        #   ABNORMAL             → a signal or an unexplained exception is never a refusal.
        _refused = not _abnormal and (
            not any(r.dispatch_result.reviewer_subprocess_started for r in _result.rounds)
            if _result is not None
            else not run_status.began()
        )
        if _started_empty and _refused:
            _failed = undo_auto_init(repo_root)
            if _failed:
                # Never abort on a failed cleanup: the run is already refusing, and a leftover
                # repo is the OLD behaviour rather than a new hazard. Printed straight to
                # stderr so it survives --quiet, like the auth block.
                print(
                    "[syncade] warning: could not remove what auto-init created: "
                    + ", ".join(_failed),
                    file=sys.stderr,
                )
        if openspec_tmp_path is not None:
            try:
                openspec_tmp_path.unlink(missing_ok=True)
            except OSError as exc:
                print(
                    f"[syncade] warning: could not remove OpenSpec tempfile: {exc}", file=sys.stderr
                )


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

        return install_skill(args.install_skill, force=args.force_install)
    if args.config is not None:
        from syncade.cli.config_mode import run_config

        return run_config(args.config, args=args)
    if args.gc:
        return _run_gc(args)
    if args.metrics:
        return _run_metrics(args)
    if args.resume is not None:
        return _run_resume(args)
    if args.selfcheck:
        return _run_selfcheck(args)
    if args.auth_check:
        return _run_auth_check(args)
    if args.doctor:
        return _run_doctor(args)
    if args.spec_audit is not None:
        return _run_spec_audit(args)
    if args.draft_spec:
        return _run_draft_spec(args)

    # repo_root here is a starting *hint* (cwd or --repo-root); _run
    # resolves it to the actual git repo root before doing anything else.
    repo_root = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    return _run(args, repo_root)
