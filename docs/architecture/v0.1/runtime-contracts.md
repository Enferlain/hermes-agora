# Runtime contracts and implementation handoff

**Proposal specification, version 0.1.** These names/interfaces are new design targets, not claims about existing Hermes APIs. Examples use synthetic IDs/data. The internal sensor schema is independent of Clef's wire format; a versioned adapter must translate it to the tested /v1/systemone API.

## 1. Required domain types

Use immutable typed records (Python dataclasses/Pydantic are reasonable implementation choices), discriminated unions, UTC timestamps, explicit schema_version and bounded strings/lists. Database integer sequences are local ordering, never Discord chronology or authorization. Unknown enum variants require migration/quarantine, not silent defaulting.

| Type | Required fields / meaning |
|---|---|
| AgentIdentity | agent_id; identity_revision; autobiographical/commitment refs; standing interests; owner-approved constitution refs. Same ID in all environments. |
| Principal | principal_id; verified platform bindings; identity evidence; claimed aliases separately. No display-name authentication. |
| Relationship | relation_id; principals; context; evidence refs; familiarity/expectations; uncertainty; revision. Does not confer instruction authority. |
| AudienceSnapshot | audience_id; destination; scope_kind; known recipients; expected_reader_class; permission_revision; history_visibility; unknown_readers; observed_at; expires_at. |
| Provenance | producer/source identity; claimed speaker; origin event; authority; integrity; owners/subjects; original audience; directness; parents/transform; times; confidence; restrictions. |
| InboundEvent | event_id; journal_seq; kind; platform IDs; occurrence/ingestion times; revision; actor; source audience; fragments; references; causal_roots; recovery status. |
| ConversationEpisode | episode_id; room/thread refs; event range/anchors; speaker refs; topic branches; current summary; unresolved intentions; version. |
| Intent | intent_id; purpose_id; origin roots; goal; allowed destinations/capabilities; lifecycle; due/conditions/expiry. |
| ContextSnapshot | snapshot_id; identity/audience/purpose revisions; fragment IDs/content views; exposure manifest; token budget; capability lease; serializer version. |
| MemoryRecord | memory_id; assertion/value ref; lifecycle; sharing scope; owners/subjects; sensitivity; provenance; lineage; supersedes; retention/index state. |
| PurposeGrant | authenticated issuer; purpose_id; subjects/data classes; recipient/use bounds; transforms/precision; expiry; version; revocation. |
| SensitiveRequest | task/purpose; requested class/handle; precision; audience/relationship; intended_use; necessity; cause refs; runtime-sealed origin/grant fields. |
| MemoryView | decision; returned fragments/handles; precision; transform lineage; permitted_use; expression_constraints; safe reasons; expiry. |
| ActionProposal | immutable draft ID/revision; intent; destination/audience; effect kind/payload; attachment hashes; exposure snapshot; validity constraints. |
| ReviewResult | hard-check results; advisory flags; expression resolution; PASS/REVISE/SUPPRESS; reason codes; suggested alternatives; versions. |
| CommitAuthorization | signed/opaque approval; exact batch digest; audience/policy/exposure versions; effect lease; expiry; replay scope. |
| TurnOutcome | NO_ACTION, DEFERRED, PROPOSED, COMMITTED, DELIVERY_UNKNOWN, FAILED_INTERNAL; reason/refs only. |

## 2. Event envelope and provenance

