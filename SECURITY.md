# Security

Syncade orchestrates AI coding-agent CLIs that get **elevated tool access on your
repository**. This page says exactly what that means, what syncade does to bound it, and
what it writes to disk — so you can decide before you run it.

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue. Use GitHub's private
vulnerability reporting: open the repository's **Security** tab → **Report a
vulnerability**. We aim to acknowledge within a few days.

## What syncade runs, and with what access

Syncade spawns the provider CLIs you already have installed (`claude`, `codex`) as **fresh
background subprocesses**. There are three roles, at three trust levels:

- **Reviewers** run headless with tool access — they read, grep, and run shell commands to
  investigate your code. Each receives a **Git-less filesystem export** of the pinned
  snapshot under `<worktree_base>/<run-id>/` (default `/tmp/syncade/<run-id>/`), *not*
  your working tree. `CLAUDE.md` / `AGENTS.md` are stripped, no repository or Git history
  is supplied, and the supplied diff identifies the change under review. At the **default**
  `permissions = "trusted-execute"`, Codex reviewers are additionally **sandboxed**
  (`codex -s workspace-write`), which scopes writes to that export. Two caveats: setting
  a reviewer to `permissions = "yolo"` **disables that sandbox**
  (`--dangerously-bypass-approvals-and-sandbox`) — supported but discouraged; and on
  Anthropic, reviewer `claude` runs with `--safe-mode` so CLAUDE.md discovery,
  skills, plugins, hooks, MCP servers, and other customizations are disabled, but
  `claude` has no equivalent OS sandbox flag. Its current working directory is not
  host confinement. An Anthropic or `yolo` reviewer could deliberately inspect or
  modify paths elsewhere as the operator. The shipped default roster is Codex on
  `trusted-execute` (sandboxed).
- **The producer** runs inside a **standalone repository** with its own real `.git`, which has
  no shared object store, alternates, remote, or gitfile pointing at your repository. At the
  default `permissions = "confined"` it is additionally **sandboxed by the provider**: Codex uses
  `workspace-write` with only that in-root `.git` as an extra writable root, and Claude uses its
  native sandbox with fail-closed availability, only sandboxed Bash auto-approved, and
  unsandboxed commands disallowed. Writes outside the repository are denied by *enforcement*, not
  by prompt text. Setting `permissions = "yolo"` **disables that sandbox** (`bypassPermissions` /
  `--dangerously-bypass-approvals-and-sandbox`) — supported, because confinement restricts the
  Anthropic producer to Bash-only editing and that trade is yours to make, but syncade then
  prints an unsuppressible notice naming what it granted on every run that can dispatch a
  producer. **Its commits reach your branch through exactly one path:** a trusted importer
  validates the commit range in a quarantine, anchors it at a durable `refs/syncade/...` recovery
  ref, and fast-forwards your branch with a compare-and-swap. It **commits to your current
  branch.** Syncade refuses to run the committing loop on your repo's *default* branch unless you
  pass `--allow-default-branch`, and it announces which branch will receive commits before
  dispatching anything.
- **The cold actors** run sandboxed (`trusted-execute`) in an isolated tempdir, but what
  each receives differs — do not assume none of them sees your code:
  - the **synthesizer** (the judge, every round) gets only the reviewers' *structured
    outputs* — never your diff or source;
  - the **auditor** (`--spec-audit`) gets only the PR brief text;
  - the **drafter** (`--draft-spec`) gets the session transcript **and a raw diff** of what
    was built, and drafts a spec from them. So `--draft-spec` *does* send your diff to a
    provider CLI (cold, in a tempdir — but it is your diff).

What bounds this:

- **Actor workspaces under `<worktree_base>/` (default `/tmp/syncade/`).**
  Reviewers operate on a Git-less *copy*, not your working tree; producers use
  standalone repositories, while trusted test/check legs retain linked Git
  worktrees. The actor state is removed after a clean run, but is
  **preserved for inspection** when a run ends NO-SHIP / max-rounds / decision-needed
  (exits 30/20/10) — so a
  full copy of your repo can remain under `<worktree_base>/<run-id>/`. `syncade --gc` removes
  these workspaces after `gc.worktree_max_age_days` (default 14 days), including runs that are
  still resume-eligible. With the default `/tmp/syncade` base they live outside your
  repo and are not committed or pushed. Configure `worktree_base` in `.syncade/config.toml` or
  `--worktree-base` to change the location; keep it **outside** your repository. Reviewer
  provisioning refuses a location that would put its export inside the operator repository.
