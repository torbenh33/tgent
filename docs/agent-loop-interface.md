# Provider-independent agent loop and model provider interface

**Status:** review specification; proposed contract, not an implementation commitment. Complements [Agent Management and Project Workflow](agent-management-architecture.md), especially Interface 2. Current behavior below is an inventory, not a normative requirement.

## 1. Scope and goals

Define stable orchestration semantics so a provider supplies one model turn without taking ownership of tgent's tool execution, transcript, or lifecycle. Keep vendor SDK objects behind adapters and preserve the same semantics for controller-local and remote agents. This does not select a provider, transport, persistence backend, or authorize a tool.

## 2. Current behavior (inspected snapshot)

- `AgentController` starts a sandboxed `agent_worker` process. It accepts initial history/task, bounds initial history by message count and serialized bytes, and configures `max_model_calls` and `max_runtime_seconds`. It proxies model calls and manages worker stop/process lifecycle.
- `agent_worker.py` implements an iterative loop with ordered message dictionaries, assistant completions, tool-call dispatch and appended tool results. It declares context, tool-result, per-turn tool-call, command, and child-task bounds. Controller allowlists/sandbox—not model output—are the security boundary.
- `llm.py:LLMClient.model_callback` consumes OpenAI-style request dictionaries and returns a chat-completion-shaped dictionary. Chat Completions returns a serialized SDK response; the Responses API adapter translates ordered chat history, tool calls/results, outputs, finish reason, and usage. Calls may raise exceptions.
- `mcp_agent.py` and `remote_agent_service.py` inject model access through the callback into `AgentController`; worker/provider credentials are not the worker's direct interface. The remote service exposes lifecycle operations but there is not yet a documented canonical provider-neutral wire schema.
- Relevant existing tests include `test_agent_worker.py`, `tests/test_agent_worker_loop.py`, `test_agent_controller.py`, `tests/test_agent_controller_proxy.py`, and remote protocol/service tests. These cover current representations, not all requirements below.

This inspection does not establish uniform cancellation, capability discovery, or typed retry policy. `docs/copilot-cli-support.md` reports that the inspected Copilot SDK API owns its session/tool loop and does not accept arbitrary tgent transcripts; it must not be assumed a drop-in one-turn provider absent verified history-import semantics.

## 3. Desired ownership and interface

**AgentLoop** owns canonical transcript/order, tool policy and dispatch, operator waiting, cancellation coordination, limits, lifecycle, and patch decisions. **ModelProvider** performs provider I/O and translation for one turn; it cannot execute tools, alter agent state, or decide lifecycle. **Controller/runtime** owns sandboxing, credentials, durable state, and hard-limit enforcement. Provider output is untrusted.

```text
AgentLoop.start(context) -> AgentStatus
AgentLoop.run_until_waiting_or_complete() -> AgentStatus
AgentLoop.deliver_operator_messages(messages) -> AgentStatus
AgentLoop.request_stop(reason) -> AgentStatus
AgentLoop.get_runtime_status() -> AgentStatus

ModelProvider.capabilities() -> ProviderCapabilities
ModelProvider.complete(request, cancellation) -> ModelResponse
ModelProvider.cancel(request_id) -> CancelResult
```

Methods have asynchronous semantics. Return typed results/errors or versioned status/event envelopes. No provider-native object, live process, or SDK session handle crosses the remote boundary.

## 4. Canonical data contract

Use versioned, JSON-serializable schemas with strict validation. Opaque provider metadata may be retained in a namespaced, size-bounded extension, but cannot affect orchestration.

### Messages and tool exchange

Canonical roles: `system`, `user`, `assistant`, `tool`. Each message has role, content (text or explicitly typed parts), and optional stable message ID/metadata. Preserve order. Define content parts/attachments before enabling multimodal data; reject unsupported content rather than silently flattening it.

Assistant messages may contain tool calls, each with unique `call_id`, `name`, and validated structured JSON arguments (not ambiguous provider-encoded text). Tool results identify the same call ID and tool name, include `ok`/`error` status and bounded content, and follow the request in transcript order. Preserve assistant text alongside calls, refusal, and finish information. Malformed IDs/arguments, unknown or duplicate calls, and unsupported roles are protocol errors and never dispatch. Legacy function-call shapes are adapter-input compatibility only.

A model request contains schema version, request/agent/turn correlation IDs, policy-selected provider/model identity, ordered messages, available tool declarations (description/schema only), output constraints, and applicable limits/deadline. Response contains matching request ID, assistant message, finish reason, optional provider/model identity, usage/budget data, and opaque metadata. Reject mismatched IDs.

### Capabilities

`ProviderCapabilities` explicitly declares supported roles/content parts, tool/schema features, parallel-call behavior, streaming, cancellation, usage reporting, context/output limits, and provider restrictions. Each is supported/unsupported/unknown with optional limit/detail. Negotiate before sending. Unsupported features must be rejected or transformed only by an explicit approved adapter rule; never silently dropped. Capabilities are advisory; validate actual responses too.

## 5. Cancellation, limits, lifecycle

