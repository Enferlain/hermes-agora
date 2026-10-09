# M0 foundation decisions

9 October 2026

Decisions made while establishing the implementation foundation (ROADMAP.md, M0). The v0.2 architecture contracts are unchanged by this note.

## Implementation location: standalone `agora` package

The v0.2 implementation handoff (section 1) framed the first coding session as an isolated replay namespace inside a Hermes checkout. The repository README instead proposes a standalone `agora` package here, with eight conceptual modules: `contracts`, `stores`, `context_memory`, `coordinator`, `commit_gate`, `fake_sidecar`, `ports`, `runner`.

Decision: implement in this repository as a standalone `agora` Python package. The eight module responsibilities and ownership boundaries from the handoff are preserved; only the location differs. Hermes integration happens later through an explicit adapter (roadmap M4), verified against the actual hermes-agent checkout rather than assumed signatures. This records the reconciliation explicitly instead of treating either document as silently overridden.

## Python version

`requires-python = ">=3.14,<3.15"`.

Evidence, checked 9 October 2026 against [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (default branch `main`, commit `1744a19e0df568c647e4f3ff9c37f2a284a282fb`, cloned read-only to `~/Projects/hermes`):

- hermes-agent declares `requires-python = ">=3.11,<3.15"`, with 3.14 documented as the newest Python with known wheels.
- Agora's pin sits inside that range and matches Hermes' newest-known target, so one 3.14 interpreter remains usable by both once the M4 adapter exists.
- Local development uses uv-managed CPython 3.14; the older system interpreter is not relied upon.

## Workflow conventions

- Beads tracks any item that needs attention, large or small; it is not reserved for big work.
- OpenSpec is reserved for bigger changes or projects that need two or more task lists with a specific design laid out. It is not used for M0; expect the first OpenSpec changes during M1–M3, when replay behavior contracts are being implemented.

## Status

M0 scope: uv-managed project (`pyproject.toml`, `uv.lock`, Python 3.14), `agora` package skeleton with a version-only CLI entry point, pytest/ruff/ty gates, and documentation navigation (architecture index, implementation notes). No replay contracts are implemented yet — see Beads for the M1+ sequence.
