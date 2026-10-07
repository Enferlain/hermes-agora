# Agent Instructions for Hermes Agora

This document provides practical guidance for AI coding agents working in this repository.

## Start Here

Before making significant changes:

1. Read `README.md`.
2. Read this `AGENTS.md`.
3. Check `docs/architecture/README.md` for the current architecture revision.
4. Read the architecture documents relevant to the area being changed.
5. Inspect the existing code and tests before proposing new structure.

Do not rely on remembered or inferred project structure when the repository can be inspected directly.

## Architecture Documents

Architecture and implementation design live under:

```text
docs/architecture/
```

`docs/architecture/README.md` identifies the current revision and document precedence.

Historical architecture versions are retained for context. Do not edit an older version to describe a newer design decision.

When code appears to conflict with the current architecture:

- verify that the conflict is real;
- identify the relevant document and section;
- surface the discrepancy before making a broad architectural change.

Do not silently resolve architectural ambiguity by inventing new behavior.

## Environment

Use the repository's `uv` environment.

```bash
uv sync
uv run python
```

Prefer `uv run ...` over invoking a system Python or globally installed tooling.

Do not manually install project dependencies with `pip` unless a task explicitly requires debugging packaging behavior.

If the repository specifies a Python version in `pyproject.toml` or `.python-version`, use that version.

## Search and Inspection

Prefer `rg` for repository search.

```bash
# Search text
rg "CommitGate"

# Search Python definitions/usages
rg "class .*Gate|def .*commit" src tests

# List tracked files
git ls-files

# Find files by name
rg --files | rg "memory|commit|replay"
```

Use `git status` and `git diff` frequently while working:

```bash
git status --short
git diff
git diff --staged
```

Inspect the implementation and its tests before modifying an interface.

Do not assume a README or architecture example exactly matches current code if the implementation has since changed.

## Python Quality Gates

Use project tooling through `uv`.

### Tests

Run the narrowest relevant tests while developing:

```bash
uv run pytest tests/path/to/test_file.py -q
```

Run a broader affected test set before handoff:

```bash
uv run pytest
```

For debugging:

```bash
uv run pytest tests/path/to/test_file.py -vv --tb=short
```

Do not repeatedly rerun the entire suite when a focused test can answer the current question.

### Linting

```bash
uv run ruff check .
```

Apply safe automatic fixes when appropriate:

```bash
uv run ruff check . --fix
```

### Formatting

Format changed Python files:

```bash
uv run ruff format path/to/file.py tests/path/to/test_file.py
```

Verify formatting:

```bash
uv run ruff format --check path/to/file.py tests/path/to/test_file.py
```

### Type Checking

```bash
uv run ty check
```

Use a focused path during development when useful:

```bash
uv run ty check src/hermes_agora
```

Do not weaken lint, test, or type-check configuration merely to make a change pass.

## WSL / Windows Filesystem

If the repository is being accessed from WSL while stored on the Windows filesystem, Python tooling can occasionally become unusually slow or appear silent.

When that occurs, prefer bounded commands:

```bash
timeout 180 uv run pytest tests/path/to/test_file.py -q
timeout 60 uv run python -c "import hermes_agora; print('OK')"
```

If a command repeatedly stalls, report the problem rather than polling or spawning repeated copies of the same command.

## Dependency Changes

Project dependencies belong in `pyproject.toml`.

Use `uv` to modify them so the lockfile remains consistent.

```bash
uv add <package>
uv add --dev <package>
uv remove <package>
```

Commit `uv.lock` when dependency resolution changes.

Avoid adding a dependency when the standard library or an existing dependency already covers the requirement cleanly.

## Repository Structure

Keep this section limited to stable top-level responsibilities.

```text
README.md                   Project overview
AGENTS.md                   Instructions for coding agents
pyproject.toml              Python/project/tool configuration
uv.lock                     Locked Python dependencies

docs/
  architecture/             Versioned architecture and implementation design
  research/                 Supporting research and investigations
  threat-model/             Threat-model material
  evaluation/               Evaluation methodology and scenarios

src/                        Project source code
tests/                      Automated tests and replay scenarios
```

Inspect the current tree rather than assuming deeper paths from this document.

Update this section only when the stable top-level layout changes.

## Editing Guidelines

Prefer small changes with clear ownership.

When modifying an existing component:

1. inspect its callers;
2. inspect its tests;
3. identify relevant contracts/types;
4. make the smallest coherent change;
5. add or update tests;
6. run focused quality gates;
7. review the final diff.

Avoid opportunistic refactors unrelated to the current task.

Do not rename or reorganize modules solely for aesthetic consistency while implementing unrelated behavior.

## Interfaces and Contracts

The project relies heavily on explicit runtime contracts.

When changing a shared type or interface:

- search for all producers and consumers;
- update tests at the boundary;
- preserve distinctions represented by separate fields instead of collapsing them for convenience;
- avoid replacing typed state with loosely structured dictionaries unless the existing design explicitly calls for it.

If a contract described in the current architecture has not yet been implemented, follow the implementation handoff rather than inventing a parallel representation.

## Tests

Prefer behavior-focused tests over tests coupled to implementation details.

For state machines and persistence-sensitive behavior, cover transitions and failure states explicitly.

For replay scenarios, keep fixtures deterministic unless the test is specifically intended to evaluate model behavior.

A bug fix should normally include a regression test demonstrating the failure.

Do not change expected test output merely because a test fails; determine whether behavior or expectation is wrong first.

## Runtime and Generated State

Do not commit:

- local databases;
- journals/outboxes produced during development;
- model weights;
- downloaded caches;
- secrets;
- `.env` files;
- transient replay results;
- logs.

Use the repository's ignored runtime/data directories for local state.

Checked-in test fixtures should live under `tests/` or another explicitly tracked fixture directory rather than generic runtime-data paths.

## Secrets

Never place credentials, tokens, Discord session data, API keys, or private runtime material in source files, fixtures, logs, or committed configuration.

Use environment variables or the repository's documented secret mechanism.

`.env.example` may document variable names but must contain no real credentials.

Before committing changes involving configuration or logs, inspect the diff for accidental secrets.

## Documentation

Update documentation when a code change alters a documented interface, workflow, or architecture contract.

Do not duplicate large architecture explanations into source comments or `AGENTS.md`; link to the relevant document instead.

Historical architecture revisions should remain historical.

If a new architectural decision supersedes the current revision, document it as a new revision/addendum rather than silently rewriting the old record.

## Working With Incomplete Design

Some parts of the project are intentionally unresolved and expected to be tested empirically.

When implementation reaches an unresolved design question:

1. confirm that existing code/docs do not already answer it;
2. identify the smallest concrete question blocking progress;
3. surface it to the user rather than burying an arbitrary choice in code.

Temporary implementation choices should be clearly local and reversible.

Do not present an experimental threshold, heuristic, or stub as an established project rule.

## Handoff Checklist

Before completing a coding task:

1. inspect `git diff`;
2. remove debugging code and accidental generated files;
3. run focused tests;
4. run the relevant lint/format/type checks;
5. run broader tests when the change warrants it;
6. update affected documentation;
7. summarize:
   - what changed;
   - tests/checks run;
   - unresolved issues or assumptions.

Do not claim a quality gate passed if it was not run successfully.
