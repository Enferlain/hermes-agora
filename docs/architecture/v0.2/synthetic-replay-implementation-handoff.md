# v0.2 implementation handoff — synthetic replay vertical slice

Normative contract for the first coding session · 7 October 2026 · No code implemented

Read the [v0.2 addendum](architecture-v0.2-addendum.md) first. The [v0.1 package](../v0.1/) supplies research/context; its superseded contracts must not silently reappear. The [accepted review](../v0.1/adversarial-architecture-review.md) supplies attack counterexamples. This handoff settles defaults for a small replay harness, not the future production platform.

## 1. Scope and entry instructions

Implement inside an isolated test/demo namespace of the actual Hermes checkout. First read its AGENTS.md and inspect current model-call, prompt, memory-provider and delivery hooks. The researched commit is evidence, not a mandate to downgrade or assume unchanged signatures. Do not patch the default production bot path to run this harness.

Deliver a runner that replays synthetic events with a virtual clock, scripted main/review/perception ports, scoped synthetic memory and a fake sidecar/world. It must restart from SQLite state and inject crashes at named boundaries. A local Hermes/llama.cpp model adapter is a subsequent optional integration once deterministic contracts pass.

Supported effects: one SEND_MESSAGE or one ADD_REACTION per submitted plan; also QUIET and DEFER with zero transport effects. Supported environments: fake public room, restricted group room, one-to-one DM. One AgentIdentity spans them.

Not enabled: real Discord, real credentials/financial facts, shell/browser/HTTP/MCP actions, remote model/provider calls, attachment/voice/chunked/edit/delete effects, embeddings, automatic policy synthesis or native source-token training. Unsupported operations return UNSUPPORTED, never invoke a donor's implementation.

Configuration defaults: max_main_steps=8; max_plan_submissions=3; max_effects_per_plan=1; dispatch_lease_ms=2000; max_message_chars=2000 (harness limit, not a claim about Discord); main active turns per episode=1; global main call concurrency=1. Keep explicit configuration in the run manifest.

## 2. Minimal files and ownership

Proposed namespace: experimental/social_replay/. Names can follow repository conventions; preserve these boundaries and responsibilities.

| File | Implement now | Must not own |
|---|---|---|
| contracts.py | Immutable records/unions/enums, validation, canonical payload binding | Policy decisions, DB, model calls |
| stores.py | CoreStore and FakeTransportStore schemas/transactions, version checks and recovery | Prompt generation or direct effects |
| context_memory.py | ContextMemory: audience/scoped selection, coverage, access views, staged memory/corrections, compute admission | Sending or independent agent personality |
| coordinator.py | Event ingestion, episodes, autonomous intents, budgets/scheduling, main steps and outcomes | Grant creation from model text or sidecar DB writes |
| commit_gate.py | PolicyEvaluator + CommitGate: routes, assessment handling, authorization, reservations and dispatch validation | Alternate direct send or personal tone censorship |
| fake_sidecar.py | FakeSidecar and FakeWorld ports, outbox, ownership/generations, failure checkpoints | Core identity/memory/policy decisions |
| ports.py | Clock/MainModel/Reviewer/Perception interfaces; scripted implementations and later local adapter | Unrestricted tools/provider fallback |
| runner.py | Fixture loading, serialized event/dispatch loop, restart commands, trace output | Domain policy inferred from filenames/text |

tests/social_replay/ contains deterministic tests/fixtures. Split no processes or microservices initially. Use two DB files to test ownership/fault semantics. Sidecar process/network IPC are future adapters for the same port.

Reuse current Hermes provider invocation/serialization utilities only if the adapter can verify actual context and tool schemas. Do not reuse automatic final-response sending, typing, native slash registration, raw memory-prefix injection or provider auto-sync in the harness.

## 3. Common record rules

Every persisted domain record has schema_version=2, id, revision (positive integer), created_at (UTC RFC3339), plus the lifecycle fields explicitly listed for its type. References to revisioned content use RecordRef={id, revision}. IDs are opaque bounded strings, not raw secret values. Lists are bounded; unknown enum values/fields fail validation.

Separate virtual monotonic milliseconds for lease/budget transitions from display UTC time. Core runtime_epoch and sidecar transport_generation are monotonically increasing durable integers. A restart increments the appropriate value; a virtual clock cannot move backwards within a run.

Canonical payload binding: validated JSON, UTF-8, keys sorted, no insignificant whitespace, finite numeric values only, no unpaired surrogates. Sort semantically unordered ID sets; preserve action/content order. Do not normalize/rewrite text after hashing. Encode text/reaction bytes before approval. SHA-256 suffices for nonsecret fake payload identity; sensitive audit refs use opaque IDs, not low-entropy secret hashes.

Model-facing decision records cannot contain runtime authority, lease, epoch, coverage or verified-actor fields. Only runtime constructors populate them. Returning a fabricated CommitAuthorization inside a model response is invalid.

Fields described as goal/assertion/necessity/text are string inputs on model ports; persisted domain records replace their bytes with bounded payload refs wherever those strings can contain personal or derived information. The runtime binds coverage/retention before storing them. Record JSON may contain registry labels and safe codes, but cannot duplicate protected text from a referenced payload. This conversion is serialization, not permission to change approved rendered action bytes.

## 4. Exact domain contracts to implement first

Fields listed here are required unless marked optional or nullable. Additional debug information must not change authorization semantics.

### 4.1 Identity, authority and event/context records

