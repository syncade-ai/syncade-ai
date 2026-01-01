# PR-5.6 parser regression fixture

This directory holds the failing-run artifact that motivated PR-5.6's
parser rewrite. Treat it as load-bearing test data — the regression
test in `tests/findings/test_parser_resilience.py::TestParserResilience::test_acme_regression_jsx_prose_with_real_verdict_at_end`
loads it.

## Contents

- `claude-reviewer-prose-with-jsx.stdout` — derived from
  `acme/.syncade/runs/2026-05-15T08-44-26/round-0/claude-reviewer.stdout`
  in the Acme repository. The full claude `-p` JSON envelope; the
  parser receives `envelope["result"]` (the unwrapped narrative + verdict
  text) after the AnthropicAdapter unwraps it.

  PR-6 backfilled the four narrative-surface fields (`summary`,
  `priority_order`, `coverage_gaps`, `dismissed_concerns`) into the
  embedded verdict JSON so the post-PR-6 schema can validate it. The
  JSX trap (`{{ color: 'var(--mm-amber)' }}`) and the verdict's
  position at the end of the document are preserved unchanged — that's
  what the regression test pins. The narrative content of the new
  fields is synthetic but plausible (matches what the Acme reviewer
  actually wrote in narrative).

## Provenance

- **Run timestamp:** 2026-05-15T08:44:26 (UTC: 13:44:26)
- **Repository:** `acme` (private), reviewing
  `phase-01-visual-scaffold.md` (frontend-only money-movement widget
  scaffold).
- **Snapshot SHA:** `192b32b9246743ef21b6282d1af46dc7e0afdac7`
- **Reviewer:** `claude-reviewer` via the Anthropic adapter,
  `claude -p`, ran for 5m22s, cost $2.18.
- **Outcome:** ReviewerOutputError; the run exited 70
  (`REVIEWER_OUTPUT_UNPARSEABLE`). The user saw `FAILED
  (ReviewerOutputError)` and a missing `parsed.json`, reasonably
  concluded "claude didn't fire," and lost the actual NO-SHIP verdict
  with 4 blocker findings that was sitting in the `.stdout` file.

## The bug it exposes

claude's narrative explanation (after running `git diff` and inspecting
the component) included this line:

> `style={{ color: 'var(--mm-amber)' }}` — this correctly uses the
> scoped variable.

The pre-PR-5.6 parser (`_extract_first_json_block`) walked the input
from the first `{` and balanced braces. Because `{{ color: ... }}` IS
balanced (depth goes 0 → 1 → 2 → 1 → 0), the parser returned that
entire JSX fragment as its first candidate, called `json.loads` on it,
and exploded:

```
ReviewerOutputError: extracted JSON block failed to parse:
Expecting property name enclosed in double quotes: line 1 column 2
(char 1); block: "{{ color: 'var(--mm-amber)' }}"
```

The actual verdict JSON — `{"verdict":"NO-SHIP","findings":[...]}` —
was at the END of the response and never got considered. Eight
substantive findings (4 blockers including "this commit bundles
7,378 unrelated files" and "CLAUDE.md was deleted") were silently
discarded.

## What the regression test verifies

`test_acme_regression_jsx_prose_with_real_verdict_at_end`:

1. Sanity-checks the fixture: the JSX trap and the real verdict are
   both present, and the JSX appears BEFORE the verdict in document
   order. (If this drifts, the fixture has lost the bug it pins.)
2. Runs `parse_reviewer_output(envelope["result"])` end-to-end.
3. Asserts `out.verdict == "NO-SHIP"` and `len(out.findings) == 8`,
   with 4 of severity blocker.

If the parser regresses to the old first-`{`-only behavior, this test
will fail with the same `json.loads` exception against the
`{{ color: 'var(--mm-amber)' }}` snippet.

## Schema relaxation note

Two of the eight findings have `file: null` (commit-message-level and
file-tree-hygiene concerns that aren't tied to a specific file).
PR-5.6 relaxed `Finding.file` from `str` to `str | None` to accept
the actual data — without that, the parser would correctly extract
the verdict JSON but pydantic would reject 2/8 findings and the
overall result would still fail. Real reviewers do emit repo-wide
findings; the schema accommodates them.
