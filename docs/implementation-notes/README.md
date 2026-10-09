# Implementation notes

Implementation notes record current implementation decisions — packaging, environment, workflow conventions — that the historical architecture revisions do not settle and that must not silently override them.

Architecture semantics live in [docs/architecture](../architecture/README.md). Sequencing lives in [ROADMAP.md](../../ROADMAP.md). These notes record the current state of the implementation and the reasoning behind local decisions.

Conventions:

- One topic per note, named `YYYY-MM-DD-<topic>.md`.
- A newer note supersedes an older note on the same topic; historical architecture documents are never rewritten to match.
- A note records a decision that was actually made, with its evidence and date.

## Notes

- [2026-10-09 — M0 foundation](2026-10-09-m0-foundation.md): implementation location, Python version pin, Beads/OpenSpec workflow split.
