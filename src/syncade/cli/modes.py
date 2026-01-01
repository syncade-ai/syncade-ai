"""One-shot CLI mode handlers: ``--selfcheck`` / ``--auth-check`` /
``--spec-audit`` / ``--draft-spec`` / ``--resume`` / ``--gc``.

Each handler resolves the git repo root, loads config, and dispatches to the
relevant module, mapping expected exceptions to the shared CLI exit-code
convention.
"""

from __future__ import annotations

import sys
from pathlib import Path

from syncade.cli.auth_gate import auth_gate
from syncade.config_auth import (
    ALL_BLOCKS,
    AUDIT_BLOCKS,
    DRAFT_BLOCKS,
    SELFCHECK_BLOCKS,
)
from syncade.config_loader import ConfigError, load_config
from syncade.exit_codes import (
    CLARIFICATION_NEEDED,
    CLI_USAGE_ERROR,
    CONFIG_ERROR,
    REVIEWER_FAILURE,
    REVIEWER_OUTPUT_UNPARSEABLE,
    SUCCESS,
    WORKTREE_ERROR,
)
from syncade.logging import Logger
from syncade.snapshot import SnapshotError, discover_repo_root

from .paths import resolve_repo_relative_input_path, unique_markdown_path
from .resolve import _resolve_scope_base