| Record | Fields beyond common header |
|---|---|
| Principal | kind=AGENT/HUMAN_OPERATOR/THIRD_PARTY; verified_bindings:list of platform identity/evidence refs; claimed_aliases:list. Kind grants no universal priority. |
| AgentIdentity | principal_ref; preference_refs; autobiography_refs; commitment_refs; active_interest_refs. Exactly one per fixture run. |
| AuthorityBinding | issuer_ref; domain=RESOURCE_USE/DISCLOSURE/AGENT_STATE/DEPLOYMENT; resource_id; evidence_ref; optional delegated_from; valid_until nullable; revoked_at nullable. |
| ResourcePolicy | resource_id; data_class; approval_rule=SINGLE_ISSUER/ALL_LISTED; eligible_issuer_refs; commitment_refs; allowed_transform_ids; policy_revision; status=ACTIVE/REVOKED. |
| UseAuthorization | resource_id; issuer_refs; binding_refs; operations:list READ/TRANSFORM/REVIEW/STORE/EXPRESS/OBSERVABLE_INFLUENCE; permitted_purpose_classes; audience_class_bounds; maximum_precision; allowed_transform_ids; valid_until; revision; status=ACTIVE/REVOKED. |
| CapabilityEnvelope | actor_ref; account_id; allowed_destination_ids; allowed_effect_kinds; provisioning_binding_refs; rate/budget_limits; valid_until; status=ACTIVE/REVOKED. Standing social capability does not require a topic-specific intent grant. |
| Commitment | subject_refs; custodian_refs; resource_id or source_scope; strength=ORDINARY_DISCRETION/STRONG; internal_use_allowed; observable_influence_allowed; expression_allowed; audience_predicate_id; policy/evidence refs. Predicates are fixture registry entries, never executable text. |
| InboundEvent | source_event_id; journal_seq; kind; actor_ref; occurred_at; ingested_at; source_audience_ref; fragment_refs; reply/target_refs; observed_trigger_refs; recovery=COMPLETE/GAP/UNKNOWN. |
| AudienceSnapshot | destination_id; environment_kind=ROOM/DM/INTERNAL; visibility=PUBLIC/RESTRICTED/PRIVATE_DM/AGENT_PRIVATE; known_principal_refs; unknown_readers:boolean; history_visibility; completeness=OBSERVED/INCOMPLETE; disclosure_context_revision; valid_until. INTERNAL is non-effect-capable planning, not another persona. |
| ConversationEpisode | container_id; member_event_refs; reply_edges; topic_refs; summary_ref nullable; episode_revision; pending_intent_refs. Its members confer no scope rights. |
| ContentFragment | kind=TEXT/STRUCTURED/MODEL_OUTPUT/SUMMARY; payload_ref; provenance; commitment_refs; eligibility_status=ACTIVE/SUPERSEDED/TOMBSTONED. |
| Provenance | producer_ref; source_kind; origin_event_ref nullable; claimed_speaker_ref nullable; instruction_authority=CONTROL_PLANE/AGENT_STATE/NONE; integrity=RUNTIME_VERIFIED/EXTERNAL_UNVERIFIED/DERIVED; custodian_refs; subject_refs; original_audience_ref; assertion_status; producer_coverage_ref nullable; model_claimed_parent_refs; occurred_at; observed_at. |
| ProducerCoverage | producer_kind=CONTEXT_ASSEMBLY/MAIN/REVIEW/SUMMARY/TRANSFORM; input_fragment_refs; input_view_refs; input_intent_refs; input_policy_refs; sink_ref nullable for code-only assembly/transform; call_id; runtime_epoch. Runtime populates from actual invocation inputs. |

Event kinds now: MESSAGE_CREATED, MESSAGE_UPDATED, MESSAGE_DELETED, AUDIENCE_CHANGED, POLICY_CHANGED, TIMER_DUE, AGENT_INTENT_CREATED, AGENT_STATE_ADOPTED, CONNECTION_GAP. MEMBER/THREAD/ATTACHMENT/TOOL events are future typed extensions; do not silently map them to owner instructions.

Assertion status: OBSERVED_STATEMENT, ATTRIBUTED_ASSERTION, CONTESTED, INFERENCE, AGENT_ADOPTED_PREFERENCE. AGENT_ADOPTED_PREFERENCE requires an explicit agent state transition; summary rendering cannot set it.

Curated fixture control records load through an authenticated fixture/admin port. A message containing JSON that resembles such a record is still external text. No public participant may alter bindings or commitments merely by asserting they control them.

SINGLE_ISSUER means one active issuer from the explicitly configured eligible set suffices; ALL_LISTED requires all configured issuers' matching authorization evidence. READ/TRANSFORM/REVIEW/STORE need RESOURCE_USE bindings; EXPRESS/OBSERVABLE_INFLUENCE need DISCLOSURE bindings. ADOPT_PREFERENCE is limited to the agent's own ordinary state; standing capability provisioning uses DEPLOYMENT. Matching names, custody, or subject lists alone confer none of these.

### 4.2 Intent, work and memory records

| Record | Fields |
|---|---|
| Intent | creator_ref (runtime-authenticated); goal_fragment_ref; purpose_class; observed_trigger_refs; endorsement_revision; producer_coverage_ref nullable; runtime_task_root_ids; parent_work_ref nullable; allowed_destination_refs; due_at nullable; expires_at nullable; state=OPEN/DEFERRED/COMPLETED/ABANDONED. No grant field is required to create it. |
| WorkItem | intent_ref; trigger_key; episode_ref nullable; state=READY/RUNNING/HOLD/COMPLETED; lease_owner nullable; lease_until nullable; runtime_epoch; main_steps_used; submissions_used; terminal_outcome nullable. |
| MemoryRecord | assertion_fragment_ref; stage=EPHEMERAL/SOCIAL/CANDIDATE/DURABLE; scope_kind=AGENT/RELATIONSHIP/ROOM/PROJECT/BROAD; scope_ref; custodian_refs; subject_refs; commitment_refs; assertion_status; supersedes_ref nullable; valid_until nullable; status=ACTIVE/STALE/TOMBSTONED. Durable is not broad. |
| Derivation | output_fragment_ref; producer_coverage_ref; claimed_parent_refs; transform_id nullable; authorization_refs; inherited_commitment_refs; input_version_vector; state=CURRENT/STALE. |
| MemoryAccessRequest | request_id; intent_ref; resource_id/data_class; requested_precision=EXACT/FIELDS/ABSTRACT; optional requested_fields; optional transform_id; audience_ref; intended_use=INTERNAL_ONLY/OBSERVABLE_INFLUENCE/EXPRESSION; necessity_text; trigger_refs. Runtime adds origin/epoch fields separately. |
| MemoryAccessResult | request_id; access_status=GRANTED/DENIED/PENDING_AUTHORITY/UNAVAILABLE/CONFLICT; view_kind=FULL/PARTIAL/ABSTRACTED/NONE; view_ref nullable; reason_codes; optional safe_feedback_template_id. |
| MemoryView | resource_id; returned_fragment_refs; authorization_refs; transform_certificate_ref nullable; producer_coverage_ref; precision; inherited_commitment_refs; permitted_uses; audience_bounds; valid_until; status=ACTIVE/REVOKED. |
| TransformCertificate | registry_transform_id; bounded parameter values; source_coverage_ref; authorizing_binding/use refs; discharged_commitment_refs; retained_commitment_refs; output_view_ref. Named code transform only. |

