# Agent Instructions for Hermes Agora

This document provides practical guidance for AI coding agents working in this repository.

## Start Here

Before significant work:

1. Read `README.md`.
2. Read `docs/architecture/README.md`.
3. Read the current architecture documents relevant to the task.
4. Inspect the existing implementation and tests before making changes.

`docs/architecture/README.md` defines the current architecture revision and document precedence.

Historical architecture revisions are retained for context. Do not rewrite an older revision to reflect a newer design decision.

If the implementation appears to conflict with the current architecture, verify the conflict and surface it before making a broad architectural change. Do not silently invent missing design decisions.

## Environment

Use the project environment through `uv`.

```bash
uv sync
uv run python
```

Prefer `uv run ...` over system Python or globally installed project tooling.

If the repository specifies a Python version in `pyproject.toml` or `.python-version`, use that version.

## Dependencies

Project dependencies belong in `pyproject.toml`.

Use `uv` to modify them:

```bash
uv add <package>
uv add --dev <package>
uv remove <package>
```

Commit `uv.lock` when dependency resolution changes.

Avoid adding a dependency when the standard library or an existing dependency already covers the requirement cleanly.

## Tests

Run focused tests while developing:

```bash
uv run pytest tests/path/to/test_file.py -q
```

Run the full suite when the scope of the change warrants it:

```bash
uv run pytest
```

For debugging:

```bash
uv run pytest tests/path/to/test_file.py -vv --tb=short
```

Prefer behavior-focused tests over tests coupled to implementation details.

For state machines and persistence-sensitive behavior, cover transitions and failure states explicitly.

Replay and fault-injection fixtures should be deterministic unless a test is specifically intended to evaluate model behavior.

Bug fixes should normally include a regression test.

Do not update expected results merely because a test fails; determine whether the implementation or expectation is wrong first.

## Linting and Formatting

```bash
uv run ruff check .
uv run ruff check . --fix
```

Format changed files:

```bash
uv run ruff format path/to/file.py tests/path/to/test_file.py
```

Verify formatting:

```bash
uv run ruff format --check path/to/file.py tests/path/to/test_file.py
```

## Type Checking

```bash
uv run ty check
```

Use a focused existing path during development when useful.

Do not weaken lint, formatting, test, or type-check configuration merely to make a change pass.

## WSL / Windows Filesystem

When the repository is on the Windows filesystem and commands are run through WSL, Python tooling may occasionally become unusually slow or remain silent.

Use bounded runs when needed:

```bash
timeout 180 uv run pytest tests/path/to/test_file.py -q
timeout 60 uv run python -c "print('OK')"
```

If a command repeatedly stalls, report it rather than repeatedly polling or spawning duplicate runs.

## Editing Guidelines

Prefer the smallest coherent change that satisfies the task.

Before changing an existing component:

1. inspect its implementation;
2. inspect relevant callers and consumers;
3. inspect its tests;
4. identify relevant shared contracts;
5. make the change;
6. add or update tests;
7. run the relevant quality gates;
8. inspect the final diff.

Avoid unrelated cleanup or structural refactors.

Do not reorganize modules merely to make them resemble an architecture diagram.

## Interfaces and Contracts

The project relies on explicit runtime contracts.

When changing a shared type or interface:

- inspect its producers and consumers;
- update boundary tests;
- preserve semantically distinct fields rather than collapsing them for convenience;
- prefer typed structures over loosely structured dictionaries where the surrounding code uses typed contracts.

If the current architecture specifies a contract that has not yet been implemented, follow the implementation handoff rather than inventing a parallel representation.

## Runtime State and Secrets

Do not commit local runtime artifacts such as:

- databases;
- journals or outboxes;
- logs;
- replay output;
- caches;
- model files;
- `.env` files;
- credentials or session material.

Checked-in fixtures should live under an explicitly tracked test or fixture path.

Never place Discord credentials, API keys, tokens, or private runtime material in source files, tests, fixtures, logs, or documentation.

`.env.example` may document variable names but must contain no real credentials.

## Changelog

`CHANGELOG.md` follows the Keep a Changelog structure with an active `[Unreleased]` section.

Update it for completed changes that materially affect:

- behavior;
- architecture;
- public or shared interfaces;
- persistence or state semantics;
- developer workflow;
- significant fixes.

Do not add entries for trivial internal edits or mechanically describe changed files.

Prefer:

```text
- Prevented stale effect revisions from being dispatched after supersession.
```

over:

```text
- Updated commit_gate.py.
```

Use the standard headings when applicable:

```text
Added
Changed
Deprecated
Removed
Fixed
Security
```

## Documentation

Update documentation when implementation changes a documented interface, workflow, or architecture contract.

Do not duplicate architecture explanations into `AGENTS.md` or large source comments. Keep them in the architecture documents.

Do not rewrite historical architecture revisions. Record newer decisions in the current revision or a new revision/addendum as appropriate.

## Working With Incomplete Design

Some project questions are intentionally unresolved.

When implementation reaches one:

1. check the current architecture documents and existing code;
2. reduce the uncertainty to a concrete question;
3. surface it rather than hiding an arbitrary choice in code.

Temporary implementation choices should be local and reversible.

Do not present an experimental threshold, heuristic, or stub as an established project rule.

## Handoff

Before completing a coding task:

1. inspect the final diff;
2. remove debugging code and generated artifacts;
3. run the relevant tests and quality gates;
4. update affected documentation;
5. update `CHANGELOG.md` when the completed change is notable;
6. summarize what changed, what was validated, and anything unresolved.

Do not claim a check passed unless it completed successfully.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
