# Fixture: first real-world syncade run (timeout failure)

These are the verbatim artifacts from the first real-world `syncade`
run against a Acme PR, on 2026-05-13. Both reviewers were SIGKILL'd
at the old hardcoded 600-second dispatcher timeout floor; the run
exited 40 (`REVIEWER_FAILURE`) with no useful output.

That run surfaced the three bugs PR-5.5 fixes:

1. **`.syncade/` landed in a subdirectory.** The user invoked syncade
   from `acme/docs/feature-work/`, so artifacts went there
   instead of the repo root. The `--add-dir` path in the `.error.txt`
   tracebacks (`/tmp/syncade/2026-05-13T11-46-55/...`) and the run
   directory this fixture came from both show the cwd-rooted layout.
2. **The 600s timeout was too aggressive.** `manifest.json` records
   `duration_seconds` ≈ 600 for both reviewers and
   `error_type: "SubprocessTimeoutError"`.
3. **Partial output was lost.** `claude-reviewer.stdout` / `.stderr`
   (and the codex pair) are 0 bytes: the dispatcher recorded
   `raw_subprocess_result=None` on timeout, so whatever the reviewer
   produced before the SIGKILL never reached disk.

`tests/test_regression_acme.py` loads these artifacts and pins the
on-disk layout — the `manifest.json` schema and the per-reviewer file
naming — so a future PR can't silently drift it. The empty `.stdout` /
`.stderr` files *are* the bug PR-5.5 task 3 fixed; the **set** of files
is unchanged, and that set is what this fixture guards.
