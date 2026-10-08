# Hermes Agora roadmap

Proposed development sequence · 8 October 2026

> **Status:** Active roadmap
>
> Milestone completion is tracked in Beads. This file describes intended sequencing and exit criteria.

Agora's goal is one continuous autonomous agent that can participate naturally in open human environments. Discord is its first live adapter. Progress means useful participation, continuity and discretion, together with bounded capabilities and reliable delivery.

This roadmap organizes implementation and experiments. It does not replace the architecture or maintain task status. Beads owns executable tasks, dependencies and completion state; OpenSpec owns approved change requirements. The architecture documents own runtime semantics. No milestone is marked complete merely because its documentation exists.

## Starting point and authority

Repository inspected: [Enferlain/hermes-agora](https://github.com/Enferlain/hermes-agora/tree/b4d59ee7936480fa23cade58f90c62966ace7645), commit `b4d59ee7936480fa23cade58f90c62966ace7645`.

Observed: README, agent guidance, Apache-2.0 license, v0.1/v0.2 architecture documents, OpenSpec configuration and Beads setup are present. There is no checked-in runtime package, Python project configuration or test suite in that snapshot. GitHub issue search returned no issues; that does not establish that the local Beads database is empty.

Current design precedence is [v0.2 addendum](https://github.com/Enferlain/hermes-agora/blob/master/docs/architecture/v0.2/architecture-v0.2-addendum.md), then [v0.2 implementation handoff](https://github.com/Enferlain/hermes-agora/blob/master/docs/architecture/v0.2/synthetic-replay-implementation-handoff.md), then unaffected v0.1 provisions. The roadmap proposes sequencing; it cannot silently override those contracts.

Dates and release numbers should follow demonstrated capability. Architecture revision v0.2 is not evidence that a software release exists. The sequence below deliberately makes the first complete replay runnable before requiring model inference or a Discord connection.

## Milestone sequence

| Milestone | Usable result | Depends on | Exit evidence |
|---|---|---|---|
| M0 — Implementation foundation | A runnable project with clear document precedence | Current repository | Environment, package, test command and architecture navigation work |
| M1 — One deterministic end-to-end turn | Event or timer → autonomous decision → fake effect or quiet outcome | M0 | Speech, reaction, silence and proactive initiation demonstrated without an LLM |
| M2 — Contextual discretion and protected views | Scoped social memory, provenance and selective review work in replay | M1 | Access/use distinction, derived restrictions and ordinary cross-context recall tested |
| M3 — Restart-safe replay slice | Full v0.2 synthetic vertical slice | M2 | I01–I16, three handoff stories and fault checkpoints pass |
| M4 — Local Hermes deliberation | The same slice uses actual Hermes and a local model | M3 | Captured requests match admitted context; safety and social behavior measured |
| M5 — Persistent live Discord observation | A real transport journals events across core restarts | M3; M4 additionally required before model-backed shadow deliberation | Custody, session ownership, recovery and retention tested without Agora-authored social sends |
| M6 — Autonomous live participation | Agent speaks, reacts, initiates and stays quiet within standing capabilities | M4 + M5 | Bounded live operation preserves identity, discretion and delivery rules |
| M7 — Measured improvements and broader environments | Better economics, memory and capabilities where justified | Relevant earlier milestone | Each addition has its own contract, usefulness evidence and regression coverage |

M0–M3 together define the first implementation phase. The immediate deliverable is M0 plus the smallest runnable M1 whole-turn replay. M4 tests whether the architecture remains useful with real inference. M5–M6 provide the actual social presence. M7 is an extension lane, not a requirement to postpone ordinary participation indefinitely.

## M0 — Implementation foundation

Keep this small. Establish `pyproject.toml`, a compatible Python version, `uv.lock`, a package entry point, and pytest/ruff/ty configuration consistent with `AGENTS.md`. Reuse the existing ignore rules for runtime databases, journals, model files and outputs. Check in only deliberate synthetic fixtures.

Fix architecture navigation before starting implementation. `AGENTS.md` references `docs/architecture/README.md`, which is absent in the inspected tree. Add that index and state precedence. The `baseline/` links in the imported v0.2 documents have been corrected to point at the extracted v0.1 tree; the remaining navigation item is the missing architecture index itself.

Record the implementation-location reconciliation. The repository README proposes `agora/`; the handoff originally described an isolated namespace inside a Hermes checkout. Proposed implementation here: a standalone `agora` package with a later explicit Hermes adapter, preserving the eight conceptual module responsibilities. Record that packaging decision in a current implementation note; do not pretend the location difference is already settled by the historical handoff.

Read the actual Hermes checkout when establishing compatibility. The researched upstream commit is not the current API contract. Do not choose a Python version or provider-hook interface from memory.

Exit: a clean checkout can sync the environment, import the package and run a small behavioral test. Documentation entry points resolve. No Discord, model or privileged resource is required.

## M1 — One deterministic end-to-end turn

Implement the contract subset needed to connect the whole path: identity/principal, normalized event, audience/episode, intent/work item, bounded context, typed main decision, effect plan, commit decision/authorization and fake receipt. Add scripted ports and virtual time. Create core and transport state stores early; do not build a disposable in-memory path that later requires replacement.

Wire coordinator → context/memory → scripted main → CommitGate → fake sidecar/world. The fake sidecar is a module at this stage. Two SQLite ownership domains exercise recovery without introducing services. The fake world persists independent visibility evidence.

Show four outcomes: an ordinary message, a reaction, explicit no action and a timer-driven voluntary initiation. A direct mention may end silently. A scripted agent may answer without a mention when its interest makes participation worthwhile. Nothing automatically converts an internal outcome or error into a public acknowledgement.

Enforce exact effect bytes and standing capabilities from the start. Unsupported tools/effect kinds stay inert. A prototype-only shortcut may narrow supported contracts, but cannot create an alternate send path that bypasses CommitGate.

Exit: a deterministic replay command produces the expected observer trace and durable state. The test proves wiring and decision freedom; scripted behavior is not evidence of natural model judgment.

## M2 — Contextual discretion and protected views

Complete the remaining v0.2 authority, memory, provenance and context contracts. Implement domain-specific bindings rather than universal operator priority. Agent-created intent remains independent of protected-resource permission.

Seed the five resource classes from the handoff: public project knowledge, agent-private ordinary preference, human financial protected information, third-party confidence and protected joint work. Use public room, restricted room and DM contexts with one identity. Episode grouping cannot widen fragment scope.

Implement runtime producer-input coverage, ordinary scoped retrieval, staged memory promotion, correction/supersession and the two fixed transforms. Access status, view precision and permitted use remain separate. Derived goals, summaries and scheduled work inherit applicable restrictions. Ordinary private knowledge remains eligible for discretion; this must not become a blanket Discord memory blackout.

Implement code-driven NONE/ADVISORY/REQUIRED/HARD_DENY routing. Reviewer stubs establish protocol behavior: missing facts or adverse required assessment holds a proposal; ordinary speech needs no extra inference; advisory advice can be declined. Bound rewrite loops. Strongly protected INTERNAL_ONLY views cannot enter an external effect-capable context.

Exit: access-result variants, authorized abstraction, denied/conflicting access, forged lineage, clean regeneration, stale summary and private-to-public context changes have focused tests. Positive cases show legitimate cross-context recall and ordinary speech alongside protected suppression.

## M3 — Restart-safe replay slice

Finish the dispatch protocol and recovery tests before connecting to live resources. Bind logical effect ID, proposal revision, attempt ID, payload digest, target/audience/policy versions, epoch, transport generation and single-use lease.

Exercise cancellation and supersession, atomic release reservations, UNKNOWN accounting, duplicate ingestion/submission, timer deduplication, journal gaps and stale context invalidation. Restart core independently from transport. A generation label alone is insufficient if an old worker can still execute.

Inject all eight handoff crash checkpoints, including lease-consumption/dispatch-start gaps and remote visibility before a lost receipt. No automatic resend or disclosure refund follows ambiguous delivery. A new intent cannot discard runtime task lineage and act as a retry bypass.

Exit: I01–I16 and the three exact handoff stories pass; all unsupported capabilities remain inert. Capture a reproducible trace/report for spontaneous public participation, the affordability probe and reaction-target revision race. Social positive cases reject an always-silent implementation. This is the first completed synthetic vertical slice.

## M4 — Local Hermes deliberation

Replace scripted main/reviewer ports using the existing local Hermes and llama.cpp environment. Retain scripted replay as the regression oracle. Start with an existing usable model rather than a model-selection project.

Implement a narrow Hermes adapter with actual request capture. Verify the messages, tool definitions and endpoint match the admitted envelope. Audit automatic memory-prefix injection, session search, provider sync, ordinary final-response delivery, typing and hidden tool execution. Reject an incompatible/unmanifested path instead of silently disabling the checks.

Map one agent identity and durable agent state across local work, social work and scheduled work. Define state ownership and revisioned updates explicitly; avoid creating duplicate persona files or two competing memory writers. Use fresh audience-specific inference contexts while preserving permitted knowledge and identity. Adapt memory-provider paths one at a time rather than enabling all of Hermes' capabilities.

Run both ordinary social and adversarial replay suites. Include paired secret worlds and observer traces covering reactions, silence, omission, timing, initiation and multi-turn composition. Report attack success, useful participation, interruption, unwanted oversharing, latency, model calls and tokens. Compare selective review with the behavioral baseline and full-review arm; include always-silent and known-leaking positive controls.

Exit: real local inference produces useful participation and valid typed decisions; every call is mediated and captured. Publish measured limitations, not a privacy certificate. Material behavioral failures become concrete restrictions on the next enabled capability or design changes, rather than silently changing expected tests.

## M5 — Persistent live Discord observation

Implement the persistent user-account transport after replay recovery is proven. Use the pinned dolfies `renamed` branch importing `selfcord`; verify the exact commit/API at implementation time. Do not substitute the unrelated PyPI `selfcord.py` project or transfer bot-only semantics to a user account.

Move transport to a process only where connection lifecycle and account/session custody justify it. Before live connection, implement credential custody, authenticated core/sidecar communication, exclusive account-session ownership and dispatch freshness. A same-user process boundary by itself is not a credential security guarantee.

Begin with selected destinations and incoming observation. Test journal cursors, reconnect, missing events, edits/deletes, audience bounds and retention while core stops and restarts. Keep Agora-originated conversational effects disabled. Observe what the platform actually supports; fake-world guarantees must not become an exactly-once claim for Discord.

Shadow deliberation, when enabled, uses the same identity and commit path but records proposals without sending. Apply compute admission and third-party retention rules even in shadow mode. Reading is already a data flow, not permission to retain whole guild history indefinitely.

Exit: transport survives controlled lifecycle failures and accurately reports uncertainty. Required preconditions and reconciliation limits are documented for each proposed outgoing effect.

## M6 — Autonomous live participation

Enable one-message sends and reactions first in selected rooms/DMs, with standing destination/effect/rate capabilities. The agent controls whether to participate and what to say within those capabilities. Do not add per-message operator approval as the default social workflow.

Carry over proactive intents, scheduled revisits, relationship memory, silence and room-aware episodes. Both inbound-triggered and autonomous work use the same identity/context/CommitGate contracts. Review is selective; hard resource restrictions remain code-enforced. Core restart must preserve outstanding intentions without replaying already visible effects.

Measure a small live cohort of conversations: continuity, unsolicited usefulness, inappropriate interruption, natural banter, legitimate cross-context recall, public/DM discretion and how often technical machinery disrupts participation. Retain bounded auditable evidence, not perpetual private transcripts.

Exit: the agent can participate continuously and recover reliably while respecting the declared capability scope. If the measured behavior becomes persistently silent or bureaucratic, that is a design failure to investigate, not a successful privacy result.

## M7 — Measured improvements and broader environments

These additions are independently staged; none is required simply because it appeared in the research map.

| Extension | Entry condition | Required evidence before adopting it |
|---|---|---|
| Clef/System-One/d1 perception | M4 has a stable model-backed baseline | End-to-end latency/compute benefit, calibration, useful catch-up during outage and resistance to wake/review floods |
| Semantic retrieval/embeddings | Scoped memory works; retrieval quality is a demonstrated limit | Every embedding/provider sink is admitted, scope filters precede retrieval, and correction/retention reaches the index |
| Better episode/context selection | Multi-person replay/live traces reveal actual misses | Fewer interruptions and better continuity without merging source permissions |
| Richer Discord effects | M6 effect/delivery contracts are stable | Effect-specific authorization, mutable-target checks and partial/UNKNOWN reconciliation for edit/delete, attachments, threads or voice |
| Broader Hermes tools/providers | An identified useful operation needs them | Complete capability/sink inventory and custody, with no bypass through native or background execution |
| Additional environment adapter | Existing ports are stable | Same identity/intents/views operate naturally under that environment's audience and delivery semantics |
| Native provenance or fine-tuning | Metadata/model behavior exposes a persistent measured weakness | Training evidence plus safety and social regressions; textual tags alone are not native provenance |

Clef can be evaluated after M4 while transport work proceeds. It should remain optional if it does not earn its resident VRAM and scheduling cost. Deterministic components may later move languages when a measured lifecycle/isolation/concurrency need justifies that boundary.

## Near-term implementation packages

Use these as inputs for Beads tasks and approved OpenSpec changes, not a second markdown status tracker.

| Package | Scope | Dependency | Demonstration |
|---|---|---|---|
| Foundation | Navigation/packaging decision, uv project, test runner | None | Clean environment and working document entry points |
| Contract and storage spine | Canonical records, virtual clock, versioned core/transport stores | Foundation | Invalid records rejected; ingestion survives crash-before-ack |
| Whole-turn wiring | Scripted ports, coordinator, ordinary CommitGate, fake world | Spine | Speak/react/quiet/initiate traces |
| Views and discretion | Authority, scoped memory, transforms, coverage, selective review | Whole-turn wiring | Public recall succeeds; protected view/use follows policy |
| Dispatch and invalidation | Leases, reservations, cancellation, UNKNOWN, correction/restart | Views and discretion | All relevant crash/race invariants |
| Replay acceptance | Remaining I01–I16 and three complete stories | Prior packages | M3 evidence report and unresolved-question list |

Avoid a giant first change that implements every record without a runnable path. Keep the next deliverable demonstrable and preserve the handoff's contracts as support expands.

## Decisions and claims to keep explicit

The coding session must not guess real confidentiality norms, unknown joint-authority rules, remote-model permissions, live Discord reconciliation guarantees, erasure semantics or model-level leakage resistance. Preserve the handoff's stub/unsupported behavior until the corresponding decision or experiment exists.

Acceptance reports should separate code invariants, model-backed observations and live platform limitations. Later scope changes require a current implementation note or architecture revision, with Beads dependencies and applicable OpenSpec requirements updated. Historical v0.1/v0.2 files remain reference evidence.

The next concrete deliverable is M0 plus a small M1 whole-turn replay, followed by the protected-view and recovery work needed to complete M3. That keeps progress visible while building toward the actual objective: a continuous agent that can know things, exercise discretion and still choose to participate naturally.
