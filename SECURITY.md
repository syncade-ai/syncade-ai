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
  investigate your code. Each runs inside a **throwaway git worktree**: a copy under
  `<worktree_base>/<run-id>/` (default `/tmp/syncade/<run-id>/`), *not* your working tree,
  with `CLAUDE.md` / `AGENTS.md` stripped so the review stays blind to project memory. At the **default**
  `permissions = "trusted-execute"`, Codex reviewers are additionally **sandboxed**
  (`codex -s workspace-write`), which scopes writes to that worktree. Two caveats: setting
  a reviewer to `permissions = "yolo"` **disables that sandbox**
  (`--dangerously-bypass-approvals-and-sandbox`) — supported but discouraged; and on
  Anthropic, `claude` has no equivalent sandbox flag, so an anthropic reviewer's
  confinement is the throwaway worktree copy alone. The shipped default roster is Codex on
  `trusted-execute` (sandboxed).
- **The producer** runs **fully unsandboxed** — `bypassPermissions` on `claude`,
  `--dangerously-bypass-approvals-and-sandbox` on `codex` — **by necessity**: a sandbox
  cannot write `.git/index.lock`, so a sandboxed producer cannot commit, which is its
  whole job. It **commits to your current branch.** Syncade refuses to run the committing
  loop on your repo's *default* branch unless you pass `--allow-default-branch`, and it
  announces which branch will receive commits before dispatching anything.
- **The cold actors** run sandboxed (`trusted-execute`) in an isolated tempdir, but what
  each receives differs — do not assume none of them sees your code:
  - the **synthesizer** (the judge, every round) gets only the reviewers' *structured
    outputs* — never your diff or source;
  - the **auditor** (`--spec-audit`) gets only the PR brief text;
  - the **drafter** (`--draft-spec`) gets the session transcript **and a raw diff** of what
    was built, and drafts a spec from them. So `--draft-spec` *does* send your diff to a
    provider CLI (cold, in a tempdir — but it is your diff).

What bounds this:

- **Worktrees under `<worktree_base>/` (default `/tmp/syncade/`).** Reviewers operate on a
  *copy*, not your working tree. The copy is removed after a clean run, but it is
  **deliberately preserved for inspection** when a run ends NO-SHIP / max-rounds /
  decision-needed (exits 30/20/10) — so a full copy of your repo can remain under
  `<worktree_base>/<run-id>/` after those outcomes, until you delete it or (for the
  default `/tmp/syncade`) the OS clears `/tmp`. With the default base it lives outside your
  repo, so it is never committed or pushed — but it is on disk. Configure `worktree_base` in
  `.syncade/config.toml` or `--worktree-base` to change the location; keep it **outside** your
  repository (or `.gitignore` it). Pointing `worktree_base` inside the repo makes each
  preserved copy an embedded, untracked git worktree that `git add` / `git status` will surface.
- **A sandbox wherever the CLI supports one** (Codex `workspace-write`).
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

**Syncade makes no network calls of its own** — no telemetry, no phone-home, no analytics.
Its own egress is the provider CLI you already authenticated: `claude` talks to Anthropic
and `codex` talks to OpenAI, exactly as they do when you run them yourself. Syncade never
transmits your code anywhere *it* controls.

**But it runs the shell commands you configure.** If you set `[loop] test_command` or any
`[[checks]]`, syncade executes those commands **through the shell** in a clean worktree
each round, capturing their stdout/stderr to run artifacts. Those are *your* commands and
can do anything a shell can — including network calls — so their egress and side effects
are yours to trust, exactly as if you ran them yourself. Syncade adds no test/check
commands of its own; it only runs what your config names.

## Supported versions

Pre-1.0: only the latest release receives security fixes.
