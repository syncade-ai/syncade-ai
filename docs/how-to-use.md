# How to use syncade

The [README](../README.md) explains *why* a blind panel finds what your coding agent can't. This
page is about *getting the most out of it* — what to hand syncade, how big a change should be, what
to do before you invoke it, and which settings are worth changing.

If you read only one section, read **[Write small PRs](#3-write-small-prs-36-items)** and
**[Prompt your agent before you invoke syncade](#4-prompt-your-agent-before-you-invoke-syncade)**.
Those two decide more about your result than any config knob.

**Contents**

1. [The one-minute model](#1-the-one-minute-model)
2. [You need a spec — or syncade writes one](#2-you-need-a-spec--or-syncade-writes-one)
3. [Write small PRs (3–6 items)](#3-write-small-prs-36-items)
4. [Prompt your agent before you invoke syncade](#4-prompt-your-agent-before-you-invoke-syncade)
5. [Running it](#5-running-it)
6. [Reading the verdict](#6-reading-the-verdict)
7. [Defaults, and what we recommend](#7-defaults-and-what-we-recommend)
8. [Changing settings](#8-changing-settings)
9. [A first run, end to end](#9-a-first-run-end-to-end)
10. [Gotchas](#10-gotchas)

---

## 1. The one-minute model

You give syncade **a spec and a branch**. Each round:

```
your diff ──▶ reviewer 1 (blind, Git-less export) ─┐
         └──▶ reviewer 2 (blind, Git-less export) ─┴─▶ cold judge ─▶ findings.md + exit code
                                                                            │
                                  optional: your test command + checks ─────┤
                                                                            │
                                             NO-SHIP ──▶ producer commits in isolated repository
```

Four things about that picture matter, and they're the whole product:

- **The reviewers are blind.** They are fresh CLI subprocesses. They do not see your session, your
  prompts, the producer's narrative, or each other. They start from the diff and the spec. That is
  why they can see the gap between what you *said* you'd build and what you *built* — the gap your
  own agent is structurally unable to see, because it holds the assumption that created it.
- **The judge is cold.** It receives the reviewers' structured findings only — never your diff,
  your tests, or their raw output. It consolidates; it cannot invent.
- **The verdict is mechanical.** Ship / no-ship is an exit code computed from the consolidated
  blockers plus your own test and check results. No LLM decides it. Unanimous blockers cannot be
  dismissed.
- **It fixes, not just reviews and reports.** On NO-SHIP a producer subprocess attempts the fix
  and commits in its own standalone repository. That commit range is then validated, anchored at
  a recovery ref, and fast-forwarded onto your branch, and the next round begins.

Everything below is about feeding that loop well.

---

## 2. You need a spec — or syncade writes one

**The spec is the reviewers' only statement of intent.** They cannot ask you what you meant. A
blind reviewer with a vague brief reviews vague code and returns vague findings; a blind reviewer
with a crisp brief returns "item 3 says X, the code does Y." Time spent on the brief is repaid at a
higher rate than time spent on any setting on this page.

There are three ways to supply one. All three converge on the same review loop.

### Tier A — you have a brief (recommended)

A short markdown file. That's it, there is no required schema:

```bash
syncade specs/my-change.md
```

What makes a brief work for a blind reader:

- **Numbered, discrete items.** "1. Refuse a populated directory. 2. Record the emptiness bit
  before the first write." Numbering gives reviewers and the judge a shared referent, and it's what
  makes a finding say *which* requirement was missed.
- **Acceptance criteria per item** — what must be observably true. "Exits 60 and names the file"
  beats "handles this safely."
- **Say what is deliberately out of scope.** A reviewer that doesn't know a thing was excluded will
  report its absence as a defect. This is the single most common source of noise findings.
- **Don't paste the implementation.** You're describing the destination, not the route. A brief
  that dictates the code teaches the panel to check the code against itself.

Audit the brief before you spend anything on reviewers:

```bash
syncade --spec-audit specs/my-change.md
```

That runs a cold auditor over the brief text alone and reports ambiguity — requirements that can be
satisfied two ways, missing acceptance criteria, contradictions. Exit `10` means "a human needs to
decide something." It is a manual, opt-in diagnostic; the review loop does not run it for you.

> **A retired requirement is indistinguishable from an unmet one.** If you drop an item mid-build,
> delete it from the brief — do not leave it there. A blind panel measures the diff against the
> document, correctly reports the missing item, and the producer dutifully builds a thing you no
> longer want. On this project that has burned whole runs more than once. The stale document is the
> defect.

### Tier B — you use OpenSpec

Point syncade at an existing OpenSpec change folder and it assembles the spec itself:

```bash
syncade --openspec my-change-id
```

It reads the change folder directly; it never shells out to OpenSpec.

### Tier C — you have no spec

If you just built something in a Claude Code session and never wrote a brief, say so:

```
/syncade review what I just did
```

A cold drafter reads the session transcript and a raw diff, writes a spec, and **shows it to you to
ratify in the pane before any reviewer is dispatched.** Read it. It is a machine's reconstruction
of your intent, and correcting one sentence there is cheaper than a round of review against the
wrong target. Once you approve it, the normal loop runs.

Tier C is the convenience path, not the good path. Tier A produces better reviews, because a brief
you wrote *before* building states what you meant, while a brief drafted afterward can only
describe what you did.

---

## 3. Write small PRs (3–6 items)

**syncade is built for 3–6 discrete items, not for large amounts of code.** This is the most common
way to get a disappointing run.

Why smallness pays:

- **Attention is finite.** A reviewer's effort is spread across the diff. Ten items in one diff get
  a shallower read each than four items would — you get more findings per item from two small runs
  than from one big one.
- **The loop converges.** On NO-SHIP the producer fixes and the panel re-reviews. With a handful of
  items a round can plausibly clear them all and ship. With thirty, each round fixes some and the
  run hits `max_rounds` still red, having spent full price for a partial result.
- **Big diffs get refused, and the ceiling is real.** The default `max_diff_bytes` is **1,000,000**
  and syncade exits `60` rather than review past it. That is deliberately just under `codex exec`'s
  hard 1,048,576-character input limit, so syncade's message fires before the provider's does. For
  calibration: across measured rounds on this codebase the *largest* reviewer-facing diff was
  **147 KB**, median **77 KB**. If you are anywhere near the cap, the PR is far too big.
- **Cost scales with rounds.** Measured across 126 priced runs: median **$4.66**, p90 **$15.75**,
  worst **$35.50**. The expensive tail is multi-round loops where the producer rewrites code every
  round — which is what a sprawling PR guarantees.

Rule of thumb: **if the brief has more than six numbered items, split it.** Ship one, then the
next. A sequence of small green runs is faster and cheaper than one long red one.

---

## 4. Prompt your agent before you invoke syncade

syncade reviews what your agent built. The quality of that build sets the floor, and a sloppy
build burns rounds on defects a decent prompt would have prevented.

This is the prompt used on this project before invoking syncade. It is effective and it minimizes
bugs — and syncade **still** finds plenty. Use it as a starting point:

```text
Let's start on <path/to/pr-name>. Let's implement this issue by issue. Work the PR as a
sequence, not a blanket implementation. First, re-anchor the exact items, then take the
first one, implement it, and only move on after concrete proof is recorded.

Before you call anything done, test everything. As applicable, per project: include
adversarial, full gate, LLM as a judge (your own, not syncade), code, tests, db, backend.
Run SQL statements to verify data where needed, verify cron jobs work, API endpoints work,
etc. Be as thorough as possible. If UI is part of the project scope, test as a user would
as well, using Playwright to ensure that functionality works and surfaces as advertised,
and most importantly, that the data in the UI is correct and actually surfaces in the UI
and matches what is in the db / persistent state / LLM synthesis.

Be sparse and greedy in implementing code. Every line has to earn its place, and simplicity
is overwhelmingly preferred. Last, no file > 600 LOC — use elegant engineering, good
architecture and separation of concerns where needed.

Most important, you must cite CONCRETE proof that each issue is correctly implemented
and/or fixed before moving forward to the next one. No implementation/fix will be accepted
as completed without statement of proof attached.
```

Why each clause earns its place:

- **"Issue by issue, not a blanket implementation."** A blanket pass produces a diff where nothing
  is individually verified and every item is half-done. Sequencing forces completion.
- **"Re-anchor the exact items."** The agent restates the brief before touching code. Drift between
  what the brief says and what the agent *thinks* it says is caught in the first minute rather than
  by a reviewer an hour later.
- **"Concrete proof before moving on."** This is the load-bearing clause. "Done" from a coding
  agent is an assertion; a pasted test run is evidence. Requiring proof per item converts the
  agent's optimism into something you can check — and it is exactly what a blind reviewer will
  demand anyway.
- **"Test everything… adversarial, full gate."** Reviewers run your test command as a convergence
  leg. Arriving with a red suite wastes a round establishing what you already knew.
- **"Sparse and greedy. Every line has to earn its place."** Reviewers flag speculative
  abstraction, and the producer then spends a round deleting it. Cheaper not to write it.
- **"No file > 600 LOC."** A file-length check is a mechanical gate you can wire in directly (see
  `[[checks]]` below), so it fails fast for free instead of consuming reviewer attention.

Then invoke syncade. Treat the panel as the thing that catches what survived a genuinely careful
build — not as a substitute for one.

---

## 5. Running it

### From a terminal

```bash
cd your-git-repo
git checkout -b my-change            # NOT your default branch — accepted work targets it

syncade --doctor --quick             # $0 readiness check: CLIs, worktree, repo state
syncade specs/my-change.md        # the review loop
```

`syncade --doctor` (without `--quick`) additionally runs two live legs — an auth probe and a
headless producer commit test — for about 30 seconds of real provider calls, and previews the cost
of the run you're about to start. It is strictly read-only: no commit, no ref move, no run
artifacts. Worth running the first time you set up a repo.

Useful flags for a single invocation:

```bash
syncade brief.md --max-rounds 1          # single pass, no producer
syncade brief.md --budget-usd 10         # stop at a dollar ceiling (exit 25, resumable)
syncade brief.md --preset thorough       # more rounds/time, same proven panel
syncade brief.md --base main             # review against an explicit base
syncade brief.md --scope since-last-review
syncade --resume                         # continue an interrupted or budget-stopped run
```

### From inside Claude Code or Codex

Install the skill once (`syncade --install-skill`), then just talk to it:

```
/syncade specs/my-change.md
/syncade run the loop on my-change for 2 rounds against main
/syncade review what I just did              # tier C: drafts a spec, ratifies it, then runs
/syncade dogfood the brief, cap it at $8
```

The skill is a Bash wrapper over the same CLI. It confirms before firing the expensive subprocess,
streams output into your session, and inlines the verdict when the run ends. **The Claude you're
talking to is never one of the reviewers** — syncade always spawns its own isolated subprocesses.
That separation is what makes the verdict worth anything.

Configuration is a separate intent the skill also understands — "show my config", "set rounds to
2", "cap cost at $5" — and it will never start a review for one of those.

### Where the output goes

Everything lands under `.syncade/runs/<run-id>/` (gitignored):

| File | What it is |
|---|---|
| `loop-summary.md` | Start here. The run's verdict and per-round story. |
| `findings.md` | The judge's consolidated findings, latest round. |
| `decision-needed.md` | Written on exit `10` — what needs a human. |
| `handoff.md` | Written when a run ends NO-SHIP — what's left. |
| `round-N/` | Per-round manifest, reviewer artifacts, judge output, transcripts. |
| `status.json` | Live breadcrumb; a stale `running` means the run was hard-killed. |

---

## 6. Reading the verdict

The verdict is an exit code. The LLMs never set it directly.

| Code | Meaning | What to do |
|---|---|---|
| `0` | **SHIP** — or nothing to review | Merge. Check `termination_reason` if you expected findings. |
| `10` | A human must decide | Read `decision-needed.md`. Answer, then `syncade --resume`. |
| `20` | Max rounds hit, still NO-SHIP | Read `findings.md`. Usually means the PR was too big. |
| `25` | Stopped at a boundary, resumable | Your budget ceiling, or the provider's usage limit. `--resume`. |
| `30` | Findings present, test failed, or producer stalled | Read `findings.md`; fix or re-run. |
| `40` | A subprocess failed | Check the round's `.stderr`. Often auth or a provider blip. |
| `50` | Config error | The message names the field. |
| `60` | Environment / repo / refused run | Dirty tree, default branch, diff too large or unreadable. |
| `70` | Reviewer or judge output unparseable | Rare. Re-run; report it if it repeats. |

**A clean verdict is evidence, not proof.** The panel is not deterministic. On this codebase, two
runs 25 minutes apart over byte-identical code produced one two-blocker NO-SHIP and one clean SHIP.
Both findings were real. If a SHIP arrives over code you have not changed since a NO-SHIP, trust it
less — compare the verdict to what the *code* changed, not to the previous verdict.

---

## 7. Defaults, and what we recommend

Zero-config works. These are the shipped defaults:

| | Default | Notes |
|---|---|---|
| **Reviewer 1** | `openai` / `gpt-5.5`, thinking `high` | `codex-reviewer` — standard lens |
| **Reviewer 2** | `openai` / `gpt-5.5`, thinking `high` | `codex-reviewer-adv` — adversarial lens |
| **Judge** | `openai` / `gpt-5.5`, thinking `high` | Cold; sees only structured findings |
| **Producer** | `anthropic` / `claude-sonnet-4-6` under Claude Code; `openai` / `gpt-5.6-terra` otherwise | Harness-aware. Runs `confined` by default — sandboxed by the provider, committing only in its own standalone repository |
| **Rounds** | `5` | Ceiling `10`. A typo-guard, not a spend guard |
| **Timeout** | `1800`s | **Per subprocess**, not per round — see below |
| **Max diff** | `1,000,000` bytes | Refuse rather than review (exit 60) |
| **Budget** | `50,000,000` tokens | Stops at exit `25` (resumable). 4% of measured runs would have tripped this; raise with `--budget-tokens 0` to opt out |
| **Test command** | none | Optional third convergence leg |
| **Reviewer permissions** | `trusted-execute` | Codex is sandboxed to its Git-less workspace; Anthropic disables auto-loaded customizations but is not host-confined |
| **Worktree base** | `/tmp/syncade` | Grows; see gotchas |

**What we recommend:**

- **Keep the reviewers on `codex` / OpenAI.** Not a shrug at the alternatives — a measurement. On
  mixed panels reviewing *the same diff in the same round*, the OpenAI reviewer raised **1.18
  findings per reviewer-round against Anthropic's 0.38**. We offlined an Anthropic reviewer and
  reverted a `gpt-5.6` panel after both audited too leniently. Read that with its limits: one
  codebase, one language, a small Anthropic sample. It is enough for a default, not a law — and the
  config exists precisely so you can test it on your own repo.
- **Keep two reviewers with different lenses.** Across 441 runs the panel raised 547 blocking
  findings and **56% were caught by only one of the two** (490 findings from the adversarial
  prompt, 420 from the standard one). Drop either and you lose half the findings — and not the
  same half.
- **Never lower the reviewer's effort tier to save money.** A cheaper panel that audits leniently
  ships bugs, which is the expensive outcome. Save money with `--max-rounds 1` or a budget ceiling,
  both of which reduce *how much* review you buy without degrading its quality. The bundled presets
  are built on exactly this principle: `cheap`, `balanced` and `thorough` vary only rounds and
  timeout, and never touch the reviewer model or effort.
- **Raise or lower the default token budget if needed.** The shipped default is `50,000,000` tokens
  (about $15–20 API-equivalent; stopped 4% of measured runs). Raise with `--budget-tokens N` or
  `[loop] budget_tokens = N`; set to `0` to opt out entirely. A dollar ceiling is also available
  (`--budget-usd` / `[loop] budget_usd`), but note it reflects an API-equivalent valuation — on a
  subscription the marginal cost is $0, so a dollar ceiling can interrupt a run over money nobody
  spent. Either ceiling stops at a phase boundary with exit `25`, resumable.
- **Add your test command and mechanical checks.** They fold into the verdict, and a lint or
  file-length gate that fails for pennies is attention the panel doesn't have to spend.

**On `timeout_seconds`** — it caps **each subprocess**, not a round. Every leg gets it: each
reviewer, the judge, the test run, each check, the producer. With two reviewers (parallel, so they
count once), a test command and three checks, one round can run **7×** the configured value.
Size it as "how long may a single model call take" (~30 min is right) and use the budget ceilings
as the actual runaway guard.

---

## 8. Changing settings

Config resolves in layers: **defaults → `~/.syncade/config.toml` (global) → repo
`.syncade/config.toml` → CLI flags**. Later wins. Paired sections (`[producer]`, the cold actors,
`[[reviewers]]`) are replaced *wholesale* by the highest layer that defines them, so a provider and
its model can never split across two files.

### With TOML

```toml
[loop]
max_rounds = 3
timeout_seconds = 1800          # per SUBPROCESS
test_command = "pytest -q"
budget_usd = 10

[[reviewers]]
name = "codex-reviewer"
provider = "openai"
model = "gpt-5.5"
thinking = "high"

[[reviewers]]                   # cross-model: a second lab's perspective
name = "claude-reviewer"
provider = "anthropic"
model = "claude-sonnet-4-6"
thinking = "high"

[[checks]]                      # mechanical gate, exit-code only — not an LLM finding
name = "lint"
command = "ruff check ."
severity = "blocking"           # or "advisory" — renders but doesn't gate
```

Every reachable knob is in **[config-reference.md](config-reference.md)**.

### From the CLI

```bash
syncade --config                              # arrow-key menu; drills into every actor
syncade --config list                         # common settings + which layer set each
syncade --config list --all                   # every settable field, with dotted keys
syncade --config get loop.max_rounds
syncade --config set loop.max_rounds 2        # writes to the GLOBAL file
syncade --config set loop.max_rounds 2 --repo # ...or this repo's file
syncade --config set reviewers.1.provider anthropic
```

`set` validates through the schema and **never writes a broken file** — a bad key exits `2`, a bad
value exits `50`, and the file is untouched. Changing a role's provider re-derives its model, so
you cannot end up handing an Anthropic model to `codex`.

### By chatting

Inside Claude Code or Codex, the skill renders the same surface conversationally — "show my
config", "set the judge to gpt-5.5", "cap cost at $5". The curses menu can't run in a chat pane, so
the skill edits via the CLI and warns you when a higher layer would shadow your edit.

---

## 9. A first run, end to end

```bash
# 1. A branch that isn't your default — accepted producer work targets this branch
git checkout -b add-rate-limiting

# 2. Write a brief: 3-6 numbered items with acceptance criteria
$EDITOR specs/rate-limiting.md

# 3. Check the brief is unambiguous before spending on reviewers
syncade --spec-audit specs/rate-limiting.md

# 4. Build it, with the prompt from section 4, demanding proof per item

# 5. Commit. Reviewers see committed HEAD
git add -A && git commit -m "add rate limiting"

# 6. Confirm the environment is sane, for $0
syncade --doctor --quick

# 7. Review, with a ceiling
syncade specs/rate-limiting.md --budget-usd 10

# 8. Read the verdict
cat .syncade/runs/*/loop-summary.md
```

On SHIP you're done. On NO-SHIP, read `findings.md` and the run artifacts. If a producer
candidate was made but could not be landed — a lost compare-and-swap race, a detached HEAD —
the run prints the `refs/syncade/...` recovery ref it is anchored at; your branch is untouched
and the candidate is still reachable from your repository at that ref.

---

## 10. Gotchas

- **The producer's commits reach your branch through the trusted importer.** It works in a
  standalone repository; syncade validates the commit range, anchors it at a recovery ref, and
  fast-forwards your branch with a compare-and-swap. In `confined` mode (default) the producer
  sandbox enforces host confinement (exterior filesystem writes are denied); `yolo` removes that confinement. The trusted importer boundary is unconditional in both modes. Your working tree is *not* updated to
  match, so `git status` afterwards looks like a staged revert — sync with
  `git stash && git reset --hard HEAD && git stash pop` rather than committing it. syncade warns
  about this and the warning is not suppressible.
- **syncade refuses your default branch** unless you pass `--allow-default-branch`, because
  accepted producer work targets the invocation branch.
- **Loop mode refuses a tracked-modified dirty tree** unless you pass `--force-dirty`. Single-pass
  review does not refuse: reviewers see committed HEAD, so uncommitted work is invisible to them.
- **`<worktree_base>` grows.** NO-SHIP runs keep reviewer exports, standalone
  producer repositories, and trusted linked worktrees so you can
  inspect what each actor saw. `syncade --gc` removes these workspaces once a run can no longer
  plausibly be resumed, controlled by
  `gc.worktree_max_age_days` (default 14 days) — this applies even to runs that remain
  resume-eligible. It reached 4.4 GB on this machine before a cleanup. Point `worktree_base`
  somewhere you don't mind, outside the repository under review (reviewer provisioning refuses a
  repo-local export). Run `git worktree prune` after manually removing linked
  test/check worktrees; reviewer exports and standalone producer repositories
  have no linked-worktree registration.
- **Run artifacts are never deleted**, only transcripts are pruned. `syncade --metrics` is derived
  from that tree and would lose your history otherwise.
- **Full LLM transcripts land in `.syncade/runs/`**, including source a reviewer read. **A secret in
  a file a reviewer opens is written there in plaintext.** The directory is gitignored. See
  [SECURITY.md](../SECURITY.md).
- **Billing is not symmetric between the two CLIs.** `claude` lets `ANTHROPIC_API_KEY` override your
  login; `codex` ignores `OPENAI_API_KEY` and uses its own. Each actor takes
  `auth = "auto" | "subscription" | "api"`, and every run prints which account is about to pay —
  even under `--quiet`. On a subscription the marginal cost is $0, which is exactly why it's easy to
  forget that on API billing it is real money.
- **`CLAUDE.md` and `AGENTS.md` are stripped** from Git-less reviewer exports and diffs by default,
  so Syncade does not supply project memory to the panel. This is not whole-host confinement for an
  unsandboxed reviewer. Add your own via `[review] strip_repo_context_files`.

---

Something here wrong, unclear, or missing? Open an issue — see
[CONTRIBUTING.md](../CONTRIBUTING.md).
