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
built-in defaults  <  --preset  <  .syncade/config.toml  <  CLI flags
```

- **`--preset cheap|balanced|thorough`** supplies a bundled base config (see [Presets](#presets)).
- **`.syncade/config.toml`** deep-merges on top of the preset (your file wins per key; a preset knob
  you don't set survives).
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
| `thorough` | `max_rounds = 3` + `timeout_seconds = 3600` (double) | unchanged (proven panel) |

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
| `worktree_base` | path | `/tmp/syncade` | Base dir for per-run git worktrees. Overridable with `--worktree-base`; point it at a fast local disk if `/tmp` is small or slow. |
| `checks` | `[[checks]]` list | none | Mechanical exit-code gates. See [`[[checks]]`](#checks--checkconfig). |
| `pricing` | `[pricing]` table | packaged price table | Per-model token pricing for cost estimation. See [`[pricing]`](#pricing--pricingconfig). |
| `synthesizer` | `[synthesizer]` table | `openai`/`gpt-5.5` | The cold judge. See [cold actors](#synthesizer--drafter--auditor-cold-actors). |
| `drafter` | `[drafter]` table | `openai`/`gpt-5.5` | The `--draft-spec` actor. See [cold actors](#synthesizer--drafter--auditor-cold-actors). |
| `auditor` | `[auditor]` table | `openai`/`gpt-5.5` | The `--audit` actor. See [cold actors](#synthesizer--drafter--auditor-cold-actors). |

## `[loop]` — `LoopConfig`

<!-- config-fields: LoopConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `max_rounds` | int, 1–3 | `3` | Max rounds of (reviewers → synth → optional test → producer-if-NO-SHIP). SHIP ends the loop early. `1` = single-pass, no producer. `--max-rounds` overrides. |
| `timeout_seconds` | float > 0 | `1800` | Per-reviewer wall-clock cap (SIGKILL past it). `--timeout` overrides. Must be finite. |
| `budget_tokens` | int > 0 or unset | unset | Optional token ceiling; aborts the loop at a dispatch boundary (exit 25). A hard cap. `--budget-tokens` overrides. |
| `budget_usd` | float > 0 or unset | unset | Optional API-equivalent-cost ceiling. Softer than tokens (an unpriced actor is uncounted). `--budget-usd` overrides. |
| `test_command` | string or unset | unset | Optional per-round test command; a non-zero exit gates the round. |
| `test_timeout_seconds` | float > 0 or unset | unset | Timeout for `test_command`; unset reuses `timeout_seconds`. |

## `[review]` — `ReviewConfig`

<!-- config-fields: ReviewConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `include_producer_summary` | bool | `false` | Whether a later round's reviewers see the previous producer's summary. Off keeps reviewers blind. |
| `strip_repo_context_files` | list of globs | repo-context set (`CLAUDE.md`, `AGENTS.md`, …) | Files whose diff hunks are stripped from reviewer-facing diffs so repo instructions don't leak. |

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
| `permissions` | `safe`/`trusted-execute`/`yolo` | `trusted-execute` | Tool-permission tier. `trusted-execute` runs unattended but keeps the OS sandbox scoped to the worktree. |
| `adversarial_lens` | bool | `false` | When true, the reviewer's prompt carries the enumerate-then-attack edge-case block. |
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
### `[auditor]` — the `--audit` actor (`AuditorConfig`)

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
deleted — only bulky subprocess transcripts. CLI `--gc-keep` / `--gc-max-age-days` override these.

<!-- config-fields: GcConfig -->
| Field | Type | Default | What it does |
|---|---|---|---|
| `keep` | int ≥ 0 | `20` | Newest N runs whose transcripts are always kept. |
| `max_age_days` | int ≥ 0 | `0` | Additional age floor: a beyond-`keep` run is pruned only if also older than this. `0` disables the age floor. |

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
