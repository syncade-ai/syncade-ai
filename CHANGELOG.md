# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, minor versions may
include breaking changes.

## [Unreleased]


## [0.7.1] — 2026-08-20

### Fixed

- **`syncade --update` no longer reports success when nothing was installed.** It decided the
  upgrade had worked from the package manager's exit code alone — but exit 0 means *the command
  ran*, not *the version moved*. A pinned install (`uv tool install syncade==X`) records that pin
  in its receipt, so `uv tool upgrade` correctly does nothing, exits 0, and 0.7.0 told you it had
  updated. You re-ran, saw the same version, and were told again the next session, indefinitely.
  `--update` now reads the version actually installed and reports honestly: it names the new
  version when one arrived, says **already up to date** when you were current, and exits non-zero
  naming the likely cause when the upgrade silently did nothing.
- **The version check is no longer confusable by anything else running in your interpreter.** It
  reads the installed version in an isolated subprocess that writes directly to its output and
  exits without running shutdown hooks, so a `sitecustomize` or `.pth` on the path cannot append
  text that would be mistaken for a version.

### Known issues

- Unchanged from 0.7.0: `syncade --metrics` reports `0 minors, 0 nits, 0 dismissed` for runs
  interrupted before their loop manifest was written. Blocker counts are unaffected.


## [0.7.0] — 2026-08-19

### Added

- **`syncade --metrics` now answers the question a second reviewer is for.** It reports what
  share of blocking findings were raised by exactly ONE of your reviewers — on this project's
  own corpus, **266 of 475 (56%)**. Drop either reviewer and you lose about half your blockers,
  and not the same half. Printed only when your runs actually had more than one reviewer: a
  single-reviewer corpus would report 100% by construction, so it prints nothing instead.
- **A per-round blocker curve, with its denominator.** The raw blocker count falls sharply by
  round, which looks like convergence and is not — fewer runs reach each round, so the total
  falls with the population. Each row shows blockers, rounds, and the per-round rate. On this
  project's corpus the rate RISES from round 0 to round 1 (0.60 → 1.59) and then drifts down,
  which is the opposite of the story the raw totals tell.
- **Rounds whose reviewer panel was never recorded are named rather than dropped.** These are
  rounds interrupted before a manifest was written; they are excluded from the consensus figure
  and counted separately, so the share above is not quietly computed on a short denominator. The
  count is in ROUNDS because that is the unit consensus is measured in — a resumed run can change
  panel between rounds, so a per-run count would not describe what was excluded.


### Changed

- **`syncade --gc` now reclaims worktrees from runs it used to protect forever.** A run that could
  still be resumed was shielded unconditionally and nothing ever ended that protection — on this
  project's own corpus that reached **4.4 GB across 73 run directories** with `--gc` reporting
  nothing to do. Worktrees are now released after `gc.worktree_max_age_days` (default **14**, `0`
  restores the old behaviour). Run history, findings, manifests and summaries are still never
  deleted; a worktree is reconstructible from the SHA the run already records, which is the whole
  reason it is the tier that may go.
- **A worktree is no longer removed just because its transcripts aged out.** Worktree selection
  used to ride transcript selection, so under normal run volume a one-day-old worktree could be
  deleted while `gc.worktree_max_age_days` advertised 14 days. The two are now selected by
  independent rules, and retention is documented as four tiers rather than two.


### Known issues

- `syncade --metrics` reports `0 minors, 0 nits, 0 dismissed` for runs that were INTERRUPTED
  before their loop manifest was written, even when the finding rows exist in `metrics.db`. The
  blocker counts — the figures this project publishes — are re-derived from the per-round
  authority and are correct for every round that left evidence, while the other three severity
  columns still come from the loop manifest. `--metrics` separately names the rounds that left no
  blocker evidence at all, which it counts as zero by convention rather than by measurement, so
  the blocker total is a lower bound over the corpus. The report names those rounds explicitly
  and notes they are excluded from the blocker curve, but does not label the top-line total as
  a lower bound.
  Raised as a non-blocking finding by syncade's own review of the release commit.

