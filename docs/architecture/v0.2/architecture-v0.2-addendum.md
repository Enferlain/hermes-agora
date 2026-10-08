# Persistent-agent architecture — v0.2 addendum

Accepted design revision · 7 October 2026 · No implementation

This addendum applies to the unchanged [v0.1 package](../v0.1/) and the accepted [adversarial review](../v0.1/adversarial-architecture-review.md). It supersedes only the decisions identified below. The original research, continuous-identity goal, transport-sidecar direction and platform-independent behavioral stages remain.

**Precedence:** v0.2 addendum → v0.2 replay handoff → unchanged v0.1 for matters not superseded. The review explains why changes were needed; its unresolved alternative proposals are settled by this revision where stated. A = original architecture, C = original runtime contracts, E = original evaluation plan.

**Readiness:** the six reviewed blockers are resolved as contract decisions below. The synthetic replay slice is ready to implement against the [handoff](synthetic-replay-implementation-handoff.md). This is design readiness, not evidence of implemented security or permission to substitute real resources for synthetic ones.

## 1. Traceable decisions

| Decision | Findings resolved/incorporated | Superseded candidate provisions |
|---|---|---|
| D01 Domain-specific authority | F01 | A §§6,9; C §§1,5–6: universal owner-centric grants/preferences |
| D02 Autonomous intent; separate authorization | F02 | C §5: pre-existing/owner-authorized purpose prerequisite |
| D03 Commit gate versus model review | F03,F04 | A §9; C §7: mandatory sensor + fresh review for all actions; unspecified expression resolution |
| D04 Concrete replay mediation boundary | F05,F20 | A §§9,13; C §§3,8: optional custody without a defined prototype capability/sink surface |
| D05 Runtime coverage and attributed adoption | F06,F10,F11 | A §§5–6; C §6: declared lineage as sufficient enforcement input |
| D06 Dispatch identity and failure protocol | F07,F08 | A §10; C §§7–8: underspecified revalidation, supersession, fencing and release settlement |
| D07 Access and use semantics | F15 | C §5: four access labels carrying authorization/availability/precision together |
| D08 Revision/correction semantics | F12,F21 | A §6; C §10: generic deletion/correction propagation and storage surface |
| D09 Audience and episode semantics | F13,F14 | A §10; C §1: “verified audience” and grouping without exact join constraints |
| D10 Optional bounded perception | F16 | A §7: sensor as a potentially mandatory wake/review dependency |
| D11 Slice scope and acceptance | F17,F18,F22 hypotheses retained | A §11; E §7: full-review default and benchmark targets treated as release prerequisites |

F09/F17/F18/F19/F22 remain empirical questions; they are not disguised as resolved guarantees. F23 remains deferred, with the fake adapter testing the general contracts.

## 2. D01 — authority is scoped; ownership is not a priority rank

The agent is a principal that controls its ordinary intentions, attention, autobiographical assertions and conversational preferences. The operator provisions the host/account and defines deployment limits. Other principals can control resources or participate in confidentiality/joint-context agreements. Data subjects have interests and contextual expectations; subjecthood alone is not an automatic grant issuer or a veto over discussion of public facts.

Replace universal OwnerGrant with **AuthorityBinding + ResourcePolicy + UseAuthorization**. Each binding identifies an issuer, domain, resource and authenticated evidence. Domains in the slice are RESOURCE_USE, DISCLOSURE, AGENT_STATE and DEPLOYMENT. No implicit cross-domain delegation. Grant issuers must have a matching active binding.

ResourcePolicy defines its actual approval rule, permitted issuer set and commitments. Slice rules: SINGLE_ISSUER or ALL_LISTED. Joint approval is required only when that explicit resource policy says so; never infer it from the number of owners/subjects in a memory record. Conflicting or missing authority yields CONFLICT/PENDING_AUTHORITY, not universal operator override.

The agent can modify its ordinary preferences via an agent-authored ADOPT_PREFERENCE event. It cannot use that event to edit authority bindings, deployment limits or another principal's confidentiality commitment. Changes to hard policies come from the authenticated policy control port, not text in a model output.

