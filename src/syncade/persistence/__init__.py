"""On-disk persistence of dispatch + synthesizer results.

Materializes a :class:`~syncade.dispatcher.DispatchResult` and
:class:`~syncade.synthesizer.SynthesizerResult` into the
``.syncade/runs/<run-id>/round-N/`` layout the PRD specifies. The
orchestrator is the only production caller; tests construct
synthesized inputs directly so they can exercise persistence without
spawning real subprocesses.

File layout per reviewer (one set per ``ReviewerRunResult``):

- ``<round_dir>/<name>.stdout``       — raw subprocess stdout (always
                                        written; empty when no
                                        subprocess ran)
- ``<round_dir>/<name>.stderr``       — raw subprocess stderr (same)
- ``<round_dir>/<name>.parsed.json``  — :class:`~syncade.findings.ReviewerOutput` as JSON
                                        (success path only)
- ``<round_dir>/<name>.error.txt``    — exception class + message
                                        + traceback (failure path
                                        only)

This adds synthesizer artifacts:

- ``<round_dir>/synthesizer.stdout``       — raw codex stdout
- ``<round_dir>/synthesizer.stderr``       — raw codex stderr
- ``<round_dir>/synthesizer.parsed.json``  — :class:`~syncade.synthesis.SynthesizerOutput`
                                             as JSON (success path)
- ``<round_dir>/synthesizer.error.txt``    — exception + traceback
                                             (failure path)
- ``<round_dir>/findings.md``              — operator-facing
                                             consolidated review
                                             report (only when synth
                                             succeeded)

This adds test re-run artifacts (opt-in; written only when the
operator configured ``[loop] test_command`` AND every prior phase
succeeded AND the synthesizer was clean):

- ``<round_dir>/test-run.stdout``          — captured test command
                                             stdout
- ``<round_dir>/test-run.stderr``          — captured test command
                                             stderr
- ``<round_dir>/test-run.exit-code.txt``   — one-line integer (or
                                             ``-1\\n`` for the
                                             subprocess-error path)
                                             so manifest-readers
                                             can grab it without
                                             parsing JSON

Plus one file per round:

- ``<round_dir>/manifest.json``       — round-level summary written
                                        by :func:`persist_round_manifest`
- ``<round_dir>/summary.md``          — human-readable dashboard
                                        written by
                                        :func:`persist_run_summary`
"""

from __future__ import annotations

from ._markdown import (
    _md_command_lines as _md_command_lines,
)
from ._markdown import (
    _md_inline_code as _md_inline_code,
)
from .checks import (
    CheckArtifactPaths as CheckArtifactPaths,
)
from .checks import (
    persist_check_result as persist_check_result,
)
from .decision_needed import (
    DECISION_NEEDED_FILENAME as DECISION_NEEDED_FILENAME,
)
from .decision_needed import (
    OPERATOR_DECISION_FILENAME as OPERATOR_DECISION_FILENAME,
)
from .decision_needed import (
    persist_deactivated_blockers_decision_needed as persist_deactivated_blockers_decision_needed,
)
from .decision_needed import (
    persist_decision_needed as persist_decision_needed,
)
from .decision_needed import (
    read_operator_decision as read_operator_decision,
)
from .findings_md import (
    persist_current_findings_md as persist_current_findings_md,
)
from .findings_md import (
    persist_findings_md as persist_findings_md,
)
from .handoff import (
    persist_handoff as persist_handoff,
)
from .last_reviewed import (
    LAST_REVIEWED_FILENAME as LAST_REVIEWED_FILENAME,
)
from .last_reviewed import (
    persist_last_reviewed as persist_last_reviewed,
)
from .last_reviewed import (
    read_last_reviewed as read_last_reviewed,
)
from .loop_manifest import (
    persist_loop_manifest as persist_loop_manifest,
)
from .loop_summary import (
    persist_loop_summary as persist_loop_summary,
)
from .producer import (
    ProducerArtifactPaths as ProducerArtifactPaths,
)
from .producer import (
    persist_producer_result as persist_producer_result,
)
from .reviewer import (
    persist_dispatch_record as persist_dispatch_record,
)
from .reviewer import (
    persist_reviewer_result as persist_reviewer_result,
)
from .round_manifest import (
    persist_round_manifest as persist_round_manifest,
)
from .run_init import (
    RUN_INIT_FILENAME as RUN_INIT_FILENAME,
)
from .run_init import (
    persist_run_init as persist_run_init,
)
from .run_summary import (
    persist_run_summary as persist_run_summary,
)
from .synth import (
    SynthesizerArtifactPaths as SynthesizerArtifactPaths,
)
from .synth import (
    persist_synthesizer_result as persist_synthesizer_result,
)
from .test_run import (
    TEST_RUN_NAME as TEST_RUN_NAME,
)
from .test_run import (
    TestRunArtifactPaths as TestRunArtifactPaths,
)
from .test_run import (
    persist_test_run_result as persist_test_run_result,
)

# Public re-export surface. The 2 underscore-prefixed helpers
# (_md_inline_code, _md_command_lines) are intentionally NOT in
# __all__ per Python convention, but they remain importable from
# syncade.persistence for tests that reach into them directly.
__all__ = [
    "CheckArtifactPaths",
    "DECISION_NEEDED_FILENAME",
    "LAST_REVIEWED_FILENAME",
    "OPERATOR_DECISION_FILENAME",
    "persist_last_reviewed",
    "read_last_reviewed",
    "ProducerArtifactPaths",
    "RUN_INIT_FILENAME",
    "SynthesizerArtifactPaths",
    "TEST_RUN_NAME",
    "TestRunArtifactPaths",
    "persist_check_result",
    "persist_current_findings_md",
    "persist_deactivated_blockers_decision_needed",
    "persist_decision_needed",
    "persist_findings_md",
    "read_operator_decision",
    "persist_handoff",
    "persist_loop_manifest",
    "persist_loop_summary",
    "persist_producer_result",
    "persist_dispatch_record",
    "persist_reviewer_result",
    "persist_round_manifest",
    "persist_run_init",
    "persist_run_summary",
    "persist_synthesizer_result",
    "persist_test_run_result",
]
