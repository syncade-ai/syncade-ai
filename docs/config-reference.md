# Syncade configuration reference

Everything you can set in `.syncade/config.toml`, plus the CLI flags and presets that override it.
Zero config is fully supported — every field has a working default, and a run with no config file and
no flags uses the shipped defaults documented here.

> This file is drift-locked. `tests/config/test_config_reference_drift.py` walks every config model
> reachable from `SyncadeConfig` and fails if any field is undocumented, renamed, or removed — so
> this reference cannot silently rot. Each `<!-- config-fields: X -->` marker pins one model's table.

## Precedence

For a single run, values resolve lowest-to-highest:

```
defaults  <  --preset  <  ~/.syncade/config.toml (global)  <  .syncade/config.toml (repo)  <  CLI flags
```

- **`--preset cheap|balanced|thorough`** supplies a bundled base config (see [Presets](#presets)).
- **`~/.syncade/config.toml`** (global) — machine-wide defaults applied in every repo. Absent by
  default; edit it with [`syncade --config`](#editing-config-with-syncade---config).
- **`.syncade/config.toml`** (repo) — per-project overrides for the repo you run in.
- Each higher layer merges onto the one below, **except the paired sections** — `[producer]`,
  `[synthesizer]`, `[drafter]`, `[auditor]`, and the `[[reviewers]]` list — which **replace
  wholesale**, so a provider/model pair can never split across layers. `[loop]` scalar knobs merge
  key-by-key (a repo can bump `max_rounds` and inherit the rest).
- **CLI flags** (`--max-rounds`, `--budget-tokens`, `--budget-usd`, `--timeout`, `--reviewer-model`
  / `--reviewer-thinking` / `--reviewer-timeout NAME=VALUE`, `--worktree-base`) win last, per knob.

A bad value — an unknown reviewer name, a non-finite timeout, a rejected enum — fails **exit 50**
before any subprocess spawns, so a typo in a flag reads the same as a typo in the file.

## Presets

Bundled at `src/syncade/templates/presets/*.toml`, selected with `--preset <name>`. Presets vary
**only** loop dimensions (rounds / timeout) and **never** the reviewer model or effort tier — a
cheaper, lenient panel is the panel-leniency hazard, and a preset must not reintroduce it.

| Preset | Effect | Reviewer model + effort |
|---|---|---|
| `cheap` | `max_rounds = 1` — single pass, no producer loop | unchanged (proven panel) |
| `balanced` | the shipped defaults (byte-identical to zero-config) | unchanged (proven panel) |
| `thorough` | `max_rounds = 5` + `timeout_seconds = 3600` (double) | unchanged (proven panel) |

## Editing config with `syncade --config`

Instead of hand-editing the TOML, `syncade --config` inspects and edits it:

| command | what it does |
|---|---|
| `syncade --config` | interactive arrow-menu (terminal only) — ↑/↓ to a row, Enter to edit a field or drill into an actor / Advanced section, Esc to go back, `t` toggle edit target (global⇄repo), `s` save, `q` quit |
| `syncade --config list` | print each surfaced (common) setting with its value and the layer that set it (default / global / repo) |
| `syncade --config list --all` | print the **full settable surface** — every actor/section field (incl. advanced retry/gc/checks and CLI-only knobs), each with value, layer, dotted key, and an `overrides global <value>` note where the repo layer masks a different global value |
| `syncade --config get <key>` | print one resolved value |
| `syncade --config set <key> <value>` | write the value to the global `~/.syncade/config.toml` |
| `syncade --config set <key> <value> --repo` | write to the current repo's `.syncade/config.toml` instead |

The top screen lists the producer, each reviewer, the judge, and an **Advanced…** entry; Enter on
one **drills in** to that actor's fields (model, thinking, permissions, auth — plus timeout for the
producer and reviewers; the cold judge has none) or the
Advanced retry / gc / review / checks screens, and Esc backs up — so every actor and Advanced field
is reachable from the menu, not just the common knobs. (A few settable fields stay CLI-only via
`--config set` by design — the cold `drafter` / `auditor`, `loop.budget_tokens` /
`loop.test_command` / `loop.test_timeout_seconds`, each actor's `api_key_env`, and `worktree_base`.)
In the menu, **`t`** switches the edit target between the global `~/.syncade/config.toml` and the
current repo's `.syncade/config.toml` (repo is available only inside a git repo). A row is flagged
**`shadowed by <layer>`** when a higher layer overrides it — so a global edit that a repo section
would mask (or vice-versa) is visible before you make it, not a silent no-op. Editing at the target
whose layer supplies the effective value always takes effect.

Settable keys: **any scalar field reachable as a flat dotted path** —
every actor's `provider` / `model` / `thinking` / `permissions` / `auth`; `timeout_seconds` for
`producer` and `reviewers.<i>` only (cold actors — `synthesizer`, `drafter`, `auditor` — have no
timeout field); `reviewers.<i>.*` and `checks.<i>.*` by index, the `[loop]` / `[review]` /
`[retry]` / `[gc]` scalars, and top-level `worktree_base`. The value is coerced to the field's type (int / float /
bool / one-of-a-fixed-set / comma-separated list / string), and an empty value clears an optional
field — **except `loop.budget_tokens` and `loop.budget_usd`**, where clearing omits the TOML key
and silently reactivates the 50M-token default or resumes-inherits a prior ceiling; set `0`
explicitly to disable those ceilings. `set` validates the file it writes and refuses an invalid value (**exit 50**) without
touching disk; setting a paired role's `provider` re-derives its `model` in the same write. Adding
or removing `[[reviewers]]` / `[[checks]]` *entries* is not a `set` operation — `set` edits existing
entries by index. The `[pricing]` model table (`pricing.models.<model>.*`) is a dict-keyed roster, not
a flat path, so those fields are **not** `set`-reachable — edit them in the TOML directly.
Outside a terminal (piped / CI / inside a harness) the menu form degrades to exit 60 — use the
`list`/`get`/`set` verbs there. In Claude Code / Codex the `/syncade` skill maps natural language
("change my producer to gpt-5", "set rounds to 2") onto these same commands.

## Top level — `[.]`

The root of `.syncade/config.toml`. Most entries are tables/lists documented in their own sections
below; `worktree_base` is the one top-level scalar.

<!-- config-fields: SyncadeConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `reviewers` | `[[reviewers]]` list | 2× `openai`/`gpt-5.5` (one plain, one adversarial) | The blind reviewer panel. See [`[[reviewers]]`](#reviewers--reviewerconfig). |
| `producer` | `[producer]` table | harness-aware (`anthropic`/`claude-sonnet-4-6` under Claude Code) | The fixer run on a NO-SHIP round. See [`[producer]`](#producer--producerconfig). |
| `loop` | `[loop]` table | — | Rounds, timeouts, budgets, test command. See [`[loop]`](#loop--loopconfig). |
| `review` | `[review]` table | — | What reviewers see. See [`[review]`](#review--reviewconfig). |
| `retry` | `[retry]` table | — | Transient-error retry bound. See [`[retry]`](#retry--retryconfig). |
| `gc` | `[gc]` table | — | Run-artifact retention. See [`[gc]`](#gc--gcconfig). |
| `update` | `[update]` table | — | The once-per-session update check. See [`[update]`](#update--updateconfig). |
| `worktree_base` | path | `/tmp/syncade` | Base dir for per-run git worktrees. Overridable with `--worktree-base`; point it at a fast local disk if `/tmp` is small or slow. |
| `checks` | `[[checks]]` list | none | Mechanical exit-code gates. See [`[[checks]]`](#checks--checkconfig). |
| `pricing` | `[pricing]` table | packaged price table | Per-model token pricing for cost estimation. See [`[pricing]`](#pricing--pricingconfig). |
| `synthesizer` | `[synthesizer]` table | `openai`/`gpt-5.5` | The cold judge. See [cold actors](#synthesizer--drafter--auditor-cold-actors). |
| `drafter` | `[drafter]` table | `openai`/`gpt-5.5` | The `--draft-spec` actor. See [cold actors](#synthesizer--drafter--auditor-cold-actors). |
| `auditor` | `[auditor]` table | `openai`/`gpt-5.5` | The `--spec-audit` actor. See [cold actors](#synthesizer--drafter--auditor-cold-actors). |

## `[loop]` — `LoopConfig`

<!-- config-fields: LoopConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `max_rounds` | int, 1–10 | `5` | Max rounds of (reviewers → synth → optional test → producer-if-NO-SHIP). SHIP ends the loop early. `1` = single-pass, no producer. `--max-rounds` overrides. Ceiling raised 3→10; budget/timeout are the real runaway guards. |
| `timeout_seconds` | float > 0 | `1800` | Per-subprocess wall-clock cap — fallback for every leg (reviewers, judge, test, checks, producer). SIGKILL past it. `--timeout` overrides. Must be finite. |
| `max_diff_bytes` | int > 0 | `1000000` | Ceiling on the REVIEWER-FACING diff in UTF-8 bytes — after repo-context stripping and binary elision, i.e. what actually reaches the model. Over it, the run is refused before any dispatch (exit 60, `diff_too_large`) rather than truncated: a verdict on a partial diff is a verdict on the wrong code. Default sits just under `codex exec`'s hard 1,048,576-CHARACTER limit so syncade's actionable message fires before the provider's opaque one, and ~7x above the largest diff observed in this repo's run corpus (147,171 B). Not a token cap — `claude -p` limits by tokens. |
| `budget_tokens` | int >= 0 | `50000000` | Per-run token ceiling; aborts the loop at a dispatch boundary (exit 25), preserving completed rounds and resumable. **Set `0` for no ceiling** — TOML has no null, so an omitted key means the default. Sized from this repo's corpus: median run 11.0M tokens, p90 38.8M, so 50M stops 4% of runs. Tokens rather than dollars because `cost_usd` is an API-equivalent valuation, not money. `--budget-tokens` overrides. |
| `budget_usd` | float >= 0 or unset | unset | Optional API-equivalent-cost ceiling. **Set `0` for no ceiling** (symmetric with `budget_tokens`; needed when `--resume` would re-inherit a ceiling the current config omits). Softer than tokens (an unpriced actor is uncounted). `--budget-usd` overrides. |
| `test_command` | string or unset | unset | Optional per-round test command; a non-zero exit gates the round. |
| `test_timeout_seconds` | float > 0 or unset | unset | Timeout for `test_command`; unset reuses `timeout_seconds`. |

## `[review]` — `ReviewConfig`

<!-- config-fields: ReviewConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `strip_repo_context_files` | list of bare filenames (**not globs**) | repo-context set (`CLAUDE.md`, `AGENTS.md`, …) | Files removed from each reviewer worktree AND stripped from the reviewer-facing diff, so repo instructions don't leak. Matched by **basename equality** — `*.md` matches nothing. An entry containing `/` (e.g. `docs/CLAUDE.md`) strips the diff hunk but is REFUSED by the worktree strip, leaving the file readable; use bare basenames. |

## `[[reviewers]]` — `ReviewerConfig`

Each `[[reviewers]]` block is one blind reviewer. `name`, `provider`, and `model` are required; the
rest default. Overridable per-run by name: `--reviewer-model NAME=…`, `--reviewer-thinking NAME=…`,
`--reviewer-timeout NAME=…`.

<!-- config-fields: ReviewerConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `name` | string | required | Stable identifier used in artifacts, findings, and the `--reviewer-*` flags. |
| `provider` | string | required | Model provider (`openai`, `anthropic`, …); resolved against the adapter registry at dispatch. |
| `model` | string | required | Model identifier within the provider. |
| `thinking` | `low`/`medium`/`high`/`xhigh`/`max` | `high` | Reasoning-effort tier. Drives audit rigor — do not lower it for cost. |
| `permissions` | `trusted-execute`/`yolo` | `trusted-execute` | Tool-permission tier. `trusted-execute` runs unattended but keeps the OS sandbox scoped to the worktree. `safe` is rejected — it prompts and would hang a headless reviewer. |
| `adversarial_lens` | bool | `false` | When true, the reviewer's prompt carries the enumerate-then-attack edge-case block. |
| `bug_class_sweep` | bool | `false` | When true, the reviewer's prompt carries a directed bug-class sweep — a short set of recurring correctness angles (removed-behavior audit, cross-file/caller-callee trace, language pitfalls, wrapper/proxy fidelity) the reviewer must run and name before any SHIP. Opt-in while an ablation measures its effect; set it on one reviewer and off on another to run that comparison yourself. Severity keys off verification state, so it does not manufacture blockers: a reproduced defect stays a blocker, an unreproduced candidate is `minor`/`coverage_gaps`. A per-repo `reviewer*.md` override that drops the `{bug_class_block}` placeholder disables it regardless of this flag. |
| `template` | basename or unset | unset | Optional prompt template basename overriding provider-based selection. |
| `auth` | `auto`/`subscription`/`api` | `auto` | Which account pays. See [auth](#authentication-fields). |
| `api_key_env` | env var name or unset | unset | For `auth = "api"`: the env var holding the key. |
| `timeout_seconds` | float > 0 or unset | unset | Per-reviewer wall-clock cap; unset reuses the loop/CLI global. `--reviewer-timeout NAME=…` overrides. |

## `[producer]` — `ProducerConfig`

The producer runs on a NO-SHIP round to fix findings. Its `provider`/`model` are harness-aware and
move as a pair (setting `provider` alone re-derives `model`).

<!-- config-fields: ProducerConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `provider` | `anthropic`/`openai` | harness-aware (`anthropic` under Claude Code, else `openai`) | Producer model provider. |
| `model` | string | harness-aware (`claude-sonnet-4-6` / `gpt-5.6-terra`) | Producer model; re-derived if `provider` is set alone. |
| `thinking` | `low`/`medium`/`high`/`xhigh`/`max` | `medium` | Producer reasoning-effort tier. |
| `permissions` | `yolo` | `yolo` | Producer tool-permission tier. `yolo`-only — a sandboxed producer cannot write `.git/index.lock` to commit. |
| `auth` | `auto`/`subscription`/`api` | `auto` | Which account pays. See [auth](#authentication-fields). |
| `api_key_env` | env var name or unset | unset | For `auth = "api"`: the env var holding the key. |
| `timeout_seconds` | float > 0 or unset | unset | Producer wall-clock cap; unset reuses the loop/CLI global. |

## `[synthesizer]` / `[drafter]` / `[auditor]` (cold actors)

The three cold actors. All default to `openai`/`gpt-5.5` at `trusted-execute`; the synthesizer (the
judge) is kept in sync with the reviewer model on purpose. They share the same field set.

<!-- config-fields: SynthesizerConfig -->
### `[synthesizer]` — the cold judge (`SynthesizerConfig`)

| Field | Type | Default | What it does |
|---|---|---|---|
| `provider` | string | `openai` | Judge model provider. |
| `model` | string | `gpt-5.5` | Judge model (kept equal to the reviewer model). |
| `thinking` | `low`/`medium`/`high`/`xhigh`/`max` | `high` | Judge reasoning-effort tier. |
| `permissions` | `trusted-execute` | `trusted-execute` | Judge tool-permission tier. |
| `auth` | `auto`/`subscription`/`api` | `auto` | Which account pays. |
| `api_key_env` | env var name or unset | unset | For `auth = "api"`: the env var holding the key. |

<!-- config-fields: DrafterConfig -->
### `[drafter]` — the `--draft-spec` actor (`DrafterConfig`)

| Field | Type | Default | What it does |
|---|---|---|---|
| `provider` | string | `openai` | Drafter model provider. |
| `model` | string | `gpt-5.5` | Drafter model. |
| `thinking` | `low`/`medium`/`high`/`xhigh`/`max` | `xhigh` | Drafter reasoning-effort tier. |
| `permissions` | `trusted-execute` | `trusted-execute` | Drafter tool-permission tier. |
| `auth` | `auto`/`subscription`/`api` | `auto` | Which account pays. |
| `api_key_env` | env var name or unset | unset | For `auth = "api"`: the env var holding the key. |

<!-- config-fields: AuditorConfig -->
### `[auditor]` — the `--spec-audit` actor (`AuditorConfig`)

| Field | Type | Default | What it does |
|---|---|---|---|
| `provider` | string | `openai` | Auditor model provider. |
| `model` | string | `gpt-5.5` | Auditor model. |
| `thinking` | `low`/`medium`/`high`/`xhigh`/`max` | `xhigh` | Auditor reasoning-effort tier. |
| `permissions` | `trusted-execute` | `trusted-execute` | Auditor tool-permission tier. |
| `auth` | `auto`/`subscription`/`api` | `auto` | Which account pays. |
| `api_key_env` | env var name or unset | unset | For `auth = "api"`: the env var holding the key. |

### Authentication fields

`auth` and `api_key_env` appear on every actor above. `auth = "auto"` uses whatever the CLI is logged
into; `"subscription"` strips the provider key vars from the child env; `"api"` requires `api_key_env`
to name a set env var. The resolved mode is printed every run. A mismatch fails **exit 50** before
any spend.

## `[retry]` — `RetryConfig`

<!-- config-fields: RetryConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `max_retries` | int ≥ 0 | `2` | Extra attempts each model leg (reviewer / synth / producer) rides out a **transient** provider error (429/5xx/dropped socket). `0` disables retries. Timeouts and parse/contract failures are never retried. |

## `[gc]` — `GcConfig`

Governs both the per-loop auto-prune and an explicit `syncade --gc`. Run directories are never
deleted. `keep` / `max_age_days` prune bulky subprocess transcripts; `worktree_max_age_days`
removes stale worktrees under `worktree_base`, which is a separate retention tier because a
worktree is reconstructible from a recorded SHA. CLI `--gc-keep` / `--gc-max-age-days` override
the first two; the worktree bound is config-only (set it with
`syncade --config set gc.worktree_max_age_days N`).

<!-- config-fields: GcConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `keep` | int ≥ 0 | `20` | Newest N runs whose transcripts are always kept. |
| `max_age_days` | int ≥ 0 | `0` | Additional age floor: a beyond-`keep` run is pruned only if also older than this. `0` disables the age floor. |
| `worktree_max_age_days` | int ≥ 0 | `14` | Days a run's **worktree** is kept, after which it is removed — including while the run is still resume-eligible. This is the ONLY rule that selects a worktree: one is never removed because its transcripts became prunable, so a young worktree you are still inspecting survives a busy week of runs. Separate from `max_age_days`, which gates transcripts only — a worktree is reconstructible from the SHA the run records, so removing one costs a `git worktree add` and never history. `0` disables the bound and restores the previous never-ending protection. |

## `[update]` — `UpdateConfig`

Whether syncade checks for a newer release. The check only ever **prints**: it cannot block a run,
change an exit code, or fail one — every error (offline, timeout, bad JSON) is silent. It fires at
most once per window, at the first syncade invocation, and is skipped entirely whenever `CI` is
set. Nothing about your repo, diff, run, or identity is sent.

The advisory source is deliberately not configurable — a repo-local file that could repoint or
silence a security notice would be a hole, not a feature.

<!-- config-fields: UpdateConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `check` | bool | `true` | Check once per session whether a newer syncade is published. Set `false` to never make the request. |

## `[[checks]]` — `CheckConfig`

Each `[[checks]]` block is one mechanical command gate run after the review. `blocking` checks can
gate the verdict; `advisory` checks only render.

<!-- config-fields: CheckConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `name` | string | required | Stable identifier (must be unique across checks). |
| `command` | string | required | The shell command; a non-zero exit is a failure. |
| `severity` | `advisory`/`blocking` | `advisory` | Whether a failure gates the verdict or only renders. |

## `[pricing]` — `PricingConfig`

Per-model token pricing used to estimate cost (`cost_usd` is an API-equivalent valuation, not
billed spend). A packaged default table ships so zero-config runs still estimate.

<!-- config-fields: PricingConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `models` | table of `model → price` | packaged table | Maps a model identifier to its per-million-token prices (see `ModelPrice`). |

### `[pricing.models.<model>]` — `ModelPrice`

<!-- config-fields: ModelPrice -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `input_per_mtok` | float ≥ 0 | required | USD per million input tokens. |
| `output_per_mtok` | float ≥ 0 | required | USD per million output tokens. |
| `cached_input_per_mtok` | float ≥ 0 or unset | unset | USD per million cached-input tokens, when the provider prices them separately. |
