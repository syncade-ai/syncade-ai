# syncade

External blind multi-judge review orchestrator for
AI-assisted coding. Reviewers and producers run as fresh, isolated CLI
subprocesses — no shared process state, no live session inheritance —
with **cross-round-within-PR continuity replayed as input**:
within a single `syncade <pr-doc>` invocation each reviewer + the
producer see their OWN prior-round response text in the next round's
prompt. The synthesizer stays cold (no cross-round context). Across
PRs each invocation is a fresh lifecycle — cross-PR isolation is hard.
Findings surface back into the same Claude Code conversation. The user
never opens another terminal and never copy-pastes between tools.

## Status

v0.1.0, in active development. `syncade <pr-doc>` runs **the full
multi-round loop**: snapshot → reviewers → cold synthesizer → optional
test re-run → mechanical verdict → producer-if-NO-SHIP → fast-forward
the operator's branch → next round. Up to `max_rounds = 3` (default;
configurable in `[loop] max_rounds` or via the `--max-rounds INT` CLI
flag). The loop terminates as soon as a round SHIPs, OR `max_rounds`
rounds have run without SHIP (exit 20), OR a token/dollar budget ceiling
is hit (exit 25 + `budget_exceeded`), OR a producer subprocess fails
(exit 40) / stalls without committing (exit 30 + `producer_stalled`).

**On "diversity" — cross-prompt today, cross-lab next.** The shipping default panel is
two `openai` / `gpt-5.5` reviewers running *different prompts* — one lab, two prompts, so
it is **cross-prompt**, not cross-model. A blind spot shared by that model is a blind spot
in both judges. Restoring a second lab (a generic OpenAI-compatible adapter, then Gemini)
is the next milestone; until it lands, syncade is described as cross-prompt, not
cross-model. Under Claude Code the *producer* is Anthropic while the panel is OpenAI, so
the panel does judge a different model than the producer wrote — but the panel itself is
single-model today.

The earlier phases that compose into one round of the loop:
wired single-pass dispatch end-to-end; enriched the
reviewer schema with structured narrative fields (`summary`,
`priority_order`, `coverage_gaps`, `dismissed_concerns`); 
shipped the cold synthesizer (a third blind LLM that
consolidates the two reviewers' outputs into one `findings.md`
with per-reviewer provenance + a mechanical verdict computed from
`consolidated_findings`); shipped the opt-in third
convergence leg (`[loop] test_command` runs in a clean worktree
after synth-clean; failure folds into the verdict). wraps
all that in the loop + adds the producer subprocess + branch
advance + `loop-summary.md` / `loop-manifest.json` top-level
aggregates + the `--max-rounds` / `--force-dirty` CLI flags.

Single-pass back-compat: `max_rounds = 1` (CLI: `--max-rounds 1`)
recovers a PR's exact code path — no producer subprocess
provisioned, no branch advance, the pinning regression class
`TestTestReRunActive` stays green.

Current front doors include `--spec-audit`, `--resume`, `--scope`,
`--openspec`, `--draft-spec --transcript`, `--gc`, and `--metrics`
(a read-only cumulative report aggregating the `.syncade/runs/` corpus into
`.syncade/metrics.db`: run count, ship-rate, findings by severity, rounds,
handoffs, and per-model reviewer stats). See
this README for the full design
doc.

### Configuring the loop

`.syncade/config.toml` (full multi-round example with codex as
producer):

```toml
[loop]
max_rounds = 3              # 1 (single-pass back-compat) | 2 | 3
test_command = "pytest -q"  # opt-in third convergence leg

[producer]
provider = "openai"         # default follows the invoking harness (see below)
model = "gpt-5-codex"       # omit to get the provider's default model
thinking = "medium"         # see syncade.config.ReviewerConfig.thinking for accepted values
permissions = "yolo"        # only supported producer permission for unattended commits

[[reviewers]]
name = "claude-reviewer"
provider = "anthropic"
model = "claude-opus-4-6"

[[reviewers]]
name = "codex-reviewer"
provider = "openai"
model = "gpt-5.5"
thinking = "xhigh"          # higher-tier reasoning value (see syncade.config.ReviewerConfig.thinking)

# The three COLD actors. All three used to be hardcoded to codex; each is now
# resolved from the adapter registry, so none of them requires a particular CLI.
# Omit any block to get its default (the values below ARE the defaults).
[synthesizer]               # the judge, every round
provider = "openai"
model = "gpt-5.5"           # omit to get the provider's default model
thinking = "high"

[drafter]                   # syncade --draft-spec
provider = "openai"
thinking = "xhigh"

[auditor]                   # syncade --spec-audit
provider = "openai"
thinking = "xhigh"
```

### Which account gets billed (`auth`)

Every actor takes `auth = "auto" | "subscription" | "api"`. **This exists because the two
CLIs resolve credentials in opposite directions**, verified live:

| CLI | with a key exported | what actually happens |
| --- | --- | --- |
| `claude` | `ANTHROPIC_API_KEY` | **the key wins** — it overrides your claude.ai login |
| `codex` | `OPENAI_API_KEY` | **the key is ignored** — auth comes only from `codex login` |

So on one machine, in one run, one provider can bill your API account while the other
bills your subscription — silently, fanned out across N reviewers × M rounds.

- `subscription` — syncade **strips** that provider's key vars from the child env, so the
  CLI physically cannot reach the API.
- `auto` (default) — whatever the CLI would do anyway, so nothing changes for anyone.
- `api` — **and this differs by provider, because the CLIs do:**

| provider | how `auth = "api"` is satisfied | `api_key_env` |
| --- | --- | --- |
| `anthropic` | the key must be **in the environment**. Missing ⇒ **exit 50 at config load**, not mid-run. Syncade routes it to `ANTHROPIC_API_KEY` (the only var `claude` reads). | **supported** — name your own var (e.g. a work key), and syncade maps it across |
| `openai` | `printenv OPENAI_API_KEY \| codex login --with-api-key`. **No env var is required or read** — codex takes its key from `CODEX_HOME`. Syncade probes `codex login status` and **refuses to start** if that contradicts your config. | **rejected** — it cannot be honoured, so it is a config error rather than a silent no-op |

  That asymmetry is not an oversight: setting `OPENAI_API_KEY` does *nothing* for codex,
  so demanding it would be a requirement you could satisfy and still be wrong.

**Every run prints which account is about to pay, even under `--quiet`:**

```
[syncade] auth:
  anthropic  → api          billed to your API account
               (ANTHROPIC_API_KEY is set and OVERRIDES your claude.ai login)
  openai     → subscription billed to your subscription — $0 marginal
               (codex is logged in with ChatGPT; OPENAI_API_KEY is ignored)
```

`auto` is only safe *because* it is announced. Silence was the bug.

### Cost reporting: money vs valuation

`--metrics` separates **`billed`** (money that left your account — `auth = "api"` traffic
only) from **`API-equiv`** (what the same traffic would cost at API list price). A
subscription run bills **$0 marginal**; the API-equivalent figure is what your plan is
worth, not what you spent. Runs from before this existed are reported as `unclassed` —
never silently counted as free.

In the blocks that have a default model — `[producer]` and the three cold actors —
`provider` and `model` move as a **pair**: setting `provider` alone re-derives
`model`, so a `provider = "anthropic"` can never hand a `gpt-5.5` to `claude`.
Set `model` explicitly whenever you want to pin something off the default map.
(`[[reviewers]]` needs no such rule: a reviewer's `model` is **required**, so
there is no default to inherit and nothing to re-derive.)