## [0.6.3] — 2026-08-18

### Changed

- **syncade is on PyPI.** `uv tool install syncade` (or `pip install syncade`) replaces the
  `git+https://…` URL as the documented install. That URL had no ref, so what you installed was
  not a release — it was whatever `main` happened to be that day, while still reporting the last
  release's version number. Releases are published by the tag-triggered workflow using PyPI
  trusted publishing (OIDC; no token, no stored secret). The git URL still works and is now
  documented as the way to track unreleased `main`.

### Added

- **syncade tells you when it is out of date.** The first syncade command in a new terminal or
  harness window checks once whether a newer release exists, and says so — including whether your
  installed `/syncade` skill has fallen behind the package, which happens whenever you upgrade
  one and not the other. It only ever prints: it cannot block a run, change an exit code, or fail
  one, and every error is silent. Nothing about your repo, diff, or run is sent. A release can
  also be flagged **critical** (a security issue, or a version known to be broken), which is
  louder and survives `--quiet` — but still never stops you. Disable with
  `syncade --config set update.check false`, or set `CI`.
- **`syncade --update`** upgrades syncade and re-installs any skill you already have. It works
  out how syncade was installed rather than guessing, and refuses — naming the manual command —
  when it cannot tell, when a review is still running, or when syncade is a source checkout.
  A running process cannot switch to the version it just installed, so it exits and asks you to
  re-run your command.

## [0.6.2] — 2026-08-17

### Changed

- **A run now has a spending ceiling by default.** `budget_tokens` defaults to 50,000,000 —
  previously there was none, so a first run was bounded only by the round count and the
  per-call timeout. Crossing it stops the loop at a phase boundary, keeps every completed
  round, and `syncade --resume` continues. Sized against our own history rather than picked
  round: the median run uses 11M tokens and 9 in 10 stay under 39M, so this stops about 4% of
  runs. **Set `budget_tokens = 0` — or `budget_usd = 0` — for no ceiling.**

  It counts tokens rather than dollars because the dollar figure is an API-equivalent
  valuation: on a subscription it is not money you spent, and a ceiling should not interrupt
  you over a number that is not real.

  A ceiling only bounds what has *not* started. A round already under way runs to completion,
  so a run can finish slightly over — and a run that reaches a verdict keeps it. Stopping is
  for work not yet done, not a re-judgement of work already finished.

### Added

- **You are warned at 80% of the ceiling**, once, with the headroom left — so a long run's stop
  is visible while there is still a round to react in, rather than arriving as a surprise.
- **The stop tells you what to do.** Which ceiling you crossed and what it was set to, what was
  actually spent, that your completed rounds are preserved, and the exact `syncade --resume`
  command.

### Fixed

- **One forbidden key no longer discards an entire review.** A reviewer that returned a
  complete, correct verdict and added one advisory field had the whole thing rejected — a real
  case cost 1.3M tokens of review, and took the other reviewer's work with it. Such a key is
  now dropped, with a warning naming it, and the review is used. A genuinely malformed
  response is still rejected: this only applies when the *sole* problem is an unrecognised
  field. Applies to reviewers, the spec drafter and the spec auditor.


## [0.6.1] — 2026-08-15

### Added

- **A run that is killed no longer loses what its reviewers had already said.** Reviewer output
  used to sit in syncade's own memory until the subprocess finished, so anything that killed
  syncade — `SIGKILL`, an OOM, a closed laptop — took the whole round's output with it, leaving
  an empty directory and nothing to diagnose from. Output is now copied to
  `<round>/<name>.stdout` / `.stderr` as it is produced. The honest limit: what is guaranteed is
  everything written so far, not every byte produced — a chunk still in flight when the parent
  dies is lost. The fixer's output is covered too: a fixer that hit its timeout previously left
  an empty file after forty minutes of work, which is exactly the record you want when you are
  trying to find out what it was doing.