Information may be agent-private, human-private, third-party-confidential, joint or public. The source record retains custodians, subjects/interests and actual commitments separately. Ordinary social discretion remains judgment-based. The slice uses curated commitments; automatic inference of real human transmission norms is outside its deterministic guarantees.

## 3. D02 — goals may be self-created

Intent creation requires a valid authenticated actor and syntactically valid goal, not an access grant. The agent may create, endorse, defer, abandon and schedule ordinary intents. Human or third-party suggestions may become triggers. The actor's endorsement does not erase trigger history or confer new resource authority.

Purpose is a description of intended use. It is not an authorization token. A protected access request references the current intent and a matching resource permission; the monitor checks issuer/domain, operation, data class, precision, transformations, permitted uses and audience conditions.

Standing social capability envelopes allow selected-room participation and ordinary memory use without topic-by-topic human approval. A broad “help someone” goal cannot authorize arbitrary protected retrieval. External origin alone cannot permanently taint future agent-created goals.

Recorded trigger refs are observable event history. Rename “sealed causal roots” to **runtime origin record**. Input coverage and model-claimed dependencies are separate. None claims complete neural causality.

## 4. D03 — mandatory commit, selective additional review

Every enabled planned effect crosses CommitGate. All effects receive deterministic checks for supported kind, capability, authority where applicable, account/destination, current relevant revisions, immutable payload, target preconditions and budgets.

Additional model review is routed:

| Route | Criterion | Disposition |
|---|---|---|
| NONE | Ordinary discretion; no active strong restriction requiring semantic resolution | Code checks plus main deliberation. Optional shadow assessment cannot block this route. |
| ADVISORY | Ordinary concern or bounded probing/tone signal | Agent may accept/ignore advice with a short disposition code. No mandatory public refusal or private explanation essay. |
| REQUIRED | Active strongly protected view/commitment requires semantic resolution; configured consequential effect | Fresh assessment is mandatory for this proposal; missing coverage, concerns or failure produces HOLD. |
| HARD_DENY | Invalid/revoked authority, unsupported protected path, forbidden sink, expired/mismatched proposal or other deterministic failure | Denied by code. Model PASS cannot override it. |

In the slice, REQUIRED uses actual authorized fact-comparison views at the local/test compute sink. Coverage is assigned by runtime from supplied inputs. PASS is only evidence under an existing policy. It neither grants rights nor certifies noninterference.

The coordinator resolves REQUIRED holds through a revised proposal, a smaller newly built context, authorized policy resolution, deferral or quiet termination. It cannot waive the relevant restriction. ADVISORY concerns stay with the agent. Code alone establishes deterministic HARD_DENY; the review model can report concerns but cannot create a new hard policy.

Strong exposure also routes a QUIET/DEFER decision through REQUIRED when it could answer an oracle. Ordinary absence needs no extra inference. Quiet creates no transport effect. Silent behavior can still leak: this routing is mitigation, not a guarantee of secret-independent silence.

Budgets: eight main steps per turn; three draft submissions including revisions; at most one required assessment per submission. Budget exhaustion completes quietly or defers once under the agent's choice; no public fallback. An advisory timeout may be ignored under its declared route. A required timeout cannot be downgraded.

A full-review arm is retained as an experiment. Clef draft signals are not a prerequisite. Mechanical connection work is bounded by transport standing authority, not sent back to the conversational model each heartbeat.

## 5. D04 — mediation scope is concrete

The first slice has synthetic memory and a fake sidecar only. Main-model outputs are typed decisions; they cannot execute Python, shell, filesystem, browser, HTTP, Discord, uploads, MCP tools or direct memory-provider writes. Those capabilities are absent from the replay tool surface. This does not modify the user's normal Hermes installation outside the harness.

