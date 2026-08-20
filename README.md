# syncade

**A blind, multi-judge review loop that catches and fixes the bugs your coding agent can't see in
its own work.**

syncade reviews your changes with a panel of blind reviewers that share none of the context, prompt,
or model that wrote the code, consolidates their findings with a cold judge, and returns a ship /
no-ship decision as a mechanical exit code. On no-ship it fixes the code and reviews again, looping
until your change either ships or runs out of budget. It runs without leaving your Claude Code or
Codex session.

> **New here? Start with [How to use syncade](docs/how-to-use.md)** — what to hand it, why your PR
> should be 3–6 items, how to prompt your agent *before* you invoke it, and which settings to change.

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
- **Diverse judges by design.** A bug survives only if every judge misses it, so the reviewers are
  deliberately not interchangeable. Out of the box you get **cross-prompt** diversity — a standard
  reviewer and an adversarial one, same model, different instructions — and that alone is worth
  more than it sounds. Across **341 runs on this codebase over ten weeks**, the panel raised 407
  blocking findings — and **56% of them were caught by only one of the two reviewers** (126 by the
  adversarial one, 101 by the standard one). Drop either and you lose half, and not the same half.
  Every actor is independently configurable, so **cross-model / cross-lab** is one setting away.
- **It finds and fixes.** On a no-ship verdict a producer attempts the fix and commits, then the loop
  runs again, converging to a shippable state instead of just handing you a report.
- **The verdict is mechanical.** Ship or no-ship is an exit code computed from the consolidated
  findings plus your own tests and checks, so an LLM never decides it directly.

The result is a review that finds what the tool that wrote your code structurally cannot.

## How it fits your flow

Point syncade at a short spec and it reviews your changes the way a careful team would: it
spawns fresh, isolated CLI subprocesses — **blind reviewers**, a **cold synthesizer** that
consolidates their findings into one mechanical verdict, and, on NO-SHIP, a **producer** that
attempts a fix and commits it — looping up to five rounds by default until it ships or runs out of
budget. The verdict comes back in the same Claude Code or Codex session; you never open another
terminal or copy-paste between tools.

## Install

```bash
uv tool install syncade   # recommended
# or with pip:
pip install syncade
```

To track unreleased `main` instead of the latest release:

```bash
uv tool install git+https://github.com/syncade-ai/syncade-ai.git
```

Then install the harness integration so you can invoke it as `/syncade`:

```bash
syncade --install-skill        # Claude Code + Codex; or: --install-skill claude | codex
```

Installing **refuses rather than overwrites**. If anything in the destination is not something
syncade wrote — a skill you hand-edited, an unrelated file you keep there — it exits without
touching the directory and names every file at risk. Ordinary upgrades need no flag: syncade
recognises its own past output by content, not by filename. Use `--force-install` to overwrite
deliberately; it still lists what it destroys.

### Staying up to date

The first time you run syncade in a new terminal or harness window, it checks once whether a
newer release exists and prints a line if so. It only ever prints — it never blocks a run,
changes an exit code, or fails one, and every error (offline, bad response) is silent. Nothing
about your repo, diff, or run is sent. Turn it off with `syncade --config set update.check false`,
or set `CI` in automation.

```bash
syncade --update        # upgrade, then re-run your command
```

`--update` works out how syncade was installed by looking for a marker inside its own install
tree — `uv`, `pipx`, or the `INSTALLER` record pip leaves — and **refuses rather than guessing**
if no marker proves one, printing the manual command instead. It also refuses while a review is still running, and declines to touch a source
checkout. A running process cannot switch to the version it just installed, so it exits and asks
you to re-run.