def _reject_diff_base_flags(args, mode_flag: str) -> int | None:
    """Reject ``--base`` / ``--scope`` for one-shot modes that render no
    reviewer diff (``--selfcheck`` / ``--auth-check`` / ``--spec-audit`` /
    ``--gc``).

    Those flags only select the reviewer's diff base for the review loop; the
    diagnostic/maintenance modes never consume one, so accepting them would
    silently ignore an operator's explicit intent. Reject up front (exit 2),
    matching the existing ``--gc-keep``-without-``--gc`` and
    ``--transcript``-without-``--draft-spec`` "meaningful only in context"
    guards. Returns the exit code on a violation, or ``None`` when clean so the
    caller proceeds. (``--base`` and ``--scope`` are already mutually exclusive
    with each other in ``main()``, so at most one is set here.)
    """
    offenders = [
        flag
        for flag, used in (("--base", args.base is not None), ("--scope", args.scope is not None))
        if used
    ]
    if offenders:
        print(
            f"[syncade] error: {' / '.join(offenders)} cannot be combined with "
            f"{mode_flag}; that flag selects the reviewer diff base and "
            f"{mode_flag} renders no reviewer diff",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    return None


def _run_selfcheck(args) -> int:
    """Dispatch ``syncade --selfcheck``.

    Loads the operator's ``.syncade/config.toml`` from the resolved
    git repo root and hands the producer block to
    :func:`syncade.selfcheck.run_selfcheck`. The selfcheck itself
    runs in a throwaway tmp_path workspace; the operator's repo is
    used only to resolve config (so the operator's ``[producer]``
    block drives provider / model / thinking / permissions).

    Error mapping mirrors :func:`_run` — config errors map to exit
    50, repo-discovery failures to exit 60, and the selfcheck's own
    exit codes (0 / 30 / 60) pass through verbatim.
    """
    rejection = _reject_diff_base_flags(args, "--selfcheck")
    if rejection is not None:
        return rejection

    # Local import to keep the selfcheck module out of the
    # default-import cost — operators not running --selfcheck don't
    # pay for it.
    from syncade.selfcheck import run_selfcheck

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

    gate = auth_gate(config, SELFCHECK_BLOCKS)
    if gate is not None:
        return gate

    return run_selfcheck(
        config,
        timeout_seconds=args.timeout,
        quiet=args.quiet,
    )


def _run_auth_check(args) -> int:
    """Dispatch ``syncade --auth-check``.

    Loads the operator's ``.syncade/config.toml`` from the resolved
    git repo root and hands the configured credentials to
    :func:`syncade.auth_check.run_auth_check`. Each distinct credential
    is probed once; the function returns SUCCESS (0) iff every probe
    passed, WORKTREE_ERROR (60) otherwise — same exit-60 semantics
    as ``--selfcheck`` for "your environment isn't ready".

    Error mapping mirrors :func:`_run_selfcheck` — repo-discovery
    failures map to exit 60, config errors to exit 50; auth-check's
    own exit codes (0 / 60) pass through verbatim.

    ``--timeout`` propagates to ``run_auth_check`` as a per-probe
    override; the auth-check module's
    :data:`DEFAULT_AUTH_CHECK_TIMEOUT_SECONDS` (30s) is the
    fallback when ``--timeout`` is unset.
    """
    rejection = _reject_diff_base_flags(args, "--auth-check")
    if rejection is not None:
        return rejection

    # Local import keeps the auth-check module out of the
    # default-import cost — operators not running --auth-check
    # don't pay for it.
    from syncade.auth_check import run_auth_check

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

    gate = auth_gate(config, ALL_BLOCKS)
    if gate is not None:
        return gate

    return run_auth_check(
        config,
        timeout_seconds=args.timeout,
        quiet=args.quiet,
    )


def _run_spec_audit(args) -> int:
    """Dispatch ``syncade --spec-audit <pr-doc>``.

    Validates the PR doc path, resolves the git repo root, loads config
    (for per-repo template overrides), then calls
    :func:`syncade.spec_audit.run_spec_audit`. Prints findings to stdout
    in a human-readable format; errors go to stderr.

    Exit codes:
    - 0 (SUCCESS): ``outcome == "ready"``, no blocker findings
    - 10 (CLARIFICATION_NEEDED): ``outcome == "needs_clarification"``
    - 40 (REVIEWER_FAILURE): subprocess error (e.g. codex CLI failure)
    - 50 (CONFIG_ERROR): config load failure (pydantic ValidationError,
      malformed TOML)
    - 60 (WORKTREE_ERROR): path validation failure
    - 70 (REVIEWER_OUTPUT_UNPARSEABLE): auditor output didn't parse
    """
    rejection = _reject_diff_base_flags(args, "--spec-audit")
    if rejection is not None:
        return rejection

    from syncade.spec_audit import SpecAuditOutputError, run_spec_audit

    repo_root_hint = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    try:
        repo_root = discover_repo_root(repo_root_hint)
    except SnapshotError as exc:
        preflight_path = Path(args.spec_audit).expanduser()
        hint_path = (
            repo_root_hint / preflight_path if not preflight_path.is_absolute() else preflight_path
        )
        if not preflight_path.is_file() and not hint_path.is_file():
            print(
                f"[syncade] error: --spec-audit path {preflight_path} is not a "
                "readable file. Pass an absolute or relative path to a PR brief "
                "markdown file.",
                file=sys.stderr,
            )
            return WORKTREE_ERROR
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    pr_doc_path = resolve_repo_relative_input_path(
        args.spec_audit, repo_root=repo_root, label="--spec-audit"
    )
    if not pr_doc_path.is_file():
        print(
            f"[syncade] error: --spec-audit path {pr_doc_path} is not a "
            "readable file. Pass an absolute or relative path to a PR brief "
            "markdown file.",
            file=sys.stderr,
        )
        return WORKTREE_ERROR

    def _emit_deprecation(message: str) -> None:
        print(f"[syncade] {message}", file=sys.stderr)

    try:
        config = load_config(repo_root, preset=args.preset, deprecation_callback=_emit_deprecation)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    gate = auth_gate(config, AUDIT_BLOCKS)
    if gate is not None:
        return gate

    timeout = args.timeout if args.timeout is not None else 300.0

    if not args.quiet:
        print(f"[syncade] spec-audit: auditing {pr_doc_path.name} ...")

    result = run_spec_audit(
        pr_doc_path=pr_doc_path,
        repo_root=repo_root,
        timeout_seconds=timeout,
        config=config.auditor,
    )

    if result.outcome == "subprocess_error":
        if isinstance(result.error, SpecAuditOutputError):
            print(
                f"[syncade] spec-audit FAILED (parse error): {result.error}",
                file=sys.stderr,
            )
            return REVIEWER_OUTPUT_UNPARSEABLE
        print(
            f"[syncade] spec-audit FAILED (subprocess error): {result.error}",
            file=sys.stderr,
        )
        return REVIEWER_FAILURE

    assert result.output is not None
    output = result.output

    # Human-readable findings report to stdout.
    print(
        f"[syncade] spec-audit {output.verdict} "
        f"({len(output.findings)} finding(s), {result.duration_seconds:.1f}s)"
    )
    if output.findings:
        print()
        print("Findings (priority order):")
        for rank, idx in enumerate(output.priority_order, start=1):
            f = output.findings[idx]
            loc = f"  {f.section}" + (f":{f.line}" if f.line is not None else "")
            print(f"  [{rank}] {f.severity.upper()} — {f.issue_class}")
            print(f"      @ {loc.strip()}")
            print(f"      {f.finding}")
            if f.suggested_remediation:
                print(f"      → {f.suggested_remediation}")
    if output.summary:
        print()
        print(f"Summary: {output.summary}")
    if output.coverage_gaps:
        print()
        print("Coverage gaps:")
        for gap in output.coverage_gaps:
            print(f"  - {gap}")

    if result.outcome == "needs_clarification":
        if not args.quiet:
            print(
                "\n[syncade] spec-audit found blocker-severity issues. "
                "Edit the brief to address them, then re-run "
                "`syncade --spec-audit <pr-doc>`. Proceeding with "
                "`syncade <pr-doc>` despite these findings is your "
                "choice — the audit is advisory in v1."
            )
        return CLARIFICATION_NEEDED

    if not args.quiet:
        print("\n[syncade] spec-audit OK: brief is ready for review dispatch.")
    return SUCCESS


_DRAFT_DIALOGUE_SOFT_CAP = 200_000
"""Char threshold above which --draft-spec warns the dialogue may exceed the cold
drafter's context. A WARNING only — the full text is always passed (never silently
truncated; truncation would drop intent)."""


def _run_draft_spec(args) -> int:
    """Dispatch ``syncade --draft-spec --transcript <jsonl> [--base/--scope]``.

    Parses the session transcript for intent, captures the diff (only if
    --base/--scope is given — dialogue-only otherwise), runs the cold drafter, and
    writes a ratifiable ``.syncade/draft-spec-<session>.md``. Advisory: the operator
    reviews/edits it, then runs ``syncade <file>``. Exit codes follow the CLI
    mode-handler convention (0 drafted / 40 subprocess / 50 config / 60
    path-or-transcript / 70 parse)."""
    from syncade.snapshot import take_snapshot
    from syncade.spec_draft import (
        DEFAULT_SPEC_DRAFT_TIMEOUT_SECONDS,
        SpecDraftOutputError,
        render_draft_spec_markdown,
        run_spec_draft,
    )
    from syncade.transcript import TranscriptError, parse_transcript

    if not args.transcript:
        print(
            "[syncade] error: --draft-spec requires --transcript <session.jsonl>",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    repo_root_hint = Path(args.repo_root).expanduser() if args.repo_root else Path.cwd()
    try:
        repo_root = discover_repo_root(repo_root_hint)
    except SnapshotError as exc:
        print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    transcript_path = resolve_repo_relative_input_path(
        args.transcript, repo_root=repo_root, label="--transcript"
    )

    logger = Logger("quiet" if args.quiet else "normal")

    def _emit_deprecation(message: str) -> None:
        print(f"[syncade] {message}", file=sys.stderr)

    try:
        config = load_config(repo_root, preset=args.preset, deprecation_callback=_emit_deprecation)
    except ConfigError as exc:
        print(f"[syncade] config error: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    gate = auth_gate(config, DRAFT_BLOCKS)
    if gate is not None:
        return gate

    try:
        dialogue = parse_transcript(transcript_path)
    except TranscriptError as exc:
        print(f"[syncade] transcript error: {exc}", file=sys.stderr)
        return WORKTREE_ERROR

    # Diff base is optional — dialogue-only draft when neither --base nor --scope.
    base_ref = args.base
    if args.scope is not None:
        base_ref = _resolve_scope_base(repo_root, args.scope, logger)
        if base_ref is None:
            return WORKTREE_ERROR
    diff = ""
    if base_ref is not None:
        try:
            diff = take_snapshot(repo_root, base_ref=base_ref).diff_text
        except SnapshotError as exc:
            print(f"[syncade] snapshot error: {exc}", file=sys.stderr)
            return WORKTREE_ERROR

    if len(dialogue) > _DRAFT_DIALOGUE_SOFT_CAP:
        logger.warning(
            f"draft-spec: dialogue is large ({len(dialogue)} chars > "
            f"{_DRAFT_DIALOGUE_SOFT_CAP} soft cap); passing it in full — the cold "
            "drafter may exceed its context. If the draft looks truncated, scope the "
            "work (--base/--scope) or draft from a shorter session."
        )

    if not args.quiet:
        print(f"[syncade] draft-spec: drafting from {transcript_path.name} ...")

    timeout = args.timeout if args.timeout is not None else DEFAULT_SPEC_DRAFT_TIMEOUT_SECONDS
    result = run_spec_draft(
        dialogue=dialogue,
        diff=diff,
        repo_root=repo_root,
        timeout_seconds=timeout,
        config=config.drafter,
    )

    if result.outcome == "subprocess_error":
        if isinstance(result.error, SpecDraftOutputError):
            print(f"[syncade] draft-spec FAILED (parse error): {result.error}", file=sys.stderr)
            return REVIEWER_OUTPUT_UNPARSEABLE
        print(f"[syncade] draft-spec FAILED (subprocess error): {result.error}", file=sys.stderr)
        return REVIEWER_FAILURE

    assert result.output is not None
    out_dir = repo_root / ".syncade"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = unique_markdown_path(out_dir, f"draft-spec-{transcript_path.stem}")
    out_path.write_text(
        render_draft_spec_markdown(
            result.output, source_label=transcript_path.stem, base_ref=base_ref
        ),
        encoding="utf-8",
    )

    n_inferred = sum(1 for c in result.output.acceptance_criteria if c.origin == "inferred")
    print(
        f"[syncade] draft-spec OK: wrote {out_path} "
        f"({len(result.output.acceptance_criteria)} criteria, {n_inferred} inferred, "
        f"{len(result.output.assumptions)} assumption(s); {result.duration_seconds:.1f}s)"
    )
    base_suffix = f" --base {base_ref}" if base_ref else ""
    print(
        "[syncade] REVIEW + edit the draft (especially 'Assumptions to confirm'), "
        f"then run: syncade {out_path}{base_suffix}"
    )
    return SUCCESS


# ``--resume`` lives in its own module (the largest one-shot handler) to keep this
# file under the blocking file-length cap. It renders a real reviewer diff, so it
# takes no ``--base``/``--scope`` guard wrapper. ``main()`` reads ``_run_resume``
# from this module, so the re-export is what dispatch invokes.
# ``--gc`` lives in its own module to keep this file under the blocking
# file-length cap. It is wrapped (not bare re-exported) here so the
# ``--base`` / ``--scope`` rejection (N13) applies uniformly across every
# diagnostic/maintenance mode from the one ``_reject_diff_base_flags`` helper,
# without reaching into gc_mode.py. ``main()`` reads ``_run_gc`` from this
# module, so the wrapper is what dispatch invokes.
from .doctor_mode import _run_doctor as _run_doctor  # noqa: E402
from .gc_mode import _run_gc as _run_gc_impl  # noqa: E402
from .resume_mode import _run_resume as _run_resume  # noqa: E402


def _run_gc(args) -> int:
    """N13 guard wrapper around :func:`syncade.cli.gc_mode._run_gc`.

    ``--gc`` renders no reviewer diff, so ``--base`` / ``--scope`` are rejected
    (exit 2) before the maintenance pass runs; otherwise the call delegates
    verbatim.
    """
    rejection = _reject_diff_base_flags(args, "--gc")
    if rejection is not None:
        return rejection
    return _run_gc_impl(args)


from .metrics_mode import _run_metrics as _run_metrics_impl  # noqa: E402


def _run_metrics(args) -> int:
    """N13 guard wrapper around :func:`syncade.cli.metrics_mode._run_metrics`.

    ``--metrics`` renders no reviewer diff, so ``--base`` / ``--scope`` are
    rejected (exit 2) before the read-only aggregation pass runs.
    """
    rejection = _reject_diff_base_flags(args, "--metrics")
    if rejection is not None:
        return rejection
    return _run_metrics_impl(args)