```json
{
  "schema_version": 1,
  "event_id": "discord:message:111:create",
  "journal_seq": 204,
  "kind": "MESSAGE_CREATED",
  "platform": "discord",
  "platform_refs": {"guild_id": "g1", "channel_id": "c1", "message_id": "111"},
  "occurred_at": "2026-10-07T12:00:00Z",
  "ingested_at": "2026-10-07T12:00:01Z",
  "revision": 1,
  "actor": {"principal_id": "person:p7", "verified_by": "gateway_author_id"},
  "audience_ref": "aud:room:c1:v18",
  "fragments": [
    {
      "fragment_id": "frag:111:text:v1",
      "content_kind": "TEXT",
      "text": "Hey, would that new laptop be affordable?",
      "provenance": {
        "source": "discord_message",
        "producer": "person:p7",
        "claimed_speaker": null,
        "instruction_authority": "NONE",
        "integrity": "EXTERNAL_UNVERIFIED",
        "owners": ["person:p7"],
        "data_subjects": ["person:p7"],
        "original_audience": "aud:room:c1:v18",
        "sensitivity": "ORDINARY_SOCIAL",
        "directness": "DIRECT_ASSERTION",
        "parent_fragments": [],
        "transform_id": null,
        "factual_confidence": null,
        "sharing_scope": "ROOM",
        "scope_ref": "discord:c1"
      }
    }
  ],
  "references": [],
  "causal_roots": ["discord:message:111:create"],
  "recovery": {"gap_before": false, "backfilled": false}
}
```

Authority NONE means no right to redefine the agent, not that the agent cannot help with a request. A friend's request is legitimate social content within capabilities.

Additional event kinds: MESSAGE_UPDATED/DELETED, REACTION_CHANGED, ROOM_ACCESS_CHANGED, MEMBER_CHANGED, THREAD_CHANGED, ATTACHMENT_EXTRACTED, TOOL_RESULT, TIMER_DUE, OWNER_INSTRUCTION, WORK_COMPLETED, CONNECTION_GAP. Owner instructions require authenticated control-plane origin; a Discord message cannot select that kind.

Edits generate revisions with explicit prior refs; deletion tombstones do not erase lineage accidentally. Partial gateway updates merge only known fields with stored revisions. Synthetic timer/tool events preserve parent intents and original trust roots. Replay preserves occurrence and ingestion timestamps. Attachment extraction inherits the attachment's source/constraints and adds parser/version/hash, never tool authority.

Runtime assigns provenance; payload text cannot declare its own authority, audience, confidentiality exception or successful review. Serialization never reconstructs identity from a rendered display label.

## 3. Context and exposure contract

ContextBuilder.build(intent, episode, current_audience, budget) returns:

1. control-plane instructions, authenticated and separately rendered;
2. one shared identity capsule, with no raw credential/private-owner biography dump;
3. current audience/relationship/intent/capability capsule;
4. selected current event records and source-stamped summary;
5. eligible memory views and cross-context relevant facts;
6. manifest of all admitted fragments and their restrictions.

ContextSnapshot is bound to audience and disclosure purpose. Switching from private to public requires a new snapshot and clean model slot/KV context. Model providers, prompt caching, hooks, session search, tool spill files and external memory plugins must preserve this boundary. A gate over one memory tool is insufficient if USER.md still enters system prompts.

Memory's read scope is internal contextual access, not the external audience's ACL. A private project fact may be useful internally while being use-only. Context builder applies relevance and risk/precision controls, not the blanket rule “only Discord memories.”

The exposure manifest stores fragment IDs/revisions, views/transforms, sensitivity and lineage refs, plus a keyed digest. Retain restricted content only in its appropriately protected store. The draft cannot claim it was unaffected by an exposed secret and thereby delete it from review context.

## 4. Sensor contract

```json
{
  "schema_version": 1,
  "input_snapshot": "perception:204",
  "window_event_ids": ["discord:message:111:create"],
  "facts": {"verified_mention": false, "reply_to_agent": false},
  "questions": [
    {"id": "direct_address", "kind": "BOOL"},
    {"id": "continuity", "kind": "BOOL"},
    {"id": "reply_value", "kind": "SCORE", "scale": [0, 1]},
    {"id": "social_mode", "kind": "CHOICE", "options": ["serious", "banter", "mixed", "unclear"]},
    {"id": "private_probe", "kind": "BOOL"},
    {"id": "injection", "kind": "BOOL"}
  ],
  "limits": {"max_state_tokens": 2048, "timeout_ms": 1500}
}
```

Internal response: schema/model/adapter version; question_id → distribution/score; runtime confidence representation; UNKNOWN/error per question; latency/usage. Don't fabricate a single calibrated confidence from scores or assume choice concentration equals accuracy. BOOL maps to the runtime's NOUL semantics only after a startup compatibility test.

