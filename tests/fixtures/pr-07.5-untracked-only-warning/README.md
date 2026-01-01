# pr-07.5-untracked-only-warning fixture

Verbatim `git status --porcelain` output from the Phase-04 Acme run
on 2026-05-25T14-09-33. The operator had two untracked
`cowork/alpha-briefings/*.md` files (scratch notes intentionally kept
out of git). Before the four-state classifier, the snapshot layer used a
boolean dirty signal and the orchestrator emitted the strong "uncommitted
changes — reviewers will only see HEAD; uncommitted work is invisible" warning,
which the operator (correctly) read as "something is wrong" and burned a few
minutes investigating before realizing the warning was firing against files
they'd kept out of git on purpose.

PR-7.5's refined classification distinguishes tracked-modified (strong
warning) from untracked-only (soft note). This fixture pins the
untracked-only case so the regression doesn't return.

The pinning test is
`tests/snapshot/test_snapshot_dirty.py::TestClassifyPorcelainSynthesizedInput::test_phase_04_acme_regression_fixture`.
