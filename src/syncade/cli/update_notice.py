"""Tell the operator an update exists — and nothing more. PR-h-field-07 item 4.

**The invariant this module exists to hold: it can only PRINT.** No exit code, no gate, no
refusal, at any severity. A `critical_below` that could stop a run would be a remote kill switch,
and a cheap one — one line in a JSON file, needing no action from the operator to take effect.
Print-only means the worst a compromised manifest can do is lie, which is visible and survivable.
Whether to act on a warning is the operator's call; every failure mode here ends in silence.

Two independent signals are surfaced together because they have the same remedy shape and the
same audience: the PACKAGE may be out of date, and the installed SKILL may be out of date. They
drift apart routinely — upgrading the package leaves the copied skill markdown untouched.

Suppression follows the auth block's precedent rather than :meth:`Logger.safety`, whose class is
deliberately narrow ("where did the producer's commits go?"). A version notice is not that class,
so it prints to stderr directly: routine notices honour ``--quiet``, critical ones do not.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from syncade import __version__
from syncade.cli.skill_status import stale_skills
from syncade.update_check import UpdateNotice, check_for_update, session_checked

_PREFIX = "[syncade]"
#: The command the notice tells an operator to run. It must EXIST — item 4 shipped naming
#: `--update` before item 5 defined it, and `test_documented_flags_exist` caught the forward
#: reference; `test_the_notice_never_names_a_flag_the_parser_would_reject` now holds the same
#: contract over the rendered text.
_UPDATE_CMD = "`syncade --update`"


def _update_lines(notice: UpdateNotice) -> list[str]:
    if not notice.critical:
        return [
            f"{_PREFIX} update available: {notice.latest} (you have {__version__})"
            f" — run {_UPDATE_CMD}"
        ]

    if notice.latest:
        head = f"{_PREFIX} CRITICAL UPDATE: {notice.latest} (you have {__version__})"
        tail = f"Strongly recommended: run {_UPDATE_CMD}. This run continues either way."
    else:
        # A version can be marked critical BEFORE the release that fixes it — the main reason the
        # manifest is ours rather than PyPI's. Pointing at an upgrade that does not exist would
        # be visibly broken advice, so the wording changes rather than the version being faked.
        head = f"{_PREFIX} CRITICAL: this version ({__version__}) has a known problem"
        tail = "No fix is published yet. This run continues."

    lines = [head]
    if notice.reason:
        lines.append(f"    {notice.reason}")
    if notice.url:
        lines.append(f"    {notice.url}")
    lines.append(f"    {tail}")
    return lines


def _skill_lines(harness: str, status: str) -> list[str]:
    flag = f"`syncade --install-skill {harness}`"
    if status == "stale":
        return [f"{_PREFIX} your installed {harness} skill is out of date — run {flag}"]
    # "unknown": files differ and no record proves who wrote them. Claim nothing; the installer
    # itself refuses rather than destroying, so pointing at it is safe advice even if the files
    # turn out to be theirs.
    return [
        f"{_PREFIX} your installed {harness} skill differs from this version's and has no",
        f"    install record, so it cannot be told apart from your own edits. {flag}",
        "    will sync it, and refuses rather than overwriting anything it did not write.",
    ]


def render(notice: UpdateNotice | None, skills: list) -> tuple[list[str], list[str]]:
    """Split the output into ``(always, suppressible)``.

    Two channels rather than one flag, because criticality belongs to the PACKAGE advisory and
    must not spread. A single flag let skill-drift lines ride the unsuppressible channel whenever
    a critical package notice happened to fire alongside them, which would make ``--quiet`` mean
    different things on different days depending on what the manifest said that morning.
    """
    always: list[str] = []
    suppressible: list[str] = []
    if notice is not None:
        (always if notice.critical else suppressible).extend(_update_lines(notice))
    for skill in skills:
        suppressible.extend(_skill_lines(skill.harness, skill.status))
    return always, suppressible


def _resolve_repo_root(hint: Path) -> Path | None:
    """The repo root ``dispatch`` would use, or ``None`` when there is not one.

    Delegates to :func:`syncade.snapshot.discover_repo_root` rather than approximating it. An
    earlier version walked parents looking for any ``.git`` entry, which accepts what
    ``discover_repo_root`` rejects — a malformed ``.git`` file, an empty ``.git`` directory — so a
    stray repo-local ``[update] check = false`` under a broken tree could suppress the check in a
    directory syncade does not consider a repository at all. Two answers to "which repo am I in?"
    is the defect; there is now one.
    """
    try:
        from syncade.snapshot import discover_repo_root

        return discover_repo_root(hint)
    except Exception:  # noqa: BLE001 — not a repo, or git unavailable: no repo layer to read.
        return None


def _check_enabled(repo_root: Path | None = None) -> bool:
    """Read ``[update] check`` from the effective merged config (global + repo layers).

    Honoring the repo layer lets a repo-level ``[update] check = false`` take effect, matching
    what the config surface accepts and what ``--config list`` reports. The hint is resolved to
    the actual git root first — a subdirectory hint would otherwise miss the root's config, and a
    non-git directory's stray ``.syncade/config.toml`` would incorrectly suppress the notice. The
    repo layer is simply absent (``include_repo=False``) when not inside a git tree.

    Any failure means enabled: a broken config file must not silently disable a security notice.
    """
    try:
        from syncade.config_loader import load_config

        hint = (repo_root or Path.cwd()).resolve()
        resolved = _resolve_repo_root(hint)
        if resolved is not None:
            return bool(load_config(resolved, check_api_keys=False).update.check)
        return bool(load_config(hint, check_api_keys=False, include_repo=False).update.check)
    except Exception:  # noqa: BLE001
        return True


def emit_update_notice(*, quiet: bool = False, repo_root: Path | None = None, stream=None) -> bool:
    """Print the once-per-session notice, if there is one. Never raises, never blocks.

    Returns ``True`` when the operator chose to update immediately (terminal TTY only).
    The caller should then dispatch to ``--update`` mode rather than continuing with the
    original command. Returns ``False`` in every other case (no notice, non-TTY, CI, quiet,
    or the operator declined).

    Called once at CLI entry, before any mode dispatch, so the operator learns of an update
    before spending anything — and never again in that window, because the session gate lives in
    :func:`syncade.update_check.check_for_update`.
    """
    try:
        # Short-circuit rather than relying on `check_for_update`'s own `enabled` guard: skill
        # drift is local and free, but it rides the SAME switch — one setting turns off update
        # nagging, not "the network half of it" — and one early return makes that total instead
        # of distributed across two call sites that could later disagree.
        # CI is the same gate: `CI` was honored only inside `check_for_update`, so a CI job
        # with a stale skill still printed the skill notice and re-printed it on every invocation
        # because the network half returned before marking the session.
        if not _check_enabled(repo_root) or os.environ.get("CI"):
            return False
        # Read BEFORE `check_for_update` marks the session, so both halves of the notice are
        # governed by one gate. The skill scan is local and free, which is exactly why it needs
        # this: left ungated it re-printed on every invocation — measured, three commands in one
        # window, three identical warnings — and "once per window" has to mean the whole notice.
        first_in_session = not session_checked()
        notice = check_for_update(__version__)
        # Re-read after the mark attempt: if the session write failed (e.g. unwritable home),
        # session_checked() is still False and the next call also sees first_in_session=True.
        # Gating on both flags prevents skill notices from re-printing on every command.
        session_confirmed = session_checked()
        show_skills = first_in_session and session_confirmed
        always, suppressible = render(notice, stale_skills() if show_skills else [])
        lines = always if quiet else always + suppressible
        if not lines:
            return False
        out = stream or sys.stderr
        print("\n".join(lines), file=out)
        # TTY path: offer the update choice so the operator can upgrade now rather than
        # manually re-running. Non-TTY (pipe, harness, CI): print and proceed — a blocked
        # stdin read would hang a harness indefinitely. --quiet implies scripted or
        # non-interactive use, so no prompt there either.
        # `notice.latest` gates the offer, not merely `notice`. A critical notice with
        # latest=None prints 'No fix is published yet' — following that with 'Update
        # now?' would send the operator at an upgrade the manifest just said does not
        # exist.
        if notice is not None and notice.latest and not quiet and sys.stdin.isatty():
            try:
                sys.stdout.write(f"{_PREFIX} Update now? [y/N] ")
                sys.stdout.flush()
                answer = sys.stdin.readline()
            except (EOFError, OSError):
                answer = ""
            return answer.strip().lower() == "y"
        return False
    except Exception:  # noqa: BLE001
        return False  # A version notice must never be able to fail a run.