All model/embedding/provider invocations are compute sinks. Context admission occurs before invocation. Default slice sink is SCRIPTED_TEST; optional LOCAL_MODEL is explicitly configured. REMOTE is unsupported in this slice. Reviewer calls use the same admission rules. Embedding, external memory providers, USER.md/MEMORY.md auto-injection, session search and asynchronous provider sync are disabled unless explicitly adapted to the context contract.

A Hermes adapter must prove its actual request messages/definitions match the constructed snapshot. It cannot claim mediation while existing hooks add unmanifested context or tools. Fail adapter validation instead of proceeding with guessed compatibility.

Replay guarantees apply to trusted harness code and synthetic resources. They do not claim OS isolation, protection from a malicious host or protection for real Discord tokens. Before a live/privileged extension, real custody, alternate-tool mediation and sink authorization must be implemented and tested. A sidecar under the same unrestricted OS user alone is not a hard secret boundary.

## 6. D05 — runtime coverage is the source of restrictions

Each derivation/model call records the complete bounded input fragment revisions actually supplied. Model-claimed parents are advisory metadata. The runtime calculates inherited commitments and restrictions from producer coverage, not from the model's attribution.

Mixed-source summaries retain conservative constraints. A public-only result can be regenerated from public-only inputs or produced by a policy-authorized transform. No output can become public merely by omitting sensitive parent IDs.

Exposure is an upper bound on possible influence for the current call/snapshot. It supports review routing, auditing and invalidation; it is neither proof of secret use nor proof of secret independence. Do not accumulate every historical exposure into permanent global taint.

Ordinary assertions retain status: observed statement, attributed assertion, contested assertion, inference or agent-adopted preference. Summaries have no direct instruction authority. Agent adoption requires a separate typed state transition and cannot mint external-resource rights.

## 7. D06 — effect identity, dispatch and settlement

One logical effect has a stable effect_id across retries/revisions. proposal_revision identifies changed drafts; attempt_id identifies dispatch attempts. A revised pending effect does not become a new independently sendable effect. Reusing an effect ID with different bytes outside the supersession protocol fails.

The fake sidecar owns its inbox/outbox DB. Core owns identity, policy, intents, context, proposals, reservations and audits. Two SQLite domains are intentional for crash testing; no distributed transaction is claimed.

In the replay harness, one serialized dispatcher event loop orders lifecycle/policy changes and the local dispatch-start transition. Core validates current relevant versions and reserves any constrained release in one transaction; the fake sidecar requests durable core consumption of its short-lived dispatch lease, then records DISPATCH_STARTED before calling the fake remote world. A crash across those DB steps leaves an orphan reservation or uncertain effect; recovery never assumes no disclosure.

Cancellation/supersession:

- Before DISPATCH_STARTED: sidecar can durably acknowledge cancellation; only that receipt permits releasing an unused reservation or authorizing the replacement.
- At/after DISPATCH_STARTED: cancellation returns TOO_LATE; do not send an automatic replacement for an uncertain first effect.
- SENT: revisions become an explicit edit intent in a later capability extension, not a retry.
- UNKNOWN: reconcile against the fake world's evidence; absent proof of non-dispatch, count possible disclosure and do not resend.

Only one active sidecar session per account is allowed. The replay fake enforces generation ownership. A later real sidecar must hold an exclusive lifetime account lock; replacement starts only after predecessor termination/release. A generation field without exclusive execution control is insufficient.

Core startup increments runtime_epoch. Queued old-epoch authorizations require revalidation, not automatic replay. Dispatch leases expire after 2,000 virtual milliseconds; epoch/version/expiry/single-use checks are mandatory.

The local linearization point is the durable DISPATCH_STARTED transition while dispatcher serialization is held. Policy changes accepted before it prevent dispatch; after it they cannot promise remote prevention. Real Discord additionally has unavoidable remote target/audience races; local checks are not atomic with Discord.

Message/reaction review binds meaningful target revision/content. Reactions to edited/deleted targets require a fresh proposal or suppression. The first slice permits one text message or one reaction per plan and rejects chunking/edit/delete. Their future partial/remote reconciliation semantics remain required before enabling them.

