---
name: syncade
description: 'Run the syncade external blind multi-judge review orchestrator from inside Codex. Use when the operator asks to run syncade on a PR brief, "review this PR for 2 rounds against main", "review path/to/pr.md since last review", references the syncade review loop, or asks to dogfood a PR brief. The skill resolves the spec source: tier A (a brief path) or tier B (an OpenSpec change via --openspec), with --scope selecting the diff base (a modifier, not a spec). If there is no brief at all it degrades gracefully (asks for a brief or --openspec) — drafting a spec from the Codex session is a follow-up. The interactive Codex agent is the operator''s UI; syncade spawns its own reviewer/producer subprocesses with process isolation from this session. It ALSO handles configuration requests — "/syncade config", "change my producer to gpt-5", "set rounds to 2", "cap cost at $5", "show my config" — which it maps to `syncade --config` to inspect or edit settings (models, rounds, timeout, cost cap) and never runs a review for.'
---

# syncade — review orchestrator bridge (Codex)

## What this skill does

`syncade <pr-doc>` runs the syncade review loop on a PR doc. Syncade
dispatches blind reviewers (two Codex prompts by default — cross-prompt
and cross-lab, both available today by config),
synthesizes their structured outputs cold, optionally re-runs tests,
and (when `max_rounds > 1`) hands NO-SHIP findings to a producer
subprocess that attempts to commit a fix. The loop runs until SHIP,
max-rounds reached, or a stop at exit 25 (your budget ceiling, or the
provider's usage limit).

This skill is a Bash orchestration layer: it runs a safety check (the
operator's auth + producer-commit path), confirms with the operator
before firing the expensive subprocess, streams output, and reads the
final `loop-summary.md` to inline a verdict in chat. After the safety
check passes it goes STRAIGHT to the reviewer loop — there is no
brief-check or automatic spec-audit between the safety check and the
reviewers. The reviewer loop answers the question that matters ("did
this get built to spec, and are there bugs?"); a separate
brief-verification pre-flight is redundant, because implementation is
itself the brief-verification pass and syncade reads the implementer's
corrected brief at invocation.

**The interactive Codex agent reading this skill is NOT a reviewer.** Syncade
spawns reviewer subprocesses (`claude -p`, `codex exec`) with no shared
context with this session. The process-isolation invariant that makes
syncade's verdict meaningful is preserved as long as this skill is a
Bash-only wrapper and never tries to substitute the interactive
session for one of syncade's subprocesses.

## When to use

- Operator asks to run syncade on a PR doc (e.g. "run syncade on
  path/to/pr.md", "review this PR").
- Operator asks to dogfood a PR brief that lives under your repo.
- Operator says "review this PR", "run the syncade loop", or
  references `loop-summary.md` / "round N producer commit".
- Operator wants to inspect or change syncade's own settings (models, rounds,
  timeout, cost cap): "/syncade config", "change my producer to gpt-5", "set
  rounds to 2", "show my config". This is a CONFIG intent, not a review — see
  **Configuring syncade** below.

Do NOT use this skill when:

- The operator only wants to read a PR brief — that's just a file read.
- The operator wants to inspect a prior run's artifacts — that's a
  read of `<repo-root>/.syncade/runs/<run-id>/loop-summary.md`.
- The operator wants to debug their auth specifically — they should
  run `syncade --auth-check` from a terminal; this skill calls it as
  step 2 but is not a substitute for the standalone diagnostic.
- The operator only wants a spec audit of the brief — they run
  `syncade --spec-audit <pr-doc>` from a terminal. `--spec-audit` is a
  MANUAL opt-in diagnostic; this skill does not run it automatically.

## Configuring syncade (a separate, non-review intent)

Some requests EDIT or SHOW syncade's own settings rather than running a review:
`/syncade config`, or natural language like "change my producer to gpt-5", "set rounds
to 2", "cap cost at $5", "use gpt-5.5 for the judge", "show my config". These map to the
`syncade --config` CLI and MUST NOT enter the review loop — handle them here and stop.

Division of labour: **browsing belongs in a terminal, changing belongs here.** The real menu is
the curses TUI, which cannot run in this pane (a skill emits text; it has no input loop, and the
harness's own menu chrome is not available to it). Do not fake one with a wide table.

**"Show me / let me browse my config"** — point at the terminal FIRST:

```
For the full menu — arrow keys, Enter to drill into any actor or Advanced section,
`t` to switch global<->repo, `s` to save:

    syncade --config

It needs a real terminal, so it can't run in this pane.
```

Then render a COMPACT summary from `syncade --config list` so the state is visible here anyway:
the common knobs (Producer / Reviewers / Judge models, Rounds, Time per subprocess, Cost cap), each with
its value and the layer that set it (default / global / repo). Use `syncade --config list --all`
when they ask about a field outside that set (it prints every settable field with its value, layer,
an `overrides global <value>` note where the repo masks a different global value, and the dotted
`[key]`). Keep it SCANNABLE — a short list, not a report: no wide tables, no wall of caveats.

**A specific change in natural language** — "change the producer to anthropic sonnet 4.6 at medium
effort", "set rounds to 2", "cap cost at $5". THIS is the thing worth doing in-pane; just do it:

1. Map it to one or more `syncade --config set <key> <value>` commands. Common keys:

   | change | key |
   |---|---|
   | producer / reviewer / judge model | `producer.model` / `reviewers.<i>.model` / `synthesizer.model` |
   | a role's provider (re-derives its model) | `producer.provider` / `reviewers.<i>.provider` / `synthesizer.provider` |
   | thinking / effort | `producer.thinking` / `reviewers.<i>.thinking` / `synthesizer.thinking` |
   | rounds (1–10) | `loop.max_rounds` |
   | time per subprocess, all legs (seconds) | `loop.timeout_seconds` |
   | cost cap (USD) | `loop.budget_usd` |
   | anything else | the `[key]` shown by `syncade --config list --all` |

   Append `--repo` to write the repo's `.syncade/config.toml` instead of the global
   `~/.syncade/config.toml`. If a requested model belongs to a different provider than the role
   uses (e.g. "gpt-5" on an `anthropic` producer), set the provider FIRST
   (`... set producer.provider openai`, which re-derives the model), then the model.
2. **Shadow check:** look at the field's row in `syncade --config list --all`. If it is set by
   `repo` (or carries an `overrides global` note) and you are about to write GLOBAL, the edit
   WON'T take effect for runs in this repo — say so and offer `--repo` before writing.
3. **Confirm:** print the exact `syncade --config set …` command(s), wait for `go`, run them.
4. Re-run `syncade --config list --all` and show the row(s) that changed, so the effect is visible.

`set` REFUSES an invalid value (exit 50, file untouched) and an unknown key (exit 2) — surface
that error verbatim. Ask ONE concise question if a value is ambiguous (e.g. which provider/tier
"opus" means).

## Workflow (Step 0 + seven steps)

**The single pane.** One natural-language request flows to a blind review
without leaving this session. Step 0 resolves which **spec tier** applies, and
both converge on the same loop (Steps 2–7):

- **Tier A — a formal brief** (a PR-doc path). Use it as-is.
- **Tier B — an OpenSpec change** (`--openspec`). syncade assembles it.
- **No spec?** This Codex skill **degrades gracefully** (see Step 0) — drafting
  a spec from the Codex session transcript is a follow-up, not wired
  here.

### Step 0 — Resolve invocation intent (natural language → command)

Before any preflight, translate the operator's request into an exact
structured command. This is interpretation done by *you* (the interactive
Codex agent reading this skill) in markdown — there is NO Python parser; the
skill only maps natural language onto the CLI's flags. Derive four values:

```
PR_DOC          the spec source: EITHER a readable markdown file (the
                  spec/contract) OR an OpenSpec change (see OPENSPEC) — exactly one
MAX_ROUNDS      optional — integer in [1, 10]
BASE_REF        optional — an explicit git ref (mutually exclusive with SCOPE)
SCOPE           optional — one of everything|local|since-last-review
                  (mutually exclusive with BASE_REF)
OPENSPEC        optional — an OpenSpec change-id (or "auto"); mutually exclusive
                  with a PR_DOC path. Sets the spec, NOT the base (PR-C)
RESOLVED_COMMAND  the exact command to run, e.g.
                  syncade [--base <ref> | --scope <token>] [--max-rounds <n>] <pr-doc>
                  syncade --openspec [<change-id>] [--base <ref> | --scope <token>] [--max-rounds <n>]
```

**Supported intent shapes (resolve these without asking):**

```
run syncade on path/to/pr.md
review path/to/pr.md for 2 rounds against main
dogfood a PR for one round
review a PR single pass
review the openspec change add-auth
review path/to/pr.md since last review
```

**PR_DOC resolution order — stop at the first that yields exactly one file:**

1. **Explicit path** in the request (e.g. `path/to/pr.md`, `./x.md`). Prefer
   this always.
2. **PR shorthand** (a bare number): search the repo's PR docs for one
   readable markdown file whose basename matches the number/prefix
   (`ls <your-brief-dir>/*<number>*.md`). Use it only if **exactly one** matches.
3. **Conversation-local "this PR" / "this brief":** use a readable markdown PR
   doc only if **exactly one** has been clearly referenced in this request or
   the immediate conversation. Be conservative.

If zero or more than one candidate results at every step, **ask one concise
question naming the ambiguity, then stop** (do not run auth-check/selfcheck).

**MAX_ROUNDS parsing:** `for 1 round` / `for 2 rounds` / `max rounds 3` →
`--max-rounds N`; `single pass` → `--max-rounds 1`. Valid values are **1–10**
— anything else, ask for a valid count and stop. If no round count is given,
**omit `--max-rounds`** (the operator's `.syncade/config.toml` stays
authoritative).

**BASE_REF parsing:** `against <ref>` / `from <ref>` / `base <ref>` / literal
`--base <ref>`. Validate the ref BEFORE continuing:
`git rev-parse --verify "<ref>^{commit}"` — if it fails, stop and ask for a
valid ref. If no base is given, **omit `--base`** (current CLI behavior stays
authoritative).

**SCOPE parsing (PR-B) — map scope language to `--scope`, do NOT hand-pick a
base:** when the operator names a *scope* instead of an explicit ref, set SCOPE
(Python owns the actual base resolution; the skill only maps the phrase):

- *"review everything"* / *"everything since main"* / *"the whole branch"* →
  `--scope everything` (the branch point off the default branch).
- *"review what I just did"* / *"my recent changes"* / *"my local commits"* →
  `--scope local` (the local-ahead commits vs the branch's upstream).
- *"since last review"* / *"what's new since last time"* / *"new since the last
  run"* → `--scope since-last-review` (the recorded last-reviewed SHA for this
  branch).

`--scope` and `--base` are **mutually exclusive**. If the operator gives BOTH an
explicit ref and scope language (e.g. *"what I did against main"*), the explicit
ref wins — set BASE_REF and omit SCOPE (an explicit ref is unambiguous). Never
emit both flags.

If the resolved `syncade --scope …` later **stops before the loop** (exit 60
with a scope/base message — e.g. no default branch to anchor the branch point
to), surface that message verbatim and ask for an explicit `--base <ref>`. The
ask-when-ambiguous rule still holds: Python decides resolvability, the skill
relays the ask. (Note: `local` with no upstream and `since-last-review` with no
prior record do NOT stop — they fall back to the branch point and syncade prints
a one-line note; that is expected, not an error.)

**OPENSPEC parsing (PR-C) — an OpenSpec change folder as the spec source:** when
the operator points syncade at an OpenSpec change instead of a PR brief, set
OPENSPEC (the Python CLI reads `openspec/changes/<id>/` directly and assembles it
into the spec — the skill only maps the phrase):

- *"review the openspec change `<id>`"* / *"run syncade on my openspec proposal
  `<id>`"* → `--openspec <id>`.
- *"review my openspec change"* / *"use openspec"* with no id named → `--openspec`
  (bare; the CLI auto-resolves IFF exactly one active change exists, else it lists
  them and asks — relay that ask).

`--openspec` is the spec source, so it is **mutually exclusive with a PR_DOC
path** (set one or the other, never both). It is NOT mutually exclusive with
`--base`/`--scope` — those still set the diff base (e.g. "review openspec change
add-auth since last review" → `--openspec add-auth --scope since-last-review`).
If the resolved `syncade --openspec …` stops before the loop (exit 60 — no
`openspec/` folder, unknown/ambiguous change-id), surface the message verbatim
and ask for a change-id or a PR brief path.

**NO SPEC? Degrade gracefully (draft-from-session is a follow-up):** when the
operator has NO brief and asks to review what was just built — *"I didn't write
a spec, review what we did this session"* / *"there's no brief, just check the
work"* — this Codex skill **cannot yet draft a spec from the Codex session
transcript** (that lands in a follow-up). Do NOT guess, summarize, or fabricate a
spec; a blind review against an invented yardstick is worse than no review.
Respond and stop:

```
[syncade] No spec to review against. Supply a spec source:
  • a PR brief path    → "run syncade on your repo<file>.md"
  • an OpenSpec change  → "review the openspec change <id>"   (--openspec)
(--scope / "since last review" / "everything" narrows the diff base, but it is
NOT a spec — it must accompany a brief or --openspec, never replace one.)
Drafting a spec from this Codex session is coming in a follow-up.
```

Do not run auth-check/selfcheck; a review needs a spec first.

**Unsupported flags — stop and ask, never pass through silently:**
The only CLI flags this skill supports are `--base <ref>`, `--scope <token>`,
`--openspec [<change-id>]`, `--max-rounds N`, `--budget-tokens N`, and
`--budget-usd N`. If the operator's request includes any other flag (e.g.
`--timeout`, `--quiet`, `--force-dirty`, `--resume`,
`--force-drift`, `--draft-spec`), stop and respond:

```
[syncade] Unrecognized option: <flag>. Step 0 supports only --base <ref>,
--scope <token>, --openspec [<change-id>], --max-rounds N, --budget-tokens N,
and --budget-usd N. Pass a valid invocation or omit the unsupported flag.
```

Do not guess the intent, do not silently drop the flag, do not forward it.
(`--draft-spec` is intentionally not supported by this Codex skill yet — see the
NO SPEC degrade block above; it arrives in a follow-up.)

**Build RESOLVED_COMMAND** by appending the flags that are present, in the order
`syncade [--openspec [<id>] | <pr-doc>] [--base <ref> | --scope <token>] [--max-rounds <n>] [--budget-tokens <n>] [--budget-usd <n>]` —
at most one spec source (`--openspec` OR a `<pr-doc>` path, never both) and at
most one of `--base`/`--scope`. Quote the path and ref safely; **never** build
the command with `eval`. Every later step uses this exact `RESOLVED_COMMAND`
(validation, confirmation, invocation, summary).

The bare structured form `run syncade on path/to/pr.md` resolves trivially to
`RESOLVED_COMMAND = syncade path/to/pr.md` and follows the unchanged path.

### Step 1 — Validate the resolved PR doc

**If the spec source is `--openspec`, SKIP this file check** — there is no
PR_DOC path; the Python CLI resolves and validates the OpenSpec change folder
itself (and stops with an actionable message if it can't). Proceed to step 2.

Otherwise `PR_DOC` (resolved in Step 0) must be an existing readable markdown
file. Check with `[ -f "$PR_DOC" ] && [ -r "$PR_DOC" ]`. If it doesn't exist or
isn't readable:

```
[syncade] error: <PR_DOC> is not a readable file. Pass a path to a PR brief markdown.
```

Stop. Do not proceed. (Any `BASE_REF` was already validated with
`git rev-parse --verify` in Step 0.) On success → Step 2.

<!-- SYNCADE-SHARED:start — from here to SYNCADE-SHARED:end is byte-identical across
     the Claude (.claude/skills/syncade) and Codex (.codex/skills/syncade) skill copies.
     tests/skills/test_skill_drift.py enforces it. Edit BOTH copies together. -->

### Step 2 — Safety check: auth-check

Run `syncade --auth-check` and capture exit code + stdout + stderr.
This is ~5–10 seconds. Stream both streams to chat as they arrive.

- **Exit 0 → proceed to step 3.**
- **Exit non-zero → report the failing provider and stop.** The
  stderr from `--auth-check` already names which provider failed and
  the remediation step (`run 'claude' interactively to re-authenticate`
  or `run 'codex login' to re-authenticate`). Don't paraphrase; surface
  the syncade output verbatim and then stop.

Auth-check failures gate the run. Do not invoke `syncade <pr-doc>` if
auth is broken — the reviewer subprocesses will all 401, the operator
will pay for ~30s of failed-call latency per reviewer, and the loop
will exit 40 with no useful output.

### Step 3 — Safety check: selfcheck

Run `syncade --selfcheck` and capture exit code + stdout + stderr.
This is ~30 seconds (slower than auth-check because it actually runs
the producer once against a throwaway repo). Stream output to chat.

- **Exit 0 → proceed to step 4.**
- **Exit non-zero → report and stop.** Selfcheck failures mean the
  producer's headless-commit path is broken even though auth works
  (claude sandbox tightened? codex sandbox tightened? CLI version bump
  changed the headless-commit flags?).
  Surface the syncade output verbatim. The selfcheck workspace is
  preserved on failure; the path is the last stderr line.

If `max_rounds == 1` in the operator's config and they object to the
selfcheck cost ("the producer never runs"), explain: the operator can
either set `max_rounds=1` in the override (`syncade --max-rounds 1
<pr-doc>` skips this skill entirely) or accept the ~30s preflight as
the safety check.

The safety check (steps 2–3) is the whole pre-flight. Once it's green,
go straight to the operator-confirmation gate and then the reviewer
loop — no brief-check, no automatic spec-audit in between.

### Step 4 — Confirm with the operator

Auth + selfcheck both green. Print to chat:

```
[syncade] Safety check green:
  --auth-check: OK (<duration>)
  --selfcheck: OK (<duration>)

Ready to run: <RESOLVED_COMMAND>
Expected timing: 15-45 minutes depending on findings + producer rounds.
The producer may commit directly to the current branch.

Reply 'go' to proceed, 'cancel' to abort.
```

Show the EXACT `RESOLVED_COMMAND` from Step 0 (e.g.
`syncade --base main --max-rounds 2 path/to/pr.md`), not a generic
`syncade <pr-doc>` — the operator confirms the precise command that will fire.

Wait for the operator's reply.

- **'go' (or 'y' / 'yes' / 'proceed') → proceed to step 5.**
- **'cancel' (or 'n' / 'no' / 'abort') → stop with `[syncade] cancelled
  at operator confirmation`.**
- **Anything else → treat as cancel.** Don't try to interpret
  ambiguous answers; the cancel surface exists specifically because
  the loop is expensive.

This gate is load-bearing. Without it, invoking `syncade <pr-doc>`
immediately commits to ~15–45 minutes of wall-clock and a
producer that may modify their branch.

### Step 5 — Invoke the resolved command and stream output

Run the exact `RESOLVED_COMMAND` from Step 0 — it already carries any
`--base` / `--max-rounds` the operator asked for. If neither was given it is
the bare `syncade <pr-doc>` and the operator's config drives `max_rounds`,
`timeout`, etc. Do NOT add, drop, or reorder flags here, and never run the
command via `eval`. Stream stdout AND stderr to chat in
real time. Do NOT aggregate; phase-level logging from syncade
(`[syncade] dispatching round 0 reviewers...`,
`[syncade] synthesizer running...`, etc.) is the operator's only
window into a long-running subprocess.

Capture the exit code.

Common exit codes the operator may see:

- `0` — SHIP at some round, or no reviewable changes found (empty
  diff). Check `termination_reason` in `loop-manifest.json`: `ship`
  means reviewed and approved; `no_changes_to_review` means the diff
  was empty before dispatch (no model cost incurred).
- `10` — clarification or operator decision needed; the loop wrote
  `decision-needed.md` at the run root. Read it and branch by the
  heading — see Step 6 for the two shapes and how each continues.
- `20` — max rounds reached without SHIP.
- `25` — stopped gracefully at a phase boundary: either YOUR budget ceiling or the
  PROVIDER's usage limit (the run summary names which). Loop stopped at a phase
  boundary (before a review bundle or a producer). Resume with
  `syncade --resume` to continue on a fresh budget tally.
- `30` — findings present (NO-SHIP), or producer stalled, or tests
  failed when reviewers shipped.
- `40` — reviewer / synthesizer / producer subprocess error.
- `50` — config error.
- `60` — worktree / dirty-tree / loop-mode refusal; or `diff_malformed` (unidentifiable diff headers, fail-closed); or `diff_too_large` (reviewer-facing diff exceeds `[loop] max_diff_bytes`); or `prompt_too_large` (assembled prompt exceeds provider ceiling).
- `70` — reviewer or synthesizer output unparseable.

(Full table in the exit-code contract above.)

### Step 6 — Read `loop-summary.md` and present inline

Locate the run directory. Syncade prints it during the run as
`[syncade] run dir: <path>`; capture the last such line. The
top-level summary is at `<run-dir>/loop-summary.md`.

Read it, then present in chat:

```
[syncade] Run complete (exit <code>).

Verdict: <SHIP / NOTHING TO REVIEW / NO-SHIP / max-rounds-reached / error>
Rounds: <N> of <max>
Termination: <termination_reason from loop-manifest.json>
Final round duration: <s>
Total wall-clock: <s, summed across rounds>

Per-round summary:
  Round 0: <verdict>, <finding_count> active blockers
  Round 1: <verdict>, ...
  ...

Artifacts: <run-dir>
- Top-level findings.md: <run-dir>/findings.md (absent on NOTHING TO REVIEW runs — no reviewers ran)
- Per-round artifacts: <run-dir>/round-N/
```

Use the actual round count and verdicts from `loop-manifest.json` —
don't paraphrase from memory.

**If exit is 10, 20, or 30, also surface the run's operator-facing document** — its "what now", and
skipping it is exactly how an escalation stalls unread. READ the file (don't paraphrase from memory,
don't paste it whole); present the verdict + a plain-language why + a clickable path:

- **Exit 10 (decision needed).** Read `<run-dir>/decision-needed.md`. It has TWO shapes; check
  which headings it contains, because the continuation differs.

  **(a) Producer escalation** — the file has a `## The decision you must make` section. Present
  that paragraph and the resume path:

  ```
  [syncade] This run needs YOUR decision (exit 10) — the loop checkpointed, nothing shipped.

  Decision: <the "decision you must make" paragraph from decision-needed.md>
  Full context + options: <run-dir>/decision-needed.md

  To continue: write your ruling into <run-dir>/decision.txt, then run
    syncade --resume <run-id>
  ```

  **(b) Reviewer blockers all deactivated** — the file has a `## What each reviewer actually
  said` section. Two or more reviewers independently raised blockers and the synthesizer ruled
  every one of them out. There is nothing to resume (no active blocker for a producer to fix)
  and `decision.txt` does not apply — do NOT offer them. Present the reviewers' own words:

  ```
  [syncade] This run needs YOUR judgment (exit 10) — nothing shipped.

  <N> reviewers each raised a blocker; the synthesizer dismissed or downgraded all of them.
  What each reviewer said, and what the synthesizer did with it:
    <run-dir>/decision-needed.md

  If the synthesizer was right, this round is effectively a SHIP. If it was wrong about
  any one of them, that concern is real and still unfixed.
  ```

- **Exit 20 / 30 (NO-SHIP, work remaining).** If `<run-dir>/handoff.md` exists, read it and list each
  active-blocker heading (the `### Blocker N — …` line — one per blocker; each may run a full sentence):

  ```
  [syncade] NO-SHIP (exit <code>) — <N> active blocker(s) remain:

    1. <Blocker 1 title>
    2. <Blocker 2 title>
    ...

  Full handoff (per-blocker file / provenance / disposition): <run-dir>/handoff.md
  ```

  If `handoff.md` is absent, the `findings.md` pointer above is the entry point — never invent a path.

### Step 7 — If producer ran, surface every commit

When `loop-manifest.json` shows any round's `producer.outcome ==
"committed"`, list each commit explicitly:

```
[syncade] Producer commits on this branch:

  Round 1: <sha-short> "<commit-subject>"
  Round 2: <sha-short> "<commit-subject>"
  ...

These commits are on the current branch. To inspect:
  git show <sha>

To roll back ALL producer commits, reset to the round-0 starting SHA:
  git reset --hard <round-0-starting-sha>

To roll back a specific commit, use `git revert <sha>` (creates an
inverting commit; preserves history).
```

The round-0 starting SHA is in `loop-manifest.json` at
`rounds[0].snapshot.commit_sha`. Don't auto-revert anything — the
operator decides what to keep.

If the producer ran but every round's `producer.outcome != "committed"`
(stall or subprocess_error every time), say so explicitly:

```
[syncade] Producer ran but made no commits across <N> rounds
(every outcome: <stalled | subprocess_error>). Inspect
<run-dir>/round-N/producer.stdout and producer.error.txt for the
failure mode.
```

## Failure modes and how to handle them

- **Auth-check or selfcheck fails (steps 2–3):** stop. Don't run the
  main loop. Surface the syncade output verbatim — the remediation
  step is in the error message.
- **Operator declines confirmation (step 4):** stop. The cancel
  surface exists for a reason.
- **`syncade <pr-doc>` exits non-zero:** still execute steps 6 + 7.
  The run dir exists, `loop-summary.md` may exist (depends on which
  phase failed). If it doesn't, point the operator at the highest-
  numbered round dir and its `manifest.json`.
- **The operator's argument is a relative path:** resolve it via
  `realpath` or `readlink -f`. The `--auth-check` / `--selfcheck`
  steps don't need the resolved path, but `syncade <pr-doc>` does so
  it picks up the right config.

## Invariants this skill must not violate

- **No Python.** This is markdown + Bash. The "code" is the workflow
  the interactive agent reads at runtime.
- **No substituting this session for a reviewer.** Reviewers are
  syncade's `claude -p` / `codex exec` subprocesses. The interactive
  agent is the operator's UI; it never feeds findings into the
  reviewer or synthesizer phase.
- **No auto-revert of producer commits.** Step 7 surfaces them; the
  operator decides.
- **No skipping the confirmation gate in step 4.** Even after the
  safety check passes, the expensive subprocess fires only on explicit
  operator consent.
- **No aggregating syncade's streaming output.** The operator needs
  phase-level visibility for a ~15-45 minute run. Real-time streaming
  is the contract.

<!-- SYNCADE-SHARED:end -->

## Pointers

- `AGENTS.md` — syncade operator contract for Codex (repo root; what Codex reads).
- `CLAUDE.md` — current syncade architecture (authoritative, developer-facing).
- `path/to/pr.md` — the brief that landed this Codex skill.
- `.codex/skills/syncade/README.md` — operator-facing description (when to use,
  prerequisites, failure-mode references).

<!-- This Codex skill shares its Step 2–Invariants span byte-for-byte with the
     Claude copy at .claude/skills/syncade/SKILL.md (see the SYNCADE-SHARED markers).
     tests/skills/test_skill_drift.py enforces it — edit BOTH copies together. -->