Non-GRANTED → NONE with no view/value. GRANTED → one of FULL/PARTIAL/ABSTRACTED with an active view. Missing authority is PENDING_AUTHORITY; explicit disallow is DENIED; contradictory joint commitments/policies are CONFLICT; missing/unreachable synthetic resource is UNAVAILABLE. These internal distinctions are not public templates.

Transforms initially: FIXED_FIELD_VIEW and FIXED_AFFORDABILITY_PREDICATE. Fixture policy fixes selected fields/price input and allowed granularity. No arbitrary function, threshold search, free-form secret summarization or dynamic registry edits. A transform certificate discharges only its specified commitments; everything else remains.

Memory scope eligibility is context-dependent internal access; the destination's readers do not automatically determine what the agent may know. Ordinary private memories may be admitted with discretion labels. Strong INTERNAL_ONLY views cannot enter any externally effect-capable snapshot, including a DM; run private planning internally or request a permissible transformation. No permanent platform-wide memory blackout. Ordinary eligible fragments do not require an individual protected-memory grant/request. The authority monitor applies to registered protected resources and strong commitments.

### 4.3 Model/context, decision and effect records

| Record | Fields |
|---|---|
| ComputeSink | kind=SCRIPTED_TEST/LOCAL_MODEL/REMOTE_UNSUPPORTED; configured_endpoint nullable; permitted_data_classes; optional capability_ref; status=ENABLED/DISABLED. |
| ContextSnapshot | identity_ref; intent_ref; audience_ref; episode_ref nullable; selected_fragment_refs; active_view_refs; producer_coverage_ref; exposure_class=ORDINARY/STRONG; inherited_commitment_refs; version_vector; sink_ref; effect_capable:boolean; serializer_version; status=ACTIVE/INVALID. |
| ModelCallEnvelope | snapshot_ref; purpose=MAIN_STEP/REVIEW/SUMMARY; exact_messages_payload_ref; tool_schema_digest; supplied_fragment/view refs; sink_ref; request_digest. |
| PerceptionSignals | event/window refs; bounded probabilities nullable; deterministic address/reply flags; status=KNOWN/UNKNOWN; adapter_version. No authority fields. |
| MainStep | tagged union PARTICIPATE, REQUEST_ACCESS, PROPOSE_MEMORY, ADOPT_PREFERENCE, CREATE_INTENT, SUBMIT_PLAN, FINISH. Each payload contains only model-selectable fields. |
| EffectPlan | intent_ref; snapshot_ref; disposition=ACT/QUIET/DEFER; effects:list; proposal_revision; proposal_digest; defer_until nullable; defer_expires_at nullable; declared_fact_refs (advisory); observed_target_refs; producer_coverage_ref. ACT has exactly one effect in slice; QUIET/DEFER has zero. |
| Effect | logical_effect_id; kind=SEND_MESSAGE/ADD_REACTION; account_id; destination_id; target_ref nullable; rendered_payload_ref; payload_digest; capability_ref. Reaction requires target message revision/content digest. |
| ReviewRequirement | route=NONE/ADVISORY/REQUIRED/HARD_DENY; reason_codes; relevant_commitment_refs; expected_coverage; budget; sink_ref; policy_version_vector. |
| ReviewAssessment | result=PASS/CONCERNS/INSUFFICIENT_CONTEXT; typed flags; runtime_assigned_coverage=FACT_COMPARISON/DESCRIPTORS_ONLY/NO_FACT_ACCESS; supplied_view_refs; proposal_digest; policy/snapshot versions. |
| CommitDecision | kind=AUTHORIZED/HOLD/DENIED/QUIET_COMPLETED/DEFERRED; reason_codes; authorization_ref nullable; assessment_ref nullable. |
| CommitAuthorization | logical_effect_id; proposal_revision; proposal_digest; payload_digest; identity/account_id; audience disclosure revision; relevant target/dependency refs; authority/policy version vector; runtime_epoch; capability_ref; status=ACTIVE/SUPERSEDED/REVOKED. |
| DispatchLease | lease_id; attempt_id; authorization_ref; logical_effect_id; payload_digest; runtime_epoch; transport_generation; reservation_ref nullable; issued_at_ms; expires_at_ms; status=ISSUED/CONSUMED/EXPIRED/CANCELLED. Runtime-only. |
| DisclosureReservation | constraint_group_id; subject/resource refs; audience class; transform/query_key; logical_effect_id; state=RESERVED/COMMITTED/POSSIBLY_COMMITTED/CANCELLED_BEFORE_DISPATCH. |
| DeliveryReceipt | effect_id; proposal_revision; attempt_id; state; fake_remote_id nullable; generation; safe error_code nullable; observed_at. |
| TurnOutcome | kind=NO_ACTION/DEFERRED/COMMITTED/DELIVERY_UNKNOWN/HELD/FAILED_INTERNAL; intent/work refs; reason_codes; effect refs. Never auto-rendered as public text. |

Every MainStep output, including goals/preferences/schedules, receives runtime producer coverage from the invocation that produced it. A private-derived intent cannot lose its restrictions simply because it fires later. It remains creatable; future context/effect admission evaluates its derived content and observable use.

Typed MainStep payloads: PARTICIPATE carries selected mode (ENGAGE/REACT/IGNORE/EXPLORE) and optional interest updates; REQUEST_ACCESS carries a MemoryAccessRequest without runtime origin; PROPOSE_MEMORY carries assertion, source claims, requested stage/scope; ADOPT_PREFERENCE carries a preference statement within ordinary agent state; CREATE_INTENT carries goal/purpose/destination bounds and optional due/expiry; SUBMIT_PLAN carries only model-selectable EffectPlan fields; FINISH carries NO_ACTION/DEFER choice. The coordinator supplies all verified actor, coverage and authority fields and validates referenced IDs. PARTICIPATE alone causes no effect.

Coordinator assigns logical_effect_id from work ID plus effect index (zero in this slice) and proposal_revision from the submission counter. The model supplies effect kind, destination, target ref when needed and content; it cannot mint identity, capability, coverage, digest, authorization or lease fields. Runtime binds valid existing capability refs. This is the model-facing SUBMIT_PLAN shape:

```json
{
  "kind": "SUBMIT_PLAN",
  "intent_ref": {"id": "intent:agent:public-research", "revision": 1},
  "disposition": "ACT",
  "effects": [
    {
      "kind": "SEND_MESSAGE",
      "destination_id": "room:public",
      "text": "That reminds me of something useful from our project.",
      "target_ref": null
    }
  ],
  "declared_fact_refs": []
}
```