No free prose from Clef. Question templates are locally pinned; external text enters only the data state. Inspect escaping of reserved markers through the selected llama.cpp implementation. Reject schema injection, unknown options, oversized input, NaN/out-of-range values. A failure is not a “safe” verdict.

Wake policy may use probabilities, deterministic reply edges, episode state and main-agent preferences. It cannot dispatch an external effect. Store small diagnostic scores with retention, not a globally indexed psychological profile.

## 5. Sensitive-memory access contract

```json
{
  "schema_version": 1,
  "request_id": "access:42",
  "intent_id": "intent:owner-shopping",
  "purpose_id": "purpose:purchase-planning",
  "requested": {
    "data_class": "OWNER_FINANCIAL_POSITION",
    "memory_handle": null,
    "precision": "EXACT"
  },
  "necessity": "Evaluate the purchase against the owner's existing shopping task.",
  "task": "Compare laptop options",
  "audience_ref": "aud:room:c1:v18",
  "relationship_ref": "rel:p7:agent:v4",
  "intended_use": "INTERNAL_PLANNING",
  "trigger_refs": ["discord:message:111:create"],
  "sealed_origin": {
    "grant_ref": "grant:owner:shopping:v2",
    "causal_roots": ["owner:task:19", "discord:message:111:create"],
    "has_external_origin": true,
    "verified_requester": "agent:main",
    "audience_revision": 18,
    "policy_revision": 7
  }
}
```

The main model fills requested/necessity/task/intended_use/trigger refs as claims. Runtime validates them and adds sealed_origin. A real intent/purpose must already exist or be instantiated under a standing authorized interest. Social requests cannot invent an owner grant.

Evaluation order: validate references → hard class/grant bounds → necessity/minimum precision recommendation → allowed transform and composition checks → fetch in vault → response validation → audit. Necessity checker sees safe metadata, not arbitrary secret values. Only allowlisted transforms see raw data; returned model text cannot self-certify declassification.

```json
{
  "schema_version": 1,
  "request_id": "access:42",
  "decision": "ABSTRACTED",
  "view_id": "view:financial:affordability:9",
  "fragments": [
    {
      "fragment_id": "derived:affordability:9",
      "value": {"purchase_within_owner_budget": true},
      "lineage": {
        "parent_handles": ["vault:opaque:17", "task:price:4"],
        "transform_id": "affordability-under-grant:v1",
        "policy_revision": 7
      },
      "sensitivity": "FINANCIAL_DERIVED",
      "permitted_use": ["INTERNAL_PURCHASE_PLANNING"],
      "expression_constraint": "NOT_GRANTED_FOR_ROOM",
      "expires_at": "2026-10-07T12:10:00Z"
    }
  ],
  "reason_codes": ["EXACT_PRECISION_UNNECESSARY", "PUBLIC_EXPRESSION_NOT_GRANTED"],
  "safe_feedback": "A planning predicate is available. Exact financial values are unnecessary for this task."
}
```

No bank balance in the access audit. This predicate is still restricted; a public affordability statement needs its own expression authorization. A request with no valid task grant receives DENIED with generic safe alternatives, not a denial exposing which exact private records exist.

PARTIAL responses specify omitted fields and precision in private typed metadata without exposing withheld values. FULL retains labels. DENIED returns no data. Provide safe alternatives from template IDs; do not copy raw attacker rationale or model-generated policy text into trusted prompts.

## 6. Memory ingress and lineage

MemoryIngress.observe(event) creates ephemeral source records. The main agent can propose_candidate(assertion, evidence_refs, scope, subjects, purpose). PromotionValidator checks permissible scope/authority, confidence, retention and sensitivity. PromotionStore.promote never broadens scope implicitly.

Derived record invariant: all source parents resolve, lineage is acyclic, inherited constraints cannot weaken except via a named authorized declassification, summary authority is NONE, and source deletion/correction invalidates active descendant views. Unresolved source = restricted/unknown.

