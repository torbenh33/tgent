# GitHub Copilot SDK history restoration: investigation and proposal

## Scope and verified SDK location

The SDK is present in this controller snapshot as ordinary source/documentation files under `copilot-sdk/` (including `copilot-sdk/python/copilot/`, `copilot-sdk/python/README.md`, `copilot-sdk/docs/`, and Python samples). `git ls-files --stage copilot-sdk github-copilot-sdk` returned no entries: these are untracked snapshot content, not a registered Git submodule. The SDK directory itself contains `.git` metadata, but its Python source and docs are readable files in the project snapshot. No network fetch/update, runtime execution, credential access, or live model request was performed.

Evidence below comes from the local SDK Python source and documentation: `python/copilot/client.py`, `python/copilot/session.py`, `python/copilot/copilot_request_handler.py`, `python/samples/manual_tool_resume.py`, `docs/features/session-persistence.md`, and `docs/features/agent-loop.md`.

## Findings: history is session-owned, not caller-supplied

**Verified:** `CopilotClient.create_session()` creates an agent conversation and accepts a `session_id`; `resume_session(session_id, ...)` reconnects to an existing, non-deleted session. Its docstring promises continuation with conversation history. The SDK persistence guide describes persisted conversation history, tool state, and planning context and demonstrates resume after a new client/process. Resume can reconfigure a number of session options; it does not accept an arbitrary list of Chat Completions messages. `CopilotSession.send(prompt, ...)` accepts one prompt string, attachments and source/delivery metadata, not role-tagged messages or tool-call records. It is therefore not an API for seeding an existing external transcript.

The manual-tool-resume sample verifies a narrower but useful behavior: pending work, permission requests and an external tool call can persist across client shutdown and resume; the application can continue that operation and provide its pending result. This is resumption of SDK-owned state, not replay/import of tgent history.

**Verified ordering/representation:** the SDK agent-loop guide says the CLI owns the model/tool-use loop and retains full conversation history across calls. It describes assistant messages containing `toolRequests`, tool execution and results fed into later model input, followed by further assistant turns. SDK events expose assistant messages/tool requests and tool execution events; they are not an API for appending arbitrary assistant/tool transcript records. The persistence docs promise retained history, but the inspected public Python session methods expose no transcript-import/seed/replay method.

**Roles and fidelity limitation:** tgent carries OpenAI-style `system`, `user`, `assistant`, and `tool` messages, with assistant `tool_calls` and corresponding tool-result messages in ordered history. Copilot SDK's public `send()` represents a new prompt; source can be omitted/default user, explicitly `user` or `system`, or an identified `AgentMessageSource`. This source metadata does not turn send into arbitrary role injection. Tool requests/results are generated and processed by Copilot's own loop, rather than accepted as historical records from tgent. The inspected API therefore cannot faithfully transplant a tgent transcript while preserving original role identity, assistant tool-call IDs/arguments, tool result pairing, and exact interleaving/order. Re-sending serialized history as one prompt (or multiple textual prompts) would only be lossy textual context: it would not restore actual message roles, tool-call protocol state, or agent state. That is a recommendation/limitation inferred from the documented public API surface, not a claim that no private/internal protocol can represent it.

## Comparison with tgent's model abstraction

`llm.py:LLMClient.model_callback(params)` is called with OpenAI-style chat/tool request parameters and returns a chat-completion-shaped dictionary. It preserves the existing callback boundary used by `mcp_agent.py` and `remote_agent_service.py` to inject model access into `AgentController`. The Responses API adapter explicitly converts ordered Chat Completions messages, tool calls and tool outputs to Responses input items, with validation of required call IDs/names. The existing tgent abstraction thus requires a model-turn provider that can consume supplied history and return a compatible assistant completion, including tool-call outputs.

The Copilot SDK is an agent/session orchestration API, not a drop-in implementation of that callback contract: the SDK chooses when to call the model, manages its own tool loop and session state, and emits events rather than returning the single chat completion required by `model_callback`. Its `CopilotRequestHandler` is a lower-level hook to observe/modify/forward Copilot runtime outbound HTTP/WebSocket inference requests; it is not a documented tgent callback accepting caller-provided conversation messages. Treat using it to extract/replay an external transcript as unsupported absent further verified contract.

## Integration recommendation

Do not implement a Copilot SDK provider behind `model_callback` on the basis of this API. Choose an explicit product direction first:

1. **Keep tgent orchestration:** use a model endpoint/provider that implements the current callback request/response shape and supports ordered chat/tool history.
2. **Delegate an agent task to Copilot:** build a separate, explicit backend/orchestration path which creates or resumes a Copilot session, sends a task prompt, handles SDK events and session lifecycle, and returns a final result to the controller. Document that this is a new Copilot conversation and cannot inherit the tgent transcript faithfully. Persist the Copilot session ID if continuation is wanted. Do not silently switch/fallback between the two modes.
3. **If history import is a hard requirement:** obtain and verify a documented SDK/runtime transcript import API (including role, tool-call/result, ordering and persistence semantics) before design or implementation. Do not assume sending synthetic prompts, event callbacks, or internal RPCs provides fidelity.

This remains a plan/documentation assessment, not provider implementation.

## Integration risks and unresolved work

A separate SDK-backed agent path would need explicit lifecycle and identity mapping, event-to-result behavior, error/cancellation handling, configuration/model selection, authentication, and persistence ownership. Verify which history and pending-work state survive in the intended runtime deployment; do not assume local examples prove remote durability. Treat repository contents sent to Copilot as an intentional external data disclosure and assess applicable organizational policy. Never expose controller credentials directly to the sandboxed coding worker. The documentation inspected here does not establish production deployment, model entitlement, privacy policy, or authentication provisioning.

## Verification

No runtime or live SDK tests were run: this is a source/docs investigation and running SDK behavior could require a runtime, network, or credentials. Documentation checks, if configured in tgent, should be run on the edited document.