No global trust score. No model-assigned actor/authority. No model-asserted coverage downgrade. No shared private/public inference slot history.

### 4.4 Payloads and contract details that must not be inferred

RecordRef is exactly an ID and positive revision. VersionVector is a sorted list of RecordRefs with at most one revision per ID. RuntimeOrigin is {actor_ref, work_ref, snapshot_ref, trigger_event_refs, producer_coverage_ref, runtime_epoch}; the model never sets it. Purpose classes are bounded strings from the fixture registry, used to match an existing permission, not sufficient evidence for issuing one. Missing registry entries deny protected matching; no semantic model decides that a new purpose equals an authorized class.

Rendered SEND_MESSAGE payload is {kind, destination_id, text, reply_target_ref nullable}; ADD_REACTION is {kind, destination_id, target_ref, target_content_digest, emoji}. Use synthetic Unicode emoji only; custom emoji and mention expansion are unsupported. A reply is supported only to a live target in the same destination. Sidecar transmits these exact bytes. No automatic mentions, splitting, reply conversion or prefix. proposal_digest binds disposition, defer fields, effect bytes, intent/snapshot refs and producer coverage. payload_digest binds the individual rendered effect. ReviewAssessment must match the proposal digest; CommitAuthorization must bind both digests.

ReviewEnvelope is {plan_ref, proposal_digest, snapshot_ref, audience_ref, intent_ref, relevant_policy_refs, relevant_commitment_refs, comparison_view_refs, bounded_quote_fragment_refs, expected_coverage_refs, sink_ref, exact_messages_payload_ref}. expected_coverage_refs is the runtime-computed closure of active STRONG restrictions on supplied snapshot/plan/intent producer inputs. Every fact-dependent restriction needs an authorized comparison view; descriptor-only restrictions need their immutable rule. An unavailable comparison view holds the proposal. The runtime verifies supplied inputs against this closure and then assigns FACT_COMPARISON; reviewer self-report cannot satisfy it. Passing assessment is keyed to these refs/revisions, not merely a draft string. Review cannot invoke a broader read or choose its own sink.

ContextSnapshot producer_coverage_ref refers to a code-created CONTEXT_ASSEMBLY coverage record. After rendering, each actual model invocation creates its own MAIN/REVIEW/SUMMARY coverage from the captured envelope. Include rendered identity/intent/preference/history/quotes/tool definitions as well as retrieved values: restriction inheritance cannot omit a private-derived goal because it was placed in an instruction-shaped field. Envelopes include no prior provider session state; reuse of a loaded inference slot requires verified reset.

source_kind registry now: FIXTURE_CONTROL, SOCIAL_MESSAGE, WEB_QUOTE, TOOL_RESULT, AGENT_OUTPUT, MEMORY, SUMMARY, INFERENCE. WEB_QUOTE/TOOL_RESULT can be synthetic hostile fixtures; they enable no live tool. history_visibility is PUBLIC_HISTORY/RESTRICTED_HISTORY/DM_PARTICIPANTS/UNKNOWN. An absent bounded social assertion belongs to OBSERVED_STATEMENT/ATTRIBUTED_ASSERTION, never runtime verified truth.

Model-only payloads additionally require: DEFER supplies defer_until and defer_expires_at; CREATE_INTENT supplies goal text, registered purpose_class, allowed destination IDs and optional due_at/expires_at; PROPOSE_MEMORY supplies assertion text, source refs, requested stage and scope, plus existing_memory_ref when promoting. Runtime chooses inherited restrictions and authenticated creator. Runtime_task_root_ids inherit all active parent work/intent roots; a spontaneous fixture interest starts a new root. A model cannot reset roots or present an unrelated episode merely to avoid reconciliation.

Exact MainStep PARTICIPATE interest updates affect ordinary preference/attention state only. FINISH is converted by the coordinator into a zero-effect QUIET/DEFER plan and evaluated by CommitGate when required; it is not a sensitive-output review bypass. Internal non-effect-capable planning may finish without an external participation outcome. QUIET under a required HOLD remains local and creates no public explanation; the runtime records HELD, not a semantic privacy certificate.

Safe error/reason code registry initially: UNSUPPORTED, INVALID_SCHEMA, INVALID_REFERENCE, AUTHORITY_MISSING, AUTHORITY_CONFLICT, POLICY_FORBIDDEN, POLICY_CHANGED, VIEW_FORBIDDEN, SINK_FORBIDDEN, CONTEXT_MISMATCH, STALE_CONTEXT, AUDIENCE_CHANGED, TARGET_CHANGED, CAPABILITY_INVALID, REVIEW_CONCERN, REVIEW_FACTS_MISSING, REVIEW_TIMEOUT, BUDGET_EXHAUSTED, RELEASE_LIMIT, STALE_EPOCH, STALE_GENERATION, LEASE_EXPIRED, LEASE_CONSUMED, SUPERSEDED, CANCELLED_BEFORE_DISPATCH, TOO_LATE, DELIVERY_UNKNOWN, CONFIRMED_NO_EFFECT, DUPLICATE, CONFLICT, NO_ACTION, DEFERRED, COMMITTED. Codes may be extended explicitly with schema tests. Never substitute a protected value or free-form model reasoning for one.

## 5. Interfaces and responsibilities

Implement these ports as ordinary module interfaces; the signatures are specifications, not supplied production code.

