# Contributing to syncade

Thanks for your interest. Syncade is a Python 3.11+ project with a `src/` layout; its one
runtime dependency is `pydantic>=2.0`.

## Setup

```bash
uv venv --seed
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Real review runs (and the smoke suite) also need `git`, and the provider CLIs you
configure — `claude` (Anthropic actors) and `codex` (OpenAI actors) — on your `PATH` and
authenticated. **Platform: macOS and Linux only** (see the README's Platform support
section).

## The gate

Run these before opening a PR; CI runs the same set:

```bash
uv run python -m pytest -q                     # full suite (smoke is deselected by default)
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall -q src
scripts/check-loc.sh 500                        # no tracked .py file over 500 CODE lines
scripts/check-no-private-identifiers.sh         # no private identifiers in content or filenames
git diff --check                                # no whitespace errors
```

Run the smoke suite separately when your change touches real CLI/model paths — it shells
out to authenticated `claude` and `codex`:

```bash
uv run python -m pytest -q -m smoke
```

## House rules

- **Smallest correct change.** Match the surrounding style; avoid speculative
  abstractions, new dependencies, or unrelated cleanup.
- **500 code-LOC cap per file** (blocking). When a module outgrows it, split it into a
  package and keep public re-exports in `__init__.py` — but patch/test the concrete lookup
  site, not the package-level name.
- **No private identifiers.** The leak gate above fails the build if a real person's or
  project's name or absolute home path lands in tracked content or a filename. Use
  synthetic values in fixtures.
- **Tests are the contract.** Add focused tests for new behavior; for a bug fix, add the
  test that would have caught it. Non-trivial logic leaves at least one runnable check.
- **Dogfood substantive changes.** Syncade reviews itself — run `syncade <pr-brief>` on
  your change (on a *feature branch*; it refuses the default branch unless
  `--allow-default-branch`) before calling a broad change done.

## Pull requests

Keep PRs focused and describe the change and how you verified it. By contributing you
agree your contributions are licensed under the project's [Apache License 2.0](LICENSE).
