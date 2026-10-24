# tgit-agent

`tgit-agent` provides Bubblewrap-isolated coding agents for Git projects, exposed through a FastMCP controller. The controller asks a configured language model to operate through tools in a worker sandbox; snapshots and submitted patch series let an operator review changes before integrating them. An optional remote-agent service can run the worker runtime on another host.

## Features

- Per-agent state, lifecycle and limits managed by `AgentController`.
- Local workers run in a Bubblewrap sandbox; project configuration is read by `agent_project.py`.
- MCP tools support Git workflows, test execution, package installation and related coding tasks. Installed MCP servers are discovered from the `tgit_agent.mcp_servers` entry-point group and still require operator allowlisting.
- Patch series can be exported for review rather than silently integrated.
- Optional remote deployment and versioned runtime preparation.

## Requirements and setup

Python 3.11 or newer is required. Local execution requires Bubblewrap and the OS facilities required by the sandbox. Install the package and its declared FastMCP and OpenAI dependencies into a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For a distributable install and deployment prerequisites, see [package installation](docs/package-installation.md). Do not install untrusted MCP server packages into the trusted agent environment.

## Configuration

The default model configuration is `config.toml`; it specifies `base_url`, `api_key`, `model`, and API mode under `[settings]`. Set `TGIT_AGENT_LLM_CONFIG` to select another configuration file. The checked-in values are an example for an OpenAI-compatible local endpoint, not production credentials.

Useful environment settings include `TGIT_AGENT_STATE_DIR` and `TGIT_AGENT_SNAPSHOT_DIR` for controller storage, `TGIT_AGENT_MAX_MODEL_CALLS` and `TGIT_AGENT_MAX_RUNTIME_SECONDS` for limits, and `TGIT_AGENT_REMOTE` to enable remote mode. Remote settings are loaded via `remote_agent_config.py`; review that module and deployment docs before enabling remote mode. `TGIT_AGENT_DEFAULT_TOOLS` / operator allowlisting gates MCP tool invocation.

The MCP server uses the directory from which it is launched as its project root. Start it with `tgit-agent-controller` (or `python -m mcp_agent`) in the target Git checkout and configure it in the MCP host.

## Common workflow

1. Launch the controller from a Git project and configure its model endpoint and tool allowlist.
2. Use the `start_agent` tool with a request containing `schema_version: 1` and a task; optional fields include a name, history and limits.
3. Call `list_remote_agents` for all agent metadata plus the cached `history_summary` field (null until summarized); call `summarize_agent_history` on demand. Cached summaries are persisted per agent and invalidated automatically whenever history is appended.
4. Review the generated commits/patch series before applying them to the main project.
5. Use `list_agents_requiring_attention` to find waiting agents and all submitted patches. `reap_agents(agent_ids)` is limited to stopped/failed agents without submissions; submitted patches and awaiting-review agents are explicitly excluded to preserve review artifacts.

`halted` means the worker exited naturally; `stopped` means it was explicitly terminated (including controller shutdown). A live waiting worker can receive follow-up messages in place. After restart, inactive halted or stopped agents can be recovered: continuation creates a new agent from the persisted transcript plus supplied messages and the saved private project snapshot. The original record and snapshot are never modified; the response identifies the child and its `parent_agent_id`. An in-progress guard prevents concurrent recovery from duplicating a child. Recovery requires a valid managed snapshot and is refused if a patch was submitted, to prevent duplicate submissions. Inactive `awaiting_review` and `review_received` agents are not restarted; submitted work stays available for review. `failed` and `reaped` agents are also not recoverable (reaped records/snapshots are removed).

When `import_pending_remote_agent_patches` imports a newly submitted patch for an agent listed as `halted`, it writes an operator notice to the review Maildir (`TGIT_REVIEW_MAILDIR`, or by default `$XDG_STATE_HOME/tgit-review/incoming`, falling back to `~/.local/state/tgit-review/incoming`). This is a local Maildir notification, not SMTP or a system `sendmail` invocation; operators can read it with Maildir-capable mail clients (for example, configure Mutt to use that path). The notice contains the agent ID/name, state, review series, submitted head and base, and guidance to review before further work. It does not include patch content. Idempotent imports and scans without a patch do not notify. Only `halted` is notified; other states are deliberately excluded. Filesystem/mailbox failures are reported as an import error rather than silently changing agent state.

## Tests

Run the repository test suite from the checkout with pytest:

```sh
python -m pytest
```

The project also exposes MCP test-runner resources for unittest, pytest and doctest execution.

## Build and deployment

Build a wheel with `python -m build --wheel` and install it in the trusted runtime environment. For remote deployments, use `tgit-deploy-remote-agent` to sync the application and environment, then `tgit-prepare-remote-runtime` to publish the remote Bubblewrap runtime. See [package installation](docs/package-installation.md) for the supported sequence and security details.
