# LSP diagnostics feedback after agent edits

**Status:** Proposed implementation plan; documentation only. No LSP behavior is implemented by this document.

## Executive recommendation

Build diagnostics as a worker-local, optional service invoked automatically after a successful changed `edit_file`, and attach a bounded diagnostic summary to that edit's tool result. Start with a small explicit server allowlist and project-rooted configuration, not automatic downloading or Mason. MVP should support one or two provisioned servers, changed-file diagnostics plus directly affected open project files where feasible, a short debounce/batch window, and graceful `unavailable` results. Keep `run_command`/test tools unchanged; diagnostics are fast feedback, not a replacement for tests.

## Verified repository facts

- `agent_worker.py` defines tool schemas, performs `_edit_file`, and routes tool calls through `_execute_tool` / `_handle_tool_calls`; edit results enter normal model tool-result history. `_edit_file` writes atomically and returns `changed`; unchanged edits do not write. This is a direct insertion point for feedback.
- Workers execute in a Git snapshot under Bubblewrap. `agent_sandbox.py` constructs trusted runtime mounts; `docs/package-installation.md` says workers use the installed trusted venv independent of project contents. `prepare_remote_runtime.py` publishes a versioned remote runtime. Server executables must be deliberately provisioned/mounted for both environments; a host editor install is not automatically visible.
- Existing checks include explicit bounded `run_command` and MCP resources for unittest, pytest and doctest (`mcp_test_runner.py`). `test_subprocess.py` runs commands, but no general syntax checker or LSP pipeline was found. Tests are explicit, not automatically run after edits.
- MCP discovery concerns trusted installed entry points and operator allowlisting, not project-defined language-server code. `pyproject.toml` requires Python >=3.11 and declares FastMCP/OpenAI; this does not define supported languages for target repositories.
- The repository appears Python-centric, but target projects are arbitrary Git checkouts. No Mason or Neovim assumptions were found in repository docs/config. Mason manages binaries for a user's Neovim environment; it does not provision the isolated local/remote worker runtime. It could only be a controlled operator source for binaries explicitly provisioned and trusted.
- `agent_project.py` parses `.agent-sandbox.toml` only for home mount requests, not language-server selection. Existing sandboxing, timeout and bounded-output practices are useful precedents, but long-lived LSP clients need dedicated lifecycle and resource controls.

These findings do not prove every server binary is installed, that target projects use a supported language, or that network/resource quotas are universally available.

## What LSP adds over current checks

Syntax checkers, compilers and tests often require project-specific commands/dependencies, full execution, and explicit agent invocation. A stateful LSP can keep a language-aware workspace index and publish syntax, type, unresolved-symbol and configuration diagnostics promptly for an edited buffer, with precise ranges and severity. Returning these in the edit result creates a same-cycle correction opportunity. LSP is not inherently more correct: results can be stale/noisy/config-dependent. It complements, not replaces, tests and runtime checks; do not run a full test suite after every edit.

## Proposed architecture and workflow

### Discovery and selection

1. Infer candidate languages from edited-file extension and a bounded scan for project markers/config (Python `pyproject.toml`/`setup.cfg`, TypeScript `package.json`/`tsconfig.json`, Rust `Cargo.toml`, Go `go.mod`). These are hints, not proof. Never execute project installation instructions or config.
2. Map file types to an operator-configured trusted server registry: server ID, executable, fixed arguments, supported languages, safe root rules, and resource budgets. Project settings may tune safe server behavior but cannot select arbitrary executable paths, add servers, or broaden filesystem/network authority. Resolve only trusted runtime PATH/paths and validate provisioning.
3. Select one server per language/workspace root. In polyglot repositories maintain independent clients keyed by canonical root/server; aggregate per-language results and deduplicate. Do not launch every recognizable server in a monorepo.
4. Choose roots by server-specific marker precedence, bounded traversal within the Git snapshot, and safe explicit overrides. Exclude generated/vendor/build/virtualenv paths by default.

### Feedback after edits

After `_edit_file` returns `changed: true`, notify a per-agent diagnostic manager with canonical relative path and new content/version. Debounce briefly (initial target 150–300 ms) and batch changes in the same tool-call wave. Use LSP `didOpen`/`didChange`, increment document versions, and wait for diagnostics matching the latest version under a short deadline. Prefer persistent managed clients over cold-start per replacement; MVP may use a client/server per batch if tested protocol support allows it.

Extend the edit result with a bounded `diagnostics` object: status (`ok`, `none`, `unavailable`, `timeout`, `error`, `not_applicable`), server/language, checked relative paths, and diagnostics (range, severity, code, source, message), plus truncation/staleness flags. Cap count, message length and serialized bytes beneath the existing 24,000-character tool-result bound. Label workspace findings as pre-existing unless baseline comparison exists. A diagnostics failure must never undo or hide a successful edit. Child-agent behavior should be explicitly tested.

Maintain clients only for the worker lifetime; close on shutdown/resume/error. Serialize writes per client, correlate request IDs, discard stale versions, coalesce bursts, cap concurrency and cancel timed-out requests. Bound marker scans, file counts, bytes, indexing/startup time and diagnostics. Partial/unavailable results are preferable to blocking an edit. Unsupported files should report `not_applicable`; multi-language results should show coverage per file/language.

