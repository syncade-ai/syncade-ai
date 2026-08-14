"""Pre-dispatch classifier for the review loop.

Split out of :mod:`syncade.orchestrator.loop` to keep that module under the LOC cap;
re-exported from there so existing imports remain stable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from syncade.config import SyncadeConfig
from syncade.diff_filter import (
    concealed_destinations,
    elide_binary_hunks,
    filter_diff_for_reviewer,
    unidentifiable_sections,
)

if TYPE_CHECKING:
    from syncade.snapshot import Snapshot


def _diff_will_dispatch(
    snapshot: Snapshot,
    config: SyncadeConfig,
    repo_root: Path | None = None,
    pr_doc_path: Path | None = None,
) -> bool:
    """Return False when the round will terminate before dispatching any subprocess.

    A False result means no reviewer, judge, or producer will run, so commit-safety
    guards (dirty-tree refusal, default-branch guard) are irrelevant for this run.
    Four cases: diff is malformed (diff_malformed, exit 60), reviewer-facing diff
    exceeds the size ceiling (diff_too_large, exit 60), diff is known-empty
    (no_changes_to_review, exit 0), or an assembled reviewer prompt exceeds the provider
    character ceiling (prompt_too_large, exit 60). All are pre-dispatch terminal decisions
    in _run_one_round; classifying here avoids the dirty/default-branch guards firing
    before the prompt-size refusal for runs that will never dispatch any reviewer.

    The prompt_too_large check renders the full round-0 prompt and requires repo_root to load
    the reviewer templates. When repo_root is None the check is skipped and the exact check
    inside _run_one_round catches it instead (though only after commit-safety guards have
    already run). pr_doc_path is the resolved PR doc path; when provided it is converted to
    the worktree-local reference the real reviewer sees (relative path inside the repo, or a
    collision-free .syncade-inputs/ path for out-of-repo docs). When omitted the placeholder
    "<pr-doc>" is used, which can undercount prompt size if the template repeats {pr_doc_path}.
    """
    # Only refuse on unidentifiable headers when there are strip targets: with an empty
    # strip list filter_diff_for_reviewer keeps every section byte-for-byte, so an
    # undecodable header is not a security concern (nothing is being hidden).
    if config.review.strip_repo_context_files and unidentifiable_sections(snapshot.diff_text):
        return False  # diff_malformed path — refuses at exit 60, no producer runs
    filtered = filter_diff_for_reviewer(snapshot.diff_text, config.review.strip_repo_context_files)
    filtered_elided, _ = elide_binary_hunks(filtered)
    if len(filtered_elided.encode("utf-8")) > config.loop.max_diff_bytes:
        return False  # diff_too_large path — refuses at exit 60, no producer runs
    if (
        snapshot.base_oid is not None
        and not filtered_elided
        # A boundary rename empties the filtered diff while concealing a reviewable
        # destination; that run DOES dispatch, so the commit guards still apply.
        and not concealed_destinations(snapshot.diff_text, config.review.strip_repo_context_files)
    ):
        return False  # no_changes_to_review path — exits 0, no producer runs
    # Exact assembled-prompt check: render the full round-0 prompt and measure it directly.
    # The former template+diff estimate omitted the JSON schema (~517 chars), adversarial-lens
    # block (~3,316 chars), and prior-round sentinel — a lower bound that let the real rendered
    # prompt exceed the provider ceiling (1,048,576 chars) while this predicate returned True,
    # causing commit-safety guards to fire first (dirty-tree or default-branch refusal) instead
    # of the intended prompt_too_large refusal. Fails open on template-load or render errors —
    # the exact check in _run_one_round catches them (though only after commit-safety guards run).
    if repo_root is not None:
        from syncade.findings import get_findings_schema_string
        from syncade.orchestrator.round import _NO_DIFF_SENTINEL
        from syncade.orchestrator.round_no_changes import _CODEX_CHAR_CEILING
        from syncade.prompts import load_reviewer_template_for, render_reviewer_prompt

        # Compute the worktree-local PR doc reference the same way _build_reviewer_prompt
        # does. Using the real path matters when the template repeats {pr_doc_path} — with
        # the placeholder "<pr-doc>" (8 chars) the rendered size is always lower than the
        # real prompt, so the guard can false-green for a prompt that later refuses.
        if pr_doc_path is not None:
            try:
                _pr_doc_ref = str(pr_doc_path.relative_to(repo_root))
            except ValueError:
                _digest = hashlib.sha256(str(pr_doc_path).encode("utf-8")).hexdigest()[:16]
                _pr_doc_ref = f".syncade-inputs/pr-doc-{_digest}-{pr_doc_path.name}"
        else:
            _pr_doc_ref = "<pr-doc>"
        _diff_text = filtered_elided if filtered_elided else _NO_DIFF_SENTINEL
        _json_schema = get_findings_schema_string()
        for reviewer in config.reviewers:
            try:
                template = load_reviewer_template_for(
                    repo_root, provider=reviewer.provider, template=reviewer.template
                )
                rendered = render_reviewer_prompt(
                    template,
                    pr_doc_path=_pr_doc_ref,
                    diff=_diff_text,
                    master_plan_path=None,
                    json_schema=_json_schema,
                    adversarial_lens=reviewer.adversarial_lens,
                    bug_class_sweep=reviewer.bug_class_sweep,
                )
            except Exception:  # noqa: BLE001 — fail open; exact check runs inside round
                continue
            if len(rendered) > _CODEX_CHAR_CEILING:
                return False  # prompt_too_large path — refuses at exit 60, no producer runs
    return True