The `thinking` value `"xhigh"` is a higher
reasoning-effort tier above `"high"`. Both providers accept it at
the CLI level: codex passes it through as
`-c model_reasoning_effort=xhigh`; claude accepts `--effort xhigh`
directly (verified live against claude 2.1.152 and codex 0.134.0).
All four syncade adapters route the value verbatim — no
per-provider rejection. The CLI also accepts `max`, the top tier used
for Anthropic's highest reasoning-effort runs.

Each reviewer can also pin a specific prompt template with `template =
"reviewer_adversarial.md"` — a plain basename that overrides the provider's
default template (resolved per-repo `.syncade/templates/<name>` override →
packaged default). This decouples the prompt from the provider, so e.g. a
`codex` reviewer can run the adversarial acceptance-audit prompt. The zero-config
default roster is two Codex reviewers — `codex-reviewer` (default prompt) and
`codex-reviewer-adv` (`reviewer_adversarial.md`) — with the Anthropic reviewer
offlined but revivable via a `[[reviewers]]` block.

### Zero-config defaults are harness-aware (producer only)

With no `.syncade/config.toml`, the roles resolve like this:

| Role | Claude Code harness | Codex harness (and no harness) | Permissions |
| --- | --- | --- | --- |
| Producer | `anthropic` / `claude-sonnet-4-6` / `medium` | `openai` / `gpt-5.6-terra` / `medium` | `yolo` (forced) |
| Reviewers ×2 | `openai` / `gpt-5.5` / `high` | same | `trusted-execute` |
| Judge (synthesizer) | `openai` / `gpt-5.5` / `high` | same | `trusted-execute` |
| Drafter (`--draft-spec`) | `openai` / `gpt-5.5` / `xhigh` | same | `trusted-execute` |
| Auditor (`--spec-audit`) | `openai` / `gpt-5.5` / `xhigh` | same | `trusted-execute` |

Only the producer is harness-aware. The judge, drafter, and auditor are
deliberately **not**: a verdict has to stay comparable across runs regardless of
which harness you happened to be coding in. All three are configurable
(`[synthesizer]` / `[drafter]` / `[auditor]`) and none requires a particular CLI —
an all-Anthropic config completes a full loop on a machine with no `codex`
installed, and vice versa.

The reviewers and the judge share a model pin on purpose — keep them in sync. A
panel on `gpt-5.6-sol` @ `medium` was tried and reverted after the
measurement showed it making ~18 tool calls per round against
`gpt-5.5` @ `xhigh`'s 90–101, with the plain reviewer shipping 2 of 2 rounds at
**zero findings** while only the adversarial one caught anything.