Cancellation is cooperative and observable: stop cancels the in-flight request; `cancel` is best-effort and idempotent; if cancellation wins, late responses cannot dispatch tools. Distinguish operator stop, deadline, runtime shutdown, and provider cancellation, recording whether cancellation was confirmed. A non-cancellable provider must declare that; controller may terminate its isolated worker/request at deadline. Cancellation is not completion.

Controller/loop enforce hard limits, passing remaining budget/deadline to provider: runtime, model-call count, request deadline, history messages/bytes, enforceable input/output tokens, tool calls per turn and total, tool-result bytes, and tool execution time. Specify defaults and policy-capped request overrides. Usage may be estimated; provider-reported usage is not sole enforcement. Invalid/non-positive limits fail validation. Exhaustion yields distinct `limit_exceeded` and preserves history.

Lifecycle distinguishes `running`, `waiting_for_operator`, `completed`, `stopped`, and `failed`; inactive worker does not imply success. Patch submission is loop/controller policy, never provider policy.

## 6. Errors and retries

Normalize errors as `{kind, code, message, retryable, retry_after, request_id, provider, details}` with bounded, redacted details. Kinds include invalid request/protocol, unsupported capability, authentication/configuration, rate limit, unavailable/transport, timeout, cancellation, refusal, and internal failure. Do not leak credentials or sensitive payloads.

Retries belong to the loop or one explicitly designated adapter layer, never both. Retry only declared transient failures, honor `retry_after` and deadlines/budget, and use bounded exponential backoff with jitter. Keep stable logical turn ID and distinct attempt IDs. Never retry invalid request, auth/configuration failure, or cancellation. A timeout may follow successful provider completion: use idempotency where available; otherwise represent uncertain outcome and apply safe policy rather than blindly duplicating a turn. Tool execution is never implicitly retried; side-effecting tools need their own idempotency policy.

## 7. Placement and remote boundary

Local placement may run loop in controller/service and call a local adapter; remote placement may run the same loop in remote service behind start/status/continue/stop. Choose placement explicitly. Both use identical canonical messages, status, limits, and authorization. Remote messages are authenticated, versioned, bounded, and validated; vendor values/process handles stay in their owning service. Define authoritative transcript ownership and recovery of in-flight state after restart. Remote placement grants no additional credential, filesystem, or network access.

## 8. Security and tool authorization

Declarations are not grants. Before dispatch, loop/controller checks agent-specific allowlist, argument schema and policy, sandbox/resource boundary, and records authorization. Unknown, disallowed, malformed, or oversized calls fail closed and never execute; return bounded tool error or terminate according to policy. Provider does not execute tools or receive execution credentials. Tool implementations enforce path confinement, argv-only commands, output/time bounds, and explicit network policy. Treat prompts/provider output/tool results as untrusted; prompt text cannot alter controller policy. Restrict and audit remote catalog changes. Sending repository/history to a provider is explicit data disclosure requiring configured provider, data policy, and operator visibility.

## 9. Acceptance tests

Require shared contract tests against a fake provider and each supported adapter, in local and remote placement:

1. Round-trip ordered roles, calls/results, multiple calls, IDs, assistant text, finish reason and usage; malformed/missing/duplicate IDs fail closed.
2. Capability tests cover supported/unsupported features, limits, refusal, and lossy content rejection; Chat Completions and Responses adapters are equivalent for supported cases.
3. Authorization tests prove allowed calls dispatch once and unknown/disallowed/malformed/oversized calls never execute; results pair and obey bounds.
4. Cancellation covers pre-request, in-flight, unsupported cancel, late response/tool call, timeout, repeated stop, and distinct terminal reasons.
5. Exact-boundary/overrun tests cover runtime, calls, history sizes, tools, output, deadlines; overrides cannot exceed controller policy.
6. Error tests verify normalized/redacted errors, bounded retry/backoff and no duplicate tool side effects or unsafe duplicate model turn.
7. Local/remote parity covers status, transcript, limits, authorization, errors; protocol mismatch, oversized input, disconnect/restart recovery are explicit.
8. Preserve existing worker history/tool loop and controller proxy regressions. Contract tests need no live credentials/network; record per-provider results.

## 10. Open questions

- Is the initial public contract Python-only or language-neutral? Schema/version compatibility policy?
- Text-only initially, or which content parts/attachments and retention controls?
- Retry owner and representation of uncertain completion for providers without idempotency keys?
- Which limits are hard global caps vs configurable defaults; what token accounting is enforceable?
- Is streaming required? If so, define event ordering, partial tool calls, cancellation, persistence first.
- Which remote transport/auth/version negotiation is authoritative; how recover in-flight turns?
- Who selects provider/model and approves external data disclosure?
- Is session-oriented Copilot a separate backend, or is supplied-transcript one-turn completion required? Do not claim continuity without validation.

## 11. Meeting decisions (record; do not infer)

No meeting decisions were supplied with this specification. Record each decision here with date, attendees/owner, decision, rationale, and follow-up. Until recorded, questions above remain open. No provider selection, streaming requirement, retry owner, or Copilot mode is approved by this document.
