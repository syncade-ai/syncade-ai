# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, minor versions may
include breaking changes.

## [Unreleased]

## [0.3.0] — 2026-08-05

Verdict integrity and review identity. This release is about two guarantees: that a verdict
comes from bytes a model actually emitted, and that reviewers judged the code you think they
judged. Several of the fixes below closed reproduced **false SHIPs** — runs that exited 0 over
code no reviewer had seen.

### Changed

- **The diff is now three-dot by default** — taken from the BRANCH POINT (the merge base of the
  base ref and HEAD) rather than the literal `base..HEAD` range. The old behaviour rendered
  commits that landed on the base but not on your branch as phantom DELETIONS, which was the
  default path for any branch behind its base. Pass `--two-dot` for the previous semantics.
  Unrelated histories have no branch point and are refused with a message naming `--two-dot`.
- **Verdict parsing is strict.** A ```json fence is authoritative (last one wins); labelled
  fences are code samples and are ignored; an unclosed fence fails closed rather than falling
  back to earlier text; duplicate JSON keys are rejected instead of resolved last-wins; and
  there is no try-the-next-candidate fallback. Reviewer or judge output that previously slipped
  through as a SHIP may now fail to parse (exit 70). That is the intended direction: a
  truncated or ambiguous verdict must never be read as approval.

### Added

- `--two-dot`, the escape hatch to the previous literal-range diff.
- Two terminal states decided BEFORE any subprocess is dispatched, so an empty review costs
  nothing: `no_changes_to_review` (round 0) and `producer_emptied_diff` (a later round). Both
  exit 0 with zero reviewers dispatched, and neither records a last-reviewed SHA.
- A `diff_malformed` refusal (exit 60) when the reviewer-facing diff cannot be read with
  confidence, instead of reviewing a partial diff.
- Hardening against `refs/replace/*` object substitution: every `git` subprocess now runs with
  `GIT_NO_REPLACE_OBJECTS=1`. Replacement refs live in the shared `.git` common dir and are
  producer-writable, and they substituted objects in commands that read a commit.

### Fixed

- **The reviewer-facing diff now decodes git's C-quoted paths.** Git quotes any path containing
  a non-ASCII byte, a quote, a backslash, or a control character, and matching the raw header
  text let most path shapes leak a file that was supposed to be stripped to a blind reviewer.
- **A rename out of a stripped file no longer conceals its destination.** `git mv CLAUDE.md
  app.py` previously hid that `app.py` appeared at all; combined with the empty-diff path that
  produced an exit-0 SHIP over a real change with zero reviewers. Such a section is now replaced
  by a placeholder naming the destination.
- **`.syncade/last-reviewed.json` records the SHA a reviewer actually saw**, not the branch tip
  re-read at the end of the run. The old behaviour could make `--scope since-last-review` skip
  work nobody reviewed. A write failure is reported rather than silently swallowed.

### Removed

- **BREAKING (no behaviour change): `[review] include_producer_summary`.** It was declared,
  documented as controlling reviewer blindness, and settable — but no code ever read it. A config
  carrying it now errors instead of being silently ignored. Reviewer blindness is unconditional
  and enforced structurally: the reviewer prompt has no parameter that can carry producer
  narrative, and cross-round context uses a different loader for reviewers than for the producer.

### Known limitations

- A bare `diff --git` header is genuinely ambiguous when a path contains `` b/``, because git
  does not quote spaces. Syncade fails closed (exit 60) — and does so over-broadly: an ordinary
  edit under a directory whose name ends in `` b`` currently makes a repo unreviewable.
- `strip_repo_context_files` entries containing `/` strip the diff hunk but leave the file
  readable in the reviewer worktree. Use bare basenames.

### Documentation

Corrections to claims the code did not support. Each is now held in place by a test.

- **Requirements were overstated.** Only `codex` (OpenAI) is needed for a default run; `claude`
  (Anthropic) is additionally required only inside Claude Code, where the default producer is
  Anthropic. In a plain terminal or Codex, every actor resolves to OpenAI.
- **`timeout_seconds` is a per-SUBPROCESS cap, not per round.** It is the fallback wall clock for
  every leg — each reviewer, the judge, the test run, each mechanical check, and the producer — so
  one round's worst case is a multiple of it. Corrected in the README, `--help`, the presets, the
  config reference, and the `--config` menu label (which previously read "Time per round").
- **Documented flags that do not exist.** `--audit` is `--spec-audit`; `--offline` and `--verbose`
  were never options. A test now asserts every flag named in the docs exists in the parser.
- **Exit code 25 (budget exceeded) was missing** from the Codex operator contract's table. A test
  now checks every exit-code enumeration against the code in both directions.
- **`strip_repo_context_files` matches bare filenames by basename, not globs.** `*.md` matches
  nothing. An entry containing `/` strips the diff hunk but leaves the file readable in the
  reviewer worktree — documented rather than silently promised otherwise.
- `syncade --doctor` quick-start guidance: plain `--doctor` makes live provider calls; `--quick`
  is the free path and skips the credential probe, so it is no longer described as checking auth.

## [0.2.0] — 2026-07-25

Full configurability (cross-prompt today, cross-model/cross-lab when you want it), an in-harness
config menu, and a config writer that no longer eats your comments.

### Added

- Interactive config editor and global config layer (`syncade --config`): an arrow-key menu
  (stdlib `curses`, no new dependency) plus a scriptable `--config list` / `get` / `set`
  primitive for the common knobs (models, rounds, timeout, cost cap). `set` validates through
  the schema and never writes a broken file.
- Machine-wide global config at `~/.syncade/config.toml`, resolved
  `defaults → global → repo `.syncade/config.toml` → CLI flags` with per-value provenance in
  `--config list`. Paired sections (`[producer]`, cold actors, `[[reviewers]]`) replace
  wholesale so a provider/model pair can never split across layers; `[loop]` scalars merge
  key-by-key. `--config set … --repo` targets the repo file instead of the global one. Purely
  additive — a machine with no global file behaves exactly as before.
- `syncade --config set <key> <value>` now sets **any** scalar field on any actor/section
 , not just the curated model/rounds/timeout/cost knobs — resolved and type-coerced
  against the pydantic schema (`cli/config_keys.py`). A bad key exits 2, a bad value exits 50, and
  a broken file is never written.
- The `syncade --config` menu is now a **drill-in tree**: Producer / Reviewer i / Judge
  and an Advanced… entry drill into full field screens (model, thinking, permissions, auth, timeouts,
  retry / gc / review / checks), Esc backs up — so every actor and Advanced field is menu-editable,
  not just the curated knobs. The menu shares one resolver with `--config set`, so a value one accepts
  the other accepts.
- The `--config` menu gains a global⇄repo **edit-target toggle** (`t`) and flags each row
  `shadowed by <layer>` when a higher layer would mask an edit at the current target — an
  ineffective edit is visible before you make it (the "menu edits did nothing in a repo that overrides
  the section" fix). `dirty` is tracked per target, so switching targets never silently drops an edit.
- `syncade --config list --all`: print the **full settable surface** — every actor/section
  field (incl. advanced retry/gc/checks and CLI-only knobs), each with value, layer, dotted key, and an
  `overrides global <value>` note where the repo layer masks a different global value. Control chars in
  values are escaped so a row never splits; bare `--config list` stays byte-compatible. Enumerated from
  the same schema walk as `--config set`, so the two can't drift.
- **In-harness config menu**: the `/syncade` skill now renders `--config list --all` as a
  browsable, numbered config menu inside the Claude Code / Codex chat — pick a field (or say what to
  change), switch the global⇄repo edit target, and edit via `--config set`, with a shadow warning when a
  repo-controlled field is edited at the global target. The terminal arrow-key menu can't run in the
  harness; this gives the same editing power conversationally. Skill-markdown only, no new dependency.

### Changed

- `loop.max_rounds` / `--max-rounds` ceiling raised from **3 to 10**. The default stays
  3; budget (`budget_usd` / `budget_tokens`) and the per-subprocess timeout remain the real runaway
  guards — the round cap is a typo-ceiling.
- Reviewer `permissions` no longer accepts `safe`: it prompts and would hang a headless reviewer,
  so it is now rejected at config-load (`ReviewerPermissions = {trusted-execute, yolo}`), not only
  at dispatch — mirroring the producer's `yolo`-only bound.

### Fixed

- `syncade --config set` and the `--config` menu no longer destroy the comments, key order, and
  formatting in your `.syncade/config.toml`. Both write paths regenerated the file from
  parsed data, silently wiping every comment on the first edit. They now surgically patch only the
  changed lines and keep that patch only if it re-parses to exactly the intended config, falling back
  to a full rewrite otherwise — so a write can never produce a value you didn't set. Preserves
  comments, blank lines, key order, spacing around `=`, the trailing-comment column, and per-line line
  endings. No new runtime dependency.

## [0.1.0]

Initial public release.

- External, blind, multi-judge code-review loop: reviewers and a cold synthesizer run as
  fresh, process-isolated CLI subprocesses; the verdict is mechanical (exit codes), never
  decided by an LLM directly.
- Multi-round loop with an optional producer (fix-it) leg, test re-run leg, and mechanical
  checks; single-pass mode via `max_rounds = 1`; `--resume` for interrupted runs.
- Provider-agnostic judge, drafter, and auditor (registry-resolved; no CLI is hardwired).
- Auth modes (`auto` / `subscription` / `api`) with a preflight that refuses to bill the
  wrong account, and `--metrics` cost reporting that separates billed money from
  API-equivalent valuation.
- Default-branch commit guard: refuses to run the committing loop on the repo's default
  branch unless `--allow-default-branch` is passed.
- Ships as a `pip`-installable CLI plus an Agent Skill for Claude Code and Codex
  (`scripts/install-skill.sh`).

[Unreleased]: https://github.com/syncade-ai/syncade-ai/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.1.0
