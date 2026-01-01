"""``syncade --selfcheck`` — verify the configured producer can headless-commit.

Pre-flight smoke for the producer's commit path: provisions a throwaway git
repo + stub ``findings.md``, runs the
configured producer adapter against the stub, and verifies that
HEAD moved AND the requested marker text is present in the seed
file.

Motivation: sandboxed producer modes cannot headless-commit reliably. The fix
was to make ``"yolo"`` the only supported producer permission. Provider
semantics can still evolve, and a real loop
would only surface that as a stall at the end of round 0. ``syncade
--selfcheck`` runs in ~30 seconds and surfaces drift before the next loop
hits it.

The selfcheck verifies whatever permission level the operator's
``[producer]`` block configures. If ``permissions="yolo"`` is what
works on their machine, that's what gets verified.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from syncade.adapters.producer import ProducerAdapter
from syncade.config import SyncadeConfig
from syncade.exit_codes import (
    FINDINGS_PRESENT,
    SUCCESS,
    WORKTREE_ERROR,
)
from syncade.persistence import persist_findings_md, persist_producer_result
from syncade.process import SubprocessError, SubprocessResult, run_subprocess
from syncade.producer import run_producer
from syncade.synthesis import (
    ConsolidatedFinding,
    FindingProvenance,
    SynthesizerOutput,
)
from syncade.synthesizer import SynthesizerResult
from syncade.worktree import WorktreeError, WorktreeManager

SELFCHECK_MARKER = "# Selfcheck PASSED"
"""The literal marker text the producer must add to the seed file.

