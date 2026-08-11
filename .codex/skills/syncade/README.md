# syncade Codex skill

Bridge between Codex and the syncade external review orchestrator. Describe what
you want in natural language from inside Codex; this skill runs a safety check
+ confirmation gate + the review loop, then summarizes the verdict inline.

It is the Codex counterpart of the Claude Code skill at
`.claude/skills/syncade/`. Both are the **same** skill: their load-bearing
Step 2 → Invariants workflow span is single-source and kept byte-identical by
`tests/skills/test_skill_drift.py`. They differ only in the harness-specific
head (invocation phrasing, spec-tier handling, `CLAUDE.md` vs `AGENTS.md`).

## Quick start

```
run syncade on path/to/pr.md
review path/to/pr.md for 2 rounds against main
dogfood a PR single pass
review the openspec change add-auth
review path/to/pr.md since last review
```

The skill also handles **configuration** without running a review — "/syncade config",
"change my producer to gpt-5", "set rounds to 2", "show my config" — rendering a config
**menu** (`syncade --config list --all`) you browse and edit via
`syncade --config set <key> <value>` (writes the global `~/.syncade/config.toml`, or the
repo's with `--repo`). See "Configuring syncade" in the SKILL.

Both a bare path and clear natural language work — the skill maps your request
onto the CLI shape `syncade [--openspec [ID] | PR_DOC] [--base REF | --scope
SCOPE] [--max-rounds N]` at the markdown layer only (no Python of its own). The
interactive Codex agent then: resolves intent → validates the doc → runs
`syncade --auth-check` + `--selfcheck` → shows the exact command and waits for
`go`/`cancel` → streams the loop → reads `loop-summary.md` and prints the verdict
→ surfaces any producer commits with a rollback pointer.

### Natural-language phrasings the skill understands

- **Round count:** `for 1 round`, `for 2 rounds`, `max rounds 3`, `single pass`
  (= 1 round). Valid: 1–10. Omit it and your config's `max_rounds` stays authoritative.
- **Base ref:** `against main`, `from origin/main`, `base HEAD~3` (validated with
  `git rev-parse` before running). Omit it and current CLI behavior stays authoritative.
- **Scope:** `everything` (branch point off the default branch), `what I just did`
  / `my recent changes` (`--scope local`), `since last review` / `what's new`
  (`--scope since-last-review`). Mutually exclusive with an explicit base; an
  explicit ref wins.
- **OpenSpec:** `review the openspec change <id>` → `--openspec <id>` (or bare
  `--openspec` to auto-resolve a single active change). syncade assembles
  `openspec/changes/<id>/` into the spec directly — no `openspec` binary needed.
- **No brief at all?** The skill **degrades gracefully** — it asks for a brief or
  `--openspec` (and `--scope` only narrows the base, so it can't stand in for a
  spec), and never invents a spec. Drafting a spec from the Codex session
  transcript is a planned follow-up.

## Prerequisites

- `codex` CLI (this harness) installed and authenticated (`codex login status`
  reports logged in) — the default reviewers, judge, and producer all resolve to
  OpenAI in a Codex / plain-terminal environment with no extra config.
- `syncade` on `PATH` (`syncade --version` works from any directory — see the
  repo README's "Global install").
- macOS or Linux (the skill assumes `bash`/`zsh`).

## When to use vs. terminal

The skill is opt-in — it adds the pre-flight diagnostics + confirmation gate over
the plain CLI. Running `syncade <pr-doc>` from a terminal still gets the same
loop; it just skips the gate. Use the terminal directly for `syncade --auth-check`
(~5s), `syncade --selfcheck` (~30s), `syncade --spec-audit <pr-doc>`, or
`syncade --resume <run-id>` — those are standalone diagnostics the skill does not
wrap.

## Producer commits land on your branch

When `max_rounds > 1` (the default), the producer commits fixes directly to the
current branch between rounds (fast-forward only). The skill lists each commit
with its SHA + subject and prints the rollback pointer
(`git reset --hard <round-0-starting-sha>`, from
`<run-dir>/loop-manifest.json` `rounds[0].snapshot.commit_sha`). Nothing is
auto-reverted; you decide what to keep. See `AGENTS.md` for the full blast-radius
contract.

## Failure modes

- **Auth-check / selfcheck fails:** the skill stops and surfaces the syncade
  output verbatim (it names the failing provider and the fix). Common selfcheck
  cause: `[producer] permissions` must be `"yolo"` for a headless commit.
- **You cancel at the gate:** the skill prints `cancelled at operator
  confirmation` and exits; nothing is modified.
- **`syncade <pr-doc>` exits non-zero:** the skill still reads `loop-summary.md`
  if present, else points at the highest-numbered round dir. Exit-code table is in
  `AGENTS.md` and the exit-code table.

## What the skill does not do

- Doesn't reimplement syncade — it's a Bash wrapper over the CLI; everything it
  does is doable from a terminal.
- Doesn't review code itself. The interactive Codex agent reading this skill is
  the operator's UI, not a syncade reviewer. The reviewer subprocesses (`claude
  -p`, `codex exec`) run with no shared context with this session.

## Where this skill lives

Install it at the user level with one command:

```bash
syncade --install-skill codex       # copies it to $CODEX_HOME/skills/syncade/
```

This works from any installed copy of syncade (the skill ships inside the
package) — `pip install .` from a checkout, or `pip install git+<repo-url>`.
syncade is not published on PyPI. Restart Codex to pick up new skills. The `syncade` CLI it shells out to
must be on your `PATH`. `~/.codex/skills` (i.e. `$CODEX_HOME/skills`) is the
verified-discovered location; format details in the CLI-format notes.
Developers who want the installed skill to track a checkout can
`ln -sfn "$PWD/.codex/skills/syncade" ~/.codex/skills/syncade` instead.