| Caller → callee | Method / result | Required behavior |
|---|---|---|
| Runner → Coordinator | ingest(event_batch) → durable_cursor | Validate source/revision; core transaction stores event refs + deduped work + cursor; then acknowledge fake sidecar. |
| Coordinator → ContextMemory | build_snapshot(intent_ref, audience_ref, episode_ref, sink_ref, effect_capable) → ContextSnapshot | Scope/relevance, producer coverage, revisions, exact serialization and sink admission. |
| Coordinator → MainModelPort | step(ModelCallEnvelope) → MainStep | No hidden tools/context. Scripted first; local adapter later. |
| Coordinator → ContextMemory | request_access(request, authenticated_runtime_origin) → MemoryAccessResult | Independently evaluate authority/use/precision; return active restricted view or typed no-data result. |
| Coordinator → ContextMemory | propose_candidate/assert_adoption(memory_or_preference, coverage_ref) → RecordRef | Scope and authority validation; adoption cannot edit hard policy. |
| Coordinator → Coordinator store | create_intent(goal, purpose, triggers, actor, coverage) → Intent | No pre-existing grant prerequisite; preserve runtime origin/coverage. |
| Coordinator → CommitGate | evaluate(EffectPlan) → CommitDecision | Code checks first, route review, relevant semantic hold, versioned authorization. |
| CommitGate → ReviewerPort | assess(ReviewEnvelope) → assessment data | Runtime fixes coverage/sink/inputs; no tools or policy changes. |
| Coordinator → FakeSidecar | submit_authorized(auth_ref, exact_effect) → DeliveryReceipt | Idempotent same effect/revision/digest; no direct model submission. |
| FakeSidecar → CommitGate | validate_and_reserve(outbox_ref, generation, now_ms) → DispatchLease or HOLD/DENIED | Latest relevant versions; atomically reserve release + issue lease in core. |
| FakeSidecar → Core control port | consume_lease(lease_ref, generation) → consumption receipt | Single use, epoch/generation/expiry checks; persist consumption. |
| Coordinator → FakeSidecar | cancel_or_supersede(effect_id, expected_revision) → CANCELLED/TOO_LATE/CONFLICT | Durable cancellation before dispatch start; never pretend in-flight prevention. |
| FakeSidecar → FakeWorld | apply(attempt_id, effect_bytes, target_preconditions) → success/failure/timeout | Testable remote visibility; optionally persists effect before losing response. |
| Coordinator → FakeSidecar | status/reconcile(effect_id) → receipt/evidence | UNKNOWN never blindly retried; reconcile exact fake-world evidence. |
| Runner → lifecycle control | restart_core/restart_transport, advance_clock, inject_fault | Bump epochs/generations, perform defined recovery; deterministic ordering. |

The runner owns a serialized DispatchCoordinator section shared by lifecycle/policy change handlers and the fake sidecar dispatch-start transition. Acquire it only for code/DB transitions, never while a main/review model waits. No second live dispatcher is accepted. The fake world call occurs after durable DISPATCH_STARTED and outside that section.

Core and transport transaction commits remain separate. Persist a PREPARED attempt record before requesting its lease and bind the attempt_id into that lease. Crash checkpoints exist between validate/reserve, lease consumption and transport DISPATCH_STARTED. If lease was consumed but transport cannot prove a cancelled/nonstarted path, record UNKNOWN/POSSIBLY_COMMITTED conservatively. Do not claim cross-DB atomicity.

ReviewEnvelope contains exact proposal/disposition, current audience/intent, immutable relevant policy/commitments, actual authorized fact-comparison views when REQUIRED, bounded external quote context and producer coverage. Runtime assigns coverage by construction. Descriptors alone cannot satisfy the slice's REQUIRED semantic resolution. Unavailable REVIEW authority or missing facts yields HOLD, not a broader vault fetch.

Minimal envelope-to-port rule: MainModelPort returns only MainStep; ReviewerPort returns only assessment data; PerceptionPort returns only signals. A provider may not auto-call tools, append sessions, recall memory, stream externally or switch endpoints. The adapter verifies context/tool request capture before accepting a response. Local Hermes integration can reuse weights and identity while still using fresh call contexts.

## 6. State machines and transition rules

### 6.1 Work, intent and plan

Work: READY → RUNNING → COMPLETED or HOLD. HOLD resumes only on a named external change (policy/resource/target/audience/review service recovery) or an agent-chosen replacement plan within budgets. It is not an automatic spin/review loop. A work lease lost during restart returns to READY with budgets and dedup key retained.

Intent: OPEN → COMPLETED/ABANDONED/DEFERRED; DEFERRED → OPEN on exactly one deduped timer occurrence. The agent can abandon at any stage. DEFER requires an explicit due condition/time and expiry; no forced eventual response.

Plan: DRAFT → CHECKING → AUTHORIZED/HOLD/DENIED/QUIET_COMPLETED/DEFERRED. A new revision supersedes an old pending effect only after fake sidecar cancellation acknowledgement. Required concerns cannot be marked advisory by the main agent.

Participation may finish before retrieval. NO_ACTION is normal; no fallback acknowledgement, typing, public error, or transport outbox row. Strong-sensitive QUIET/DEFER gets applicable assessment; other silence does not.

### 6.2 Outbox and lease

Outbox transitions:

- QUEUED → HELD / CANCELLED / DISPATCH_STARTED.
- HELD → QUEUED after fresh authorization, or CANCELLED.
- DISPATCH_STARTED → SENT / FAILED_CONFIRMED / UNKNOWN.
- UNKNOWN → SENT or FAILED_CONFIRMED only through positive reconciliation evidence; unresolved remains UNKNOWN.
- SENT is terminal for that logical effect. CANCELLED is terminal for that proposal revision; an explicitly authorized replacement may requeue the same logical effect ID only through acknowledged supersession. A later edit of a sent effect is unsupported in this slice.

Retry after FAILED_CONFIRMED requires evidence that no effect occurred plus still-valid policy/dependencies and a new attempt ID; preserve logical effect ID. Retry count is one in slice. No retry of timeout/ambiguous failure. Exact duplicate submit returns current state; conflicting bytes return CONFLICT.

Creating a new intent must not bypass UNKNOWN. Preserve runtime retry/task lineage on agent-derived intents; hold any ACT to the same destination sharing an unresolved task root, including paraphrases/reactions. Also hold a same-operation fingerprint (account, destination, kind, target and payload digest) when its related episode still has an unresolved possible effect. Do not globally ban repeated ordinary text across unrelated episodes. A new task label or model claim “not a retry” cannot erase the unresolved lineage. Detecting an equivalent operation in a genuinely independent root is not guaranteed by this structural rule; test and report that limitation.

FakeWorld.reconcile returns APPLIED, CERTIFIED_NOT_APPLIED or INDETERMINATE. CERTIFIED_NOT_APPLIED requires the fake-world protocol to prove no accepted/pending operation can later become visible; absence from a message list is insufficient. Real Discord reconcilers must not assume this proof is available.

Lease: ISSUED → CONSUMED once, EXPIRED or CANCELLED. On core epoch change, unused leases become invalid and queued effects hold. Sidecar restart advances transport generation and rejects stale submit/dispatch calls. DISPATCH_STARTED at restart becomes UNKNOWN, including when the fake world would have proven no send if queried: query first, do not guess.

Local dispatch linearization: while DispatchCoordinator serialization holds, validate current state, reserve/consume valid lease, and durably record DISPATCH_STARTED. A relevant change ordered before this transition prevents it. Changes after it are too late for a prevention guarantee. The separate commits may crash; conservative recovery handles uncertainty.