Release reservations are atomic, scope-specific and conservative: RESERVED → COMMITTED/POSSIBLY_COMMITTED/CANCELLED_BEFORE_DISPATCH. UNKNOWN and observed partial effects never refund automatically. The slice ledger tracks configured transforms/constraints, not mathematically estimated leaked bits.

## 8. D07 — access status, view shape and use are independent

MemoryAccessResult has:

- access_status: GRANTED, DENIED, PENDING_AUTHORITY, UNAVAILABLE or CONFLICT;
- view_kind: FULL, PARTIAL, ABSTRACTED or NONE;
- safe reason codes; granted view ref when present;
- independent internal-use, observable-influence and expression permissions;
- producer coverage, actual transform/policy basis and inherited commitments.

Non-GRANTED status always returns NONE/no value. FULL/PARTIAL/ABSTRACTED describe precision, not sensitivity or publicness. DENIED remains a display shorthand, not a representation for timeout or joint-authority conflict.

Default derived restrictions come from bounded producer inputs. Declassification removes only commitments expressly covered by a valid transform/disclosure authority; unrelated commitments survive.

An INTERNAL_ONLY strongly protected view is not admitted into any externally effect-capable model context in the slice, including a DM. If it is useful for private planning, run it in a non-effect-capable internal snapshot; only a separately permitted view may enter an external continuation. Ordinary personal/private memory remains eligible for contextual discretion and does not inherit this blanket restriction.

A view allowing observable influence but forbidding explicit expression routes REQUIRED, with acknowledged probabilistic semantic limits. An affordability predicate may itself reveal finances and needs its own use permissions. No arbitrary attacker-selected threshold function is allowed.

## 9. D08–D10 — continuity, audience and optional perception

Correction, retention expiry, grant revocation and message deletion are distinct events. Corrections/changed restrictions invalidate affected derivations and active snapshots; a late summary job cannot promote against stale input versions. Message deletion removes eligibility of source text under its retention policy, without inventing either proof the assertion was false or a global erasure requirement. Erasure requests need separate defined authority and are not inferred from a deleted message.

The slice keeps scoped social/candidate/durable records, bounded synthetic journal text and opaque audit IDs. No embeddings or unrestricted global index. Storage retention runs on virtual time; tombstoned payloads cannot re-enter context through audit/outbox/debug copies. Sent effect receipts keep nonsecret identifiers/digests, not perpetual copies of private inputs.

Audience represents observed bounds, not all future readers. Material disclosure-context revision changes on private/public class, relevant recipients/commitments or uncertainty changes; ordinary public chatter does not change it. Episode revision tracks conversation organization separately. A merge/split does not widen fragment scope.

Main chooses attention interests and participation. Sensor signals cannot grant rights or call CommitGate. Catch-up, deterministic reply/address features and bounded sampling operate even without Clef. The slice uses deterministic/scripted perception first; optional local Clef must demonstrate savings before becoming a resident dependency. Review floods and busy-room fairness are tested.

## 10. Scope, invariants and remaining research

The handoff implements synthetic replay with one identity, two rooms, one DM, scheduled intent, message/reaction/quiet, versioned scoped memory, protected views, selective review and fake dispatch failures. No production Discord, privileged host tools, remote providers, embeddings, uploads, voice, client scripting or automatic policy generation.

Core invariants: autonomous intent creation; no right from purpose text; no authority from external framing; restriction inheritance from runtime coverage; no unmanifested compute context; no effect outside CommitGate; no protected semantic HOLD waiver; no automatic resend/refund for UNKNOWN; no episode-to-scope elevation; no public fallback for quiet/errors.

The addendum does not settle semantic leakage, review independence, real-world norm classification, local model performance or unknown collusion. The coding agent implements the defined tests/ports, reports evidence and leaves these as hypotheses. It must not invent policy answers to make tests pass.

Original evaluation targets are exploratory. Replay release requires deterministic invariant/fault tests and explicit social positive cases, not an assertion of statistically proven privacy. Model-backed results must compare selective/full-review and behavioral baselines with grouped uncertainty and positive controls.
