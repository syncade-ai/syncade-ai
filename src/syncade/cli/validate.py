"""Command-shape validation for the ``syncade`` CLI (split out of ``cli/__init__`` for the LOC cap).

The mutual-exclusion and context-only-knob checks that must run BEFORE any filesystem work, so a bad
invocation fails for $0 — never a config/snapshot/auth cost from a stale ``.syncade/`` or non-git
cwd.
"""

from __future__ import annotations

import sys

from syncade.exit_codes import CLI_USAGE_ERROR

from .modes import _reject_config_mode_conflicts, _reject_diff_base_flags


def validate_command_shape(args, parser) -> int | None:
    """Return an exit code if ``args`` is an invalid command shape (mutually exclusive flags, a
    context-only knob used out of context, or no command at all), else ``None`` to proceed to
    dispatch. Pure argument inspection — NO filesystem work."""
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
    # --base "" is always invalid: take_snapshot treats an empty string as absent
    # (``if base_ref:`` guard), silently falling back to the no-diff full-HEAD
    # path. Catch it before any filesystem work so the operator gets a clear
    # usage error rather than a silent wrong diff.
    if args.base is not None and not args.base:
        print(
            "[syncade] error: --base requires a non-empty ref; got an empty string",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    # --resume "" is always invalid: nargs="?" means bare --resume gives "latest";
    # an explicit empty string is neither a run-id nor a valid alias.
    if args.resume is not None and not args.resume:
        print(
            "[syncade] error: --resume requires a non-empty run-id; "
            "pass --resume alone to resume the latest eligible run",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    # --spec-audit "" is always invalid: the path argument cannot be empty.
    if args.spec_audit is not None and not args.spec_audit:
        print(
            "[syncade] error: --spec-audit requires a non-empty path",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
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
    # --two-dot selects a diff RANGE, and a resumed run has no range left to
    # select: run-init.json records the base OID the original run already
    # resolved, so the resumed diff is taken against that pinned commit under
    # either mode. Accepting the flag here would silently do nothing.
    if args.two_dot and args.resume:
        print(
            "[syncade] error: --two-dot cannot be combined with --resume; the "
            "resumed run diffs against the base OID recorded in run-init.json, "
            "which the original run already resolved",
            file=sys.stderr,
        )
        return 2
    # --two-dot switches the diff mode from three-dot (branch-point) to literal
    # two-dot (base..HEAD), so it is only meaningful when a base is also supplied.
    # Without --base/--scope there is no range to switch, and the flag would be
    # silently accepted but do nothing — reject it up front.
    if args.two_dot and args.base is None and args.scope is None:
        print(
            "[syncade] error: --two-dot requires --base or --scope; it selects the "
            "literal base..HEAD range instead of the default branch-point diff, "
            "which only has meaning when a base is also provided",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    # --force-drift only has meaning during resume.
    if args.force_install and args.install_skill is None:
        print(
            "[syncade] error: --force-install requires --install-skill; it overrides the "
            "installer's refusal to destroy files it did not write, and has no meaning "
            "outside that context",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
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
            ("--update", args.update),
            ("--config", args.config is not None),
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
    # --update replaces the running package and exits; like --gc/--metrics it renders no
    # reviewer diff, so --base / --scope / --two-dot are meaningless here too.
    if args.update:
        rejection = _reject_diff_base_flags(args, "--update")
        if rejection is not None:
            return rejection
        for _flag, _used in (
            ("--gc", args.gc),
            ("--metrics", args.metrics),
            ("--selfcheck", args.selfcheck),
            ("--auth-check", args.auth_check),
            ("--spec-audit", bool(args.spec_audit)),
            ("--draft-spec", args.draft_spec),
            ("--openspec", args.openspec is not None),
            ("--resume", args.resume is not None),
            ("--config", args.config is not None),
            ("--install-skill", args.install_skill is not None),
            ("PR_DOC", bool(args.pr_doc)),
            # Review-loop-only flags: accepted by the parser but meaningless for --update,
            # consistent with the same guard already present for --install-skill.
            ("--force-dirty", getattr(args, "force_dirty", False)),
            ("--allow-default-branch", getattr(args, "allow_default_branch", False)),
            ("--timeout", args.timeout is not None),
            ("--preset", args.preset is not None),
            ("--max-rounds", args.max_rounds is not None),
            ("--worktree-base", args.worktree_base is not None),
        ):
            if _used:
                print(f"[syncade] error: --update cannot be combined with {_flag}", file=sys.stderr)
                return 2

    # --install-skill is a file-copy one-shot mode; it renders no reviewer diff,
    # so --base / --scope / --two-dot are meaningless and would be silently ignored.
    if args.install_skill is not None:
        rejection = _reject_diff_base_flags(args, "--install-skill")
        if rejection is not None:
            return rejection
        # ...and it is mutually exclusive with every OTHER mode, which it was not.
        # Measured: all 9 pairings — including `syncade <brief> --install-skill claude` —
        # were ACCEPTED, exit 0, with the other intent silently dropped. An operator asking
        # for a review got a skill install and a success code. Same shape as --doctor's list
        # above; `test_mode_pairs_are_all_rejected` derives the matrix from the parser, so a
        # mode added later is covered without editing either list.
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
            # --config is deliberately ABSENT: _reject_config_mode_conflicts below already
            # covers that pair and words it from --config's side. Listing it here would be
            # duplicate coverage that only changes which flag the message names first.
            # Review-loop-only flags: accepted by the parser but meaningless in installer mode.
            # Silently ignoring them lets an operator supply real review intent that is lost.
            ("--force-dirty", getattr(args, "force_dirty", False)),
            ("--allow-default-branch", getattr(args, "allow_default_branch", False)),
            ("--timeout", args.timeout is not None),
            ("--preset", args.preset is not None),
            ("--max-rounds", args.max_rounds is not None),
            ("--worktree-base", args.worktree_base is not None),
        ):
            if _used:
                print(
                    f"[syncade] error: --install-skill cannot be combined with {_flag}",
                    file=sys.stderr,
                )
                return 2
    # --allow-auto-init controls whether the review entry path may git-init a populated
    # directory; it is meaningless (and silently ignored) in every other mode.
    _allow_auto_init_applies = bool(args.pr_doc) or (args.openspec is not None)
    if args.allow_auto_init and not _allow_auto_init_applies:
        print(
            "[syncade] error: --allow-auto-init is meaningful only with a review invocation "
            "(a PR_DOC or --openspec); it has no effect in one-shot modes",
            file=sys.stderr,
        )
        return CLI_USAGE_ERROR
    _config_conflict = _reject_config_mode_conflicts(args)
    if _config_conflict is not None:
        return _config_conflict
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
        and args.config is None
        and not args.update
    ):
        parser.print_help(sys.stderr)
        return 2
    return None