### 6.3 Memory and derived context

Memory lifecycle: EPHEMERAL → SOCIAL → CANDIDATE → DURABLE with retained scope. Implement explicit promotion; do not infer BROAD from DURABLE. Competing assertions can coexist as CONTESTED. Summary output is DERIVED with authority NONE.

Correction: append new assertion revision → mark superseded/stale input descendants → invalidate affected snapshots/plans → update eligible context selection. A derivation job's captured input_version_vector must still match before promotion; otherwise retain only as stale diagnostic metadata without eligible payload.

Revocation: mark policy/view revoked → invalidate dependent contexts/authorizations → cancel queued effects where possible. Tombstoning stored data does not remove an already loaded model context; invalidate/retire that call's continuation.

Source message deletion: tombstone message text and dependent eligibility under fixture retention policy; do not declare every derived assertion false. An erasure request is unsupported until its authority/semantics are defined; never infer it from deletion.

### 6.4 Replay scheduling, retention and scope defaults

Reception appends an EPHEMERAL observation automatically; it does not create a durable memory assertion or an obligation to respond. Explicit agent PROPOSE_MEMORY may retain ordinary eligible observations as SOCIAL/CANDIDATE in their source room/relationship. DURABLE promotion requires an existing CANDIDATE ref and explicit agent proposal. Fixture-protected material needs a matching permitted storage/use policy; requests cannot widen scopes or discharge commitments. Broad promotion requires an explicit curated scope-change permission. No embeddings/global retrieval index in this slice.

Fixture scheduling defaults: select last 20 eligible episode events plus explicitly fixture-ranked memory refs, within a 16 KiB serialized social-content limit; omit excess content whole-fragment with metadata indicating omission. These are replay defaults, not an empirically optimized context budget. Identity/control records and admitted protected views also have fixture bounds; fail validation rather than truncating a policy. Each work item has one current destination/audience; cross-room retrieval preserves each fragment's original audience/scope. A change of intended destination builds a new snapshot.

Deterministic wake candidates: authenticated control/timer event, reply/direct-address event, fixture interest match, or every tenth unscheduled eligible event per room as catch-up. Direct address only wakes deliberation. Perception can suggest earlier candidates but cannot suppress these candidates or create authority/review requirements. Coalesce ordinary pending events per episode, one READY item per episode, process round-robin across destinations with due timers participating in the same queue. max_wakes_per_destination_per_virtual_minute=6 and max_wakes_global_per_virtual_minute=20; excess observations stay in bounded catch-up history. Sensor output UNKNOWN uses deterministic candidates. Scripted participation makes silence explicit; do not infer silence from a missing main port.

Only fixed-time DEFER/TIMER is implemented; condition-language schedules remain UNSUPPORTED. Timer trigger_key is (intent ID, due timestamp, endorsement revision), one occurrence. Intent expiry abandons without a visible effect. Work HOLD cannot consume unbounded retries: all resumptions retain counters. Delivery-UNKNOWN reconciliation uses status work, not a new conversational main turn.

Fixture retention defaults: EPHEMERAL event text 1 virtual day; SOCIAL 7 days; CANDIDATE 7 days; DURABLE 30 days, renewable only by an explicit policy-permitted promotion/retention event. Per-record fixture values may shorten/override these defaults and must be included in the run manifest. Envelopes/drafts expire after work completion plus 1 day; synthetic outbox text after terminal settlement plus 1 day. UNKNOWN text remains for bounded reconciliation, at most 7 days; expiry retains identity/digests/reservations and never refunds possible disclosure. FakeWorld observer traces are separate explicitly retained synthetic test data, not a future third-party retention policy.

## 7. Minimal persistence

Use core.sqlite and transport.sqlite, SQLite WAL and bounded synthetic payload stores. A generic versioned-record table avoids premature ORM/table proliferation; validated record types remain exact. Do not use one giant mutable JSON state file.

| DB/table | Key and records | Constraints / indexed fields |
|---|---|---|
| core.records | (record_id, revision) primary key; record_type; validated payload_json; current/status; timestamps | Unique current revision per record ID; typed refs; no in-place historical edits. Stores principals/policies/intents/memory/contexts/plans/reviews/authorizations/coverage. |
| core.payloads | payload_id primary key; owner_ref; bounded UTF-8 bytes; sensitivity/commitment refs; expires_at; status=ACTIVE/PURGED | Secret/third-party text is out-of-line, not repeated in record JSON. Purge bytes while retaining permitted metadata refs. |
| core.memory_scope | memory_id + revision; stage, scope_kind/ref, status, expiry, assertion/commitment refs | Filter eligibility/scope before reading candidate payload; update with record promotion/tombstone transaction. |
| core.dependencies | child_ref, parent_ref, dependency_kind=PRODUCER_INPUT/DECLARED/CONTROL | Acyclic producer graph; DECLARED cannot lower inherited constraints; indexed parent for invalidation. |
| core.ingestion | consumer_id + source_event_id + source_revision; transport seq and fragment refs | Unique ingestion; durable contiguous cursor in core.meta. |
| core.work | work_id; unique trigger_key; intent/episode refs; state, budgets, lease, epoch | Dedup timers/turns; one active lease per episode; restart recovery. |
| core.release_reservations | reservation_id; effect_id; constraint_group/query_key; state; accounting units | Unique reservation per effect/constraint; atomic limit check and reservation. Accounting unit is fixture query/release, not leaked bits. |
| core.dispatch_leases | lease_id; attempt/effect/auth refs; epoch/generation; expiry; state | Single use; reference valid authorization/active reservation. |
| core.audit | audit_id; operation/entity opaque refs; safe reason codes; versions/time | No secret payloads, free-form reasoning or full necessity text. |
| core.meta | keys for schema version, runtime epoch, cursor, clock, fixture/config/model manifest | Migrations explicit; unknown schema refuses startup. |
| transport.events | journal_seq primary key; unique source event/revision; normalized bounded event/payload refs | Gap/deletion/revision records; scoped retention. |
| transport.payloads | payload_id primary key; owner_event/effect ref; bounded UTF-8 bytes; expires_at; status=ACTIVE/PURGED | Incoming/outgoing synthetic text with separate retention; normalized metadata does not duplicate payload bytes. |
| transport.outbox | logical_effect_id primary key; proposal revision/digest; payload ref; state; auth/lease refs; generation; target preconditions | Same ID cannot send two revisions; supersession acknowledged; attempt metadata separate. |
| transport.attempts | attempt_id primary key; effect_id; state; fake remote receipt; timing/error | Immutable attempt identity; DISPATCH_STARTED recovered conservatively. |
| transport.meta | active generation/account session, consumer cursor, fake-world state reference | Exclusive fake transport owner; cursor monotonic. |

