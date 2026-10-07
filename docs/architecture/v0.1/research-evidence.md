# Research evidence and source audit

Research cut-off: 7 October 2026. This is a source audit and design input, not a claim that any reviewed project is secure or that its dependencies were executed here.

**Evidence labels used throughout the package:** **Observed** means found in the cited implementation or paper; **Inference** means a conclusion drawn from that evidence; **Proposal** means our design. Repository inspections cover the listed files and symbols, not entire projects. Papers establish results under their own experimental assumptions. No local Clef latency, live Discord account behavior, or prototype security results were measured.

GitHub supplied repository source; Hugging Face supplied model metadata; Context7 supplied llama.cpp endpoint documentation; Consensus supplied discovery and bibliographic cross-checks for the compositional-privacy and contextual-injection papers. Architectural evidence comes from primary papers, author model cards, and source, rather than literature-search summaries.

## 1. Closest privacy, retrieval, and policy work

| ID / primary source | Observed contribution and evaluated setting | Useful piece; what is not established |
|---|---|---|
| R01 — [AirGapAgent, arXiv:2405.05175v2](https://arxiv.org/html/2405.05175v2) | A data-minimization agent prepares task-relevant user information before another agent interacts with a third party. Evaluates synthetic task conversations and context-hijacking attacks. Purpose originates in the owner's task; changed context can require owner involvement. | Closest precursor to purpose-conditioned retrieval. It does **not** establish safety when an attacker induces the main agent to invent a new sensitive purpose mid-conversation. Borrow trusted purpose grounding and minimization, rather than its permanent separation of conversational knowledge. |
| R02 — [Agent-Memory Protocol, Wu et al., PMLR 317, 2026](https://proceedings.mlr.press/v317/wu26a.html) | Describes deterministic redact-at-rest, pack-for-purpose, and hydrate-on-return operations at the user/external-model boundary, illustrated with medical and financial use cases. | Useful identifier indirection and purpose packing. Identifier removal is narrower than preventing inference about finances or health. The proceedings abstract and indexed PDF excerpt were inspected; direct PDF retrieval failed with an unsupported content-type. No implementation or adversarial benchmark was verified. Do not confuse this paper with the unrelated markdown-first “Agent Memory Protocol” standard. |
| R03 — [FIDES, arXiv:2505.23643v2](https://arxiv.org/pdf/2505.23643) | Tracks integrity and confidentiality labels, uses hidden variables and controlled extraction, and evaluates prompt injection in tool-agent tasks. Its security definitions distinguish integrity noninterference from explicit confidentiality. **Implicit confidentiality flows, including whether/order of actions, are permitted** in the discussed design. | Strong donor for labels, restriction-preserving transformations, and enforced tool boundaries. Results depend on correct labels and policies. Not proof against timing, silence, or behavioral privacy leakage; broad taint can make open social participation unusable. A related concrete runtime was inspected below. |
| R04 — [PrivacyLens, author project](https://salt-nlp.github.io/PrivacyLens/) | Builds contextual privacy evaluations from norms through vignettes to agent trajectories and external actions. | A benchmark and case-generation approach, not a retrieval monitor. Useful for ordinary disclosure norms and action-level utility. Static trajectories do not establish security against persistent adaptive social adversaries. |
| R05 — [CI-CoT / CI-RL, arXiv:2506.04245v1](https://arxiv.org/html/2506.04245v1) | Uses contextual-integrity reasoning and reinforcement learning to improve privacy judgments/generation. Its synthetic dataset has 729 samples; reward construction includes required/forbidden keyword checks and output format. Reports transfer to PrivacyLens. | Useful training pattern for sender, recipient, subject, attributes, and transmission norms. Keyword-oriented rewards cannot establish semantic noninterference. No guarantee for an untrained local model, social tone, adaptive probing, or behavioral channels. Avoid exposing private reasoning traces merely to reproduce CI-CoT. |
| R06 — [PrivacyChecker / Privacy in Action, arXiv:2509.17488v1](https://arxiv.org/html/2509.17488v1) | Extracts and judges information flows; integrates guidance or checking tools with agents, MCP, and A2A. Evaluates static and live workflows. Residual failures include incorrect judgments, missed flows, and a gap between judgment and final action. | Supports a fresh, bounded flow review with feedback. The authors explicitly leave memory poisoning and adversarial ambiguity beyond the principal scope. Additional tools increase ambiguity; model checking is mitigation, not a proof or an injection-proof policy engine. |
| R07 — [ContextGuard-RAG, Symmetry 18(9), 1572](https://www.mdpi.com/2073-8994/18/9/1572) | Retrieved primary excerpts describe contextual-integrity filtering before generation, using public QA/domain datasets with privacy annotations and a private corporate pilot for adversarial robustness. | Related to context selection. **Partial inspection:** direct full-page retrieval repeatedly returned 429; implementation and complete experimental methods were not verified. Treat as preliminary related evidence, not a dependency or validated production donor. |
| R08 — [AIM, arXiv:2609.12320v1](https://arxiv.org/html/2609.12320v1) | Describes private/public multiuser memory scopes, LLM classification, deterministic visibility filtering, and create/read/update/delete/no-op operations. Evaluates a chronological multiuser benchmark. Limitations include editable public memories and relevance/classification errors; downstream response privacy is not established. | Useful scoped memory lifecycle. Scope selection alone is not purpose limitation, permission to express, or semantic secrecy. Public memories can be poisoned. Paper inspected; its implementation was not audited. |
| R09 — [Conseca, arXiv:2501.17070v3](https://arxiv.org/html/2501.17070v3) | Generates contextual policies from trusted task/context, isolates that generation from attacker content, and deterministically checks proposed actions. Linux computer-use proof of concept uses regex argument policies and 20 constructed tasks. | Particularly close to preserving planner autonomy while bounding execution. Enforcement is only as good as policies, trusted-input classification, and complete mediation. A directory name or tool description is not inherently trusted in our environment. Not evidence that an LLM may safely rewrite its own policy from Discord input. |
| R10 — [Progent, arXiv:2504.11703v2](https://arxiv.org/html/2504.11703v2) | Uses a declarative tool-privilege policy language and argument constraints, with dynamic policy mechanisms and benchmark evaluations of agent attacks and utility. | Donor for typed action constraints and deterministic enforcement. Automatic policy generation remains fallible; low benchmark attack success does not prove arbitrary confidentiality. Paper inspected, implementation not audited. |
| R18 — [CaMeL, arXiv:2503.18813v2](https://arxiv.org/html/2503.18813v2) | Separates privileged planning from quarantined data extraction, tracks capabilities/values in a custom interpreter, and enforces tool policies. Evaluates AgentDojo. STRICT mode tracks additional implicit dependencies; the paper discusses conditional requests, exceptions and timing channels. | Strong alternative for bounded consequential workflows. Guarantees depend on interpreter/policies and the defined property, not unrestricted social cognition. Planning before seeing data can impair adaptive participation. Its research repository was identified, not source-audited. |
| R20 — [Nissenbaum, Privacy as Contextual Integrity, 2004](https://digitalcommons.law.uw.edu/wlr/vol79/iss1/10/) | Defines privacy in terms of appropriate collection and distribution within contextual norms, including public surveillance. | Explains why public availability does not imply appropriate permanent retention or reuse. A normative framework, not an executable access engine or security proof. |

**Interpretation, not an existing turnkey system:** purpose limitation supplies “why”; contextual integrity supplies the social flow parameters; a reference monitor supplies complete mediation; selective declassification supplies explicit authorized transformations; progressive disclosure supplies precision control. None alone supplies social discretion. “Purpose-aware RAG” often means ranking/filtering task-relevant chunks; it only becomes an access-control mechanism when a trusted component enforces owner, purpose, recipient, transformation, and use restrictions. A rationale written by the agent is evidence to evaluate, never authority to grant access.

## 2. Attacks and provenance/model-level work

| ID / primary source | Observed finding and threat model | Architectural consequence; limits |
|---|---|---|
| R11 — [Inadvertent Context Leakage, arXiv:2608.19857](https://arxiv.org/pdf/2608.19857) | Demonstrates that sensitive context can change benign output distributions without literal disclosure. Black-box estimation and adaptive decoding target predicates/digits, including identifier recovery; evaluates eight proprietary APIs. | Supports paired-world, distributional and behavioral tests. Depends on target/context knowledge and estimation queries; not evidence of universal arbitrary-secret recovery or a measured attack on our local model. A fresh reviewer cannot certify distributional independence. |
| R12 — [The Sum Leaks More Than Its Parts, arXiv:2509.14284v1](https://arxiv.org/html/2509.14284v1) | Multiagent disclosures of separate structured mappings can combine into private facts. Controlled experiments give adversaries a correct compositional plan and examine coordination/utility tradeoffs. | Supports cross-channel/coalition disclosure accounting. Does not directly evaluate a single continuous Discord agent, arbitrary unstructured memory, or prove a budget mechanism sufficient. |
| R13 — [AI Agents May Always Fall for Prompt Injections, arXiv:2605.17634v1](https://arxiv.org/html/2605.17634v1) | Studies contextual-integrity parameter spoofing, norm manipulation, and simultaneous flows. Includes adaptive email attacks with forged owner instructions. | Source attribution and fixed rules do not resolve unverifiable social claims. Bind owner grants to authenticated control-plane records. This is an argument plus experiments about a difficult tradeoff, not a theorem that hard capability boundaries are impossible. |
| R14 — [MINJA, arXiv:2503.03704v1](https://arxiv.org/html/2503.03704v1) | Query-only attacks induce malicious entries into an agent memory bank through bridging reasoning and progressive shortening, affecting later tasks. | Memory promotion is a security boundary even without direct attacker write access. Tested memory/task settings do not cover every autobiographical store; source lineage and promotion checks need their own evaluation. |
| R15 — [Spotlighting, arXiv:2403.14720v1](https://arxiv.org/html/2403.14720v1) | Delimiting, datamarking, or encoding untrusted text maintains source cues. Tests document injection with keyword-targeted payloads in older GPT systems; delimiter spoofing and model-dependent utility matter. | Supports persistent source marking. Encoding all social speech would harm comprehension and tone. Prompt formatting is a probabilistic cue, not a reference monitor, identity authentication, or semantic privacy guarantee. |
| R16 — [Instruction Hierarchy, arXiv:2404.13208v1](https://arxiv.org/html/2404.13208v1) | Trains models to prioritize privileged instructions over lower-priority text. Evaluates instruction conflicts and injection scenarios. | Authority must be represented in training as well as runtime roles. Does not establish that arbitrary XML attributes, custom source tokens, or a stock local model honor our hierarchy. Source identity, accuracy, relationship, and authority remain separate dimensions. |
| R17 — [StruQ, USENIX Security 2025 paper](https://www.usenix.org/system/files/usenixsecurity25-chen-sizhe.pdf) | Combines reserved delimiters, frontend filtering, and structured instruction tuning. Evaluates manual and optimization-based injection against Llama/Mistral; strong optimization attacks retain substantial success. | Concrete near-model-level source/instruction separation. Not immunity and not confidentiality enforcement. Adding delimiters without the trained frontend/model pair is not equivalent. Its author [code repository](https://github.com/Sizhe-Chen/StruQ) was identified, not source-audited here. |
| R19 — [Can CaMeLs Talk?, arXiv:2610.05640v1, 5 October 2026](https://arxiv.org/html/2610.05640v1) | Constructs boundary laundering where tool-derived data becomes a downstream agent's trusted input; proposes separate instruction/data channels preserving provenance and evaluates hierarchical agent-as-tool systems. | Fresh relevant preprint supporting provenance across checker/agent boundaries. Tree topology and control-flow integrity are explicit limits. Utility failures include precommitted control flow and substituted task scope. It does not prove full privacy/noninterference for cyclic, stateful social environments. Paper inspected, implementation not audited. |

Dedicated role/source embeddings, signed provenance fingerprints, and attention biases are **research proposals in this package**, not verified features of the current local runtime. Signatures authenticate an envelope's origin, not the truth of its content. Model representations cannot replace hard credential custody or action authorization.

## 3. System-One feasibility

| ID / primary source | Observation | Consequence |
|---|---|---|
| M01 — [Typesafe: Jev / System One](https://typesafe.ai/blog/introducing-system-one-models-and-jev) | Describes bounded decision inference rather than prose generation, with probability-like outputs and hosted speed claims. | Useful category and schema concept. No local Jev weights or local latency were verified in this audit. Well-typed decisions can still be wrong or adversarially manipulated. |
| M02 — [Cloudflare Clef-Flash author card](https://huggingface.co/Cloudflare/clef-flash/raw/main/README.md) | Qwen3.5-9B backbone plus a schema-conditioned decision head; boolean, choice, and score questions can be evaluated jointly. Author reports median 38.8ms / p95 122.4ms in its serving benchmark, not on our workstation. Some task results vary sharply, including weaker RAGTruth performance than full Clef. | Suitable candidate for an always-running perception experiment, not a certified leakage detector. Use the actual decision interface; generic Hugging Face chat-pipeline examples do not describe this inference path. |
| M03 — [bartowski GGUF card](https://huggingface.co/bartowski/Cloudflare_clef-flash-GGUF) | Q4_K_M file is 6.04 GB, Q8_0 9.68 GB; decision-head tensors remain Q8_0. Card identifies llama.cpp release b11430 for its quantization and provides an image projector. | File bytes are a weight-memory floor, not total VRAM. Runtime buffers, context, and optional vision add overhead. Text-only is a prototype choice, not a limitation of the current release. Pin files and server revision; do not assume an older llama.cpp already supports the endpoint. |
| M04 — [llama.cpp server-decision.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-decision.cpp) and [header](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/server-decision.h) | Source supports NOUL/CHOICE/SCORE, joint and per-question model variants, a systemone template, joint schema construction, head-token limits, and escaping occurrences of Clef's special marker in supplied data. Context7 documents the /v1/systemone endpoint. | Joint Clef passes do not generate an explanatory response. More questions/options consume head/context work; no universal “all questions are free” or verified social-task maximum. Runtime semantics differ across model variants. Benchmark 4/8/16/32 questions. |

llama.cpp files were fetched from master; recorded blob SHAs: server-decision.cpp **9ed68009f6f42e2b228d470154b6cb9fa9271b89**, server-decision.h **f2921194195236038cbdfd93665280be89ce24b9**. These are content identifiers, not an installable whole-repository commit. Pin a tested repository commit during prototype bring-up.

## 4. Repository implementation audit

Use the pinned commit links below when implementing. “Reuse” means a conceptual or narrowly audited code donor, subject to license/dependency checks. None of these projects was installed or run.

### D01 — NousResearch/hermes-agent

Commit: [7dab93b06e2bb3757dc18229169efcee1b5b47a3](https://github.com/NousResearch/hermes-agent/tree/7dab93b06e2bb3757dc18229169efcee1b5b47a3).

Inspected plugins/platforms/discord/adapter.py, gateway/session.py, gateway/platforms/base.py, tools/memory_tool_store.py and agent/memory_manager.py. Key symbols: Discord adapter _handle_message, _fetch_channel_context; build_session_key; MemoryStore.format_for_system_prompt; MemoryManager.build_system_prompt/prefetch_all/sync_all.

**Observed:** current Discord source lives in a platform plugin. Group sessions default per user, but thread sessions default shared and group behavior is configurable. Mention requirements, free-response channels, history backfill, attachment/reply handling, batching, and automatic thread creation already exist. DM behavior assumes incoming messages trigger the bot. MemoryStore maintains a frozen prompt snapshot of MEMORY.md/USER.md; text sanitization does not provide rich provenance. Base gateway supports automatic typing/delivery flows.

**Inference:** the original blanket “Hermes isolates every Discord user” claim is outdated. A shared session switch helps but does not create episode/audience-aware social cognition.

**Reuse unchanged initially:** the normal bot adapter for existing bot uses; core model/provider machinery outside the new social execution path. Pure formatting/chunking utilities can be reused only after checking final rendered payloads. Reuse persistence/backfill ideas and tests. **Replace or intercept:** default admission, auto-thread creation before decisions, automatic typing/progress/final-text delivery, unrestricted send tools, raw persistent-memory prompt injection, and transcript-search bypasses. Do not fork the whole adapter as the architecture.

### D02 — dolfies/discord.py-self, renamed branch

Pinned branch head: [7e2305c4e0926f334e24c439d9e80b80c33cbab4](https://github.com/dolfies/discord.py-self/tree/7e2305c4e0926f334e24c439d9e80b80c33cbab4). Inspected selfcord/__init__.py and pyproject.toml there, plus .github/workflows/rename.yml on master.

**Observed:** renamed code imports as selfcord; Git distribution metadata calls it selfcord.py. The workflow checks out **dolfies master**, renames the package and imports, and pushes renamed. This supports coexistence with the normal discord.py namespace.

**Qualification:** synchronization means the fork's own master, not proven identity with the latest official discord.py. The workflow is evidence of automated synchronization, not a guarantee that every sync succeeded.

Install from that **pinned Git commit**, for example the dependency form “selfcord.py @ git+https://github.com/dolfies/discord.py-self.git@7e2305c4e0926f334e24c439d9e80b80c33cbab4”. The [PyPI project selfcord.py](https://pypi.org/project/selfcord.py/) is unrelated. Namespace coexistence was source-verified, not empirically installation-tested.

### D03 — OpenClaw

Commit: [d9a083657dba5c5e1f1324b6aca06852e1171be2](https://github.com/openclaw/openclaw/tree/d9a083657dba5c5e1f1324b6aca06852e1171be2).

Inspected extensions/discord/src/monitor/inbound-context.ts, message-handler.preflight.ts, message-handler.history.ts.

**Observed:** preflight separates sender identity, group/DM routing, thread/session bindings, mention admission, and command authorization. Channel metadata gets a structured source wrapper. History entries freeze admission-time sender facts including role IDs, and supplementary history can be filtered by sender policy.

**Reuse:** platform routing, thread bindings, sender snapshots, echo prevention, and contextual history as patterns. **Do not copy:** mention gating as social intent, bot/webhook assumptions, or configuration system prompts derived from arbitrary room content. Its ACL/history mechanisms are useful but do not implement the discretion system proposed here.

### D04 — Terminally-Online/discord-agent

Commit: [b4fbede75d7b89c8529a4a7ca9be99a9842e180a](https://github.com/Terminally-Online/discord-agent/tree/b4fbede75d7b89c8529a4a7ca9be99a9842e180a).

Inspected src/daemon/router.ts, classifier.ts, brain.ts, context.ts.

**Observed:** ambient classifier requests seven structured predicates through Ollama; weighted rules, confidence, cooldowns, and continuity affect participation. Direct mention/reply routing is separate. Its policy discourages banter-only interjections and “being clever”; router initiates typing during work.

**Reuse:** separating cheap ambient perception, context, and main inference; audit predicates, catch-up, cooldown concepts. **Change:** banter suppression and utility weights; direct triggers must remain optional; all side effects must cross commit. This is an ambient participation donor, not a validated adversarial privacy design.

### D05 — ElizaOS

Commit: [b026785e91729bd8a480e79663e77d9c5b68b125](https://github.com/elizaos/eliza/tree/b026785e91729bd8a480e79663e77d9c5b68b125).

Inspected packages/core/src/services/message.ts, especially DefaultMessageService.shouldRespond. The separately named elizaos-plugins/plugin-discord repository was not available; this audit covers the current **core response path**, not every Discord integration.

**Observed:** private rooms and mentions/replies can skip response evaluation and be treated as respond-true; other messages proceed to evaluation. Room/entity/source mechanics distinguish channels and scheduled triggers.

**Reuse:** room/entity and response-evaluation abstraction. **Replace:** automatic DM/mention obligation. A shouldRespond hook alone does not separate retrieval, willingness to disclose, and committing observable effects.

### D06 — cappyeo/discord-mcp

Commit: [7ed828f9979f261fbb3ce94c440a8f46c28b5716](https://github.com/cappyeo/discord-mcp/tree/7ed828f9979f261fbb3ce94c440a8f46c28b5716).

Inspected packages/mcp-core/src/pipeline/executor.ts and tools/guild/change_plan.ts, _lib/blueprint.plan-token.ts.

**Observed:** sequential pipelines support conditional/skipped steps and abort/continue-on-error results. Blueprint tokens canonicalize, hash, compress, and HMAC-authenticate a plan to a profile; decoding bounds input/decompression and validates schema/checksum.

**Reuse:** typed action breadth and authenticated exact-plan binding. **Do not infer:** ACID transactions or automatic rollback of Discord operations, or token expiry from the inspected token codec. Our expiry/audience/version binding is an additional proposal. Bot guild-management tools require separate user-account permissions and risk tiers.

### D07 — Microck/discord.py-self-mcp

Commit: [625c40df34c42384bdff9b7355fcfef585288768](https://github.com/Microck/discord.py-self-mcp/tree/625c40df34c42384bdff9b7355fcfef585288768).

Inspected scripts/daemon.py, discord_py_self_mcp/bot.py and tools/interactions.py.

**Observed:** persistent connection daemon, private Unix socket, separate random local auth token and permission helpers; imports the discord namespace. Interaction tools include application commands, buttons, and modal lifecycle handling. Bot setup also includes compatibility monkeypatches and CAPTCHA-related integration.

**Reuse:** connection ownership, authenticated IPC, interaction discovery and modal correlation as implementation donors. Port typed operations to the pinned selfcord package. **Exclude:** automated challenge solving, broad powers by default, and unreviewed direct sends. A private socket under the same OS identity as unrestricted shell access is not a hard secret boundary.

### D08 — tensakulabs/discord-mcp

Commit: [1f0fdd4f86ba12430a8b337f26baf7a5e829c050](https://github.com/tensakulabs/discord-mcp/tree/1f0fdd4f86ba12430a8b337f26baf7a5e829c050).

Inspected src/daemon.ts and db.ts.

**Observed:** persistent gateway connection with heartbeat/session sequence/resume; SQLite WAL message inserts, deduplication, FTS5, seen state and pruning.

**Reuse:** persistent transport/cache ownership. This is **not** the complete provenance event journal or per-consumer durable cursor scheme we specify. Message-create caching alone cannot account for every edit, deletion, membership change, or disconnected gap.

### D09 — caesarnine/discord-agent-gateway

Commit: [88f93215a2def920ad148e8390b0189a29f52e5d](https://github.com/caesarnine/discord-agent-gateway/tree/88f93215a2def920ad148e8390b0189a29f52e5d).

Inspected discord_agent_gateway/db.py and api/agent_routes.py.

**Observed:** SQLite WAL posts with sequence numbers, per-agent receipts, ingestion state, inbox cursor/next_cursor and explicit acknowledgement routes. Outbound posts use webhooks/chunking.

**Reuse:** journal/cursor/receipt pattern. **Do not copy:** webhook/multiagent identity as normal-user transport, or infer exactly-once Discord delivery. Our durable-ingestion acknowledgement and outbox reconciliation add required semantics.

### D10 — Vencord/client bridge

Concrete donor: [fagnersales/discord-mcp-bridge, 172d18215e7beef068187e2ca38008d89060ac6d](https://github.com/fagnersales/discord-mcp-bridge/tree/172d18215e7beef068187e2ca38008d89060ac6d).

Inspected daemon.ts and discordMcp/index.tsx.

**Observed:** localhost token-gated HTTP polling connects a daemon to the renderer; the plugin can evaluate arbitrary supplied JavaScript via new Function and access Discord internals, token stores, messages, and read-state APIs.

**Inference:** unique access to current client/UI state may help features absent from the library, but desktop renderer lifecycle and unstable internals couple failures. Arbitrary evaluation can bypass every typed commit rule. Do not use as default transport; a future bridge must expose a narrow audited operation set and treat the renderer as privileged.

### D11 — Concrete FIDES-related runtime

Commit: [microsoft/agent-framework, 40763bed1a5e94a1698ba9ebf009e5622eb099aa](https://github.com/microsoft/agent-framework/tree/40763bed1a5e94a1698ba9ebf009e5622eb099aa).

Inspected python/packages/core/agent_framework/security.py.

**Observed:** ContentLabel separates integrity and confidentiality; restrictive joins, hidden VariableStore, controlled inspection/extraction and policy middleware exist. Remote MCP annotations can tighten restrictions, not confer trust; locally pinned tool rules control overrides.

**Reuse:** concrete label and mediation concepts. Avoid permissive default labels for unknown data. Model-level influence is not reconstructed perfectly from runtime lineage, and broad context taint still needs explicit narrow permissions to talk about untrusted social input.

## 5. Account semantics and unresolved verification

[Discord's official self-bot policy](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots) forbids user-account automation and warns of termination. This package acknowledges that account risk; it does not propose evasion or automatic CAPTCHA solving. No Discord action was performed.

Pending empirical checks: install/import coexistence at the selected pin; platform feature support and permission failures for user accounts; Clef endpoint/schema compatibility at a tested llama.cpp commit; local latency and resident memory; main/reviewer correlated errors; poisoning resistance; semantic privacy leakage; and social usefulness under realistic mixed-party conversations. All numerical acceptance thresholds in the evaluation plan are **proposed engineering gates**, not research findings.