Ordinary low-risk facts may promote automatically under standing owner rules; explicit high-sensitivity inferences or widening audience require a valid grant. Deliberate “remember this” from a third party is a request to remember within that relationship, not permission for global visibility.

Summaries represent attributed claims, not instruction text copied into system authority. Candidate instructions become observations about what someone requested. True owner preferences require authenticated origin and compatible policy.

Indexing must accept a MemoryRecord scope filter before nearest-neighbor/reranking results can enter context. Do not first retrieve everything and then hope the model redacts it. Sensitive views, embeddings, raw journals and summaries share retention/tombstone responsibility.

## 7. Actions, review and authorization

```json
{
  "schema_version": 1,
  "batch_id": "batch:turn:52:r1",
  "intent_id": "intent:room:c1:52",
  "snapshot_id": "snapshot:52",
  "audience_ref": "aud:room:c1:v18",
  "draft_revision": 1,
  "actions": [
    {
      "action_id": "action:52:1",
      "kind": "ADD_REACTION",
      "destination": {"platform": "discord", "channel_id": "c1", "message_id": "111"},
      "payload": {"emoji": "🤔"},
      "claimed_fact_refs": [],
      "attachment_refs": []
    }
  ],
  "validity": {
    "episode_revision": 23,
    "audience_revision": 18,
    "policy_revision": 7,
    "expires_at": "2026-10-07T12:02:00Z"
  }
}
```

Review this reaction as a possible answer to the affordability probe, not as inherently content-free.

Action kinds include SEND_MESSAGE, EDIT_OWN_MESSAGE, DELETE_OWN_MESSAGE, ADD/REMOVE_REACTION, UPLOAD_ATTACHMENT, OPEN_DM, CREATE/JOIN_THREAD, INVOKE_INTERACTION, SET_PRESENCE, EMIT_TYPING, ACK_READ, JOIN_VOICE, SPEAK_AUDIO, and approved administrative operations. Read/search network effects have separate capability/event accounting even when not publicly visible in the room.

CommitCoordinator.submit(batch):

1. Normalize final payloads/chunks before hashing. Freeze mention rules, embed/unfurl options, filenames/bytes/metadata.
2. Validate destination, account permissions, owner/capability grant, stale references, attachment classes and rate/size limits.
3. Invoke sensor flags and fresh semantic/social review.
4. Return typed feedback to main. Hard failures cannot be waived; advisory concerns require recorded disposition, not compulsory refusal.
5. Require expression resolution for identified restricted flows. The model cannot create a grant; resolution refers to an existing policy/view.
6. On unchanged approved batch, mint CommitAuthorization over exact bytes, audience and policy versions, exposure digest, expiry and unique effect IDs.
7. Send only that authorization to the transport; revalidate versions/revocation before dispatch.

No public token streaming. No implicit final-response send. Rewrites create a new batch/review. Initial draft plus two resubmissions is the limit; policy-related owner interaction is a new intent, not an endless private review loop. No downgrade after checker failure.

ReviewContext contains draft, current audience, immutable purpose/grants, relevant approved facts, exposure labels/lineage, recent release ledger and minimal necessary quotes. It excludes arbitrary policy documents from tool results and raw full history. Any included quote remains explicitly external.

Mandatory checks: schema, authority/grants, capability, audience/destination binding, staleness, disclosure resolution for restricted flows, exact payload, retention/lineage requirements, review completion in prototype. Advisory: ordinary salience, tone, banter, social appropriateness, and uncertain probing signals. A same-model reviewer is never a deterministic proof of semantic non-disclosure.

## 8. IPC, transactions and restart behavior

Minimal storage layout (proposed names; map existing Hermes tables through adapters rather than duplicate identity):

