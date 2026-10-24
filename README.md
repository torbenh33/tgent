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

A halted worker can be continued after controller restart. Continuation creates a new agent record and worker from the halted agent's preserved transcript and private project snapshot; the original record and snapshot remain unchanged. The response identifies the new agent and its `parent_agent_id`. Agents with submitted patches, and inactive review-waiting agents, are not restarted this way; use existing live continuation/review workflows to avoid duplicate patch submissions.

## Tests

Run the repository test suite from the checkout with pytest:

```sh
python -m pytest
```

The project also exposes MCP test-runner resources for unittest, pytest and doctest execution.

## Build and deployment

Build a wheel with `python -m build --wheel` and install it in the trusted runtime environment. For remote deployments, use `tgit-deploy-remote-agent` to sync the application and environment, then `tgit-prepare-remote-runtime` to publish the remote Bubblewrap runtime. See [package installation](docs/package-installation.md) for the supported sequence and security details.