- **Producer confinement by default** (Codex `workspace-write`; Claude's native sandbox with
  fail-closed availability), plus sandboxed default Codex reviewers. `permissions = "yolo"` turns
  the producer's sandbox off; that is announced on every run that can dispatch a producer, never silent. A single-pass
  review (`max_rounds = 1`) dispatches no producer and is not warned about one.
- **One validated path into your object database — in BOTH modes.** The producer works in a
  standalone repository with no shared object store, alternates, remote, or gitfile pointing at
  yours, so its ordinary Git commands cannot reach your refs at all; only a validated, linear,
  ancestry-checked commit range crosses, through the recovery ref and compare-and-swap above.
  Neither the standalone store nor the importer is conditioned on `permissions`. What
  `permissions = "yolo"` removes is *host confinement* — the OS sandbox that also stops a
  producer which goes looking for your repository by path rather than using Git normally. That
  is a real reduction and it is announced on every run that can dispatch a producer, but it does not hand the producer
  your refs back.
- **The default-branch commit guard** (above) and **the auth gate** — syncade refuses to
  run rather than silently bill an account you did not intend (see the README's auth
  section).

The honest framing: **running syncade on a repo is comparable to running that repo's
`Makefile` or an `npm` post-install script** — it executes code with your permissions.
Point it at repositories you would run those from.

## What syncade writes to disk

Every run writes the **full transcripts** of every reviewer, producer, and judge
subprocess to `.syncade/runs/<run-id>/round-N/*.stdout`. These are large — hundreds of KB;
a real reviewer transcript here measured **~415 KB** — and they contain **whatever of your
code the reviewer read, grepped, or printed** while investigating. **If a file the reviewer
opens contains a secret, that secret lands in the transcript in plaintext.**

Mitigations that ship by default:

- `.syncade/runs/` is **gitignored** (via `.gitignore`, and — on a zero-config first run in
  a fresh repo — also written to `.git/info/exclude` *before* the baseline commit), so run
  artifacts cannot be committed or pushed by accident. The same list also excludes
  `.syncade/secrets.*` and `.syncade/last-reviewed.json`.
- Transcripts **auto-prune** once a run falls outside the GC keep window, so they do not
  accumulate forever. Every *structured* artifact (manifests, findings, summaries) is kept.

## Where your code goes

**Syncade's own egress is a small number of update checks and the provider CLIs.** At the
first invocation in each terminal or harness window, syncade makes a GET to its public update
manifest (`update-manifest.json` on the default branch of its GitHub repo) to check whether a
newer version is available — once per session, suppressible via `[update] check = false` or `CI`.
`syncade --update` and `syncade --doctor` check every time you run them; these are
operator-requested and so not session-gated. **At most one manifest GET per invocation** in
every case — a single syncade process fetches once and shares that answer, so an explicit
command adds no request on top of the session check it already performed. No telemetry, no
phone-home, no analytics, and no code or diffs leave in any of these requests — they are
version-number polls only. `syncade --update` also invokes your package manager
(`uv tool upgrade syncade`, `pipx upgrade syncade`, or `<your python> -m pip install -U syncade`)
— only when you run that flag. All other egress is the provider CLI you already authenticated:
`claude` talks to Anthropic and `codex` talks to OpenAI, exactly as they do when you run them
yourself. Syncade never transmits your code anywhere *it* controls.

**But it runs the shell commands you configure.** If you set `[loop] test_command` or any
`[[checks]]`, syncade executes those commands **through the shell** in a clean worktree
each round, capturing their stdout/stderr to run artifacts. Those are *your* commands and
can do anything a shell can — including network calls — so their egress and side effects
are yours to trust, exactly as if you ran them yourself. Syncade adds no test/check
commands of its own; it only runs what your config names.

## Supported versions

Pre-1.0: only the latest release receives security fixes.