| DB / table | Key and essential constraints |
|---|---|
| transport.events | journal_seq primary key; unique source event/revision; actor/audience/fragment refs; gap and retention metadata |
| transport.consumer_cursors | consumer_id primary key; monotonic durable_seq; acknowledgement time |
| transport.outbox | action_id unique; immutable payload_digest; preparation/authorization refs; state; expiry; attempt/receipt |
| transport.connection_state | account/connection key; best-effort session hints; last_ingested; completeness/gap status |
| core.ingested_events | unique transport event/revision and cursor domain; fragment refs |
| core.work_items | unique intent + event/trigger key; status; episode lease/version; retry budget |
| core.principals/relationships/episodes | stable IDs; verified binding uniqueness; relationship/episode revisions |
| core.memory_records/derivations | immutable record revision; scope/owner/subject; parent edges; supersession/index/retention state |
| core.purposes/grants/leases | authenticated issuer; version; expiry/revocation; destination/data/transform/capability bounds |
| core.intents/drafts/reviews | immutable draft revision/digest; origin roots; disposition; reviewed versions |
| core.release_ledger/access_audit | opaque subject/data/view refs; audience coalition; precision/transform; safe decisions/versioned bindings |

Store restricted text/value blobs separately from metadata where useful; avoid secrets in primary keys. Scope/index queries must filter before context construction. For multi-owner nodes, normalize owner/subject and policy relations rather than concatenate fragile labels.

Minimal sidecar API:

- read_events(after_seq, limit) → events, next_seq, gap indicators;
- acknowledge_ingestion(consumer_id, durable_seq) → durable receipt;
- audience_snapshot(destination) → verified current transport facts;
- prepare_action(batch_digest, normalized_payload_refs) → opaque preparation ID;
- commit(preparation_id, authorization) → queued receipt;
- action_status(action_id) → queued/sending/sent/failed/unknown and platform IDs;
- reconcile(action_id) → evidence or unresolved;
- health() → connection state, last event, gap and outbox counts.

Authenticated local socket and length-delimited JSON are sufficient. Pin schema versions and request size; no arbitrary eval, Python import, unrestricted HTTP proxy or memory query endpoint. Model does not receive the IPC credential. Production hard custody requires OS separation if shell tools can read core/sidecar files.

Core ingestion is a local DB transaction: insert immutable event refs + deduplicated work item + consumer cursor. Ack only after commit. It is not ack-after-model completion. Crashes may replay events but cannot produce a new work item for the same key.

Outbox uniqueness is by action_id and immutable payload digest; repeated IPC calls return existing status. Remote sends are not a local ACID transaction. On restart SENDING becomes UNKNOWN until reconciliation. Never blindly retry an unknown message, button press, or destructive operation. Signed authorizations expire and require revalidation; already SENT records remain receipts.

Connection hints are best-effort; authentication tokens live only in the protected transport credential store. Reconnect emits gap markers when completeness cannot be established.

## 9. Capabilities and user-account risk tiers

These are proposed autonomy defaults, conditional on platform permissions and account policy risk.

| Actions | Default decision |
|---|---|
| Read selected rooms/history, bounded search | Autonomous under scoped subscription/purpose; no whole-guild surveillance |
| Reply, initiate, react, own edit/delete | Autonomous through commit; rate limits, audience/disclosure review |
| Attachments, DMs, threads | Contextually gated for recipient/content/metadata; prototype delays attachments; no unsolicited mass DMs |
| Presence, typing, read state | Standing bounded grants; explicitly review secret-dependent changes; typing off initially |
| Voice | Later: contextual entry consent/norms and same commit for speech; recording/transcription retention separate |
| Existing buttons/slash/modals | Contextually gated by actual effect; discovery is not permission to purchase/delete/grant access |
| Pins, invites, events | Contextually gated if legitimately permitted; broad invites/privacy changes need explicit approval |
| Channel/role changes | Exact-plan approval unless narrow preauthorized housekeeping |
| Bans/kicks/timeouts, bulk/mass operations | Excluded from default social autonomy; later only explicit scoped approvals |
| Irreversible external/high-impact operations | Explicit owner authority/approval and robust capability boundary |

Normal user-account features differ from bot endpoints. Do not implement bot registration/management just because a donor exposes it. Permission and rate errors are ordinary outcomes; authentication challenges pause transport for manual handling.

## 10. Audit, proactivity and quiet outcomes