FakeWorld persistence is independent test evidence: attempt/effect bytes, visibility time and remote IDs. It may be a third fixture DB or durable test log; core cannot read it directly except through reconciliation port. It must survive the simulated process loss or UNKNOWN tests are meaningless.

Core ingestion transaction: store refs, create/dedup work, advance contiguous cursor → commit → transport acknowledgement. Out-of-order batches cannot advance past a missing seq. If retained journal entries no longer exist, emit GAP and explicitly resynchronize; do not pretend completeness.

Access result/view audit contains IDs/classes/transforms/version refs only. Necessity/draft text belongs to bounded restricted payload records, not operational logs or immutable record JSON. On payload expiry, remove eligible payload copies from synthetic stores and keep only permitted tombstone/digest refs. Immutable revision history refers to payload IDs whose bytes may be purged; it does not require immutable secret bytes. Tests cover record payloads, model envelopes, outbox copies and fake-world observer traces according to their separate retention roles. This is application eligibility/deletion, not a claim that SQLite WAL/backups provide forensic erasure.

Memory jobs and scheduled intent creation use stable idempotency keys (parent event/call ID + operation index). Replaying a completed model step cannot promote the same fact or create the same schedule twice. If a model call finished but its result was never durably accepted, rerunning may produce a different result; accept at most one continuation with compare-and-set on the work step version.

## 8. CommitGate algorithm and invariants

In order:

1. Validate plan kind/budget, identity, enabled capability, destination and exact rendered bytes.
2. Resolve actual snapshot/producer coverage, current material audience/target, resource/authority/commitment versions and compute admission.
3. Reject deterministic failures; otherwise route NONE/ADVISORY/REQUIRED.
4. For REQUIRED, build approved fact-comparison ReviewEnvelope. PASS with full expected runtime coverage permits continuation under existing policy; CONCERNS/INSUFFICIENT_CONTEXT/error yields HOLD. For ADVISORY record bounded advice/disposition; for NONE optional shadow review has no authority over delivery.
5. Recheck relevant versions after any model wait. A model assessment against changed versions does not authorize current bytes.
6. For zero effects, finish QUIET or persist the bounded deferred intent. For one effect, create a versioned CommitAuthorization and submit to sidecar.
7. Sidecar dispatch invokes current validation/reservation/lease checks, not a cached PASS. Relevant permission change before dispatch-start blocks the effect.

Exact routing in the first slice: invalid capability/sink/view-admission/version/authority or an explicitly requested forbidden protected operation → HARD_DENY. Otherwise, any active STRONG commitment in the snapshot, plan producer inputs or derived intent that requires expression/observable-use resolution → REQUIRED (including relevant QUIET/DEFER). Without that condition, fixture-configured ordinary concern → ADVISORY; all other cases → NONE. Sensor scores alone cannot create REQUIRED. A disclosed-but-unclaimed dependency is still included through runtime coverage. A strong expression restriction does not automatically prohibit unrelated words; REQUIRED assesses semantic compliance under the existing restriction. A strongly INTERNAL_ONLY view in an externally effect-capable snapshot is an admission failure, not a reviewer-waivable exception.

Code invariants to assert/test:

| ID | Invariant |
|---|---|
| I01 | Any valid agent-origin intent can be created without a resource grant; protected operations still require matching authority. |
| I02 | No external text, principal kind, agent endorsement or purpose rationale upgrades instruction/resource authority. |
| I03 | Joint approval follows configured issuer policy, not universal operator priority or automatic all-subject veto. |
| I04 | Actual supplied producer inputs determine conservative inherited constraints; claimed parents/fact IDs cannot remove them. |
| I05 | Every model invocation passes compute admission and its actual context/tool schemas match the envelope. |
| I06 | All planned fake effects use CommitGate; model output cannot directly submit an authorization or invoke sidecar/world. |
| I07 | REQUIRED missing facts/reviewer or adverse assessment holds that proposal; NONE/advisory does not force extra inference/refusal. |
| I08 | Payload/target/audience/policy/dependency versions remain bound through dispatch; revisions never edit approved bytes in place. |
| I09 | Stable effect IDs plus acknowledged cancellation prevent r1/r2 duplicate intentional dispatch. |
| I10 | One sidecar generation executes; stale runtime epochs, expired/consumed leases and revoked authorities cannot start dispatch. |
| I11 | UNKNOWN/possible release cannot be blindly resent/refunded; remote side effects are not rolled back by local DB recovery. |
| I12 | Room/episode merge, memory promotion and private→public switching do not widen source/view permissions. |
| I13 | QUIET/error/budget outcomes create no automatic visible fallback. |
| I14 | Corrections/revocation invalidate affected active contexts and late derivations, not just search indices. |
| I15 | Release limit checking and reservation are one core transaction; concurrent queued releases cannot both spend the same allowance. |
| I16 | Resumed jobs/timers/main steps deduplicate accepted state changes while preserving one continuous identity. |

These assert structural properties. They do not assert perfect semantic confidentiality, truthful model beliefs or calibrated sensor probability.

## 9. Stubs and explicit extension points

| Piece | First implementation | Forbidden fallback |
|---|---|---|
| MainModelPort | Scripted typed decisions; optional local Hermes bridge later | Existing unrestricted chat/tool loop |
| ReviewerPort | Scripted fixture assessments with runtime coverage; optional local model for experiments | PASS on exception/missing facts |
| PerceptionPort | Deterministic features + scripted scores/UNKNOWN | Mandatory Clef service or scores granting permissions |
| Resource vault | Curated synthetic values and policy registry | Read real USER.md, files, credentials or banking tools |
| Transform registry | Two fixed transforms with fixture parameters | Executing model-generated code or arbitrary predicates |
| FakeSidecar / FakeWorld | Durable deterministic journals/outbox/remote visibility with fault injection | Actual Discord import/network calls |
| Semantic leak metrics | Observer traces, paired fixtures and assessment harness | Claiming no leak from substring checks alone |
| Coalition inference | Configured audience classes/query groups | Inferring real people/coalitions automatically |
| Retention | Virtual-clock expiry and explicit synthetic tombstones | Claiming forensic OS deletion or universal erasure |
| Native provenance/model training | Metadata serializer and control/data roles | Pretending new textual tags are trained security features |

