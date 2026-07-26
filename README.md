# syncade

**A blind, cross-model review loop that catches and fixes the bugs your coding agent can't see in
its own work.**

syncade reviews your changes with a panel of blind reviewers that share none of the context, prompt,
or model that wrote the code, consolidates their findings with a cold judge, and returns a ship /
no-ship decision as a mechanical exit code. On no-ship it fixes the code and reviews again, looping
until your change either ships or runs out of budget. It runs without leaving your Claude Code or
Codex session.

## Why syncade

The model that writes your code is the worst reviewer of it. It reviews with the same context that
produced the bug, so a wrong assumption in the code is a wrong assumption in the review: it can't see
the gap between what the spec says and what it actually wrote. It's anchored to work it just committed
to, so review turns into rationalization instead of scrutiny. And by the end of a long session the
real diff is buried under thousands of tokens of its own narrative. That is why bugs sail past the
harness and land in your PR.

Stacking more of the same doesn't help. An IDE assistant, a PR bot, a "review this file" prompt: each
is one model, one lens, one failure distribution. Every model has systematic blind spots, whole
categories of bug it reliably misses, and when the same model writes and reviews, those blind spots
pass straight through.

syncade attacks the code from outside that failure distribution:

- **Blind and isolated.** Reviewers run as fresh CLI subprocesses with zero shared context: not your
  session, not each other, not the producer's narrative. They start from the diff and the spec, so the
  gap between the two is visible to them and invisible to whatever wrote the code.
- **Cross-prompt and cross-model by design.** A bug survives only if every judge misses it. Independent
  reviewers running different prompts (a standard pass and an adversarial one) and different models
  from different labs have far less overlap in what they miss, so the surviving set shrinks toward
  zero.
- **It finds and fixes.** On a no-ship verdict a producer attempts the fix and commits, then the loop
  runs again, converging to a shippable state instead of just handing you a report.
- **The verdict is mechanical.** Ship or no-ship is an exit code computed from the consolidated
  findings plus your own tests and checks, so an LLM never decides it directly.

The result is a review that finds what the tool that wrote your code structurally cannot.

## How it fits your flow

Point syncade at a short spec and it reviews your changes the way a careful team would: it
spawns fresh, isolated CLI subprocesses — **blind reviewers**, a **cold synthesizer** that
consolidates their findings into one mechanical verdict, and, on NO-SHIP, a **producer** that
attempts a fix and commits it — looping up to three rounds by default until it ships or runs out of
budget. The verdict comes back in the same Claude Code or Codex session; you never open another
terminal or copy-paste between tools.

## Install

```bash
uv tool install git+https://github.com/syncade-ai/syncade-ai.git   # recommended
# or with pip:
pip install git+https://github.com/syncade-ai/syncade-ai.git
```

Then install the harness integration so you can invoke it as `/syncade`:

```bash
syncade --install-skill        # Claude Code + Codex; or: --install-skill claude | codex
```

**Requirements**
- **Python 3.11+**, on **macOS or Linux** (on Windows, use WSL).
- The provider CLIs syncade drives, on your `PATH` and authenticated: **`claude`**
  (Anthropic) and **`codex`** (OpenAI). The default roster uses both.
- `git`. (`lsof` is optional — only `syncade --gc` uses it.)

Check your setup any time with `syncade --doctor`.

## Quick start

```bash
cd your-git-repo
syncade --doctor                 # green/red readiness check (CLIs, auth, worktree) — costs $0
syncade path/to/brief.md         # run the review loop against a short markdown spec
```

Or, from inside Claude Code / Codex once the skill is installed:

```
/syncade path/to/brief.md
/syncade review what I just did          # drafts a spec from the session, then reviews
/syncade dogfood the brief for 2 rounds
```

syncade finds the repo root itself, writes all artifacts under `.syncade/runs/`
(gitignored), and **refuses to run on your default branch** unless you pass
`--allow-default-branch` — because the producer commits to the current branch.

## How it works

Each round of `syncade <brief>`:

1. **Snapshot** the repo — HEAD plus the diff under review.
2. **Reviewers** (two, blind, in parallel) investigate inside throwaway worktree copies and return structured findings.
3. **Cold synthesizer** — a third blind judge — consolidates them into one `findings.md` with a **mechanical verdict**; unanimous blockers can't be dismissed.
4. Optional **test / check legs** run in a clean worktree and fold into the verdict.
5. On **NO-SHIP**, a **producer** attempts a fix and commits; the branch fast-forwards and the next round begins.

It ships the moment a round is clean, or stops at `max_rounds` (default 3), a budget
ceiling, or a producer stall.

**Cross-prompt and cross-model.** Reviewer diversity is the point — a blind spot shared by every
judge is a blind spot in the verdict. The default panel is two `codex` (OpenAI `gpt-5.5`) reviewers
running *different prompts* — a standard reviewer and an adversarial one — so it's **cross-prompt** out
of the box. And every actor is now fully configurable: set the provider and model per reviewer to run
**cross-model / cross-lab** (e.g. one OpenAI reviewer + one Anthropic reviewer) whenever you want a
second lab's perspective.