Every tier above runs **fully unattended** — nothing in the default path ever
prompts. `trusted-execute` is `-s workspace-write -c approval_policy=never` on
Codex and `bypassPermissions` on Anthropic; it keeps the OS sandbox active and
scoped to the subprocess's worktree while never asking a human anything. It is
the reviewer default because reviewers only read the repo and run its
test/lint commands inside their worktree, so worktree confinement may as well be
enforced structurally rather than by asking the model to stay put. `yolo`
(Codex's `--dangerously-bypass-approvals-and-sandbox`) turns that sandbox off and
buys a reviewer nothing. The producer is the exception: it is `yolo`-only,
because a sandboxed Codex cannot write `.git/index.lock` and so cannot commit.
`safe` is rejected by the real adapters — it prompts, which hangs a headless run.

Only the **producer** follows the harness you are coding in, detected from
`CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID` (Claude Code wins if both are set;
neither falls back to the Codex shape). The reviewers and the judge stay pinned
to one cold OpenAI tier on purpose: they are the blind panel, and a verdict
should not depend on which editor the operator happened to have open. It also
keeps the panel judging a different model than the producer wrote under Claude
Code — the model-diversity the loop still gets on that axis, even while the panel
itself is single-model today (see Status).

With the default roster, reviewers and the judge use OpenAI, so `codex` auth
is required; `claude` auth is additionally required only under Claude Code.
If you configure cold actors to use a different provider, that provider's
CLI auth applies instead. `syncade --auth-check` probes **every** unique
credential your resolved config needs — across `[[reviewers]]`, `[producer]`,
and the cold actors (`[synthesizer]`, `[drafter]`, `[auditor]`), deduped by the
credential each actor presents (not by provider).

Covering the cold actors matters more than it sounds: the judge runs every
round, so an all-Anthropic reviewer/producer config still needs `codex` auth
for its (default OpenAI) judge. Probing only reviewers + producer returned a
green check on a machine that could not finish a run — and you'd discover that
only after both reviewers had already run and billed. Auth is per-token, not
per-model, so covering the cold actors costs nothing unless you actually gave
one a different credential.

Setting `[producer] provider` alone re-derives `model` for that provider, so the
pair can never disagree; set `model` explicitly to pin a different tier.

### Commit history note

When `max_rounds > 1`, **syncade advances your branch**. The
producer commits land on the operator's named branch via
`git update-ref` (fast-forward only). Inspect the
`loop-summary.md`'s commit series after each run to see what
landed. To roll back if you don't want to keep the loop's
commits: `git reset --hard <round-0-starting-sha>` (the starting
SHA is the first entry in the commit series).

The producer's `git commit` requires headless approval. The supported
producer permission is `yolo`, which maps to Claude's `bypassPermissions`
and Codex's `--dangerously-bypass-approvals-and-sandbox`.

## Security

Syncade runs AI coding-agent CLIs with **elevated tool access on your repo**, so before
you install it, know the shape of that:

- **Reviewers** run headless with shell/tool access, confined to a **throwaway worktree**
  copy under `<worktree_base>/` (default `/tmp/syncade/`; Codex reviewers are additionally
  sandboxed to it). Configure `worktree_base` in `.syncade/config.toml` or `--worktree-base`.
- **The producer** runs **fully unsandboxed** (`bypassPermissions` /
  `--dangerously-bypass-approvals-and-sandbox`) by necessity — a sandbox can't write
  `.git/index.lock` to commit — and **commits to your current branch**. Syncade **refuses
  the default branch** unless you pass `--allow-default-branch`, and announces the target
  branch first.
- Syncade writes **full LLM transcripts** (including source the reviewer read) to
  `.syncade/runs/**/*.stdout` — **gitignored by default** and auto-pruned. **Secrets in
  files a reviewer opens land in those transcripts in plaintext.**
- Syncade makes **no network calls of its own** — no telemetry; its own egress is the
  provider CLI you already authenticated. It **does** run the shell commands you configure
  (`[loop] test_command`, `[[checks]]`) through the shell each round — *your* commands, with
  whatever side effects and egress they have.

Running syncade is comparable to running the target repo's `Makefile` — it executes code
with your permissions. **See [SECURITY.md](SECURITY.md)** for the full threat model, the
data-handling details, and how to report a vulnerability.

## Install

```bash
pip install syncade      # or: uv pip install syncade
```

Requires **Python 3.11+** and the provider CLIs you configure on your `PATH`
(`claude` for Anthropic actors, `codex` for OpenAI actors), each already
authenticated. Then install the skill for your harness so `/syncade` works —
see [the skill install](#install-the-skill) below. **Platform:** macOS and Linux;
Windows is not supported (see [Platform support](#platform-support)).

### Development install

To work on syncade itself (editable, with the dev gate tooling):

```bash
uv venv --seed
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Global install (invoke from any project)

The commands above are a repo-local editable install — use it via `uv run
syncade …` from inside this checkout. To run `syncade` from any directory (for
example, to review other projects), put it on your `PATH` as an editable tool:

```bash
uv tool install --editable .   # `syncade` on ~/.local/bin; tracks this checkout
```

`--editable` keeps the tool pointed at this checkout, so your edits take effect
without reinstalling. Because it stays tied to this directory, moving or deleting
the repo breaks the command. (Pin the interpreter with `--python 3.11` to match
CI's primary if you like.)

### Install the skill

The `syncade` CLI is harness-agnostic, but the ergonomic front door — resolve a
natural-language request → pre-flight safety checks → confirmation gate → stream
the loop → inline the verdict — ships as an Agent Skill for **both** Claude Code
and Codex. The skill is markdown + Bash over the CLI above; it adds no Python.

**One command** installs it into your harness skill directories — it works from a plain
`pip install` (the skill is bundled in the package, no checkout needed):

```bash
syncade --install-skill             # both harnesses; or: --install-skill claude | codex
```

It copies the skill to `~/.claude/skills/syncade/` and
`${CODEX_HOME:-~/.codex}/skills/syncade/` (re-run any time to update). From a repo checkout
the equivalent is `scripts/install-skill.sh`. Then restart the harness — skills load at
session start — and make sure the `syncade` CLI is on your `PATH` (the Global install
above).

Developers who want the installed skill to *track* this checkout can symlink instead:

```bash
ln -sfn "$PWD/.claude/skills/syncade" ~/.claude/skills/syncade
ln -sfn "$PWD/.codex/skills/syncade"  "${CODEX_HOME:-$HOME/.codex}/skills/syncade"
```

The two copies are the **same** skill: their load-bearing Step 2 → Invariants workflow
span is single-source and kept byte-identical by `tests/skills/test_skill_drift.py` (edit
both copies together).
The heads differ only where the harnesses do — invocation phrasing, spec-tier
handling (the Codex copy's draft-from-session lands in a follow-up), and
`CLAUDE.md` vs `AGENTS.md` as the project-instructions file.

### External prerequisites

Syncade is a Python package, but real review runs also depend on local
operator tools:

- Python 3.11+ and `uv` for the editable install and local gate commands.
- `git` on `PATH`; syncade snapshots commits, provisions worktrees, and
  fast-forwards the operator branch through git.
- `claude` and `codex` on `PATH`, authenticated, for the default real provider
  reviewers/producers and the smoke suite. Missing or unauthenticated provider
  CLIs are release-blocking for real-provider validation; do not count them as
  a passed or skipped release gate.
- `lsof` for `syncade --gc` orphan-subprocess reaping. If it is unavailable,
  GC still plans/removes eligible run directories, but reaping is skipped with a
  warning.

## Platform support

**macOS and Linux only.** On Windows, use WSL (which is Linux). Syncade is developed and
CI-tested on macOS and Linux and is not tested on native Windows; several paths assume
POSIX:

- worktrees default to `/tmp/syncade/` (a POSIX temp path);
- subprocess teardown kills the process group via `os.killpg` / `os.getpgid`;
- `syncade --gc` reaps orphaned subprocesses with `lsof`.

Some of these degrade rather than crash — `--gc` skips reaping with a warning when `lsof`
is absent, and `SIGHUP` handling is feature-guarded — but native Windows is unsupported.
The PyPI classifiers reflect this (`Operating System :: MacOS` and `POSIX :: Linux`, no
Windows).

## Usage

Run against a PR doc from inside a git working tree (or any
subdirectory of one — syncade discovers the repo root itself). If the
directory is not yet a git repo, the main review path **initializes
one for you** and makes a baseline commit (writing an ignore floor
that keeps secrets and generated run state out of that commit) rather
than refusing:

```bash
# On a FEATURE branch — syncade refuses the default branch unless --allow-default-branch.
$ syncade path/to/pr.md
[14:02:11] snapshotting repo at /home/you/project
[14:02:11] snapshot taken — 3f9a1c2b4d5e on fix-search-timeout
[14:02:11] producer commits will land on: fix-search-timeout
[14:02:11] round 0: provisioning 2 worktree(s) under /tmp/syncade/2026-05-12T17-04-22
[14:02:12] round 0: dispatching 2 reviewer(s) in parallel (timeout 1800s each)
[14:05:31] round 0: persisting reviewer outputs to .syncade/runs/2026-05-12T17-04-22/round-0
[14:05:55] round 0: synthesizer clean? no — 2 active blockers; dispatching producer
[14:08:42] round 0: producer committed cb1a429 on top of 3f9a1c2b4d5e; advancing refs/heads/fix-search-timeout
[14:08:43] round 1: provisioning 2 worktree(s)
[14:08:43] round 1: dispatching 2 reviewer(s) in parallel
[14:11:55] round 1: persisting reviewer outputs to .syncade/runs/2026-05-12T17-04-22/round-1
[14:12:18] round 1: synthesizer clean? yes — SHIP
[14:12:18] run complete
  run id:    2026-05-12T17-04-22
  exit code: 0
  rounds:    2 (terminated: ship)
  branch:    refs/heads/fix-search-timeout advanced 3f9a1c2b4d5e → cb1a429
             NOT automatically synced — run `git reset --hard cb1a429`
             to pick up the producer's fixes in your working tree.
  artifacts: .syncade/runs/2026-05-12T17-04-22
  summary:   .syncade/runs/2026-05-12T17-04-22/loop-summary.md
$ echo $?
0
```

When `--repo-root PATH` is supplied, relative input paths such as `PR_DOC`,
`--spec-audit`, and `--transcript` are resolved against the discovered repo
root first, with a cwd-relative fallback warning for compatibility.

`--quiet` suppresses the informational phase-level progress; the final
summary line still prints to stdout. Exit 70 also prints one concise
`.stdout` / `.error.txt` pointer line per unparseable reviewer, and any
reviewer failure/error lines still go to stderr — a broken reviewer is
never silent.
`--timeout <SECONDS>` overrides the per-reviewer wall-clock cap
(default 1800s / 30 min, also settable via `[loop] timeout_seconds` in
`.syncade/config.toml`):

```bash
$ syncade --quiet --timeout 3600 path/to/pr.md
[syncade] run complete — exit 30, summary at .syncade/runs/2026-05-12T17-04-22/round-0/summary.md
```

The exit code follows
this contract:

- `0` — synth consolidated everything, no non-dismissed blockers in
  `consolidated_findings`. With `[loop] test_command` set
  (opt-in), additionally requires the test re-run to
  pass (three-legged convergence).
- `25` — a configured token/dollar budget ceiling
  (`--budget-tokens` / `--budget-usd`) was crossed; the loop aborted
  gracefully before spending more. Distinct from 20 so a
  script can tell "hit my cost ceiling" from "ran out of rounds," and
  `--resume`-eligible (the crossing round is persisted whole).
- `30` — either synth surfaced at least one non-dismissed
  blocker, OR the opt-in test re-run leg exited
  non-zero. The mechanical verdict OR's both signals.
- `40` — a reviewer OR synthesizer OR test-leg
  subprocess failed (auth, network, model unavailable, timeout,
  CLI not on PATH; any `ReviewerInvocationError` /
  `SubprocessError` subclass). Transient provider blips (HTTP
  429/5xx, dropped sockets) are first retried with jittered backoff
  (up to 2 extra attempts) for the reviewer, synthesizer, and
  producer legs (the producer's retry is side-effect-safe);
  the per-round `manifest.json` records the total in `retried`.
- `50` — config error (typo in `.syncade/config.toml`, unknown
  provider)
- `60` — worktree or snapshot error (bogus `--base` ref, worktree
  provisioning failed, loop-mode dirty-tree refusal, or "not in a git
  repo" from a diagnostic mode — the main review path auto-initializes
  instead of erroring here)
- `70` — reviewer OR synthesizer output unparseable (the test
  leg has no parse path — only a raw exit code — so this code
  never fires for it)

Mixed-failure precedence on the reviewer phase: 60 > 50 > 70 > 40
(one `ReviewerOutputError` plus one `ReviewerInvocationError`
returns 70). The synthesizer phase runs only when every reviewer
succeeded; its failures map to the same 70/40 buckets but the
persisted `synthesizer.error.txt` names the phase so you open the
right `.stdout`.

Per-run artifacts land at `<repo>/.syncade/runs/<run-id>/`,
with one `round-N/` subdirectory per round of the loop plus
top-level loop aggregates:

```
.syncade/
└── runs/
    ├── .gitignore                          # auto-written: `*`
    └── 2026-05-12T17-04-22/
        ├── loop-summary.md                 # top-level loop dashboard (rounds, commit series, termination reason)
        ├── loop-manifest.json              # top-level loop manifest (per-round status + producer outcomes, for tooling)
        ├── findings.md                     # mirror of the latest round's findings.md, for quick `cat .syncade/runs/<id>/findings.md`
        ├── round-0/
        │   ├── manifest.json                  # round-level summary (for tooling)
        │   ├── summary.md                     # human-readable round summary
        │   ├── findings.md                    # consolidated review (synth success only)
        │   ├── claude-reviewer.stdout         # raw subprocess output
        │   ├── claude-reviewer.stderr
        │   ├── claude-reviewer.parsed.json    # ReviewerOutput as JSON (success)
        │   ├── claude-reviewer.error.txt      # exception + traceback (failure)
        │   ├── codex-reviewer.stdout
        │   ├── codex-reviewer.stderr
        │   ├── codex-reviewer.parsed.json
        │   ├── synthesizer.stdout             # cold-Codex synth subprocess output
        │   ├── synthesizer.stderr
        │   ├── synthesizer.parsed.json        # SynthesizerOutput as JSON (success)
        │   ├── synthesizer.error.txt          # exception + traceback (failure)
        │   ├── test-run.stdout                # test leg stdout (when configured)
        │   ├── test-run.stderr
        │   ├── test-run.exit-code.txt         # one-line int (0 / positive / -1 on subprocess error)
        │   ├── producer.stdout                # producer subprocess narrative/raw output (when NO-SHIP triggers a producer)
        │   ├── producer.stderr
        │   ├── producer.commit.txt            # producer ending SHA, one line
        │   └── producer.error.txt             # exception + traceback (failure)
        └── round-1/                        # present when round 0 was NO-SHIP and the producer committed; same layout
            └── ...
```

The top-level `findings.md` always mirrors the latest round's
`round-N/findings.md`. When a single-pass run ends at
round 0, the mirror is the round-0 findings; when the loop ran
two rounds the mirror is round-1's; etc. Tooling and operators
can shell-grep `<run-dir>/findings.md` without having to know
which round terminated.

Every operator-facing artifact (`findings.md`, `loop-summary.md`,
`handoff.md`) carries a SHA header in its top block so
each file is self-contained when read asynchronously — open a
handoff.md a week after the run, and the file itself tells you
the commit SHA the findings are against without having to grep
`manifest.json` or correlate against `git log`. Per-round
`findings.md` carries that round's snapshot SHA; the top-level
mirror carries the latest round's SHA via `shutil.copy2`;
`loop-summary.md` carries the operator's pre-loop "Round 0
starting SHA" in the headline plus every per-round SHA in the
commit-series section; `handoff.md` carries the final round's
snapshot SHA (the state the remaining blockers are against).

### Configuring the test re-run leg

By default, syncade verdicts come from the reviewers + the
synthesizer alone — exit 0 means "reviewers agreed and the
synthesizer is clean." To opt into three-legged convergence (also
requiring an independent test re-run to pass), add a one-line
block to `.syncade/config.toml`:

```toml
[loop]
test_command = "pytest -q"
# test_timeout_seconds = 300   # optional; defaults to reuse `timeout_seconds`
```

When `test_command` is set:

- After every reviewer succeeds AND the synthesizer is clean, the
  orchestrator provisions one more clean worktree (same
  `WorktreeManager` mechanism + `CLAUDE.md` / `AGENTS.md` stripped
  as reviewer worktrees) and runs the command via `sh -c`. Pipes,
  env exports, and `&&` chains all work (e.g.
  `"npm test && playwright test"`).
- Test passed (exit 0) → exit 0. Test failed (non-zero) → exit 30.
  Test subprocess failed (binary missing, timeout) → exit 40.
- The test command's full stdout / stderr / exit code land at
  `<round_dir>/test-run.{stdout,stderr,exit-code.txt}` for the
  operator to inspect. `manifest.json` carries a `test_run`
  section; `summary.md` has a `## Test Suite` subsection;
  `findings.md` puts a `## Test Suite` section at the top when
  the leg fired (so the operator sees the test outcome before
  the synth's consolidated findings).

When `test_command` is unset (default), the test leg is skipped
entirely and exit 0 reflects synth-clean only — previously
behavior preserved. **shell=True is intentional**: the command
comes from your own config file, not from untrusted input (same
threat model as a Makefile or a `package.json` `"scripts"`
entry).

`findings.md` is the operator-facing consolidated review report —
one entry per `ConsolidatedFinding` with per-reviewer provenance,
active/dismissed status, severity-change rationale, original
verbatim descriptions, and dismissal rationale. Recommended next
read on exit 0 (no blockers) and exit 30 (active blockers present).
`summary.md` is the run-level dashboard: exit code, each reviewer's
verdict or error, the synthesizer's outcome, links to the
per-reviewer + synthesizer files, exit-code-specific next steps.
`manifest.json` carries the same facts in machine-readable form for
tooling.

The orchestrator auto-writes
`<repo>/.syncade/runs/.gitignore` on first run (content: `*`) so an
accidental `git add -A` after a review doesn't sweep run history
into source control. Pre-existing custom `.gitignore` files at
that path are preserved. When the main review path initializes a new
repo (see Usage), the baseline ignore floor it writes to
`.git/info/exclude` also covers `.syncade/secrets.*`, `.syncade/runs/`,
and `.syncade/last-reviewed.json`, while keeping `.syncade/config.toml`
trackable.

Common errors:

- **`syncade pr.md` against a non-git directory** → the main review
  path **initializes a repo** and makes a baseline commit, then
  proceeds (no exit 60). The diagnostic modes (`--resume`,
  `--selfcheck`, `--auth-check`, `--spec-audit`, `--gc`) keep the
  hard-stop: exit 60, stderr `[syncade] snapshot error: ... is not
  inside a git repository`.
- **`syncade --base bogus-ref pr.md`** → exit 60, stderr names the
  bad ref. The `--base REF` flag is optional; without it, reviewers
  run against the full HEAD state with no diff in the prompt.
- **Stale `.syncade/config.toml` with a typo** → exit 50, stderr
  names the offending field.

### Pre-flight diagnostics

Two opt-in diagnostics let you verify your environment before
firing a real review loop. Both are cheap to re-run, and both
exit `0` when healthy / `60` when broken (same exit-60 bucket as
"worktree error" — semantically "your environment isn't ready").

| Flag | Time | Scope | When to use |
|---|---|---|---|
| `--auth-check` | ~5–10 s | One probe per unique configured CREDENTIAL (provider + auth mode + key); verifies the OAuth/API token works | Before every review run as a habit, especially after a long pause. Cheaper than selfcheck. |
| `--selfcheck` | ~30 s | One real producer subprocess against a throwaway repo; verifies the producer can headless-commit | After a `claude` or `codex` CLI update, or whenever you suspect sandbox-semantics drift. |
| `--spec-audit PR_DOC` | ~30–90 s | Single cold subprocess auditing the PR brief for spec-level issues (unverified claims, contradictions, ambiguous acceptance criteria, scope drift, etc.); provider from `[auditor]` config | Before a real review loop to catch cheap upstream issues. Advisory — exit 10 (NEEDS-CLARIFICATION) surfaces blockers but the operator decides whether to proceed. |

The skill bridge (`/syncade <pr-doc>` from inside Claude Code) runs
`--auth-check`, then `--selfcheck` as the pre-flight before the real loop
fires; `--spec-audit` is a separate manual diagnostic, NOT part of the skill
flow. Operators using the terminal directly are encouraged to run the
pre-flights when context warrants.

#### `--auth-check`

For each unique credential in `[[reviewers]]` + `[producer]`
(deduped by the credential each actor presents, not by provider),
`--auth-check` runs a cheap auth probe and reports the result:

- **anthropic:** `claude -p "respond with exactly: AUTH OK"
  --output-format json --model <model>`. Healthy auth responds in
  well under 5 s with `is_error: false` and the sentinel in the
  response.
- **openai:** `codex login status` (filesystem-only, no network
  call). Healthy auth completes in well under 1 s.

```bash
$ syncade --auth-check
[syncade] auth-check: probing 2 credential(s)
[auth-check] anthropic: OK (3.7s)
[auth-check] openai: OK (0.2s)
[syncade] auth-check OK: 2 credential(s) verified in 3.9s
$ echo $?
0
```

Failure surface — both providers' failures land on stderr (even
under `--quiet`) so a CI environment never misses the diagnostic:

```
[auth-check] anthropic: FAILED (rc=1, api_error_status=401): Invalid API key. Run 'claude' interactively to re-authenticate.
[auth-check] openai: OK (0.3s)
[syncade] auth-check FAILED: one or more credentials could not authenticate. See the per-credential detail above; re-authenticate before invoking syncade for a real review.
```

Exit codes:

- `0` — every probe succeeded.
- `60` — one or more probes failed; see the per-credential detail on
  stderr.

Mutually exclusive with `<PR_DOC>`, `--selfcheck`, `--resume`,
and `--spec-audit`. Same `--quiet` semantics as `--selfcheck`:
silences stdout progress but not stderr failure output.

#### `--selfcheck`

`syncade --selfcheck` runs in ~30 seconds and verifies the
configured `[producer]` adapter can make a headless commit. It
provisions a throwaway `tmp_path` git repo with a one-line seed
file, renders a stub `findings.md` instructing the producer to
add a marker comment + commit, and asserts that the producer's
HEAD moves AND the marker text is present afterward.

```bash
$ syncade --selfcheck
[syncade] selfcheck: provisioning producer worktree (anthropic, model=claude-sonnet-4-6, permissions=yolo)
[syncade] selfcheck: dispatching producer (timeout 1800s)
[syncade] selfcheck OK: producer committed with marker (4f3e2d1a8b7c, 21.4s)
$ echo $?
0
```

Useful after a `claude` or `codex` CLI update to surface
sandbox-semantics drift early — a PR discovered that sandboxed producer
modes cannot complete unattended commits, which is why the producer
permission is `permissions="yolo"`. If a future provider release tightens
these semantics further, `--selfcheck` catches
it before a real review loop hits the same wall.

Exit codes:

- `0` — producer committed with the marker; producer can run
  headlessly in your current configuration.
- `30` — producer committed but didn't follow the marker
  instruction. Producer ran headlessly but isn't reading
  `findings.md` correctly (rare; usually means the model returned
  a non-instruction-following response).
- `60` — producer subprocess error, stall (no commit), or
  worktree provisioning failure. Inspect the preserved workspace path
  printed to stderr.

`--selfcheck` is mutually exclusive with `<PR_DOC>`, `--resume`,
`--auth-check`, and `--spec-audit`.

If a producer subprocess fails after moving HEAD (for example, it commits and
then times out), syncade still exits through the subprocess-error path but records
the observed ending SHA in `producer.commit.txt`, marks `manifest.json` with
`indeterminate_commit`, and calls it out in `summary.md` / `producer.error.txt`.
The operator can inspect that commit with `git show <sha>`; the branch is not
advanced on a failed producer outcome.

### Using syncade from inside Claude Code

The Claude Code skill at `.claude/skills/syncade/SKILL.md` lets operators
invoke syncade from inside Claude Code via a slash command. It is a
Bash-orchestration wrapper: it resolves the request into an exact command,
runs two safety pre-flights, asks for confirmation, runs the command, then
reads `<run-dir>/loop-summary.md` and presents the verdict inline.

**Make it available in every project.** Install it into your user-level skills
directory with `syncade --install-skill` (works from a `pip install` — see
[Install the skill](#install-the-skill) above). It is also bundled in the repo at
`.claude/skills/syncade/`.

Skills load at session start — restart Claude Code (or open a session in the
target project) to pick it up. The skill shells out to the `syncade` CLI on your
`PATH` (see **Global install** above) and operates on the current project's git
repo and its `.syncade/config.toml`, or the shipped defaults if the project has
none. Both `codex` and `claude` must be authenticated (the default roster is two
Codex reviewers with an Anthropic producer).

**Structured and natural-language invocation both work:**

```
/syncade path/to/pr.md
/syncade review path/to/pr.md for 2 rounds against main
/syncade dogfood a PR single pass
```

Natural language maps onto the CLI shape — `syncade [--openspec [ID] | PR_DOC]
[--base REF | --scope SCOPE] [--max-rounds N]` — at the skill (markdown) layer
only; the skill adds no Python of its own (PR-B added `--scope`, PR-C added
`--openspec`, PR-D added `--draft-spec`, the flags it targets). The skill asks one
concise question when the target, round count, or base is ambiguous, **maps scope
phrases** ("review what I just did" → `--scope local`, "everything" → `--scope
everything`, "since last review" → `--scope since-last-review`), and **maps
OpenSpec intent** ("review the openspec change <id>" → `--openspec <id>`) — Python
derives the concrete base / assembles the spec, the skill never hand-picks one.

**The single pane (PR-E).** Step 0 resolves which of three spec **tiers** applies,
all converging on the same loop: **A** a formal brief (a path), **B** an OpenSpec
change (`--openspec`), or **C** *no spec* ("review what we did this session") —
where syncade drafts one from the session transcript, you **ratify it in the same
chat pane** (it shows the draft + its flagged assumptions verbatim and asks "what
did I miss?"; your corrections edit the spec), and then the blind loop runs. One
request, one ratification step (tier C only), then the review — without leaving
Claude Code. A manufactured spec is never reviewed against unratified.

Workflow:

0. **Resolve intent** — natural language → the tier (A brief / B `--openspec` /
   C draft-from-transcript). Ask if ambiguous; never silently draft.
1. **Validate** the resolved PR doc is readable (and any explicit base ref).
1.5. **Ratify** (tier C only) — present the manufactured spec + its verbatim
   "Assumptions to confirm"; the operator's corrections edit it before the loop.
   Tiers A/B skip this (already authoritative).
2. **`--auth-check`** (~5–10 s). Non-zero → report and stop.
3. **`--selfcheck`** (~30 s). Non-zero → report and stop.
4. **Confirm** — print the pre-flight summary + the EXACT resolved command +
   expected timing + the "producer may commit to your branch" caveat; wait for
   `go` / `cancel`. The cancel gate is the last off-ramp before ~15–45 min of
   wall-clock fires.
5. **Run the resolved command** and stream output inline.
6. **Read `loop-summary.md`** and surface verdict + round count.
7. **If the producer ran**, list each commit (short SHA + subject); `git show
   <sha>` to inspect, `git reset --hard <round-0-sha>` for full rollback.

There is **no automatic spec-audit** in the skill flow — spec audit is a manual
terminal diagnostic (`syncade --spec-audit <pr-doc>`), not part of the skill.

The skill is opt-in. The interactive Claude reading the skill is
the operator's UI — not a syncade reviewer. Syncade's reviewer/
producer subprocesses still run with full process isolation from
the interactive session; the skill doesn't violate the blind-cross-
model invariant. Operators preferring the terminal can keep
running `syncade <pr-doc>` directly — same loop, no skill layer.

See `.claude/skills/syncade/README.md` for the operator-facing
description (when to use vs. terminal, prerequisites, failure
modes, producer-commit blast radius).

### First run

A few things worth knowing the first time you run syncade for real:

- **Reviewers timing out.** A thorough multi-judge review can take
  10-30 minutes. The default per-reviewer cap is 1800s (30 min); if a
  reviewer is SIGKILL'd you'll see `exit 40` and a
  `SubprocessTimeoutError` in its `.error.txt`. Raise the cap with
  `--timeout <seconds>` or `[loop] timeout_seconds` in
  `.syncade/config.toml`. Even on a timeout, whatever the reviewer
  produced before the kill is preserved in its `.stdout` / `.stderr`.
- **Artifacts landing somewhere unexpected.** syncade always writes
  `.syncade/runs/` to the **git repo root**, discovered via
  `git rev-parse --show-toplevel` — not to whatever subdirectory you
  invoked it from. If you can't find a run, look at the repo root; the
  `artifacts:` / `summary:` lines syncade prints on completion give
  the exact paths.
- **Reading the result.** Open `<run-dir>/round-0/summary.md` — a
  human-readable digest of the run: exit code, each reviewer's verdict
  or error, links to its per-reviewer files, and exit-code-specific
  next steps. `manifest.json` next to it is the same facts for tooling.
- **Quiet runs.** Pass `--quiet` to suppress the informational
  phase-by-phase progress. On a clean (or non-parse-error) run the
  end-of-run summary collapses to a single stdout line pointing at
  `summary.md`. On exit 70, that summary line is followed by one
  concise stdout pointer line per `ReviewerOutputError` reviewer
  naming the `.stdout` (raw response) and `.error.txt` (parse
  exception) paths — quiet stays bounded but never silent on
  exit 70. Any reviewer failure/error lines still go to stderr
  regardless of verbosity — a broken reviewer is never silent.
- **Dirty working tree.** Reviewers always see HEAD only, so any
  changes not yet at HEAD are invisible to them. a PR splits the
  dirty-tree warning into two operationally-distinct cases (these
  apply to **single-pass mode**, `--max-rounds 1` or
  `[loop] max_rounds = 1`):
  - **Strong warning** (tracked-modified): "working tree has
    uncommitted modifications to tracked files — reviewers will
    only see HEAD; your local changes are invisible to them.
    Commit before running syncade if you want them reviewed." This
    is the actually-dangerous case — you have local code changes
    the reviewers can't see.
  - **Soft note** (untracked-only): "working tree has untracked
    files (not reviewed): N files. These are invisible to
    reviewers, which is usually intentional. Run 'git status' to
    see them." Usually fine — scratch files, in-progress notes,
    things you're keeping out of git on purpose. The count helps
    you quickly check it matches your expectation.
  - Both can fire on the same run when the tree has tracked
    modifications AND untracked files. Strong message first, soft
    note second. A clean tree is silent. In **single-pass mode**
    the run still proceeds regardless — syncade does not refuse
    to run on a dirty tree.
  - **Loop mode dirty-tree refusal.** When
    `max_rounds > 1` (the default — 3 — and any explicit
    `--max-rounds 2` / `--max-rounds 3`), syncade **refuses to
    start with exit 60** if the tree has tracked-modified files,
    and prints:
    `[syncade] working tree has uncommitted modifications to`
    `tracked files; loop mode would commit on top of them. Commit`
    `or stash before running, or pass --force-dirty to override`
    `(your WIP may interleave with the producer's commits).`
    The reason is concrete: loop mode advances your branch via
    `git update-ref` after each producer commit, and the producer
    is committing on top of your WIP whether you wanted it to or
    not. Two recovery paths:
    - **Recommended:** `git stash` (or commit) your WIP, then
      re-run.
    - **Escape hatch:** pass `--force-dirty` to acknowledge the
      interleave risk and let the loop proceed anyway.
    Untracked-only stays a soft note even in loop mode —
    untracked files don't interleave with producer commits.
- **Exit 70 (`REVIEWER_OUTPUT_UNPARSEABLE`).** A reviewer ran
  successfully but its output couldn't be parsed as findings JSON. The
  raw response is preserved at
  `.syncade/runs/<run-id>/round-0/<reviewer-name>.stdout`. Look for a
  `result` field in the JSON envelope (claude `-p` wraps its narrative
  + verdict that way), or for inline JSON inside the model's narrative
  — the verdict and findings are usually one envelope-unwrap or
  ctrl-F away. The parse exception itself is in the matching
  `.error.txt`.

```bash
syncade --help
```

```
usage: syncade [-h] [--resume [RUN_ID]] [--force-drift] [--spec-audit PR_DOC]
               [--repo-root PATH] [--preset {cheap,balanced,thorough}]
               [--worktree-base PATH] [--base REF] [--scope SCOPE]
               [--openspec [CHANGE_ID]] [--timeout SECONDS] [--max-rounds INT]
               [--budget-tokens N] [--budget-usd USD]
               [--reviewer-model NAME=MODEL] [--reviewer-thinking NAME=TIER]
               [--reviewer-timeout NAME=SECONDS] [--force-dirty]
               [--allow-default-branch] [--selfcheck] [--auth-check]
               [--doctor] [--quick] [--install-skill [{all,claude,codex}]]
               [--draft-spec] [--transcript PATH] [--gc] [--metrics]
               [--metrics-last N] [--gc-keep N] [--gc-max-age-days D]
               [--gc-dry-run] [--quiet] [--version]
               [PR_DOC]

External blind multi-judge review orchestrator for AI-assisted coding. Invoked
from inside Claude Code via the syncade skill.

positional arguments:
  PR_DOC                Path to the PR doc that describes the work to review.

options:
  -h, --help            show this help message and exit
  --resume [RUN_ID]     Resume an aborted, interrupted, or decision-needed
                        run. Pass a specific run-id (the timestamped directory
                        name under .syncade/runs/), the literal string
                        'latest', or pass --resume alone (equivalent to
                        --resume latest). Eligibility: the original run was
                        aborted by an environment failure (exit 40/60/70),
                        stopped at a budget ceiling (exit 25), needs an
                        operator decision after a producer escalation (exit
                        10), OR was interrupted before the loop terminator
                        wrote loop-manifest.json. Mutually exclusive with
                        PR_DOC, --selfcheck, --auth-check, --spec-audit,
                        --draft-spec, --base.
  --force-drift         With --resume only: accept tree drift between the
                        original run's expected snapshot SHA and the
                        operator's current HEAD. The resumed round will
                        snapshot from current HEAD; cross-round context from
                        prior rounds may reference findings against a
                        different SHA than the new tree. Without --resume,
                        passing --force-drift is a CLI error (exit 2).
  --spec-audit PR_DOC   Audit the PR brief at PR_DOC for spec-level issues
                        (unverified claims, internal contradictions, ambiguous
                        acceptance criteria, missing references, scope drift,
                        missing structural sections). Advisory-only for v1 —
                        exits 0 on a clean brief, 10 when blocker-severity
                        findings are present, 40 on subprocess error, 50 on
                        config load error, 60 on path/worktree error, 70 on
                        parse failure. Mutually exclusive with PR_DOC
                        positional, --selfcheck, --auth-check, --draft-spec,
                        --resume.
  --repo-root PATH      Directory to run from (default: current working
                        directory). Used to locate .syncade/config.toml and as
                        the starting hint for git repo-root discovery —
                        syncade writes run artifacts to the actual repo root
                        (git rev-parse --show-toplevel) regardless of which
                        subdirectory this points at. Relative PR_DOC, --spec-
                        audit, and --transcript paths resolve against the
                        discovered repo root first, then cwd as a
                        compatibility fallback.
  --preset {cheap,balanced,thorough}
                        Start from a bundled config preset: `cheap` (single
                        pass, no producer loop), `balanced` (the shipped
                        defaults), or `thorough` (full rounds + double the
                        per-reviewer timeout). Your .syncade/config.toml still
                        layers on top (user file wins). Presets vary only
                        rounds/timeout — never the reviewer model or effort
                        tier.
  --worktree-base PATH  Base directory under which per-run git worktrees are
                        created (overrides [worktree_base] in config; default
                        /tmp/syncade). Use a fast local disk when /tmp is
                        small or slow. Applies to review runs, --gc, --resume,
                        and --doctor's writability preview.
  --base REF            Git ref to render the reviewer's diff against (e.g.
                        `main`, `HEAD~3`, a tag or commit SHA). When omitted,
                        reviewers run against the full HEAD state with no diff
                        included in the prompt — the model decides what to
                        review from the repo contents alone.
  --scope SCOPE         Derive the diff base from scope instead of an explicit
                        --base: `everything` (branch point off the default
                        branch), `local` (your local-ahead commits vs the
                        branch's upstream), `since-last-review` (the recorded
                        last-reviewed SHA for this branch). Mutually exclusive
                        with --base and --resume.
  --openspec [CHANGE_ID]
                        Review an existing OpenSpec proposal folder instead of
                        a PR_DOC: assemble openspec/changes/<CHANGE_ID>/
                        (proposal + spec deltas) into the spec the loop
                        reviews against. Pass a proposal-id, or pass
                        --openspec alone to auto-resolve when exactly one
                        active proposal exists (else it lists them and asks).
                        Reads the markdown directly — no openspec binary
                        required. Mutually exclusive with PR_DOC, --selfcheck,
                        --auth-check, --spec-audit, --draft-spec, --resume.
                        --base/--scope still set the diff base.
  --timeout SECONDS     Per-reviewer timeout in seconds (must be > 0).
                        Overrides `[loop] timeout_seconds` in
                        .syncade/config.toml (default 1800, i.e. 30 minutes).
  --max-rounds INT      Per-run maximum rounds of (reviewers → synthesizer →
                        optional test → producer-if-NO-SHIP). Must be in [1,
                        3]. Overrides `[loop] max_rounds` in
                        .syncade/config.toml. Default 3. Set to 1 for single-
                        pass review without producer code changes.
  --budget-tokens N     Per-run total-token ceiling. When the running tally of
                        every actor's usage crosses it at a phase boundary,
                        the loop aborts gracefully (budget_exceeded). This is
                        the TIGHTEST bound — exact when all actors report
                        usage, a lower bound only if an actor reports none.
                        Overrides `[loop] budget_tokens`. Default: no token
                        ceiling (only --max-rounds and --timeout bound a run).
  --budget-usd USD      Per-run cost ceiling on the API-EQUIVALENT valuation
                        (NOT billed money — the marginal dollar is $0 on a
                        subscription), matching what `syncade --doctor`
                        previews. A LOWER-BOUND tally: actors with incomplete
                        cost are uncounted, so a dollar-budgeted run can
                        overshoot — use --budget-tokens for the tighter cap.
                        Overrides `[loop] budget_usd`. Default: no cost
                        ceiling.
  --reviewer-model NAME=MODEL
                        Override ONE reviewer's model for this run: NAME is a
                        reviewer's `name` in .syncade/config.toml. Repeatable
                        (once per reviewer). An unknown NAME fails exit 50
                        naming the configured reviewers. E.g. --reviewer-model
                        codex-reviewer-adv=gpt-5.6-sol.
  --reviewer-thinking NAME=TIER
                        Override ONE reviewer's thinking/effort tier for this
                        run (the tiers accepted by `[reviewers] thinking`).
                        NAME is a reviewer's `name`; repeatable. Bad TIER or
                        unknown NAME fails exit 50.
  --reviewer-timeout NAME=SECONDS
                        Override ONE reviewer's wall-clock timeout for this
                        run (seconds, > 0). NAME is a reviewer's `name`;
                        repeatable. Overrides that reviewer's
                        `timeout_seconds` (else the loop timeout). Bad value
                        or unknown NAME fails exit 50.
  --force-dirty         Allow loop mode (max_rounds > 1) to start even when
                        the working tree has tracked-modified files. WARNING:
                        the producer will commit on top of the operator's WIP,
                        which may interleave with their uncommitted edits in
                        confusing ways. Use only when you understand the
                        consequences. The same refusal applies to a resumed
                        loop-mode run (--resume), where --force-dirty is
                        likewise the only escape. max_rounds=1 (single-pass)
                        bypasses this guard entirely.
  --allow-default-branch
                        Allow loop mode (max_rounds > 1) to run while HEAD is
                        the repo's default branch. By default syncade REFUSES
                        this, because the producer fast-forwards the current
                        branch and would land commits directly on your default
                        branch. Pass this to commit there deliberately.
                        Single-pass (max_rounds=1) commits nothing and
                        bypasses the guard.
  --selfcheck           Verify the configured producer can headless-commit.
                        Provisions a tmp_path git repo + stub findings.md,
                        runs the producer once, asserts HEAD moved + the
                        requested edit is present. Useful after a claude/codex
                        CLI update to detect sandbox-semantics drift before a
                        real loop hits it. Mutually exclusive with PR_DOC,
                        --auth-check, --spec-audit, --draft-spec, --resume,
                        --openspec, --gc.
  --auth-check          Verify every configured credential can authenticate.
                        Faster than --selfcheck (~5-10s total vs
                        ~30s/producer); covers the common 'did my OAuth token
                        rotate?' diagnostic. Mutually exclusive with PR_DOC,
                        --selfcheck, --resume, --spec-audit, --draft-spec.
  --doctor              Preflight a run without dispatching one. Read-only:
                        prints a green/red table of readiness checks (resolved
                        config; each configured provider's CLI on PATH;
                        worktree root + disk; branch/dirty-tree refusal
                        preview; run-plan + cost preview; credential auth;
                        producer headless-commit) and exits 0 iff every check
                        is green, else 60. Mutates nothing. The auth +
                        producer-commit legs make real provider calls (~30s)
                        and are skipped when a cheap check already reds — pass
                        --quick to skip them outright. Unlike the other one-
                        shot modes it ACCEPTS --base/--scope (it previews that
                        diff). Mutually exclusive with PR_DOC, --selfcheck,
                        --auth-check, --spec-audit, --draft-spec, --resume,
                        --openspec, --gc, --metrics.
  --quick               With --doctor only: skip the two LIVE legs (the auth
                        credential probe and the producer headless-commit
                        smoke), which make real provider calls and take ~30s.
                        Leaves the instant config / PATH / worktree / branch /
                        plan / cost checks. The skipped legs are reported as
                        'skipped', never as passed.
  --install-skill [{all,claude,codex}]
                        Install the bundled /syncade Agent Skill into your
                        harness skill directories (~/.claude/skills and
                        $CODEX_HOME/skills), then exit. Works from a pip
                        install (no checkout needed). Default target 'all';
                        pass 'claude' or 'codex' to install just one.
  --draft-spec          Manufacture a ratifiable spec from a session
                        transcript. Requires --transcript. Reads the
                        transcript for INTENT + the diff (via --base/--scope,
                        optional), runs a cold drafter, and writes an
                        OpenSpec-shaped .syncade/draft-spec-<session>[-N].md
                        with an 'Assumptions to confirm' section. Advisory —
                        review/edit it, then run `syncade <file>`. Mutually
                        exclusive with PR_DOC, --selfcheck, --auth-check,
                        --spec-audit, --resume, --openspec.
  --transcript PATH     Path to a Claude Code session JSONL transcript.
                        Required with --draft-spec; the cold drafter reads it
                        for intent.
  --gc                  Run a one-shot maintenance pass. Retention is TWO-
                        TIER: run history is kept FOREVER (loop-manifest.json,
                        findings.md, run-init.json, round manifests,
                        summaries) and only the bulky subprocess transcripts
                        (round-*/*.stdout, *.stderr - 90% of the corpus) are
                        pruned, for runs beyond --gc-keep and any --gc-max-
                        age-days floor. NO run directory is ever deleted:
                        .syncade/metrics.db is a derived view over
                        .syncade/runs/, so deleting a run would destroy its
                        history the next time that view rebuilds. Also removes
                        matching <worktree_base>/<run-id>/ worktree leftovers
                        whose directory identity still matches the GC plan,
                        and safely reaps orphaned reviewer/producer
                        subprocesses whose working dir is INSIDE a worktree
                        being removed. Runs also auto-prune their transcripts
                        at the start of every fresh loop, so --gc is for
                        worktree/process cleanup and one-off maintenance
                        rather than routine disk hygiene. Resume-eligible runs
                        are ALWAYS protected (they keep even their
                        transcripts); non-run state (config.toml, last-
                        reviewed.json, draft-spec-*.md) is never touched.
                        Mutually exclusive with PR_DOC, --selfcheck, --auth-
                        check, --spec-audit, --draft-spec, --resume,
                        --openspec.
  --metrics             Aggregate the .syncade/runs/ artifact corpus into
                        .syncade/metrics.db (read-only over the artifacts) and
                        print a cumulative report: run count, ship-rate,
                        blockers/minors/nits, rounds, handoffs, and per-model
                        reviewer stats. Rebuildable + idempotent. Mutually
                        exclusive with PR_DOC, --selfcheck, --auth-check,
                        --spec-audit, --draft-spec, --resume, --openspec,
                        --gc.
  --metrics-last N      With --metrics only: also print a billed/API-
                        equivalent breakdown scoped to the N most recent runs
                        (e.g. --metrics-last 20).
  --gc-keep N           With --gc only: number of most-recent (non-protected)
                        runs whose TRANSCRIPTS are kept; older runs have their
                        transcripts pruned (their history is kept either way).
                        Default 20. (Passing this WITHOUT --gc is an error —
                        it is meaningful only with --gc.)
  --gc-max-age-days D   With --gc only: optional age floor in days. 0
                        (default) disables the age gate (prune transcripts for
                        every candidate beyond --gc-keep); D>0 only prunes a
                        beyond-keep candidate that is ALSO older than D days.
                        (Passing this WITHOUT --gc is an error.)
  --gc-dry-run          With --gc only: report exactly what would be
                        pruned/removed/reaped (including the bytes that would
                        be freed) and modify NOTHING on disk (prune nothing,
                        delete nothing, kill nothing).
  --quiet               Suppress phase-level progress output; print only the
                        final summary line, plus exit-70 artifact pointers and
                        any error messages.
  --version             show program's version number and exit
```

All flags are live. `--resume [RUN_ID]` resumes an aborted, interrupted,
or decision-needed run (exit 10, 25, 40, 60, or 70); pass a specific run-id,
`latest`, or bare `--resume` to target the newest eligible run on the current
branch. `--force-drift`
accepts tree drift when resuming (requires `--resume`). `--force-dirty`,
`--spec-audit`, `--auth-check`, `--selfcheck`, `--max-rounds`, and `--gc` are
also fully implemented.

The full architecture (worktree-based reviewer isolation, the
synthesis subprocess, the 3-round convergence loop, the producer
subprocess + branch advance, the exit-code contract with the
Claude Code skill) is documented in
this README

## Development

```bash
# create a venv (requires Python 3.11+)
uv venv --seed
source .venv/bin/activate

# install with dev dependencies
uv pip install -e ".[dev]"

# default pytest deselects smoke via pyproject addopts
uv run python -m pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q src
scripts/check-loc.sh 500
git diff --check
```

Dependencies are intentionally minimal: `pydantic>=2.0` at runtime;
`pytest>=8.0` and `ruff>=0.5.0` for development. Anything beyond that
should be discussed before adding.

### Release checklist

This checklist is the operator-facing release gate. Record command output and
artifact paths for each step. A missing external provider, failed auth probe,
provider skip, or smoke skip is **RELEASE-BLOCKING**, not a pass.

1. Confirm local tools:

   ```bash
   python3 --version
   uv --version
   git --version
   command -v lsof
   command -v claude && claude --version
   command -v codex && codex --version && codex login status
   ```

2. Run the non-smoke CI parity gates:

   ```bash
   uv run python -m pytest -q
   uv run ruff check .
   uv run ruff format --check .
   uv run python -m compileall -q src
   scripts/check-loc.sh 500
   git diff --check
   ```

3. Run real-provider preflights:

   ```bash
   uv run syncade --auth-check
   uv run syncade --selfcheck
   ```

4. Run real-provider smoke, including one tiny real loop:

   ```bash
   uv run python -m pytest -q -m smoke -s
   uv run python -m pytest -q -m smoke tests/smoke/test_loop_smoke.py::test_full_loop_ships_at_round_less_than_max_rounds -s
   ```

5. Inspect the smoke run artifact path printed by pytest/syncade. The tiny loop
   must show a real provider-backed run with a producer commit, a later SHIP
   round, and persisted run artifacts under `.syncade/runs/` or the smoke temp
   repository. Do not claim final release readiness until the separate
   user-POV/persistence QA and final judge gate have also passed.

## License

[Apache-2.0](LICENSE). © 2026 Nick Ciubotariu.