> **Installed before 0.6.3? You will never be told about updates.** The check itself arrived in
> **0.6.3**; every release before it — `0.1.0` through `0.6.2` — has no such code, so it cannot
> announce anything and `--update` is not a flag it knows. There is no way for us to reach those
> installs, which is why this paragraph exists. Check with `syncade --version`, and if it is below
> `0.6.3`, upgrade once by hand:
>
> ```bash
> uv tool install --force syncade          # if you installed with uv
> pipx upgrade syncade                     # if you installed with pipx
> python3 -m pip install --user -U syncade   # if you installed with pip --user
> python3 -m pip install -U syncade          # if you installed with pip
> ```
>
> **Check `syncade --version` afterwards either way** — do not trust the success message. Each
> of these can report success while changing nothing: `pipx` refuses a package you have
> `pipx pin`ned, `pip` may target a different interpreter than the one `syncade` resolves to,
> and on `0.6.3`/`0.7.0` `--update` itself could claim success having installed nothing (fixed
> in `0.7.1`). The `uv` line is the exception: it upgrades *and* clears any pin.
>
> From `0.6.3` on syncade tells you about updates itself — **when it can reach the manifest**.
> That check is silent on failure by design, so a machine that cannot reach it looks exactly
> like a machine that is up to date. If you want certainty, compare `syncade --version` against
> the [releases page](https://github.com/syncade-ai/syncade-ai/releases).

**Requirements**
- **Python 3.11+**, on **macOS or Linux** (on Windows, use WSL).
- **`codex`** (OpenAI), on your `PATH` and authenticated — required for the default
  reviewers and judge. **`claude`** (Anthropic) is additionally required if you run from
  inside Claude Code (where the default producer uses Anthropic); in a plain terminal or
  Codex, all actors resolve to OpenAI.
- `git`. (`lsof` is optional — only `syncade --gc` uses it.)

Check your setup any time with `syncade --doctor`.

**What a run costs.** Measured across 102 priced runs on this repo: **median $4.07**, 90th
percentile **$14.90**, worst observed **$35.50**. A clean single-round review lands nearer $2;
the expensive tail is multi-round loops where a producer rewrites code each round. If your
`claude` / `codex` CLIs are signed in to a subscription the marginal cost is **$0** — that is how
every run in this project has been paid for, which is precisely why these numbers are easy to
forget. On API billing it is real money. `syncade --doctor` previews the cost of the run you are
about to start, and `--budget-usd N` (or `[loop] budget_usd`) stops the loop at a ceiling instead
of reporting the damage afterwards.

## Quick start

```bash
cd your-git-repo
syncade --doctor --quick         # green/red readiness check (CLIs, worktree) — skips auth probe; no live provider calls
syncade path/to/brief.md         # run the review loop against a short markdown spec
```

Or, from inside Claude Code once the skill is installed:

```
/syncade path/to/brief.md
/syncade review what I just did          # drafts a spec from the session, then reviews
/syncade dogfood the brief for 2 rounds
```

From inside Codex (draft-from-session is a follow-up; supply a brief or OpenSpec):

```
/syncade path/to/brief.md
/syncade dogfood the brief for 2 rounds
```

syncade finds the repo root itself, writes all artifacts under `.syncade/runs/`
(gitignored), and **refuses to run on your default branch** unless you pass
`--allow-default-branch` — because the producer commits to the current branch.

For the full walkthrough — writing a brief the blind panel can use, keeping the change small enough
to converge, and the pre-review prompt that raises the floor — see
**[How to use syncade](docs/how-to-use.md)**.

## How it works

Each round of `syncade <brief>`:

1. **Snapshot** the repo — HEAD plus the diff under review.
2. **Reviewers** (two, blind, in parallel) investigate inside throwaway worktree copies and return structured findings.
3. **Cold synthesizer** — a third blind judge — consolidates them into one `findings.md` with a **mechanical verdict**; unanimous blockers can't be dismissed.
4. Optional **test / check legs** run in a clean worktree and fold into the verdict.
5. On **NO-SHIP**, a **producer** attempts a fix and commits; the branch fast-forwards and the next round begins.

It ships the moment a round is clean, or stops at `max_rounds` (default 5), a budget
ceiling, or a producer stall.

**Cross-prompt and cross-model.** Reviewer diversity is the point — a blind spot shared by every
judge is a blind spot in the verdict. The default panel is two `codex` (OpenAI `gpt-5.5`) reviewers
running *different prompts* — a standard reviewer and an adversarial one — so it's **cross-prompt** out
of the box. And every actor is now fully configurable: set the provider and model per reviewer to run
**cross-model / cross-lab** (e.g. one OpenAI reviewer + one Anthropic reviewer) whenever you want a
second lab's perspective.

**We recommend `codex` (OpenAI) models as your blind reviewers.** Not a shrug at the alternatives —
a measurement. On mixed panels in our own dogfooding, reviewing the *same diff in the same round*,
the OpenAI reviewer raised **1.18 findings per reviewer-round against the Anthropic reviewer's
0.38**; across the whole corpus it is 6.37 findings per run against 0.57. We offlined an Anthropic reviewer and reverted a
`gpt-5.6` panel after both audited too leniently.

Read that with its limits: one codebase, one language, and a small Anthropic sample (13
reviewer-rounds). Fewer findings is not automatically worse — it can mean fewer false positives —
so the leniency reading rests on the two reversals as much as on the ratio. It is enough for a
default, not enough for a law. Reach for cross-model deliberately; the config is there precisely
so you can test that recommendation against your own repo, and we would like to hear if it does
not hold.

## The verdict is an exit code

The verdict is mechanical — the LLMs never decide the exit code directly.

| Code | Meaning |
|---|---|
| `0`  | SHIP — or nothing to review (`termination_reason: no_changes_to_review` in `loop-manifest.json`) |
| `10` | Clarification or operator decision needed (see `decision-needed.md`) |
| `20` | Max rounds reached, still NO-SHIP |
| `25` | Stopped early, resumable — your budget ceiling, or the provider's usage limit |
| `30` | Findings present, test failed, or producer stalled |
| `40` | A subprocess failed |
| `50` | Config error |
| `60` | Environment / worktree / repo problem — also a refused run: the diff is unreadable, or too large for a reviewer to be asked to read (`diff_too_large` / `prompt_too_large`) |
| `70` | Reviewer or synthesizer output couldn't be parsed |

## Configuration

Zero-config works out of the box. To customize, add `.syncade/config.toml`:

```toml
[loop]
max_rounds = 5                  # recommended default; hard ceiling is 10
timeout_seconds = 1800          # per SUBPROCESS, not per round; see below
test_command = "pytest -q"      # optional third convergence leg
max_diff_bytes = 1_000_000      # refuse rather than review a diff this large (exit 60)

[[reviewers]]
name = "codex-reviewer"
provider = "openai"             # codex/OpenAI recommended for blind review; swap per reviewer for cross-model
model = "gpt-5.5"
thinking = "xhigh"
```

**Rounds & time budget.** We recommend **5 rounds**, and `timeout_seconds` around **~30 minutes**.

`timeout_seconds` is a cap on **each subprocess**, not on a round. It is the fallback wall clock for
every leg — each reviewer, the judge, the test run, each mechanical check, and the producer — so a
round's worst case is a multiple of it. With two reviewers (parallel, so they count once), a test
command and three checks, one round can run **7×** the configured value before anything times out.
Size it as "how long may a single model call take". The actual runaway guard is the token
ceiling, and it is **on by default** — `budget_tokens = 50000000`, which stops about 4% of runs
in our own corpus and is `--resume`-able when it does. Set `budget_tokens = 0` to remove it, or
add a `budget_usd` ceiling alongside. You can raise `max_rounds` up to **10**; the round cap
is a typo-guard, not a spend guard. Per-invocation: `syncade --max-rounds N`, `--budget-usd N`.

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
- syncade makes a small number of network calls of its own: a **once-per-session** GET to its public update manifest at the first invocation in each terminal or harness window (suppressible via `[update] check = false`, or set `CI`). `syncade --update` and `syncade --doctor` check every time you run them — operator-requested, so not session-gated. **At most one manifest GET per invocation** either way: a single `syncade` process fetches once and shares it, so `--doctor` does not add a request on top of the session check. `syncade --update` also invokes your package manager (`uv tool upgrade`, `pipx upgrade`, or your own interpreter's `-m pip install -U`) — only when you run that flag. All other egress is the provider CLIs you already authenticated and the test/check commands you configure.

Running syncade is comparable to running the target repo's `Makefile` — it executes code
with your permissions. See **[SECURITY.md](SECURITY.md)** for the full threat model and how
to report a vulnerability.

## Known limitations

Early access. These are measured, not suspected.

- **A clean verdict is evidence, not proof.** The panel is not deterministic: on two runs 25
  minutes apart over byte-identical code, one raised two blockers and the other shipped clean.
  Both findings were real. If a SHIP arrives over code you did not change since a NO-SHIP, treat
  it as the weaker signal — compare the verdict to what the *code* changed, not to the previous
  verdict.
- **Worktrees accumulate under `<worktree_base>`.** NO-SHIP runs keep their worktrees for
  inspection. `syncade --gc` removes worktrees once a run can no longer plausibly be resumed —
  controlled by `gc.worktree_max_age_days` (default 14 days), which applies even to runs that are
  technically still resume-eligible. It reached 4.4 GB on this machine before a cleanup. Point
  `worktree_base` somewhere you do not mind, and run `git worktree prune` after manual removals:
  removing the directory does not remove git's registration of it.
- **A hard-killed run keeps almost all of what its reviewers had written.** Reviewer stdout and
  stderr are copied to `<round>/<name>.stdout` / `<round>/<name>.stderr` as the child produces
  them, rather than held in memory until it exits, so a run ended by `SIGKILL` (or a machine
  going away) leaves those files behind instead of an empty directory. The honest limit: the
  guarantee is everything written *so far*, not every byte produced — a chunk still in flight
  when the parent dies is lost. Completed rounds are unaffected, and `--resume` picks up from
  the last one and reports that the previous run was hard-killed and in which phase.
- **The producer commits to your current branch.** That is the design — it fast-forwards only,
  refuses the default branch without `--allow-default-branch`, and prints every commit it made.
  Your working tree is *not* updated to match, so `git status` afterwards shows what looks like a
  staged revert; sync with `git stash && git reset --hard HEAD && git stash pop` rather than
  committing it. syncade warns about this and the warning is not suppressible.

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
