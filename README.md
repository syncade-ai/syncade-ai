# syncade

**A blind, multi-judge code-review orchestrator for AI-assisted coding.**

Point syncade at a short spec and it reviews your changes the way a careful team would: it
spawns fresh, isolated CLI subprocesses — two **blind reviewers**, a **cold synthesizer**
that consolidates their findings into one mechanical verdict, and, on NO-SHIP, a
**producer** that attempts a fix and commits it — looping up to three rounds until it ships
or runs out of budget. The verdict comes back in the same Claude Code or Codex session; you
never open another terminal or copy-paste between tools.

The reviewers never see each other's output, the producer's narrative, or your live
session. That process isolation is the core property, not an implementation detail.

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

**On diversity:** the shipping default panel is two OpenAI `gpt-5.5` reviewers running
*different prompts* — **cross-prompt, not yet cross-model**. Under Claude Code the producer
is Anthropic, so the panel does judge a different model than the producer wrote. A second
lab (a generic OpenAI-compatible adapter, then Gemini) is the next milestone.

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
max_rounds = 3
test_command = "pytest -q"      # optional third convergence leg

[[reviewers]]
name = "codex-reviewer"
provider = "openai"
model = "gpt-5.5"
thinking = "xhigh"
```

Every reachable knob — reviewers, producer, the cold actors, retry, GC, budgets — is in
**[docs/config-reference.md](docs/config-reference.md)**, and three bundled presets
(`--preset cheap | balanced | thorough`) cover the common cases.

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
