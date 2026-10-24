# RFC: MCP resource support in the sandboxed agent loop

**Status:** Proposed; documentation/design only  
**Decision requested:** Approve the resource-call, authorization, and compatibility contract before implementation.

## 1. Summary

The worker already discovers MCP `list_resources` and `list_resource_templates` metadata from project-local FastMCP modules. For enabled modules it projects descriptors into function-shaped model tools; a call interpolates template variables, invokes `server.read_resource(uri)`, and serializes returned items. This is discovery/dispatch scaffolding, not a complete resource feature: policy is module-wide, URI validation is implicit, and there are no resource-specific size, MIME, cache, provenance, privacy, or history guarantees.

Recommend explicit, read-only, model-callable resource operations using the existing function-tool adapter, initially scoped to operator-enabled modules. Keep assistant tool-call/tool-result history so the existing loop, model callbacks, and durable transcript can replay it. Add per-resource authorization and bounded normalization before persistence/context. Do not preload resource contents or send model-native resource declarations: current model adapters support function tools, and model-selected reads provide better least privilege and provenance.

## 2. Scope and goals

Enable discovery and bounded reading of static resources and templates, with metadata, policy, replayable results, remote implications, and tests specified in advance. This RFC does not implement behavior. It does not propose arbitrary URL fetching, writes, a general MCP transport, or changing `README.md`. Project MCP modules are imported from the agent snapshot and their server functions execute in the sandbox worker, not through a new controller MCP proxy.

## 3. Current behavior (grounded in source)

`AgentWorker.initialize()` runs `_discover_mcp_tools()` before handshake/history retrieval. It imports top-level `mcp_*.py`, reads tool/resource catalogs, reports metadata through `agent.tools.report`, and exposes callable entries only for modules in the controller allowlist. `AgentController` seeds module allowlists from defaults/environment and offers trusted live `set_agent_tools`; remote `set_tools` is similarly module-based.

Resource discovery looks for `list_resources`, `read_resource`, and optional `list_resource_templates`. It records name, kind, URI/template and description. Static descriptors become function tools without arguments; template variables matching `{identifier}` become required string arguments. `_call_discovered_tool` percent-encodes values with `quote(..., safe="")`, substitutes them, and reads the URI. It serializes model-dumpable results or extracts `mime_type`, `text`, and `blob` fields. Names are sanitized/truncated, which can collide and is not a suitable future identity rule.

The worker’s normal `_handle_tool_calls()` catches exceptions into a generic error object, formats output, truncates at `MAX_TOOL_RESULT_CHARS` (24,000 characters), and persists a `role=tool` message with `tool_call_id`. Assistant calls are persisted before results. `_api_messages()` takes at most 120 messages / 1,000,000 bytes; controller/history and model-request limits apply separately. `AgentStore` persists opaque message dictionaries in `history.json`. Generic post-serialization character truncation does not cap raw binary allocation or guarantee valid bounded JSON.

`agent_ipc.py` is bounded JSON-RPC over a private Unix socket. The controller owns persistence and catalog reporting. Remote mode has a versioned allowlisted SSH JSON protocol, bounded request/response sizes and module-level tool controls; it has no resource-specific operation. Model adapters in `llm.py` translate function-call format to Responses API functions and translate tool history; there is no native MCP-resource adapter.

Tests inspected include `test_agent_worker.py`, controller/store tests, remote protocol/adapter/service tests. They exercise ordinary tools, context/history and protocol allowlisting, not resource authorization, URI validation, payload handling, caching or replay. `AGENTS.md`, `COMMIT_RULES.md`, and `docs/remote-agent-review.md` were consulted. No documentation build configuration was found.

## 4. Terminology and result contract

- **Resource URI:** MCP identifier naming a readable resource; it is not automatically a filesystem path or URL for the worker to fetch.
- **Static resource:** listed concrete URI requiring no model-supplied variables.
- **Resource template:** URI template with named variables; each expansion is a separate read and is not guaranteed to exist or be authorized merely because the template was listed.
- **Discovery/listing:** retrieve descriptors and descriptions, not contents.
- **Read:** call the owning MCP server’s `read_resource` for an authorized static URI or validated expansion.
- **MIME type:** content-type metadata. Unknown/missing MIME is not presumed text.
- **Metadata:** name, description, URI/template, MIME, content kind/size/encoding where available, status and truncation information. Metadata is untrusted, not authority.

MCP may return multiple content items. Proposed normalized output preserves order and represents each item with nullable `mime_type`, `kind` (`text`, `blob`, `unknown`), bounded content representation, and explicit truncation/omission status. Avoid silently flattening heterogeneous results.

## 5. Discovery and API options