## 10. Test order and exact replay stories

RunManifest is a validated fixture record: schema/version; fixture ID/seed; AgentIdentity ref; account ID; principal/policy/authority/capability/commitment/transform registries; room/DM/audience/episode seeds; resource values and memory refs; SCRIPTED_TEST sink; scripted main/review/perception sequences; clock origin; scheduling/retention/budget overrides; selected fault checkpoint(s); expected state transitions and observer events. Fixture control supplies this through its trusted port. Hash the manifest excluding low-entropy protected values in shared audit output; keep the full synthetic manifest privately with the test run. Unknown registry IDs fail startup. No environment-variable credential discovery or automatic default-account selection.

Run deterministic tests in this order; no live account and no large model required.

1. **Types and records:** schema/version bounds, runtime-only fields, canonical bindings, invalid references and state transitions.
2. **Authority/autonomy:** create spontaneous goal; reject grantless protected access; allow agent own preference; operator cannot override third-party confidence; configured joint grant/conflict; endorsement retains external trigger.
3. **Context/provenance:** multi-person episode; ordinary cross-context recall; source marking; declared-parent omission; public-only regeneration; private/public fresh snapshot; sink/provider mismatch.
4. **Memory:** all five access statuses/all view shapes; fixed transform inheritance; staged promotion without broad scope; stale summary after correction; timer/private-derived intent lineage; no leaked audit payload.
5. **Commit routes:** ordinary message/reaction without extra review; advisory ignore; required PASS/CONCERNS/insufficient facts/timeout; quiet-oracle disposition; bounded rewrite loop.
6. **Dispatch/faults:** r1/r2 supersession; duplicate submit; stale generation/epoch; expiry; changed reaction target; audience material change versus harmless public chatter; crash before/after each core/transport/world boundary; confirmed failure versus UNKNOWN.
7. **Composition/recovery:** concurrent query reservations; UNKNOWN conservative accounting; repeated timers/main-result acceptance; gap/cursor recovery; sidecar continuity across core restart.
8. **Social positive stories:** voluntary initiation, ignore mention, reaction instead of reply, legitimate banter, follow-up suppression, correction/relationship continuity across restart. An always-silent implementation fails.
9. **Optional model experiments:** replace only the model/reviewer/perception ports; compare prompt-only behavioral baseline, selective review and all-review at matched budgets; record model/config hashes and held-out observer traces.

Seed at least these five resource classes: public project fact; agent-private ordinary preference; human financial protected fact; third-party confidential disclosure; joint project protected record. Every record has explicit fixture scope/commitments, not a name-based classifier.

One end-to-end story: stranger suggests public research → agent creates its own intent → public/ordinary scoped retrieval → casual main draft → NONE route code gate → fake send → restart → appropriate continuation. There is no owner approval.

Second story: public affordability probe → protected request lacks matching use/disclosure basis or receives a constrained view → main revises, jokes, stays quiet or chooses allowed influence → applicable review → exact commit/suppress → later scheduled behavior retains producer coverage. Use varied secret worlds to examine the full sequence.

Third story: r1 reaction authorized → target edit/policy change/core restart before dispatch → r1 held/cancelled → r2 can proceed only after acknowledged pre-dispatch cancellation and fresh checks. If remote visibility is uncertain, no replacement retry.

Record tests by I01–I16 and review finding IDs, plus named crash checkpoints: AFTER_INGEST_BEFORE_ACK, AFTER_MODEL_BEFORE_ACCEPT, AFTER_RESERVE_BEFORE_LEASE_RETURN, AFTER_LEASE_CONSUME_BEFORE_DISPATCH_START, AFTER_DISPATCH_START_BEFORE_WORLD, AFTER_WORLD_BEFORE_RECEIPT, DURING_CANCEL, DURING_CORRECTION_JOB.

## 11. Questions the coding agent must not answer by guessing

These do not block the defined fake slice; they block the corresponding extension or security claim.

| Unknown | Required behavior now | Evidence/decision needed before extension |
|---|---|---|
| Real Hermes hooks/signatures/context pollution | Inspect current checkout; adapter refuses incompatible/unmanifested input | Actual hook integration and request capture tests |
| Real social confidentiality/authority disputes | Curated fixture policies; CONFLICT/PENDING_AUTHORITY on unresolved strong rules | Norm elicitation and explicit authority policy |
| Native model obedience and leak resistance | Scripted baseline first; honest experimental result | Held-out active-context/behavioral leakage and social evaluation |
| Clef fit/latency/calibration | Optional disabled sensor by default | Local measured end-to-end benefit |
| Remote model/embedding disclosure | REMOTE_UNSUPPORTED | Approved sink/data-flow/custody design |
| Production credential/capability isolation | No real privileged resources | OS/service custody and complete bypass audit |
| Discord atomic target/audience conditions and delivery evidence | Fake-world defined preconditions; no exactly-once claim | Pinned transport capability/reconciliation tests |
| Cross-process revocation/fencing | Serialized fake dispatch; real transport not enabled | Account lock, authenticated IPC, lease/version freshness and remote-race policy |
| Multi-chunk/edit/delete/voice/attachments | UNSUPPORTED | Effect-specific commit/preconditions/partial accounting contracts |
| Numeric privacy/coalition budget | Fixed query/transform constraints only | Validated threat model and composition metrics |
| Erasure versus source deletion | Distinct events; erasure operation unsupported | Authorized retention/erasure semantics |
| Fine-tuning/source tokens | Metadata only | Training/model work with safety and social regressions |

If implementation requires an answer outside these defaults, report the exact blocked port/invariant. Do not silently add universal owner priority, hidden provider fallback, a secret-derived public response, or a hard reviewer veto over ordinary social behavior.

## 12. Coding-session deliverables and exit

Deliver runner/fixtures, typed contracts, two-domain state stores, scripted ports, invariant/fault tests, and a short integration map to the actual Hermes checkout. Demonstrate both spontaneous permitted speech and protected-action suppression, plus restart/UNKNOWN behavior.

Exit when deterministic I01–I16 tests and the three end-to-end stories pass, unsupported capabilities remain inert, and social positive fixtures prevent universal silence. State plainly that model-backed semantic privacy, local inference performance and live Discord behavior remain untested until their experiments run.

The coding session should implement these decisions, not reopen the original literature review or reinvent the agent's autonomy model.
