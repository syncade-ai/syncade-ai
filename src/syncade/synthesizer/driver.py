"""Cold-synth subprocess driver.

:func:`run_synthesizer` runs once per round after reviewer output parsing. It
owns cold-workspace provisioning, prompt rendering, codex invocation, output
extraction, and provenance validation.

See the package docstring for the cold-synth architectural invariants
(no worktree, active sandbox via trusted permissions, env scrub, no
verdict, cold inputs).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from syncade import retry
from syncade.adapters.base import ReviewerAdapter, ReviewerInvocationError
from syncade.adapters.registry import get_adapter
from syncade.config import ReviewerConfig
from syncade.config_cold import SynthesizerConfig
from syncade.dispatcher import ReviewerRunResult
from syncade.pricing_config import PricingConfig
from syncade.process import (
    SubprocessError,
    SubprocessNotFoundError,
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from syncade.prompts import load_synthesizer_template, render_synthesizer_prompt
from syncade.synthesis import (
    SynthesizerOutputError,
    get_synthesizer_schema_string,
    parse_synthesizer_output,
)
from syncade.usage import Usage, _add_usage, _auth_mode, usage_for

from .constants import SYNTHESIZER_NAME
from .rendering import render_reviewer_outputs_blob
from .result import SynthesizerResult
from .validation import (
    _validate_cluster_quotes_against_reviewers,
    _validate_duplicate_blockers_not_split_deactivated,
    _validate_provenance_against_reviewers,
    _validate_reviewer_blockers_passed_through,
)
from .workspace import _init_workspace_git, _scrub_env_for_cold_synth


def run_synthesizer(
    reviewer_results: list[ReviewerRunResult],
    *,
    repo_root: Path,
    pr_doc_path: Path,
    timeout_seconds: float,
    master_plan_path: Path | None = None,
    config: SynthesizerConfig | None = None,
    adapter: ReviewerAdapter | None = None,
    pricing: PricingConfig | None = None,
    max_retries: int = retry.MAX_RETRIES,
) -> SynthesizerResult:
    """Run the cold synthesizer subprocess.

    Lifecycle:

    1. Build the synthesizer prompt: load the template via
       :func:`syncade.prompts.load_synthesizer_template` (per-repo override at
       ``.syncade/templates/synthesizer.md`` wins), render via
       :func:`syncade.prompts.render_synthesizer_prompt` with the
       reviewer outputs serialized as the
       ``reviewer_outputs_json`` placeholder.
    2. Build the invocation from ``config`` (the ``[synthesizer]`` block) routed
       through ``adapter`` — which, when not injected, is resolved from the
       ADAPTER REGISTRY by ``config.provider``, exactly like a reviewer's. The
       ``worktree_path`` arg is the isolated tempdir workspace, so the CLI's
       cwd/add-dir flags both scope to that workspace rather than to
       ``repo_root``.
    3. Run the subprocess via :func:`syncade.process.run_subprocess`. The
       per-reviewer timeout is reused.
    4. Extract the model's final text via
       :meth:`~syncade.adapters.base.ReviewerAdapter.extract_final_text`, passing
       :class:`SynthesizerOutputError` for empty-output diagnostics.
    5. Parse the text via :func:`syncade.synthesis.parse_synthesizer_output` — the
       shared extractor handles fenced JSON, JSON-in-prose, and JSX-shaped
       snippets.

    Returns a :class:`SynthesizerResult` regardless of outcome. The
    caller (orchestrator) inspects it to decide the exit code:

    - ``output is not None`` → mechanical verdict from
      ``consolidated_findings``.
    - ``isinstance(error, SynthesizerOutputError)`` → exit 70
      (parse failure).
    - ``isinstance(error, (ReviewerInvocationError, SubprocessError))``
      → exit 40 (subprocess failure).

    The synthesizer is NOT given a worktree. See module docstring for
    why this is a deliberate architecture choice.

    Args:
        reviewer_results: The dispatcher's per-reviewer results. Only
            successful reviewers (with ``output is not None``)
            contribute to the prompt; failures would not have reached
            here under normal orchestrator flow.
        repo_root: The git repo root. Used only for (a) per-repo
            template-override lookup
            via :func:`~syncade.prompts.load_synthesizer_template`,
            which checks
            ``<repo_root>/.syncade/templates/synthesizer.md``, and
            (b) the env-scrub substring check via
            :func:`_scrub_env_for_cold_synth`. It is NOT passed to the codex
            subprocess as ``cwd``, ``-C``, or
            ``--add-dir`` — those scope to the tempdir workspace.
            The synth never reads files directly from ``repo_root``.
        pr_doc_path: Absolute path to the PR doc. Substituted into the
            prompt's ``{pr_doc_path}`` placeholder.
        timeout_seconds: Per-process wall-clock timeout. Reuses the
            same value as the reviewer dispatch (v1 doesn't split
            timeouts per phase).
        master_plan_path: Optional path to the master plan. Mirrors the
            reviewer prompt's master-plan handling: ``None`` renders
            as ``"(none)"`` in the prompt.
        config: The ``[synthesizer]`` block. Defaults to
            :class:`~syncade.config_cold.SynthesizerConfig` defaults, which
            reproduce the pre-PR-v2-23 hardcoded constants exactly.
        adapter: Optional :class:`~syncade.adapters.base.ReviewerAdapter`.
            Defaults to ``get_adapter(config.provider)`` — the same registry the
            dispatcher uses, which is what makes the judge provider-agnostic
            instead of codex-only. Tests pass duck-typed fakes so the subprocess
            can be short-circuited without a real CLI.

    Returns: :class:`SynthesizerResult` carrying either the parsed output
        or the captured exception, plus the raw subprocess result
        (when available) for persistence.
    """
    import time

    run_start = time.monotonic()
    # H5: count the extra synth subprocess attempts spent riding out transient
    # provider blips and stamp it onto EVERY SynthesizerResult, so persistence
    # surfaces the synth retry count next to the reviewer one. _SR aliases the
    # dataclass; _result injects ``retries=`` so the many return sites below
    # stay DRY (mirrors the dispatcher's per-result ``retries=``).
    synth_retries = 0
    synth_usage: Usage | None = None  # set once the subprocess returns (finding #5)
    _SR = SynthesizerResult

    synth_cfg = config if config is not None else SynthesizerConfig()

    def _result(**kwargs: object) -> SynthesizerResult:
        # Default usage to the completed subprocess's, so failure returns keep it
        # too — not only the success path (dogfood finding #5).
        kwargs.setdefault("usage", synth_usage)
        kwargs.setdefault("provider", synth_cfg.provider)
        kwargs.setdefault("model", synth_cfg.model)
        return _SR(retries=synth_retries, **kwargs)

    # The registry — not a hardcoded class — is what lets an all-Anthropic user
    # finish a run on a box with no codex installed.
    adapter = adapter if adapter is not None else get_adapter(synth_cfg.provider)

    # --- Cold workspace -----------------------------------------
    # Provision an isolated tempdir workspace containing ONLY the PR
    # doc (and the master plan if supplied). The synth subprocess
    # runs from here; codex's -C / --add-dir flags scope to this
    # workspace, NOT to the repo root.
    #
    # The workspace is also git-init'd up front (trusted-execute
    # codex requires cwd to be a git working tree), and the env is
    # scrubbed of repo_root-leaking values before launch. Combined
    # with permissions=trusted-execute, the codex sandbox enforces the
    # workspace boundary structurally instead of relying on the
    # model honoring the prompt's "do not read the diff" instruction.
    #
    # The TemporaryDirectory is the with-block scope; the workspace
    # disappears after we exit (even on early-return failure paths).
    # The captured SubprocessResult contains strings (stdout/stderr),
    # not paths, so persistence after the with-block exit is safe.
    with tempfile.TemporaryDirectory(prefix="syncade-synth-") as workspace_str:
        workspace = Path(workspace_str)

        # git-init the workspace so trusted-execute codex accepts
        # it as a working tree. Failures bucket as SubprocessError →
        # exit 40 with a clear synthesizer.error.txt.
        try:
            _init_workspace_git(workspace)
        except SubprocessError as exc:
            return _result(
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
            )

        # Copy the PR doc into the workspace. Each input file
        # lives in its OWN subdirectory so basename collisions
        # between pr_doc and master_plan can't make one overwrite
        # the other (e.g. ``acme/pr-7/spec.md`` and
        # ``acme/master/spec.md`` both have basename ``spec.md``;
        # without per-input subdirs, the second ``shutil.copy2`` would
        # silently clobber the first and the prompt would reference
        # the wrong content).
        pr_doc_subdir = workspace / "pr-doc"
        try:
            pr_doc_subdir.mkdir()
            workspace_pr_doc = pr_doc_subdir / pr_doc_path.name
            shutil.copy2(pr_doc_path, workspace_pr_doc)
        except (OSError, shutil.SameFileError) as exc:
            # Copy failure (permissions, missing source) is a workspace
            # setup problem, not a model problem. Surface as a generic
            # SubprocessError-bucket failure so the operator gets
            # exit 40 + a clear .error.txt.
            return _result(
                output=None,
                error=SubprocessError(
                    f"synthesizer: failed to set up cold workspace at "
                    f"{workspace}: could not copy {pr_doc_path} — {exc}"
                ),
                duration_seconds=time.monotonic() - run_start,
            )

        workspace_master_plan: Path | None = None
        if master_plan_path is not None:
            master_plan_subdir = workspace / "master-plan"
            try:
                master_plan_subdir.mkdir()
                workspace_master_plan = master_plan_subdir / master_plan_path.name
                shutil.copy2(master_plan_path, workspace_master_plan)
            except (OSError, shutil.SameFileError) as exc:
                return _result(
                    output=None,
                    error=SubprocessError(
                        f"synthesizer: failed to set up cold workspace — "
                        f"could not copy master plan {master_plan_path}: {exc}"
                    ),
                    duration_seconds=time.monotonic() - run_start,
                )

        # --- Build the prompt -------------------------------------------
        # The workspace input path the prompt references is the WORKSPACE-relative
        # one (so a model that reads it gets the workspace copy, not the
        # original in the repo).
        try:
            template = load_synthesizer_template(repo_root)
            reviewer_outputs_blob = render_reviewer_outputs_blob(reviewer_results)
            prompt = render_synthesizer_prompt(
                template,
                pr_doc_path=str(workspace_pr_doc),
                reviewer_outputs_json=reviewer_outputs_blob,
                master_plan_path=(
                    str(workspace_master_plan) if workspace_master_plan is not None else None
                ),
                json_schema=get_synthesizer_schema_string(),
            )
        except (KeyError, ValueError) as exc:
            return _result(
                output=None,
                error=SubprocessError(f"synthesizer template render failed: {exc}"),
                duration_seconds=time.monotonic() - run_start,
            )

        # --- Build the invocation ---------------------------------------
        # The judge's knobs, carried on a synthetic ReviewerConfig so we can reuse
        # the adapter's build_invocation rather than open-coding each provider's
        # flag plumbing. The "worktree_path" arg is the WORKSPACE, not repo_root —
        # that's the cold-isolation invariant: the CLI's cwd/add-dir flags scope to
        # the workspace.
        synth_config = ReviewerConfig(
            name=SYNTHESIZER_NAME,
            provider=synth_cfg.provider,
            model=synth_cfg.model,
            thinking=synth_cfg.thinking,
            permissions=synth_cfg.permissions,
            # PR-v2-24: carry the auth declaration onto the synthetic config the
            # adapter sees. Without this the cold actors would silently run
            # unenforced -- the classic 'guarded four of five actors' leak.
            auth=synth_cfg.auth,
            api_key_env=synth_cfg.api_key_env,
        )
        try:
            invocation = adapter.build_invocation(synth_config, workspace, prompt)
        except ValueError as exc:
            # narrowed from `except Exception`. The only
            # exception class build_invocation legitimately raises is
            # ValueError (every adapter's provider/permissions validation
            # raises ValueError on bad input — e.g. a `safe` judge, which
            # would prompt and hang). Programming bugs (TypeError, AttributeError,
            # KeyError, ImportError, etc.) SHOULD crash the
            # orchestrator visibly rather than getting bucketed as a
            # normal exit-40 subprocess failure with a misleading
            # `synthesizer.error.txt` — those bugs need a stack
            # trace, not a polite failure mode.
            return _result(
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
            )

        # --- Run the subprocess + extract (with bounded transient retry) -
        # cwd is the WORKSPACE — not repo_root. Codex defaults relative
        # file access to here. Trusted permissions keep the codex
        # sandbox active and scoped to this workspace.
        #
        # Bounded retry (H5): a transient provider blip surfaced by
        # extract_final_text as a ReviewerInvocationError
        # (a 429/5xx or a dropped socket) re-runs the codex subprocess up
        # to max_retries extra times with jittered backoff, instead
        # of aborting the round at exit 40. Re-running the subprocess (not
        # just re-parsing the same stdout) is required: the transient
        # failure lives in the subprocess output, so a fresh invocation is
        # the only way to get a clean turn. Timeouts, missing-binary, and
        # parse/contract failures are terminal and never retried (see
        # syncade.retry).
        subprocess_result: SubprocessResult | None = None
        final_text: str | None = None
        for attempt in range(1, max_retries + 2):
            subprocess_result = None
            try:
                subprocess_result = run_subprocess(
                    invocation.argv,
                    cwd=workspace,
                    # scrub repo-root-leaking vars (PWD,
                    # OLDPWD, repo-local path-list segments, and scalar
                    # repo-root path references) from the env before
                    # launch. Combined with
                    # workspace-scoped cwd/-C/--add-dir AND trusted
                    # permissions (active codex sandbox), this enforces
                    # the cold-isolation invariant: the synth subprocess
                    # cannot trivially discover or read repo_root.
                    # Pass the scrubbed env directly; do not fall back to
                    # os.environ.
                    env=_scrub_env_for_cold_synth(invocation.env, repo_root),
                    timeout=timeout_seconds,
                    input_text=invocation.stdin_text,
                )
            except SubprocessTimeoutError as exc:
                # Same partial-output preservation pattern as
                # syncade.dispatcher._run_single_reviewer: synthesize a
                # SubprocessResult from the timeout exception's partial
                # stdout/stderr so persistence still writes the .stdout /
                # stderr files. Sentinel returncode=-1 marks "killed
                # before exit". A timeout is the budget, not a blip — terminal.
                elapsed = time.monotonic() - run_start
                partial = SubprocessResult(
                    returncode=-1,
                    stdout=exc.stdout,
                    stderr=exc.stderr,
                    duration_seconds=elapsed,
                )
                synth_usage = _add_usage(
                    synth_usage,
                    usage_for(
                        partial, synth_cfg.provider, synth_cfg.model, pricing, _auth_mode(synth_cfg)
                    ),
                )
                return _result(
                    output=None,
                    error=exc,
                    duration_seconds=elapsed,
                    raw_subprocess_result=partial,
                )
            except SubprocessNotFoundError as exc:
                # Codex binary missing — the subprocess never started, so
                # there's no partial output to preserve. Persistence writes
                # empty .stdout/.stderr and the .error.txt.
                return _result(
                    output=None,
                    error=exc,
                    duration_seconds=time.monotonic() - run_start,
                )
            except SubprocessError as exc:
                # Other launch failures (bad cwd, OS error). Same shape as
                # SubprocessNotFoundError — no subprocess output to preserve.
                return _result(
                    output=None,
                    error=exc,
                    duration_seconds=time.monotonic() - run_start,
                )

            # The subprocess completed — extract usage now so a later extraction /
            # parse / validation failure still records it (dogfood finding #5).
            synth_usage = _add_usage(
                synth_usage,
                usage_for(
                    subprocess_result,
                    synth_cfg.provider,
                    synth_cfg.model,
                    pricing,
                    _auth_mode(synth_cfg),
                ),
            )

            # --- Extract the final agent message ------------------------
            # Pull the final agent_message text out of codex's JSONL
            # stream, then hand it to parse_synthesizer_output. The extract
            # helper raises ReviewerInvocationError on subprocess-side
            # failures (turn.failed, auth, non-zero rc) and raises
            # SynthesizerOutputError if no agent_message is present
            # (caller-configurable via the
            # empty_output_exception_class kwarg).
            try:
                final_text = adapter.extract_final_text(
                    subprocess_result,
                    empty_output_exception_class=SynthesizerOutputError,
                )
            except ReviewerInvocationError as exc:
                # Subprocess-side provider failure. A transient blip
                # (429/5xx/dropped socket) earns another fresh subprocess
                # attempt; every other provider verdict is terminal.
                # Preserve the raw subprocess result on the terminal path so
                # persistence writes the .stdout / .stderr files — the user
                # inspecting exit 40 can read the raw codex output.
                if retry.is_transient_api_error(exc) and attempt <= max_retries:
                    synth_retries += 1
                    retry.backoff_sleep(attempt)
                    continue
                return _result(
                    output=None,
                    error=exc,
                    duration_seconds=time.monotonic() - run_start,
                    raw_subprocess_result=subprocess_result,
                )
            except SynthesizerOutputError as exc:
                # No agent_message present — a contract failure, not a blip.
                # Preserve the raw subprocess result so persistence
                # writes the .stdout / .stderr files — the user
                # inspecting exit 70 can read the raw codex output
                # even when the typed extraction failed.
                return _result(
                    output=None,
                    error=exc,
                    duration_seconds=time.monotonic() - run_start,
                    raw_subprocess_result=subprocess_result,
                )
            except Exception as exc:  # noqa: BLE001 — preserve subprocess failure contract
                # Unexpected exception classes from the extractor (e.g.
                # AttributeError / IndexError / ValueError raised against a bizarre
                # JSONL shape from the codex subprocess) must become a persisted
                # subprocess failure rather than crashing run_review and leaving no
                # manifest.json, summary.md,
                # or synthesizer.error.txt — the operator had nothing to
                # diagnose with.
                #
                # The extractor processes stdout from a foreign process
                # (codex), so unexpected exception shapes are more
                # plausibly triggered by model-output variation than by
                # programming bugs in our own code. Map to a polite
                # SynthesizerResult so persistence writes every artifact
                # the operator needs. _compute_exit_code's defensive
                # branch maps this to exit 40 (REVIEWER_FAILURE) — same
                # bucket as a codex subprocess failure but with the
                # original exception class preserved in synthesizer.error.txt
                # so the operator can route by failure shape.
                #
                # Compare with the build_invocation catch: build_invocation
                # receives only OUR input, so unexpected
                # exceptions there ARE programming bugs and we re-raise.
                # Different threat models, different catch widths.
                return _result(
                    output=None,
                    error=exc,
                    duration_seconds=time.monotonic() - run_start,
                    raw_subprocess_result=subprocess_result,
                )
            break

        # The loop breaks with final_text set on success; every failure path
        # returns, and the transient branch is the only `continue` (it falls
        # through to a terminal return once attempts are exhausted). So this
        # guard is unreachable — it narrows the type and mirrors the
        # dispatcher's unreachable-exit assertion.
        if final_text is None:
            raise AssertionError("unreachable synthesizer retry loop exit")

        try:
            output = parse_synthesizer_output(final_text)
        except SynthesizerOutputError as exc:
            # The text was present but didn't parse — e.g. the model
            # invented a finding (empty provenance) or tried to dismiss
            # a unanimous blocker. Preserve the raw subprocess result
            # so persistence has the full context.
            return _result(
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
            )

        # cross-input provenance validation. The schema's
        # `min_length=1` on provenance prevents the most obvious
        # "invented finding" case (zero provenance entries) but does
        # NOT catch the model fabricating a provenance entry with a
        # wrong reviewer_name OR an out-of-range original_index — both
        # would render into findings.md as if the synth had real
        # attribution. The schema can't check this because it has no
        # awareness of the input reviewer set; the orchestrator does,
        # so the check lives here.
        try:
            _validate_provenance_against_reviewers(output, reviewer_results)
            # cluster quotes must be verbatim substrings of the
            # reviewer-original finding text — the cannot-invent guarantee for
            # the descriptive-only root-cause clusters. Runs after provenance
            # validation so each member's provenance is already known-valid.
            _validate_cluster_quotes_against_reviewers(output, reviewer_results)
            # Finding R: every reviewer-surfaced blocker must pass through to
            # consolidated_findings (active or dismissed-with-rationale) — the
            # cannot-omit partner of the cannot-invent provenance check. An
            # omitted blocker would falsely SHIP (the verdict reads only
            # consolidated_findings). Runs last so the referenced provenance
            # pairs are already known-valid.
            _validate_reviewer_blockers_passed_through(output, reviewer_results)
            # Defense-in-depth for the mechanically provable split-evasion
            # case: exact duplicate source blocker text from distinct reviewers
            # must not be split into separate downgraded/dismissed findings.
            _validate_duplicate_blockers_not_split_deactivated(output, reviewer_results)
        except SynthesizerOutputError as exc:
            return _result(
                output=None,
                error=exc,
                duration_seconds=time.monotonic() - run_start,
                raw_subprocess_result=subprocess_result,
            )

        return _result(
            output=output,
            error=None,
            duration_seconds=time.monotonic() - run_start,
            raw_subprocess_result=subprocess_result,
        )