Privacy-safe access audit: request/view opaque IDs; authenticated purpose/grant; data class; subject pseudonym; transform/precision; owner/policy/audience versions; decision and safe reason codes; external-origin flag; timing/expiry. No raw values, scratch reasoning or exact low-entropy secret hashes. Use keyed digests for binding; sanitize exception messages and restrict access to audit metadata.

Debug full-context logging is disabled for sensitive turns. Logs, vector stores, temporary files, crash dumps and review caches need their own retention/custody. The event journal may contain received private speech and is therefore not a public audit log.

Scheduled work stores the original intent's roots, grants, destination bounds and expiry. TIMER_DUE triggers the same attention/participation/context/review pipeline. Execution reconstructs current audience and topic state; the agent may suppress a now-unwelcome follow-up. A cron job created from social content cannot become an owner-authorized job through scheduling.

NO_ACTION(reason_code, episode_ref) closes work privately; no transport row is created. After sensitive exposure, a DiscretionDecision includes the proposed SEND/QUIET/DEFER disposition in semantic review, even if its action batch is empty. Known oracle probes require secret-independent disposition; reviewing only text cannot protect conditional silence. DEFERRED persists a bounded intent. DELIVERY_UNKNOWN is operational uncertainty, not permission to resend. FAILED_INTERNAL can be retried within the same work identity; it is not automatically communicated publicly.

## 11. Answers to the 25 implementation questions

Hermes retrofit points actually observed in source: MemoryManager.build_system_prompt, prefetch_all, sync_all, provider tool handling and lifecycle hooks need the task context contract; MemoryStore.format_for_system_prompt needs a safe identity/salience replacement for social snapshots; build_session_key is a routing compatibility point, not the episode model; Discord _handle_message and base typing/delivery paths must not automatically commit social effects. These are change sites, not a claim that current signatures already accept the proposed records.

| # | Answer / contract location |
|---|---|
| 1 | Domain types in §1; coordinator owns a single AgentIdentity. |
| 2 | Revisioned platform/internal fact envelope, §2; arbitrary incoming text is data. |
| 3 | Separate provenance dimensions, source records and lineage, §§2–3. |
| 4 | Sidecar SQLite WAL transport journal; core durable ingestion refs, §8. |
| 5 | Connection/cache/account receipts in transport; identity/context/goals/memory in core. |
| 6 | Journals, cursors, scopes/grants, intents, work, outbox/receipts survive; KV/scratch do not. |
| 7 | Selected source-stamped episode, identity/audience/purpose, memory views and exposure manifest, §3. |
| 8 | Bounded event window or exact draft with safe descriptors, §4; no unrestricted vault. |
| 9 | Structured request with runtime-sealed causes/audience/grants, §5. |
| 10 | Core reference-monitor module; deterministic bounds plus bounded necessity advice. |
| 11 | Typed field views or allowlisted derivations; restrictions survive, §5. |
| 12 | Parent handles, transform/policy ID, inherited labels and invalidation graph, §6. |
| 13 | CommitCoordinator invokes checks/review on every prototype batch, §7. |
| 14 | Yes; new immutable revision and full review. |
| 15 | Initial draft plus two resubmissions; then quiet suppression/defer. |
| 16 | Mandatory capability/authority/binding/restricted-flow checks; ordinary tone/discretion advisory. |
| 17 | Typed NO_ACTION completes private work with no transport fallback. |
| 18 | Discriminated ActionProposal; reactions reviewed for semantics, §7. |
| 19 | Agent-created standing-interest intent enters same pipeline; no message required. |
| 20 | Cron preserves origin/grants and refreshes audience, §10. |
| 21 | Scoped observation/candidate promotion, indexing filters and no implicit widening, §6. |
| 22 | Opaque IDs, classes/transforms/reasons/versioned bindings; no raw secret or CoT, §10. |
| 23 | Immutable control plane, bounded schemas/templates, restriction-only remote metadata. |
| 24 | Paired secret worlds and observer traces including silence/reactions/timing; evaluation plan. |
| 25 | Platform-independent envelopes, audiences/intents/views/effects; adapters only supply semantics. |