Proposed interaction: (1) worker enumerates descriptors and reports a bounded catalog at startup/resume, without reading bodies; (2) controller authorization determines which descriptors can be exposed; (3) model selects a static resource or template tool; (4) worker validates arguments/URI/policy, reads only from the owning server, normalizes bounded content, and returns a tool result. Listing alone never grants read authorization. Static tools accept no URI override; template tools accept exactly declared variables. Detect name collisions rather than sanitizing them into the same exposed identifier.

Conceptual example, not a wire-schema promise:

```json
{"name":"mcp_docs__resource__guide","arguments":{}}
{"role":"tool","tool_call_id":"call-7","content":"{\"uri\":\"docs://project/guide\",\"items\":[{\"mime_type\":\"text/markdown\",\"kind\":\"text\",\"text\":\"...\"}]}"}
```

| Design | Benefits | Costs |
|---|---|---|
| Dedicated function tool per resource/template (**recommended first**) | Fits `_handle_tool_calls`, existing model adapters, and transcript; constrained static/template arguments; model chooses reads | Catalog/tool count and schema churn; model API limits and naming collisions |
| Model-native resource declarations | Could match provider-native MCP support | Provider-specific; current `llm.py` handles function tools; policy, result, and history semantics diverge; risk provider gets URI without worker validation |
| Preloaded context | No tool round trip | Reads unused/private data, consumes context, increases injection surface, obscures provenance and refresh |

Start with bounded opt-in per-resource function descriptors. If catalog scale is a demonstrated problem, consider a paired explicit `list_resources`/`read_resource` interface with policy-bound IDs; a generic arbitrary-URI argument is compact but too permissive and complicates template validation.

## 6. Authorization, URI validation and untrusted content

Current module opt-in is a coarse gate, not resource policy. Proposed authorization binds `(agent, snapshot, module, descriptor, URI)` and fails closed at read time, including after live policy changes. Operator module controls may be augmented with URI selectors/schemes; model arguments, URI descriptions and schemes never imply permission.

Validate bounded descriptor and expanded URI strings: reject controls/NUL, malformed/ambiguous braces, duplicate variables, missing or extra arguments, invalid/overlong URI and unsupported schemes under policy. Encode each variable exactly once as a URI component, never replace arbitrary model-provided URIs, and do not fetch `file:`/`http:` directly in the worker. Server-specific URI semantics and FastMCP guarantees need verification before final policy. Reads must remain within sandbox/network and snapshot boundaries.

Resource bodies and metadata are untrusted data. Preserve tool-result boundaries and provenance; explicitly instruct the model that resource text cannot override system/developer policy. Never execute embedded instructions/code or follow links by implicit fetch. Any secondary read must be a separate authorized operation.

Resources may contain secrets or personal data. Tool output is persisted in controller history and selected content is sent to the configured model; remote history also exposes transcript data to authorized callers. Require explicit operator opt-in and disclose persistence/model transmission. Avoid content in logs/metrics; consider URI redaction, retention/deletion, redaction and encryption-at-rest as deployment decisions. Do not imply these controls already exist.

## 7. Content limits, lifecycle, cache and failures

Set hard limits for descriptors, URI/description lengths, raw bytes per item/read, aggregate bytes per call, blocks, and serialized result. Enforce while consuming data where possible, before allocating unbounded buffers. Text must be valid UTF-8 or use explicit replacement; binary should be bounded base64 only for small payloads, otherwise metadata plus explicit omission. Report truncation accurately; do not infer full size if not measured. Character truncation after JSON serialization is insufficient.

Recommended v1 cache policy: no durable or cross-turn cache; duplicate reads are fresh. Optional in-flight coalescing is a separate decision and must preserve result/error semantics and cancellation. If later caching is justified, key by agent, server/module identity, canonical URI and authorization generation; never share across agents. Specify TTL, size cap, refresh/bypass, invalidation on resume and policy changes, and stale/revoked-data behavior. Caching improves latency but risks stale content and bypassing revocation.

Return bounded stable error classes: `not_found`, `unauthorized`, `invalid_uri`, `invalid_arguments`, `unsupported_content`, `too_large`, `timeout`, `server_error`, `cancelled`. Missing reads are recoverable tool results. Avoid existence detail where it leaks protected resource names. Bound timeouts/cancellation. Catalog/import failures should be visible and not falsely imply completeness; isolate module failures if safe.

## 8. History, compatibility and remote protocol

Use ordinary assistant `tool_calls` and `role=tool` content. Persist the bounded normalized result, including resource identity, MIME/content metadata, status and truncation marker. Replay must use the captured result, not re-read a changing resource. This supports exact reproducibility but stores sensitive contents; reference-only/redacted history would not exactly replay and requires product decision. Keep result within the existing function-tool transcript shape for `llm.py`’s Responses conversion. Old versions treat tool content as opaque text; no history schema migration is expected if metadata remains in bounded content.

