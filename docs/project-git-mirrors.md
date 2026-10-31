# Project Git mirrors and working-tree synchronization

**Status:** review draft for discussion; this specifies a proposed workflow, not implemented behavior.

## Scope and vocabulary

Define how an uploaded project becomes durable, how working-tree changes reach an agent checkout, how agents share Git objects without writable checkouts, and how commits (including submodule commits) are published and consumed. A *project* is an operator-authorized stable identity, not an upload ID/path. A *reference mirror* is a trusted bare repository. A *submodule path* is relative to its parent repository; identity includes project and full ancestry/path, not just URL. An *agent worktree* is private writable state. A *published commit* is reachable through a service-managed agent ref; it is not thereby merged or approved.

## Current behavior (observed)

- Remote project starts use per-upload staging. `RemoteAgentAdapter.start` rsyncs the source tree (including `.venv/`) and only requests start after successful transfer (`remote_agent_adapter.py`, `test_remote_agent_adapter.py`). `RemoteUploadManager` allocates private incoming directories, seals uploads by rename, and retains content if startup fails (`remote_agent_upload.py`). This is not a durable project mirror.
- Continuation transfers a verified Git bundle, but rejects superproject gitlinks because its bundle lacks independent submodule history (`remote_agent_adapter.py`; `test_remote_agent_adapter.py::test_continue_with_project_rejects_submodule_gitlinks_before_staging`).
- Snapshots copy project/Git metadata, specially handling linked worktrees, but explicitly reject repositories containing submodules (`agent_snapshot.py`, `test_agent_snapshot.py`). There is no mirror-backed agent worktree implementation.
- Git tools can inspect/status/diff submodules and fetch recursively (`mcp_tgit.py`); this does not mean upload, snapshot, or publication supports them. `docs/agent-management-architecture.md` sketches mirrors and publication, but those are proposals. Deployment/runtime rsync is separate.

## Proposed contract

### Identity, ownership, and layout

The trusted controller/service owns project registration, mirror directories/manifests, upload staging, publication refs, and cleanup. Agents own only their worktree and private refs. Never mount shared mirrors writable into agent sandboxes. A trusted publisher receives objects through a controlled channel and updates authorized refs. Project identity must be explicitly registered/selected by an authorized caller; do not derive it only from user-controlled paths, `.git` config, remote URL, or upload ID.

Maintain one reference mirror per project and one per submodule path in that project. Distinct paths remain distinct even for identical URLs. A manifest maps project and normalized repository-relative path (including parent ancestry) to mirror identity, canonical source/policy, object format, pinned gitlink, and sync/publication state. Confine paths; reject traversal, symlink escapes, collisions, and ambiguous identities on case-insensitive filesystems. Do not deduplicate mirrors across project security boundaries.

### Upload and rsync working-tree sync

Authenticate/authorize before allocating a unique, permission-restricted staging directory. Bind upload IDs to caller/project, enforce quotas/timeouts, and reject replay/duplicate finalization. Rsync is transport only: fixed server destination, argument-vector invocation, safe path handling, and no client-controlled remote command/destination. Validate completely before atomic promotion; partial transfer is never a project version.

Record project/upload identity, source commit (if present), source tree fingerprint, selected policy/paths, discovered submodules, and outcome. Never rsync `.git` metadata into mirrors or worktrees: obtain Git objects using Git operations and transfer working-tree content separately. Define treatment of tracked dirty files, index state, untracked/ignored files, deletions, modes, symlinks, hardlinks, special files, and case collisions. Proposed default: include tracked working-tree modifications and ordinary untracked non-ignored files; preserve deletions, executable bits, and symlinks; exclude ignored files, `.git`, and special files unless opted in. The index is not authoritative/copied; reconstruct from a pinned commit and apply the declared content delta. Record exclusions; fail on unsafe entries rather than silently dropping work. Treat secrets as sensitive: no logging/persistence and reject or warn according to agreed patterns.

For no-commit trees require explicit import policy (create initial commit under defined identity or reject); never invent a base. Source commit and dirty delta remain distinct. Do not commit uploaded changes for an agent without explicit identity/policy.

### Mirrors and agent worktrees

Verify repository format, connectivity and refs on import/fetch. Update mirrors under project-scoped locking/transaction. Pin immutable base object IDs. Each agent gets a separate writable worktree/clone with private branch, index, config, hooks and metadata. Reference objects from mirrors only with safe lifetime semantics: worktree must remain usable during mirror outage and alternates must not create dangling-object risk. Record base, mirror generation, sync manifest and ownership. Never execute agent-controlled hooks/config in trusted mirrors.

### Publish and fetch agent commits

