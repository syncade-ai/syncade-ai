# syncade Claude Code skill

Bridge between Claude Code's slash-command interface and the syncade
external review orchestrator. Operators type `/syncade <pr-doc>` from
inside Claude Code; this skill orchestrates the safety check
+ confirmation gate + the actual review loop, then summarizes inline.

## Quick start

```
/syncade path/to/pr.md
/syncade review path/to/pr.md for 2 rounds against main
/syncade dogfood a PR single pass
```

The skill also handles **configuration** without running a review — "/syncade config",
"change my producer to gpt-5", "set rounds to 2", "show my config" — rendering a config
**menu** (`syncade --config list --all`) you browse and edit via
`syncade --config set <key> <value>` (writes the global `~/.syncade/config.toml`, or the
repo's with `--repo`). See "Configuring syncade" in the SKILL.

Both the structured form and clear natural language work: the skill
maps your request onto the CLI shape `syncade [--openspec [ID] | PR_DOC]
[--base REF | --scope SCOPE] [--max-rounds N]` — at the skill (markdown) layer
only; the skill adds no Python of its own (PR-B added `--scope`, PR-C added
`--openspec`, the flags it targets). The interactive Claude will:

0. **Resolve intent** — turn natural language into an exact command. It asks
   one short question if the target / round count / base is ambiguous, and
   **maps scope phrases** ("review what I just did" → `--scope local`,
   "everything" → `--scope everything`, "since last review" →
   `--scope since-last-review`) onto the `--scope` flag (PR-B); Python derives
   the concrete base, the skill never hand-picks one.
1. Validate the resolved PR doc is readable (and any explicit base ref).
2. Run `syncade --auth-check` (~5–10 s safety check).
3. Run `syncade --selfcheck` (~30 s safety check).
4. Print the safety-check result + the EXACT resolved command + expected
   timing and ask you to confirm with `go` / `cancel`. After the safety check
   the skill goes straight to the reviewer loop — no brief-check, no automatic
   spec-audit in between.
5. On `go`, run the resolved command and stream output to chat.
6. Read `<run-dir>/loop-summary.md` and print the verdict + round
   count + finding count.
7. If the producer committed, list each commit with its short SHA +
   subject and a `git reset --hard <round-0-sha>` rollback pointer.

### Natural-language phrasings the skill understands

- **Round count:** `for 1 round`, `for 2 rounds`, `max rounds 3`, `single pass`
  (= 1 round). Valid: 1–10. Omit it and your config's `max_rounds` stays
  authoritative.
- **Base ref:** `against main`, `from origin/main`, `base HEAD~3`, or literal
  `--base <ref>` (validated with `git rev-parse` before running). Omit it and
  current CLI behavior stays authoritative.
- **Scope (PR-B):** `everything` (the branch point off the default branch),
  `what I just did` / `my recent changes` (`--scope local` — local-ahead
  commits vs the branch's upstream), `since last review` / `what's new`
  (`--scope since-last-review` — the recorded last-reviewed SHA for this
  branch). Mutually exclusive with an explicit base ref; an explicit ref wins.
  `local` with no upstream and `since-last-review` with no prior record fall
  back to the branch point with a one-line note rather than erroring.
- **OpenSpec (PR-C):** `review the openspec change <id>` / `run syncade on my
  openspec proposal` → `--openspec <id>` (or bare `--openspec` to auto-resolve a
  single active change). syncade assembles `openspec/changes/<id>/` (proposal +
  spec deltas) into the spec it reviews against — reading the markdown directly,
  no `openspec` binary needed. Replaces the PR-doc path (one spec source);
  `--base`/`--scope` still set the diff base.
- **Draft-spec / tier C (PR-D + PR-E):** no brief at all? `I didn't write a spec,
  review what we did` / `draft a spec from this session`. A cold drafter reads the
  session transcript for *intent* (the skill resolves the transcript path; it never
  summarizes the conversation itself) and writes a draft spec with an "Assumptions
  to confirm" section. **PR-E ties this into one pane:** syncade drafts → you
  **ratify it right here in chat** (it shows the draft + the flagged assumptions
  verbatim and asks "what did I miss?"; your corrections edit the spec) → the blind
  review loop runs — no second command. A manufactured spec is never reviewed
  against unratified.
- **Target:** an explicit path always wins; a PR number / `this PR` resolve only
  when exactly one readable PR doc matches, else the skill asks.

## Prerequisites

- `claude` CLI installed and authenticated (`claude --version` works).
- `codex` CLI installed and authenticated (`codex login status`
  reports "Logged in...").
- `syncade` installed and on `PATH` (`syncade --version` works from any
  directory — see the repo README's "Global install").
- `<repo-root>/.syncade/config.toml` exists and specifies at least
  one `[[reviewers]]` block plus a `[producer]` block.
- macOS or Linux (the skill assumes `bash` / `zsh`; Windows-WSL is
  fine the same way running `syncade` manually is).

## When to use vs. terminal

| Situation | Skill | Terminal |
|---|---|---|
| First syncade run on a PR brief | yes — gets you pre-flight diagnostics + confirmation gate | works, but you'll want to run `--auth-check` and `--selfcheck` yourself |
| Repeat dogfood after a code change | yes — same pre-flights catch token rotation cheaply | yes if you've already verified your auth recently |
| You only want auth diagnostics, not a review | no — terminal: `syncade --auth-check` is ~5 s | yes |
| You only want producer-commit diagnostics | no — terminal: `syncade --selfcheck` is ~30 s | yes |
| You only want a spec audit of the brief | no — terminal: `syncade --spec-audit <pr-doc>` (manual; not part of the skill flow) is ~30–90 s | yes |
| Resuming a prior run | terminal: `syncade --resume <run-id>` (when that ships) | yes |

The skill is opt-in. Operators running `syncade <pr-doc>` from a
terminal still get the same loop; they just don't get the pre-flight
+ confirmation gate.

## Failure modes

### Auth-check fails (step 2)

The skill stops. Output shows which provider failed and the
remediation:

- `[auth-check] anthropic: FAILED (...). Run 'claude' interactively to re-authenticate.`
- `[auth-check] openai: FAILED (...). Run 'codex login' to re-authenticate.`

Fix the named provider and re-run `/syncade <pr-doc>`.

### Selfcheck fails (step 3)

The skill stops. Common causes:

- **Producer stalled** — the producer edited files but never
  committed, so the round has nothing to accept. This is not a
  permissions misconfiguration: `[producer] permissions` accepts
  **only** `"yolo"` (schema-enforced), precisely because sandboxed
  modes cannot commit headlessly — claude's `acceptEdits` prompts for
  bash, and codex's `workspace-write` blocks `.git/index.lock` writes.
  Look at the producer's own output instead: it usually means the model
  described a fix without running `git commit`.
- **Producer subprocess error** — the producer CLI itself failed.
  The selfcheck workspace is preserved; the path is the last stderr
  line. `cat <workspace>/round-0/producer.error.txt` for the cause.
- **Producer committed but didn't add the marker** — the producer's
  prompt template may be broken. Inspect
  `<workspace>/round-0/producer.stdout` to see what it actually did.

### Operator cancels at step 4

The skill prints `[syncade] cancelled at operator confirmation` and
exits. Nothing was modified.

### `syncade <pr-doc>` fails mid-loop (step 5)

The skill still tries to read `loop-summary.md`. If it exists, the
verdict is presented; if not, the skill points at the highest-numbered
round dir under `<run-dir>/`. Common exit codes:

| Exit | Meaning | Where to look |
|---|---|---|
| 20 | max rounds reached, no SHIP | every round's `findings.md` |
| 25 | budget ceiling hit; loop stopped at a phase boundary (before a review bundle or a producer) | resume with `syncade --resume` or start a fresh run with a higher limit |
| 30 | non-dismissed blocker, or producer stalled, or tests failed | last round's `findings.md` or `test-run.stdout` |
| 40 | reviewer/synthesizer/producer subprocess error | last round's `<provider>.stderr` or `producer.error.txt` |
| 50 | config error | the message says exactly which TOML field |
| 60 | worktree error or dirty-tree refusal | the message names the cause |
| 70 | output unparseable | `<reviewer>.stdout` for the actual reviewer output |

Full table at the exit-code table.

## Producer commits on your branch

When `max_rounds > 1` (the default), the producer can commit to the
current branch between rounds. The blast radius:

- **Each producer commit lands on the operator's branch**, not on a
  separate branch. Reviewer worktrees share `.git`, so the next
  round's reviewers see the new commit.
- **Branch advance is fast-forward only.** If the producer makes an
  orphan commit (which shouldn't happen — the producer's prompt
  requests a real commit on top of HEAD), the orchestrator refuses
  to advance the branch and exits 30 with `producer_stalled`.
- **Step 7 of the skill surfaces every commit** with its SHA + subject
  before declaring the run done. Inspect each via `git show <sha>`
  before merging.
- **Rollback is `git reset --hard <round-0-starting-sha>`**. The
  starting SHA is in `<run-dir>/loop-manifest.json` at
  `rounds[0].snapshot.commit_sha`. The skill prints this command in
  step 7; you don't have to look it up.

If you want to keep some producer commits and reject others, use
`git reset --hard <round-0-sha>` + `git cherry-pick <sha>` for the
ones you keep, or `git revert <sha>` to invert specific ones in
place.

## What the skill does not do

- Doesn't reimplement syncade. It's a Bash wrapper over the syncade
  CLI; everything the skill does is also doable from a terminal.
- Doesn't review code itself. The interactive Claude reading this
  skill is the operator's UI, not a syncade reviewer. The reviewer
  subprocesses (`claude -p`, `codex exec`) run with no shared
  context with this session.

## Where this skill lives

`.claude/skills/syncade/` in the syncade repo, so `/syncade` is available only
when you work inside this repo.

**To use it in any project**, install it at the user level with one command:

```bash
syncade --install-skill claude      # copies it to ~/.claude/skills/syncade/
```

This works from a plain `pip install syncade` (the skill is bundled in the
package). Skills load at session start, so restart Claude Code (or open a session
in the target project) to pick it up. The `syncade` CLI it shells out to must be on
your `PATH`. Developers who want the installed skill to track a checkout can
`ln -sfn "$PWD/.claude/skills/syncade" ~/.claude/skills/syncade` instead.

## Codex counterpart (single source)

There is a sibling Codex skill at `.codex/skills/syncade/` (install into
`~/.codex/skills/`). It is the **same** skill: the load-bearing Step 2 →
Invariants workflow span is shared and kept byte-identical by
`tests/skills/test_skill_drift.py` — the two copies are wrapped in
`<!-- SYNCADE-SHARED:start/end -->` markers, and **any edit to the shared span
must be made in both copies** or the drift test fails. Only the harness-specific
head differs (invocation phrasing; Claude's tier-C draft-from-session vs the
Codex copy's graceful degrade, which lands draft-from-session in a follow-up; and
`CLAUDE.md` vs `AGENTS.md`). See the Codex skill format and `path/to/pr.md` for the brief.
