# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, minor versions may
include breaking changes.

## [Unreleased]

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