A trusted publisher accepts commit/ref sets and expected project/base identity, verifies object format, graph/base policy, received object completeness and limits, and rejects arbitrary refs/default-branch updates. Publish under immutable or compare-and-swap refs such as `refs/agents/<agent-id>/<publication-id>`. Bind identity to authentication; prevent cross-agent overwrite; make retries idempotent. Success means durable and fetchable, not reviewed/merged. Record commits, base, scope, timestamp and dependencies.

Fetching is explicit and scoped into private refs such as `refs/remotes/agents/<id>/...`; never implicitly merge/check out. Distinguish source base, published commits, merged branch, and review submission. Retain refs/objects while any worktree or review depends on them. GC only unreferenced expired data after grace and dependency checks.

### Nested submodules

Parse `.gitmodules` as untrusted input. Resolve relative URLs only under policy; canonicalize and authorize scheme/host/path, block prohibited local/file/credential URLs and network destinations (SSRF), use service-managed credentials, and repeat checks at every depth. Enforce depth/count/bytes/time limits and detect cycles. Each nested path has its own project/path-scoped mirror and manifest entry. Pin exact gitlink object, never silently substitute branch tip. Dirty/untracked module content follows sync policy.

Publish referenced submodule commits first and retain them, then expose the superproject ref/manifest as one logical transaction. Failure of a dependency means no successful parent publication. Unchanged gitlinks also require pinned objects to be available for reproducible worktrees.

## Security, failure, and recovery

Separate untrusted staging from trusted mirror roots; least-privilege identities and filesystem permissions. Validate real paths/ownership at each transition and protect locks against symlinks. Reject unsafe archive/symlink entries, never follow upload symlinks outside staging, avoid shell interpolation, cap inputs and rate-limit. Audit identifiers and decisions, never secrets/content. Agents cannot mutate mirrors, other worktrees, shared refs, or credentials.

Stage operations: allocate → receive → validate → import/fetch → create isolated worktree → atomically record manifest → expose. Use temporary refs/directories and atomic rename/ref transactions; incomplete work is never success. Lock contention is retryable. Disk-full, interruption, invalid gitlink, unavailable source, permission or verification failure leaves previous state authoritative.

Recovery must distinguish incomplete uploads, imported objects without manifest, abandoned worktrees, partial publication and failed submodule dependencies. Verify objects/checksums, then idempotently finish or quarantine/remove only owned staging. Never overwrite newer refs or remove reachable objects. Retain failed uploads for bounded retry/diagnostics, then auditable cleanup. Back up manifests and durable refs together and test restore.

## Acceptance tests (proposed)

1. Dirty tracked edits/deletions, staged/unstaged state, untracked/ignored files and no-commit imports match the documented policy/manifest.
2. Inject rsync interruption, retries, duplicate IDs/finalization, traversal, symlink escape, special files and disk-full; no partial version appears and prior state survives.
3. Two agents have independent writable worktrees at pinned base; neither can mutate mirror/other worktree. Recovery works during mirror outage.
4. Publish valid commits; reject arbitrary refs, cross-agent overwrite, wrong base, missing objects, malformed graph and oversize input. Retry is idempotent; authorized later agent fetches unpublished-to-main commit without merging it.
5. Race mirror updates/publications; verify locking/CAS and no lost refs. GC respects grace and retained dependencies.
6. Test distinct submodule paths (including same URL), nested modules, dirty content, pinned non-tip, unavailable/disallowed URL, cycles, limits and malformed metadata. Parent never succeeds if a child dependency fails.
7. Verify URL/egress policy, credential redaction, quotas, permission boundaries, path normalization, links and audit events.
8. Crash at each transaction boundary; reconciliation twice is idempotent and loses no prior refs or staging evidence.

## Uncertainties and meeting decisions

Current code does not establish project identity, upload inclusion rules, mirror lifecycle, publisher protocol, or recursive submodule support. Decide: (1) project registration and durability across agent deletion; (2) ignored/untracked/secrets and no-commit policies; (3) submodule schemes/hosts, relative URL semantics, credentials and offline mode; (4) mirror storage, locking, backup and retention; (5) publication ancestry, visibility, naming and review relationship; (6) recursive submodule edits and cross-repository transaction representation; (7) quotas and failed-upload retention; (8) alternates versus independent objects; (9) who may register projects and authorize network fetches.

## Evidence reviewed

Reviewed `remote_agent_adapter.py`, `remote_agent_upload.py`, `agent_snapshot.py`, `mcp_tgit.py`, related upload/adapter/snapshot tests, `README.md`, and `docs/agent-management-architecture.md`. Existing architecture text overlaps with this proposal; this file separates implementation facts from decisions. No implementation was changed.