### Provisioning and Mason

Do not assume `~/.local/share/nvim/mason/bin` exists inside Bubblewrap or remote runtime. Preferred provisioning: operator-managed version-pinned server binaries in a trusted runtime image/venv; alternatively explicitly configured read-only runtime mounts. For remote mode, prepare the same approved versions and validate ABI/dependencies. Document install/license/update ownership. Mason may be an operator's source only after explicit controlled provisioning; no runtime Mason dependency, auto-download, or project-requested install.

## Security and resource constraints

- Treat project/config/server output and diagnostics as untrusted. Fixed executable/argv, no shell; no arbitrary wrappers, code actions, or project-driven downloads. Keep execution inside Bubblewrap and snapshot boundaries.
- LSP servers may spawn children or execute project-specific plugins/imports. Enforce process-group cleanup, startup/request deadlines, bounded pipes/output, concurrency and CPU/memory/process/fd budgets where available. Default-disable server features that evaluate project code; constrain workspace reads to snapshot and avoid host home credentials or writable runtime mounts.
- Network off by default; any exception is operator-granted. Prevent server/config mechanisms from silently fetching plugins/indexes.
- Limit scans/payloads and do not log source or full diagnostic text. Tool results become persisted conversation history and are sent to configured model endpoints; return only relevant bounded diagnostics.

## Components/files likely to change

- `agent_worker.py`: invoke diagnostics after changed successful edit; extend result/description; manage lifecycle and child-agent policy.
- New `agent_diagnostics.py`: language/root hints, trusted registry interface, LSP framing/client, debounce/batch scheduler, diagnostic normalization, limits and cleanup. Keep protocol independently testable.
- `agent_sandbox.py`, `prepare_remote_runtime.py`, `remote_agent_config.py`, possibly `agent_project.py`: trusted executable mounts, configuration and local/remote parity. Do not put arbitrary executable authority in project config.
- `test_agent_worker.py` plus new diagnostics unit/integration tests; `test_agent_sandbox.py`, `test_prepare_remote_runtime.py`, remote integration tests if provisioning changes.
- `pyproject.toml` / `docs/package-installation.md` only if adopting a reviewed client dependency or packaged servers; prefer a narrowly scoped dependency or independently tested stdlib client. Update README/operator docs when shipped.

## Tests and rollout

1. Unit tests: language/root selection, ambiguous extensions, nested roots, symlink/path escape, polyglot and ignored trees; LSP framing with partial/multiple/malformed/oversized messages; version correlation, normalization, deduplication and output caps.
2. Fake-server integration: initialize/shutdown, open/change, batching/debounce, published/request diagnostics, stale versions, timeout/crash, malformed protocol, cancellation, process cleanup and no deadlock.
3. Worker tests: no invocation for unchanged/failed edit; diagnostics attached to changed edit; batch multiple edits; failure never rolls back; disabled/unsupported languages and bounded results; child-agent behavior.
4. Sandbox/runtime tests: only trusted executables visible, project cannot select arbitrary binary/access host home/cause implicit network or install. Remote tests verify pinned parity and graceful absence.
5. Benchmark representative small/large Python and polyglot projects for latency, memory, process count, relevance and correction-turn/false-positive impact.

Roll out default-off: (a) fake-server test harness and manual opt-in; (b) Python-only with pinned server, measure bounded aggregate latency/errors without content; (c) opt-in multi-language and remote parity; (d) broader defaults only after reliability/security and correction-turn gains. Never block patch submission on unavailable diagnostics.

## Minimal MVP

One operator-provisioned, pinned Python server; default-off operator setting; Python files under one root; one managed client per agent; debounced/batched changed-file diagnostics attached to each successful edit; strict output/time/resource limits; graceful `unavailable`/`timeout` while preserving edit success; fake-server and cleanup tests; local/remote provisioning docs. Exclude Mason integration, auto-install, arbitrary project-selected commands, full workspace indexing, code actions and test execution. Benchmark candidate server quality/provisioning (e.g. Pyright versus basedpyright) before choosing.

## Assumptions and open questions (not verified facts)

- Which languages to support beyond Python, and whether feedback should cover only edited files or related workspace files.
- Whether local/remote deployments can provision non-Python binaries and enforce consistent memory/process quotas.
- Server choice, license, offline behavior, indexing costs, and client-library choice.
- Whether baseline diagnostics are necessary to identify newly introduced issues; MVP should disclose findings may predate the edit.
- Actual debounce/deadline, project-size, and output budgets need measurement.
- Whether operator-only configuration is sufficient (recommended initially) and how users opt out.
- Safe project-specific server settings may be useful, but must not authorize executable selection, broader file access or network.

## Inspected sources

`agent_worker.py`, `agent_sandbox.py`, `agent_project.py`, `test_subprocess.py`, `mcp_test_runner.py`, `pyproject.toml`, `prepare_remote_runtime.py`, `docs/package-installation.md`, `docs/mcp-resource-support-rfc.md`, `README.md`, and `AGENTS.md`; searched docs/config for Mason and syntax-check references. This is a design plan, not a claim that LSP support, universal target-language support, resource quotas, or Mason provisioning already exists.
