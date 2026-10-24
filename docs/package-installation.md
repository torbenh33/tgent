# Installing the agent runtime

Build and install the trusted controller distribution into its isolated virtualenv (also used by local Bubblewrap workers):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip build
.venv/bin/python -m build --wheel
.venv/bin/python -m pip install dist/tgit_agent-*.whl
```

The wheel is built using the standard PEP 517 build backend. It declares dependencies and the `tgit-agent-controller`, `tgit-prepare-remote-runtime`, and `tgit-deploy-remote-agent` commands. Deployment syncs the app and its venv, then prepares a versioned remote Bubblewrap runtime containing the same environment and worker runtime sources. Local controllers mount the active venv and trusted runtime modules. Workers run with the venv on sys.path, independent of target project contents.

MCP servers are trusted code. Register a server as an installed package entry point in the `tgit_agent.mcp_servers` group, with a name such as `mcp_example` and a target resolving to a FastMCP server object (or factory). Only registered entry points are enumerated; arbitrary `mcp_*.py` files in a project are never imported. Discovery reports installed servers, while the controller continues to gate invocation with `TGIT_AGENT_DEFAULT_TOOLS` / operator allowlisting; installation alone does not enable a server. Do not install untrusted packages into the agent venv.

For development, use `pip install -e .` in the isolated venv, run tests from the checkout, then build/install the wheel before remote deployment. Remote runtime preparation publishes an immutable version manifest after transferring the venv and runtime code.