Pinned as a module constant so tests and the producer-facing
instruction stay in lockstep — a refactor that changes the marker
in the instruction text but not in the verifier (or vice versa)
would let a broken producer pass selfcheck."""

SELFCHECK_SEED_BASENAME = "selfcheck.py"
"""Basename of the seed file the producer edits. A ``.py`` extension
keeps the file looking like real code (a producer that refuses to
edit non-source files still treats this as code-shaped)."""

SELFCHECK_PR_DOC_BASENAME = "pr-doc.md"
"""Basename of the stub PR doc passed to the producer's
``{pr_doc_path}`` placeholder."""

_SELFCHECK_GIT_USER_NAME = "Syncade Selfcheck"
_SELFCHECK_GIT_USER_EMAIL = "selfcheck@syncade.test"

_KNOWN_GIT_SETUP_STEPS = ("init", "symbolic-ref", "add", "commit", "rev-parse")
"""Setup-phase git subcommands the selfcheck issues against the
throwaway repo. Used by :func:`_identify_git_step` to extract a
human-readable step name from a failed git invocation's ``cmd``
list when mapping :class:`subprocess.CalledProcessError` to
``WORKTREE_ERROR``."""


def _identify_git_step(cmd: list[str]) -> str:
    """Return the git subcommand from a failed git invocation's
    ``cmd`` list (``init`` / ``symbolic-ref`` / ``add`` / ``commit`` /
    ``rev-parse``).

    Handles both the real :func:`_git` cmd shape
    (``["git", "-c", "user.email=...", "-c", "user.name=...",
    <subcommand>, ...]``) and the test-monkey-patched shape
    (``["git", <subcommand>, ...]``) by scanning for the first
    known subcommand token rather than indexing by position. Falls
    back to ``"<unknown>"`` if no known token is found — the
    surrounding error message still names the failure (the rc and
    stderr are emitted alongside).
    """
    for arg in cmd or []:
        if arg in _KNOWN_GIT_SETUP_STEPS:
            return arg
    return "<unknown>"


_SELFCHECK_GIT_TIMEOUT_SECONDS = 30.0


def _git(cwd: Path, *args: str) -> SubprocessResult:
    """Run a git command with selfcheck-scoped author identity,
    raising :class:`subprocess.CalledProcessError` on non-zero rc.

    Used only for the throwaway selfcheck repo's setup commits. It
    still routes through :func:`syncade.process.run_subprocess` so git
    setup inherits the same timeout and launch-failure behavior as the
    orchestrator's other critical git calls.
    """
    argv = [
        "git",
        "-c",
        f"user.email={_SELFCHECK_GIT_USER_EMAIL}",
        "-c",
        f"user.name={_SELFCHECK_GIT_USER_NAME}",
        *args,
    ]
    result = run_subprocess(
        argv,
        cwd=cwd,
        timeout=_SELFCHECK_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            returncode=result.returncode,
            cmd=argv,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def _build_stub_synth_output() -> SynthesizerOutput:
    """Construct a :class:`SynthesizerOutput` with one blocker
    finding instructing the producer to add the marker line.

    The finding is shaped to look like what a real synthesizer
    would emit for a one-line fix request — a blocker with single-
    reviewer provenance, file-scoped, with an actionable
    description. This way the producer's reading of ``findings.md``
    encounters the same structure it would in a real loop, so a
    drift in the findings.md format (e.g. a new required section)
    breaks selfcheck the same way it would break the real loop.
    """
    return SynthesizerOutput(
        consolidated_findings=[
            ConsolidatedFinding(
                description=(
                    f"`{SELFCHECK_SEED_BASENAME}` is missing a "
                    f"`{SELFCHECK_MARKER}` comment. Add a single new "
                    "line of comment text below the existing comment "
                    "with that exact text, then commit. Commit subject: "
                    "`selfcheck: add passed marker`. No other changes."
                ),
                file=SELFCHECK_SEED_BASENAME,
                severity="blocker",
                provenance=[
                    FindingProvenance(
                        reviewer_name="selfcheck",
                        original_severity="blocker",
                        original_index=0,
                        original_description=(
                            f"Missing `{SELFCHECK_MARKER}` marker comment in "
                            f"`{SELFCHECK_SEED_BASENAME}`."
                        ),
                    )
                ],
            )
        ],
        synthesis_summary=(
            "Selfcheck stub: producer must add the marker comment "
            f"to {SELFCHECK_SEED_BASENAME} and commit."
        ),
    )


def _resolve_timeout(config: SyncadeConfig, override: float | None) -> float:
    """Resolve the per-producer wall-clock timeout.

    Same precedence the orchestrator's producer phase uses:
    ``override`` (CLI ``--timeout``) > ``config.producer.timeout_seconds``
    > ``config.loop.timeout_seconds``.
    """
    if override is not None:
        return override
    if config.producer.timeout_seconds is not None:
        return config.producer.timeout_seconds
    return config.loop.timeout_seconds


def run_selfcheck(
    config: SyncadeConfig,
    *,
    timeout_seconds: float | None = None,
    adapter: ProducerAdapter | None = None,
    quiet: bool = False,
    always_cleanup: bool = False,
) -> int:
    """Run the producer selfcheck and return a CLI exit code.

    Steps:

    1. Create a tmp_path git repo with one sentinel file
       (``selfcheck.py``) seeded with a single-line comment, and
       commit it.
    2. Render a stub ``findings.md`` via the real
       :func:`persist_findings_md` renderer so the producer sees a
       structurally-correct input. Hand-rolling the markdown would
       risk diverging from the real findings.md schema and the
       selfcheck would stop catching real producer failures if
       findings.md format ever changes.
    3. Provision a producer worktree at the seed commit
       (``strip_files=[]`` — same asymmetry as the real producer
       path).
    4. Call :func:`syncade.producer.run_producer` once with the
       operator's ``[producer]`` config and the stub findings.md.
    5. Verify ``outcome == "committed"``, HEAD moved, and the marker
       text is present in the worktree's copy of ``selfcheck.py``.
    6. Persist the producer's artifacts via
       :func:`persist_producer_result` into ``<workspace>/round-0/``
       (``producer.stdout``, ``producer.stderr``,
       ``producer.commit.txt``, and on subprocess_error
       ``producer.error.txt``). Same file layout as a real round.
    7. Clean up the tmp_path workspace on success; preserve it on
       failure with a printed path so the operator can
       ``cat <workspace>/round-0/producer.stdout`` (or
       ``producer.error.txt`` on the subprocess-error path) to
       inspect the raw producer output. The selfcheck does NOT
       write a manifest / summary.md — it's a one-shot smoke, not
       a review run. ``always_cleanup=True`` removes the workspace
       even on failure (no preserved-path print): ``syncade --doctor``
       reuses this smoke as an inert preflight and must leave nothing
       behind — it points the operator at ``syncade --selfcheck`` (which
       DOES preserve) for the raw output instead.

    Args:
        config: The operator's loaded :class:`SyncadeConfig`. The
            ``[producer]`` block drives provider / model / thinking
            / permissions. ``[loop] timeout_seconds`` is the
            fallback when ``[producer] timeout_seconds`` is unset
            AND the ``timeout_seconds`` arg is ``None``.
        timeout_seconds: Optional ``--timeout`` override. Same
            resolution as the orchestrator: this >
            ``config.producer.timeout_seconds`` >
            ``config.loop.timeout_seconds``.
        adapter: Optional :class:`ProducerAdapter` injection for
            tests. ``None`` (default) routes through the real
            registry per ``config.producer.provider``.
        quiet: Suppress informational stdout. Stderr (failure
            paths) is unaffected. Wired from ``--quiet``.

    Returns:
        ``SUCCESS`` (0) when the producer committed AND the marker
        is present.

        ``FINDINGS_PRESENT`` (30) when the producer committed but
        did not add the marker — producer ran but didn't follow the
        instruction.

        ``WORKTREE_ERROR`` (60) for producer subprocess error,
        producer stall, or worktree provisioning failure — any
        path where the producer couldn't make a verifiable commit.
    """

    def _info(msg: str) -> None:
        if not quiet:
            print(msg)

    def _err(msg: str) -> None:
        print(msg, file=sys.stderr)

    workspace = Path(tempfile.mkdtemp(prefix="syncade-selfcheck-"))
    preserve_on_failure = False
    try:
        # 1. Create the throwaway repo with one seed commit. The
        # init/add/commit + post-subprocess rev-parse calls are
        # wrapped so a CalledProcessError (git binary missing,
        # workspace permissions, corrupt git config, ...) maps to
        # WORKTREE_ERROR with the preserved-workspace contract
        # instead of leaking a Python traceback through the CLI.
        repo_root = workspace / "repo"
        repo_root.mkdir()
        try:
            # init --quiet suppresses the noisy git-2.30+ "hint:"
            # output about the default branch name. Set the branch
            # with symbolic-ref (git 1.7+) rather than
            # --initial-branch=main (git 2.28+) so selfcheck also
            # runs on older platforms; it does not affect the test
            # surface (the seed commit is what matters).
            _git(repo_root, "init", "--quiet")
            _git(repo_root, "symbolic-ref", "HEAD", "refs/heads/main")
            seed = repo_root / SELFCHECK_SEED_BASENAME
            seed.write_text("# selfcheck seed\n", encoding="utf-8")
            _git(repo_root, "add", SELFCHECK_SEED_BASENAME)
            _git(repo_root, "commit", "--quiet", "-m", "selfcheck: seed commit")
            head_result = _git(repo_root, "rev-parse", "HEAD")
            starting_sha = head_result.stdout.strip()
        except (subprocess.CalledProcessError, SubprocessError) as exc:
            preserve_on_failure = True
            if isinstance(exc, subprocess.CalledProcessError) and exc.cmd:
                step = _identify_git_step(list(exc.cmd))
            else:
                step = "<unknown>"
            _err(
                f"[syncade] selfcheck FAILED: git setup error: "
                f"`git {step}` returned rc={getattr(exc, 'returncode', '<launch-error>')}."
            )
            stderr = getattr(exc, "stderr", None)
            if stderr:
                stderr_text = str(stderr).strip()
                if stderr_text:
                    _err(f"[syncade] git stderr: {stderr_text}")
            _err(
                "[syncade] git failed during selfcheck repo setup. "
                "Check git installation and workspace permissions; "
                "the preserved workspace below has the partial repo "
                "state for inspection."
            )
            return WORKTREE_ERROR

        # 2. Stub PR doc the producer template's {pr_doc_path}
        # points at. The producer reads it for context; making it
        # a real file lets a "where's the spec?" producer
        # implementation see something coherent.
        pr_doc = workspace / SELFCHECK_PR_DOC_BASENAME
        pr_doc.write_text(
            (
                "# Selfcheck PR doc\n\n"
                "This is the `syncade --selfcheck` stub PR. The producer is "
                "expected to read `findings.md`, edit "
                f"`{SELFCHECK_SEED_BASENAME}` to add a "
                f"`{SELFCHECK_MARKER}` comment, and commit.\n"
            ),
            encoding="utf-8",
        )

        # 3. Render findings.md via persist_findings_md so the
        # producer sees the real artifact format.
        round_dir = workspace / "round-0"
        round_dir.mkdir()
        synth_result = SynthesizerResult(
            output=_build_stub_synth_output(),
            error=None,
            duration_seconds=0.0,
            raw_subprocess_result=None,
        )
        findings_md_path = persist_findings_md(
            round_dir,
            synth_result,
            datetime.now(UTC),
        )

        # 4. Provision the producer worktree. defer_cleanup=True
        # so the with-exit doesn't try to remove worktrees we'll
        # rmtree along with the workspace anyway; the workspace's
        # finally branch is the single cleanup point.
        worktree_base = workspace / "worktrees"
        producer_manager = WorktreeManager(
            repo_root,
            "selfcheck",
            base_dir=worktree_base,
            defer_cleanup=True,
        )
        resolved_timeout = _resolve_timeout(config, timeout_seconds)

        _info(
            f"[syncade] selfcheck: provisioning producer worktree "
            f"({config.producer.provider}, model={config.producer.model}, "
            f"permissions={config.producer.permissions})"
        )

        try:
            with producer_manager as mgr:
                producer_worktree = mgr.create(
                    reviewer_name="producer",
                    commit_sha=starting_sha,
                    strip_files=[],
                )

                # 5. Run the producer once.
                _info(f"[syncade] selfcheck: dispatching producer (timeout {resolved_timeout:g}s)")
                producer_result = run_producer(
                    worktree_path=producer_worktree.path,
                    starting_sha=starting_sha,
                    pr_doc_path=pr_doc,
                    findings_md_path=findings_md_path,
                    test_run_stdout_path=None,
                    producer_config=config.producer,
                    timeout_seconds=resolved_timeout,
                    round_number=0,
                    max_rounds=1,
                    repo_root=repo_root,
                    adapter=adapter,
                    max_retries=config.retry.max_retries,
                )
                # Persist producer artifacts
                # (producer.stdout / stderr / commit.txt / error.txt)
                # into the round dir on every outcome so the
                # "operator can `cat worktree/producer.stdout`"
                # contract is met. Without this, only the tmp_path workspace's
                # findings.md + repo were
                # preserved; the producer's raw output (the most
                # interesting diagnostic file) was lost.
                persist_producer_result(round_dir, producer_result)

                # 6. Verify.
                if producer_result.outcome == "subprocess_error":
                    preserve_on_failure = True
                    err_cls = type(producer_result.error).__name__
                    _err(
                        f"[syncade] selfcheck FAILED: producer subprocess "
                        f"error ({err_cls}): {producer_result.error}"
                    )
                    _err(
                        f"[syncade] your `{config.producer.provider}` "
                        "producer is in a state where it cannot run "
                        "headlessly. Inspect the preserved worktree below."
                    )
                    return WORKTREE_ERROR
                if producer_result.outcome == "stalled":
                    preserve_on_failure = True
                    _err(
                        "[syncade] selfcheck FAILED: producer stalled "
                        "(no commit made). The producer ran without error "
                        "but did not move HEAD."
                    )
                    _err(
                        f"[syncade] your `{config.producer.provider}` "
                        f"producer with permissions="
                        f"{config.producer.permissions!r} is in a state "
                        "where it cannot commit headlessly. Common "
                        "causes: claude prompts for bash (use "
                        'permissions="yolo") or codex sandbox blocks '
                        '`.git/index.lock` writes (use permissions="yolo").'
                    )
                    return WORKTREE_ERROR

                # outcome == "committed" — but did the producer add
                # the marker the instruction asked for?
                seed_in_worktree = producer_worktree.path / SELFCHECK_SEED_BASENAME
                content = seed_in_worktree.read_text(encoding="utf-8")
                if SELFCHECK_MARKER not in content:
                    preserve_on_failure = True
                    _err(
                        "[syncade] selfcheck FAILED: producer committed "
                        f"(ending_sha={producer_result.ending_sha[:12]}) "
                        f"but `{SELFCHECK_SEED_BASENAME}` does not contain "
                        f"{SELFCHECK_MARKER!r}. Producer ran headlessly "
                        "but did not follow the instruction."
                    )
                    return FINDINGS_PRESENT

                _info(
                    f"[syncade] selfcheck OK: producer committed with "
                    f"marker ({producer_result.ending_sha[:12]}, "
                    f"{producer_result.duration_seconds:.1f}s)"
                )
                return SUCCESS
        except WorktreeError as exc:
            preserve_on_failure = True
            _err(f"[syncade] selfcheck FAILED: worktree error: {exc}")
            return WORKTREE_ERROR
    finally:
        if preserve_on_failure and not always_cleanup:
            _err(f"[syncade] selfcheck workspace preserved at: {workspace}")
        else:
            # Success, or an inert caller (doctor) that must leave nothing behind.
            shutil.rmtree(workspace, ignore_errors=True)