Remote mode should initially use the same worker-side discovery/policy, not add arbitrary resource reads to `remote_agent_protocol.py`. Any remote policy configuration must be explicit, versioned and bounded; do not tunnel MCP names/URIs through a generic request. Respect remote envelope limits and ensure remote access is no broader than local. Existing tools and history remain unchanged; no resources become default-on. Modules without resources continue normally. Decide whether existing module `set_tools` enables resources or whether resources need a distinct default-off control; recommendation is distinct control due to data exposure, despite API complexity.

## 9. Observability and configuration

No model-controlled enablement. Provide operator policy by agent/module and, if feasible, resource selector. Record bounded metadata events: agent, module, redacted or hashed URI, allow/deny, error class, bytes, truncation and duration (cache status only if implemented); never content or credential-bearing raw URI. Avoid high-cardinality metric labels. Catalog inspection may expose descriptors/status, not contents. Decide whether `resources_enabled` is separate from `tools_enabled`; recommendation: separate default-off resource control, but resolve `set_tools` naming/compatibility before implementation.

## 10. Phases and acceptance matrix

**Phase 0 — contract approval:** decide enablement, URI policy, limits, content normalization, privacy/retention, remote config. No code.  
**Phase 1 — local text:** bounded discovery, static/template validation, operator authorization, UTF-8/MIME handling, stable errors and history replay.  
**Phase 2 — binary/operations:** bounded binary representation, concurrency/timeouts/cancellation, metrics/privacy, remote policy if required.  
**Phase 3 — scale/optional caching:** measure schema/catalog pressure; add paging or cache only with invalidation contract and tests.

| Area | Acceptance tests required before enabling |
|---|---|
| Authorization | Disabled module/resource, forged name, stale descriptor, revoked allowlist denied at read time; allowed resource succeeds |
| URI/templates | Static URI cannot be overridden; reserved template chars encoded correctly; missing/extra vars, malformed/duplicate variables, controls/NUL, unsupported/oversized URI rejected; valid template and nonexistent expansion |
| Limits/payload | Malformed result, invalid UTF-8, binary, huge block/multiple blocks, catalog overflow and oversized result bounded during read; truthful truncation/omission |
| Errors | Missing, denied, timeout, server error and cancellation are recoverable bounded results without traceback/secret leakage |
| History/replay | Assistant call/result persist in order; replay unchanged after source changes/disappears; context limits avoid orphan tool results; remote history round-trip opaque output |
| Duplicate/concurrency/cache | Serial duplicate reads fresh without cache; concurrent behavior deterministic; if coalescing/cache exists, test invalidation for refresh, URI/content generation, permission changes and resume |
| Remote | Unknown operations/fields rejected; byte bounds; remote resource policy cannot exceed operator allowlist; remote history compatibility |
| Regression | Existing tools, model function calls, Responses conversion, history, local/remote allowlists, sandbox restrictions and doctests unchanged |

## 11. Unresolved decisions

1. Independent resource enablement versus module `set_tools` (safer separation versus simpler API).
2. URI scheme/canonicalization policy across custom MCP servers (compatibility versus fail-closed control).
3. Exact byte/count limits against IPC, model, history and remote envelopes.
4. Full output persistence for replay versus privacy-oriented redaction/reference history.
5. Mapping MIME, invalid encodings, images and multi-item results to text-capable models.
6. Whether to coalesce concurrent reads; MCP resources are conceptually read-only but implementations may be dynamic.
7. Whether denied and nonexistent resources share errors (diagnostic detail versus existence privacy).
8. Refresh on resume/start versus every turn (cost and schema churn).
9. Remote config versioning and opt-in behavior.
10. Actual retention/deletion/redaction guarantees for local and remote history.

## 12. Source references inspected

`agent_worker.py` (`_discover_mcp_tools`, `_call_discovered_tool`, `_api_messages`, `_persist`, `_execute_tool`, `_handle_tool_calls`, `initialize`, `run`); `agent_controller.py` (catalog, allowlists, history/model bounds); `agent_ipc.py`; `agent_store.py`; `llm.py`; `remote_agent_protocol.py`, `remote_agent_service.py`, `remote_agent_adapter.py`; `test_agent_worker.py`, `test_agent_controller.py`, `test_agent_store.py`, `test_remote_agent_protocol.py`, `test_remote_agent_adapter.py`, `test_remote_agent_service.py`; `AGENTS.md`, `COMMIT_RULES.md`, `docs/remote-agent-review.md`. README was not changed. This proposal does not establish that resource runtime behavior or these proposed tests exist.
