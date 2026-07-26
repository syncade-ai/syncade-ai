# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, minor versions may
include breaking changes.

## [Unreleased]

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
  3; budget (`budget_usd` / `budget_tokens`) and the per-round timeout remain the real runaway
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
