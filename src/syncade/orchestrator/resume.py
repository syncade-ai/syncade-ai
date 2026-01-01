"""Resume detection + eligibility helpers.

Pure-function layer: takes a ``runs_root`` (or a specific ``run_dir``)
and returns resume plans without invoking any syncade subprocess.
The CLI handler (``_run_resume`` in :mod:`syncade.cli`) wires these
into ``run_review`` after resolving the target run-id.

Eligibility rule:

- ``run-init.json`` present AND ``loop-manifest.json`` MISSING →
  ELIGIBLE (operator Ctrl-C, crash, SIGTERM — the loop terminator
  never ran).
- ``run-init.json`` present AND ``loop-manifest.json`` malformed/unreadable →
  ELIGIBLE enough to surface during resume-target resolution; the planner
  reports the specific corruption.
- ``run-init.json`` present AND ``loop-manifest.json`` present AND
  ``final_exit_code in {10, 25, 40, 60, 70}`` → ELIGIBLE (decision-needed
  checkpoint, a budget stop (25), or environment failure mid-loop —
  subprocess error, worktree provisioning, parse failure).
- ``loop-manifest.json`` present AND ``final_exit_code in (0, 20, 30)``
  → NOT ELIGIBLE (loop completed normally; operator's next step is
  a fresh run).
- ``run-init.json`` MISSING → NOT ELIGIBLE (not a syncade run, or missing the
  required resume artifact).

Within an eligible run, the "round to retry" is determined by
walking ``round-0/``, ``round-1/``, ... up to ``max_rounds`` and
stopping at the FIRST round that didn't complete cleanly.

this module was decomposed into ``resume_types`` (shared
constants + the ``ResumeError`` / ``ResumePlan`` / ``TreeDriftError``
types), ``resume_target`` (find/resolve eligible runs), ``resume_plan``
(``plan_resume`` + the phase-failure classifier + decision read + SHA
derivation), and ``resume_load`` (tree-drift checks + completed-round
rehydration). This module is now a thin re-export shim so every
``syncade.orchestrator.resume.<name>`` import path is unchanged.
"""

from __future__ import annotations

from syncade.persistence import read_operator_decision as read_operator_decision

from .resume_load import (
    _current_head_branch,
    _current_head_sha,
    _safe_read,
    check_tree_drift,
    load_completed_round,
)
from .resume_plan import (
    _expected_sha_for_resumed_round,
    _round_manifest_indicates_phase_failure,
    plan_resume,
    read_resume_decision,
)
from .resume_target import (
    _read_operator_branch,
    find_resumable_runs,
    resolve_resume_target,
)
from .resume_types import (
    _GIT_TIMEOUT_SECONDS,
    _RESUMABLE_EXIT_CODES,
    LOOP_MANIFEST_FILENAME,
    ROUND_MANIFEST_FILENAME,
    ResumeError,
    ResumePlan,
    TreeDriftError,
)

__all__ = [
    # types + constants
    "ResumeError",
    "ResumePlan",
    "TreeDriftError",
    "LOOP_MANIFEST_FILENAME",
    "ROUND_MANIFEST_FILENAME",
    "_RESUMABLE_EXIT_CODES",
    "_GIT_TIMEOUT_SECONDS",
    # target resolution
    "find_resumable_runs",
    "resolve_resume_target",
    "_read_operator_branch",
    # planning
    "plan_resume",
    "read_operator_decision",
    "read_resume_decision",
    "_round_manifest_indicates_phase_failure",
    "_expected_sha_for_resumed_round",
    # drift + rehydration
    "check_tree_drift",
    "load_completed_round",
    "_current_head_sha",
    "_current_head_branch",
    "_safe_read",
]
