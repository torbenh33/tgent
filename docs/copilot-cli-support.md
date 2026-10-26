# GitHub Copilot CLI backend: feasibility and proposal

## Assessment

The model seam is `LLMClient.model_callback(params)` in `llm.py`: it accepts OpenAI-style chat/tool requests and returns chat-completion-shaped data. `mcp_agent.py` and `remote_agent_service.py` construct this client on the controller/service side and inject the callback into `AgentController`; workers send requests over controller IPC. This is the appropriate future integration point, not a CLI invocation inside the coding worker. The existing default is the OpenAI-compatible API configured in `config.toml` (`[settings]`: `base_url`, `api_key`, `model`, `api`); preserve it.

A Copilot CLI adapter is not safe to implement yet. The repository contains no verified CLI invocation/protocol, machine-readable output contract, model-selection behavior, or authentication lifecycle. The reported package in the project venv is awaiting deployment; that does not establish which executable/version or runtime is available in production. Guessing risks mishandling tool-call semantics and exposing prompts or credentials. No installation, credential access, live request, or deployment was attempted.

## Proposed architecture

Add an explicit backend selector under `[settings]` (for example `backend = "openai"` by default and eventually `copilot_cli`) while retaining `LLMClient` as the callback facade. Implement CLI transport behind that callback, translating supported messages/tools using the CLI’s documented protocol and adapting responses to the existing chat shape. Do not select implicitly or silently fall back on errors. Controller-side invocation keeps CLI credentials out of the project worker.

## Configuration and deployment

Before implementation, establish the exact package/executable/version, supported non-interactive invocation, stdin/stdout contract, model-selection options, timeout behavior, and function/tool-call support. Define explicit executable configuration (prefer an absolute path or controlled `PATH` lookup), bounded timeout, and documented options. Do not put credentials in TOML. Determine how authentication is provisioned/refreshed for an unattended controller account; never pass tokens into worker environments, snapshots, logs, or project mounts.

For remote mode the CLI and dependencies must be in the deployed controller/service runtime and authentication provisioned there; installing into the local venv is not deployment. Review `prepare_remote_runtime.py` and service setup for the actual packaging path. Avoid mounting the host CLI or credentials into worker sandboxes: Bubblewrap clears the environment, uses a restricted `PATH`, and workers normally have networking disabled. Worker-side execution would require separate sandbox, network, and credential review. No deployment should occur until these requirements are resolved.

## Security and failure behavior

Use `subprocess` with an argument vector, never a shell; use controlled environment, no credential-bearing argv, bounded execution time/output, and strict exit-status/protocol validation. Bound and sanitize stderr diagnostics; avoid logging prompts/authentication data. Treat malformed output, unsupported tool calls, missing executable/authentication, timeout, and nonzero exit as explicit errors. Do not retry through another model or accept partial output silently. Confirm where prompt contents are sent and review policy for private repositories.

## Focused verification after interface confirmation

Mock the process boundary; tests must never invoke the real CLI, use network, or require credentials. Cover serialization, successful response/tool adaptation, malformed output, nonzero exit, missing executable, timeout, bounded diagnostics, and unchanged default configuration. Test controller and remote-service construction for consistent selection.

## Open questions

1. What exact package, executable, and supported version are intended?

i have put it into the venv now.
not sure about the actual binary. but at least the python module should be availabel now.

2. What documented noninteractive protocol/schema exists, especially for tools and multi-turn results?
3. How are model selection, quota, authentication/refresh, and unattended operation handled?

we normally have a config file that configures that.
4. What data is sent to GitHub, and what privacy policies apply?

it basically sends the whole code right ? its an agent working on the code.

5. Which local/remote deployments will install the CLI, and how will credentials be provisioned without worker exposure?

the worker uses a socket to control with the runner outside the sandbox for the api/llm access. so the normal oerator account on the local machine or the remote machine will be used