**We recommend `codex` (OpenAI) models as your blind reviewers.** In our own dogfooding they review
harder and ship less leniently than the alternatives — we offlined an Anthropic reviewer and reverted
a `gpt-5.6` panel after both audited too leniently. So the recommended default stays two codex
reviewers; reach for cross-model deliberately, not by
default.

## The verdict is an exit code

The verdict is mechanical — the LLMs never decide the exit code directly.

| Code | Meaning |
|---|---|
| `0`  | SHIP |
| `10` | Clarification or operator decision needed |
| `20` | Max rounds reached, still NO-SHIP |
| `25` | Budget (token/dollar) ceiling hit |
| `30` | Findings present, test failed, or producer stalled |
| `40` | A subprocess failed |
| `50` | Config error |
| `60` | Environment / worktree / repo problem |
| `70` | Reviewer or synthesizer output couldn't be parsed |

## Configuration

Zero-config works out of the box. To customize, add `.syncade/config.toml`:

```toml
[loop]
max_rounds = 3                  # recommended default; hard ceiling is 10
timeout_seconds = 1800          # per round, recommended ~30 min; set higher for big diffs
test_command = "pytest -q"      # optional third convergence leg

[[reviewers]]
name = "codex-reviewer"
provider = "openai"             # codex/OpenAI recommended for blind review; swap per reviewer for cross-model
model = "gpt-5.5"
thinking = "xhigh"
```

**Rounds & time budget.** We recommend **3 rounds** at **~30 minutes each** — enough for the producer
to converge on most PRs without runaway cost. You can raise `max_rounds` up to **10** and set any
per-round `timeout_seconds`; the real runaway guards are the budget ceilings (`budget_usd` /
`budget_tokens`), not the round cap. Per-invocation: `syncade --max-rounds N`, `--budget-usd N`.

Every reachable knob — reviewers, producer, the cold actors, retry, GC, budgets — is in
**[docs/config-reference.md](docs/config-reference.md)**, and three bundled presets
(`--preset cheap | balanced | thorough`) cover the common cases.

Prefer not to hand-edit TOML? `syncade --config` opens an arrow-key menu that **drills in** to every
actor and Advanced section (Producer / Reviewer / Judge → model, thinking, permissions, auth — plus
timeout for Producer/Reviewers; Advanced… → retry / gc / review / checks); `syncade --config set <key> <value>` sets **any** scalar
field on any actor/section (the scriptable form, one shared resolver); `--config list` shows the common
surfaced settings with the layer that set it, and `--config list --all` dumps the **full** settable
surface (every field, with layer + `overrides global` shadow notes + the dotted key). A bad key exits 2,
a bad value exits 50 — never a broken file. Edits go to a machine-wide **`~/.syncade/config.toml`** by
default — set "my producer is Opus everywhere" once — with precedence `defaults → global → repo
`.syncade/config.toml` → CLI flags`; in the menu, `t` switches the edit target and a `shadowed by
<layer>` flag warns when a higher layer would mask your edit. Add `--repo` to target the current repo
instead. Inside Claude Code / Codex, the `/syncade` skill renders that same surface as a **conversational
config menu** — browse and edit by chatting, no terminal needed.

**Billing caveat.** The two CLIs resolve credentials *oppositely* — `claude` lets
`ANTHROPIC_API_KEY` override your login, while `codex` ignores `OPENAI_API_KEY` and uses
its own login. Each actor takes `auth = "auto" | "subscription" | "api"`, and **every run
prints which account is about to pay**, even under `--quiet`. Details in
[SECURITY.md](SECURITY.md) and the config reference.

## Security

syncade runs AI coding-agent CLIs with **elevated tool access on your repo**:

- **Reviewers** run headless inside throwaway worktree copies under `/tmp/syncade/` (Codex reviewers are additionally sandboxed to theirs).
- **The producer** runs unsandboxed and **commits to your current branch** (syncade refuses the default branch by default).
- Full LLM transcripts — including source a reviewer read — are written to `.syncade/runs/` (gitignored, auto-pruned). **A secret in a file a reviewer opens lands there in plaintext.**
- syncade makes **no network calls of its own**; its only egress is the provider CLIs you already authenticated and the test/check commands you configure.

Running syncade is comparable to running the target repo's `Makefile` — it executes code
with your permissions. See **[SECURITY.md](SECURITY.md)** for the full threat model and how
to report a vulnerability.

## Development

```bash
uv venv --seed && source .venv/bin/activate
uv pip install -e ".[dev]"
uv run python -m pytest -q        # smoke tests are opt-in: add -m smoke
uv run ruff check . && uv run ruff format --check .
```

The only runtime dependency is `pydantic`. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for the
full contributor guide.

## License

[Apache-2.0](LICENSE). © 2026 Syncade.