- **`dispatch.json` records each reviewer's child process id.** Written as the child appears,
  which is what lets a post-mortem tell "something killed syncade and orphaned its reviewers"
  from "the reviewers died and it followed" — `ps` those pids, or observe their absence.

- **Reviewers can be given a directed bug-class sweep** (`bug_class_sweep`, per reviewer,
  off by default). A short set of correctness angles the reviewer must work and name before it
  can ship: what a deleted line used to guard, whether a changed function still suits its
  callers, the classic footguns for the diff's language, and whether a new wrapper really
  forwards to the thing it wraps. Severity keys off whether the reviewer could reproduce the
  defect, not how confident it feels, so it raises what gets looked at without inventing
  blockers. Opt-in for now while we measure whether the checklist helps recall or narrows the
  search — set it on one reviewer and off on another to see for yourself.
  Contributed by [@one-kash](https://github.com/one-kash).

### Changed

- **Default `max_rounds` is now 5, up from 3.** Rounds still end the moment a round is clean, so
  this is a ceiling rather than a target — but it does raise the worst case. There is still no
  default cost ceiling; set `[loop] budget_usd` (or `--budget-usd`) if you want one.

## [0.6.0] — 2026-08-12

### Added

- **Running out of provider quota no longer throws the run away.** When your account's usage
  window empties mid-review, the provider refuses to answer. syncade used to read that as a
  subprocess crash, exit 40, and discard everything — in one case after both reviewers had
  already finished. It now stops the way a budget ceiling does: exit 25, every completed round
  kept on disk, and `syncade --resume <run-id>` continues once the window resets. Recognised
  wherever it happens — a reviewer, the judge, or the fixer.
- **You are told about it, even under `--quiet`.** The notice names which provider ran out (the
  first question on a mixed panel), says your completed work survived, warns that retrying now
  fails the same way rather than letting you burn another attempt, and prints the exact resume
  command. A terminal condition you can act on is not progress chatter, and `--quiet` is exactly
  when nobody is watching the scrollback.
- **Every round now records what it dispatched, before it dispatches it.** A small
  `dispatch.json` lands in the round directory naming the reviewers in flight, the timeout they
  were given, the parent process id and the UTC time. Round artifacts are otherwise written after
  the whole panel returns, so a run killed mid-review left an empty directory and nothing to
  diagnose.
- **`syncade --resume` says so when the previous run was hard-killed.** A run stopped by
  `SIGKILL` (or a machine that went away) cannot finalize its own status file, so it leaves one
  claiming the run is still in progress. Resume now recognises that signature and reports the
  phase it died in, instead of resuming in silence.

### Changed

- **Exit 25 now has two causes, and every surface that mentions it says which.** It has always
  meant "stopped early at a phase boundary, resumable"; it now covers both your configured
  token/dollar ceiling (`budget_exceeded`) and a provider refusing on an exhausted usage limit
  (`provider_usage_limit`). They want opposite responses — raise the cap, versus wait for a
  window you do not control — so `termination_reason` distinguishes them and the exit-code
  tables, `--resume` help and refusal text no longer describe the budget alone.
- **`syncade --metrics` reports a quota stop as `QUOTA`, not `BUDGET`.** Labelling it by exit
  code alone blamed your configuration for someone else's rate limit.

## [0.5.1] — 2026-08-11

### Fixed

- **A warning that could cost you the review's work is no longer silenced by `--quiet`.** When
  the fixer commits, syncade advances your branch but does not touch your working tree, so
  `git status` afterwards shows what looks like a staged revert of the work it just did —
  committing that silently undoes the run. syncade warns about this and offers the
  non-destructive recovery, but the warning went out at normal verbosity only, so anyone running
  quietly saw nothing. Disclosures about where committed work ended up are now always printed:
  that one, plus the cases where the branch could not be advanced, the fixer committed onto a
  detached HEAD, or its commit was not a descendant of where the round started.

### Changed

- **The README says what the default review panel actually is, and why.** The recommended setup
  is two OpenAI reviewers running different prompts — one standard, one adversarial — which is
  diverse in prompt but not across model vendors. That is a measured recommendation rather than
  a default nobody revisited: reviewing the same change in the same round, the OpenAI reviewer
  raised roughly three times as many findings as the Anthropic one. Every reviewer, the judge
  and the fixer remain independently configurable, so a cross-vendor panel is one setting away.
- **What a run costs is now stated before you start one**, from measurement rather than
  estimate, along with a short list of known limitations — including that a clean verdict is
  evidence rather than proof, since the panel is not deterministic.

## [0.5.0] — 2026-08-11

### Fixed

- **`--install-skill` no longer destroys files it did not write.** It replaced the whole
  destination directory: measured on a real skill folder, a hand-edited `SKILL.md` was
  overwritten and two unrelated files were deleted — at exit 0, with no prompt, warning or
  backup.

  It now checks first, and refuses (exit 60) if installing would lose anything, naming every
  file and what would happen to it. `--force-install` overrides that deliberately and still
  lists what it destroys.

  Deciding what is yours is done by **content**, not by filename or a marker file: syncade
  records the checksum of every file it writes, so a file it wrote in an older version is
  recognised and an ordinary upgrade needs no flag, while anything else is treated as yours.
  A missing or damaged record makes it more cautious, never less. Installing for both
  harnesses at once is all-or-nothing — a failure on one no longer leaves the other
  half-upgraded, and syncade refuses outright if your two harness directories are the same
  or one sits inside the other, since installing both would have one write into the other. A destination that is a symlink to your own checkout is still replaced as
  documented; a symlink you created *inside* the destination is now treated as yours.

- **Declared dependency floors are now the versions that actually work.** The build
  requirement said `setuptools>=68`, which cannot build this project at all — the license
  metadata format it uses needs 77 or newer, so an environment resolving to the declared
  floor failed outright. `pydantic>=2.0` was also untrue in practice: one configuration
  default was an integer where the schema says float, which older pydantic warns about, and
  syncade's own test leg treats warnings as errors. The default is now a float, and CI
  installs the declared floors on every run so a floor that stops working is a build failure
  rather than a surprise for whoever installs from source.

- **`ruff` is bounded to a tested range.** It was declared with no upper bound, so a new
  release could fail the formatting check with no code change.

- **Continuous integration reports again.** Every gate ran as a sequential step in one job,
  so the first failure stopped the rest: a single test-fixture bug meant linting, formatting,
  the file-length cap and both leak checks had not executed on any push since this repository
  went public. Gates are now independent jobs, and each reports its own result.

- **The documentation no longer implies syncade is on PyPI.** The bundled skill guides said
  installation works from `pip install syncade`; it does not — install from the repository as
  the README shows.

### Removed

- **`scripts/install-skill.sh`.** `syncade --install-skill` is the supported installer and
  the only one that was ever published; the shell copy was a dev-only convenience that had
  to be taught every safety rule twice.


## [0.4.0] — 2026-08-09

Don't hurt the operator, and don't throw away work you already paid for. Two themes: a
refused command now leaves your directory as it found it, and a run no longer dies for a
mechanical reason unrelated to the code under review. The second theme came from another
project running syncade as a tool and hitting two aborts that discarded whole runs — an
argument-list limit that killed every reviewer in 0.0s, and a single mistyped character in a
quotation that ended a 12-minute run at exit 70.

### Changed

- **A refused command no longer leaves a repository behind.** A mistyped brief path, an
  unknown OpenSpec change-id, a broken `.syncade/config.toml`, an invalid CLI override or a
  failed auth check are all detected before syncade touches your directory. Previously any of
  them could create `.git`, a baseline commit, a tracked `.gitignore` and 33 exclude rules on
  the way to exiting — and in an existing repo a mistyped filename was reported as a
  default-branch problem.
- **Auto-init only initializes an EMPTY directory.** A populated one is refused with
  instructions, because the baseline commit captures whatever it finds and the exclusions are
  defeatable — a key under a name no denylist knows, or your own `.gitignore` re-including
  `.env` with `!`, both reached git history. `--allow-auto-init` overrides this deliberately;
  it is informed consent, not a safe mode, and the warned-about leaks still occur under it.
  Any pre-existing `.git` is refused regardless of the flag.
- **`--install-skill` can no longer silently replace another mode.** Combining it with a
  PR_DOC or any other one-shot mode was accepted, exited 0, and dropped the other intent —
  `syncade <brief> --install-skill claude` ran no reviewers and reported success. All such
  combinations are now rejected naming both flags.
- **A refused run now REMOVES the repository it auto-initialized.** The protection above is
  validate-before-mutate; this is its backstop, for refusals that happen after the mutation
  (an unresolvable `--base`, an unreachable scope, a failure inside the run itself). If the
  directory was empty before syncade touched it, the `.git` it created is removed again.

  It removes **`.git` and nothing else.** Two things deliberately survive:

  - the starter `.gitignore` — syncade never deletes a file whose bytes you could have edited;
  - anything under `.syncade/` — run records are never deleted, by anything (`--gc` has the
    same rule, because `--metrics` is rebuilt from that tree).

  So a refused run in a fresh directory leaves no repository, and nothing else changes. Two
  limits, both deliberate: with `--allow-auto-init` in a **populated** directory nothing is
  removed at all (syncade cannot tell which files are yours, and leaving a repository behind is
  the safer error), and an interrupted run keeps its repository so it stays recoverable.

### Fixed

- **A repo with committed binary files is reviewable again.** Screenshot baselines, vendored
  fonts, any committed binary: syncade renders its diff with `--text` (the only defence
  against a `.gitattributes` that hides real source changes), which turned 66 KB of real diff
  into 3.1 MB of raw image bytes on the repo that reported this. That was passed to the
  reviewer CLI as a command-line argument, and the OS refused it — `[Errno 7] Argument list
  too long`, both reviewers dead in 0.0s, exit 40, **no review at all**. `--auth-check` and
  `--selfcheck` had passed a minute earlier; neither builds a prompt, so neither could see it.

  Three changes, all needed: the prompt now travels on **stdin**, binary file content is
  **left out of the prompt** (paths and withheld byte counts are listed instead, so the
  omission is disclosed), and the size is **checked before dispatch**. Binary detection reads
  the bytes, never `git diff --numstat` — measured, git reports a plain text file marked
  `*.py -diff` as binary exactly as it reports a PNG, so trusting it would let a committed
  `.gitattributes` erase source changes from the reviewer's diff.

- **A committed binary can no longer smuggle content into a reviewer's prompt.** syncade
  captures subprocess output as bytes and decodes it explicitly. Previously it read child
  output in text mode, where Python rewrites a lone carriage return to a newline — and since
  binary files are rendered as raw text in the diff, a payload containing `\r` could forge a
  `diff --git` boundary and slip past the binary filter. Found by the blind review panel.

- **A synthesizer typo no longer discards the whole run.** The judge quotes each reviewer's
  finding verbatim into `provenance[].original_description`, and syncade checks it. One
  dropped backtick — in a quotation, not in the finding — ended a run at **exit 70** after
  713 seconds of reviewer time, throwing away six valid findings and three unrun rounds.

  syncade now **repairs** the quotation from the reviewer's own text and continues, recording
  both strings in the round manifest. This is stricter than before, not looser: the rendered
  quote used to be verbatim only because the check passed, and is now verbatim because it was
  copied from the source. Inventing a `reviewer_name`, an out-of-range finding index, or a
  wrong severity is still fatal — those are attribution claims with no ground truth to
  restore, and the abort is reserved for them.

### Added

- **`[loop] max_diff_bytes`** (default `1000000`) — a ceiling on the diff a reviewer is
  actually handed, measured after repo-context stripping and binary elision. Over it, the run
  is refused before anything is dispatched (exit 60, `diff_too_large`) with the size, the
  ceiling and what to do about it, rather than truncated — a verdict on a partial diff is a
  verdict on the wrong code. `--doctor` predicts the same refusal for $0.

  The default clears the largest diff this project has ever reviewed by ~7x, and sits just
  under `codex exec`'s hard 1,048,576-character input limit so syncade's message arrives
  before the provider's. Lower it if you want a tighter review surface.

- `--allow-auto-init`, to initialize a git repository in a directory that already has files
  in it. See the auto-init note above for what that commit can capture.
- **Round manifests record diff size**: `snapshot.diff_bytes` (what the repo produced) and
  `snapshot.diff_bytes_reviewed` (what a reviewer was handed, after repo-context stripping),
  both UTF-8 byte counts. Durable artifact fields, so anything parsing
  `round-N/manifest.json` will see them.

  A size is recorded **only by the path that measured it**. Either field is `null` when a run
  did not measure it — notably on a resumed round, which does not inherit the original run's
  numbers. `null` therefore means "not measured", and is distinct from `0`, which means
  "measured, and empty" (the all-stripped case, where a real diff filters to nothing). No
  path infers a size, so a count is never fabricated for a diff nobody had.

  There is still no diff size cap; this is the measurement a future cap needs, and it cannot
  be backfilled.

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
- **All-deactivated-blockers is now exit 10 (decision needed), not SHIP.** When two or more
  distinct reviewers each raise a blocker and the synthesizer deactivates every one of them,
  the result is exit 10 (`blockers_all_deactivated`) with a `decision-needed.md`. Previously
  this was mechanically indistinguishable from a clean SHIP. This is a terminal state — no
  producer runs — because there is no active blocker to fix.

### Fixed

- **The reviewer-facing diff is now hermetically isolated from repo config.** Every diff
  invocation pins `--no-ext-diff`, `--no-textconv`, `core.attributesFile=/dev/null`, and
  every setting that demonstrably changes diff bytes (prefix format, context lines, algorithm,
  rename detection, etc.) via `-c` flags that outrank all config files. A repo-controlled
  `diff.external` driver, textconv filter, or `.gitattributes` `-diff` marker can no longer
  substitute the bytes reviewers see for something controlled by the repo being reviewed.
- **The CLI pre-auth default-branch commit guard now defers for based, scoped, and resume
  runs** instead of refusing them upfront. Whether those runs commit depends on the filtered
  diff, which the CLI cannot compute before snapshotting. The CLI now refuses only when it can
  *prove* the run commits (no `--base`/`--scope`, not a resume). All other cases are deferred
  to `run_review`, which decides authoritatively at the run-entry choke — still before any
  reviewer or producer subprocess. Previously, valid based/scoped/no-change runs on the
  default branch were falsely refused.
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

[Unreleased]: https://github.com/syncade-ai/syncade-ai/compare/v0.7.1...HEAD
[0.7.1]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.7.1
[0.7.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.7.0
[0.6.3]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.6.3
[0.6.2]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.6.2
[0.6.1]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.6.1
[0.6.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.6.0
[0.5.1]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.5.1
[0.5.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.5.0
[0.4.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.4.0
[0.3.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.3.0
[0.2.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.2.0
[0.1.0]: https://github.com/syncade-ai/syncade-ai/releases/tag/v0.1.0
<!-- 0.2.0 - 0.4.0 shipped as commits and were TAGGED RETROSPECTIVELY later, at
     the commits whose pyproject carried each version. Two public commits are titled
     "syncade v0.2.0"; the later one is the tag target. -->
