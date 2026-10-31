# Agent state and lifecycle storage: review specification

**Status:** Proposed contract for review; not an implementation change. This document distinguishes observable behavior in this repository from a desired backend-neutral contract. Source proposal: [agent-management-architecture.md](agent-management-architecture.md).

## 1. Scope and terminology

The store is controller-owned durable identity, lifecycle, conversation, patch-disposition, and artifact-reference data. It is not the worker process, project Git repository, model-provider state, or the artifacts themselves. “Agent ID” is an opaque controller identifier, not a commit hash. Local and remote APIs are transports over the authoritative management service, not independent state authorities.

This inventory is based on `agent_store.py`, `agent_controller.py`, controller/remote service adapters, README and tests. The proposed sections below are requirements for review, not assertions about current implementation.

## 2. Current behavior (inventory, not desired behavior)

### State files and authority

`AgentStore` uses a configured filesystem root. Each ID directory holds `metadata.json`, `history.json`, and optionally `history_summary.json`; metadata and transcript are separate JSON documents. Metadata carries lifecycle fields and references such as snapshot paths, patch submission head/disposition, parent ID and timestamps. History is a CopilotChat-format array preserved without normalization. Summary cache validity is keyed to history mtime and size. JSON writes use a same-directory temporary file, flush/fsync, `os.replace`, then fsync the directory. Creation refuses an existing directory and removes partial files on caught creation failure.

IDs are validated against `[A-Za-z0-9][A-Za-z0-9_-]{0,79}`. Metadata updates read/merge/write; transcript appends read and rewrite the full history. These store operations have no interprocess lock or revision/CAS. The store docstring assigns concurrent serialization to the controller (e.g. asyncio lock), which does not establish cross-process coordination. Atomic replacement prevents torn individual files, not lost updates or atomic multi-file transactions. `AgentStore` enforces no persisted schema-version or migration mechanism.

Controller list/resolve operates over persisted metadata; remote service operations delegate to the controller. Remote transport is not a separate state authority. Current layout differs from the architecture proposal’s `agents/<id>/state/record.json`.

### Lifecycle and patch disposition

Controller states include `starting`, `running`, `halted`, `awaiting_review`, `review_received`, `failed`, `stopping`, `stopped`, and `reaped`. Natural worker exit is distinguished from explicit stop; stopping can be transient. Continuation may deliver messages to a live review-waiting worker, or create a new child from inactive `halted`/`stopped` history and snapshot. Child metadata records `parent_agent_id`; restart-in-progress protection is in controller memory, not a durable cross-process lease.

Patch submission is recorded with fields including `submitted_head`; acknowledgement and rejection are tied to that head. Unresolved submitted work and `awaiting_review` are protected from reaping. Acknowledgement/rejection preserve submission metadata and review artifacts while allowing eligible controller state/snapshot cleanup. Bulk reap resolves IDs, returns per-item outcomes, and excludes ineligible states, duplicates, or unresolved submissions. Current bulk eligibility is stopped/failed, while private `_reap_agent` also accepts halted/reaped; this discrepancy is not the desired eligibility policy.

Reaping is destructive today: it removes the managed project snapshot and then removes the entire state directory. A crash between these steps may leave a record referencing a missing snapshot; after state removal there is no identity record. No durable `reaping` intent or tombstone remains. Independent review artifacts can be outside snapshot/state and are intentionally retained by patch-disposition workflows.

### Artifact paths and ownership

State directories are `<state_root>/<agent-id>/`; logs are separately stored at `<state_root>/logs/<agent-id>.log`. Snapshot roots are separately configured. Metadata records `snapshot_root` and `snapshot_project`; controller checks snapshot containment before continuation/reaping and delegates deletion to snapshot cleanup. There is no single canonical per-agent root for all artifacts. Patch/review artifacts may have independent management and must not be assumed owned by snapshot cleanup. Project data is not automatically agent-owned. Upload staging/finalization is a separate flow.

### Tests observed

`test_agent_store.py` covers JSON create/read, transcript preservation and append ordering, metadata merge/timestamp, duplicate creation, invalid IDs/message shapes, explicit state removal, and summary invalidation. `test_agent_controller.py` covers reap eligibility, patch preservation, acknowledgement/rejection idempotence and batch outcomes; remote adapter/integration tests cover transport and lifecycle calls. Existing tests do not establish schema evolution, interprocess CAS, tombstone/crash recovery, or complete artifact ownership boundaries.

## 3. Desired contract

### 3.1 Authority, identity, and APIs

There MUST be exactly one authoritative state service/store per deployment. Local and remote callers MUST route reads, ID resolution, lifecycle writes and reaping to it; caches and artifact directories do not prove agent existence. Migration has one explicit cutover after which only one store accepts authoritative writes. Artifact files are payloads; records are authoritative for existence, state, patch disposition, ownership and artifact status.

The backend-neutral API SHOULD provide `create`, `get`, scoped `list`, canonical `resolve_id`, revision-checked `transition`, history read/append, publication/disposition updates, and `begin_reap`/`finish_reap`. Resolution MUST prefer exact full ID, otherwise accept only a unique valid prefix, and report unknown and ambiguous separately. All downstream operations use canonical IDs. Remote validation MUST NOT reject a prefix before authoritative resolution.

### 3.2 Schema and versioning

The canonical record SHOULD include `schema_version` (required integer), canonical `agent_id`, monotonic `revision`, created/updated timestamps, lifecycle state and separate worker/stop reason where useful, parent relationship, patch submission/disposition evidence, artifact manifest, and reaping intent/progress/results. Artifact entries identify kind, owner, managed relative path or validated URI, status, retention/protection reason, and optional digest/size.

History may initially remain separate, but its format/version and relationship to record revision must be defined. Summary is derived cache data and must be disposable. Readers MUST reject unsupported future versions without silently rewriting them. Migrations MUST be deterministic, idempotent, tested, preserve compatible unknown fields, and define backup/rollback and failed-migration behavior. Schema version describes representation; revision supports concurrency.

### 3.3 Lifecycle and patch rules

Transitions MUST follow an explicit transition table and use expected-revision compare-and-swap (or transaction). Lifecycle state and process liveness are distinct facts. Reap requires an inactive worker, eligible lifecycle state, and resolved disposition for any submission. “Rejected” and “acknowledged applied” are explicit auditable operator decisions, never inferred from Git state. Review/submission artifacts stay protected until policy explicitly permits deletion. Batch outcomes distinguish invalid, unknown, ambiguous, duplicate, ineligible, error and success; independent IDs continue after failures.

### 3.4 Atomicity and concurrency

JSON records MUST be replaced with a same-filesystem temporary file, durable flush, atomic rename, and directory durability where supported. Creation and multi-document changes require a recoverable protocol (e.g. record-last publication or journal); atomic rename of one file is not a transaction. Writers MUST coordinate read-modify-write and transitions with locking whose scope/staleness semantics are specified across supported processes/hosts. Where shared-storage locking cannot be guaranteed, use a single writer/service boundary. Revision/CAS prevents stale writes and retries must not duplicate effects. Concurrent history appends must be lossless. Failures leave the prior committed revision or recoverable intent, never a success-shaped partial record.

SQLite requires transactions/constraints for record and revision changes, plus specified journal/synchronous mode, busy/retry policy, backup, filesystem placement and multi-process access. Do not assume safety on arbitrary network filesystems. Artifact deletion is outside either backend transaction and needs durable intent and idempotent reconciliation.

### 3.5 Tombstones

Desired default: retain a minimal tombstone after successful reap: canonical ID, schema version, final revision/state, completion time, patch-disposition reference and per-artifact outcomes (deleted, retained by policy, absent, failed/retry). Tombstones prevent ID reuse and support audit, retries and “already reaped” responses. They contain no transcript or sensitive payload. Retention duration, erasure/privacy obligations and possible ID reuse require a meeting decision. Current behavior deletes the record; this proposal does not imply indefinite tombstone retention.

### 3.6 Artifact ownership and safe paths

Every artifact reference MUST identify owner and management boundary. Agent-owned artifacts may include private snapshot/worktree, log and temporary state. Project mirrors/runtimes are shared; upload staging belongs to an upload ID until finalization; patch/review artifacts may be independently retained. Reaping a snapshot must not delete shared project data or external review evidence.

Before deletion, validate canonical containment under configured managed root, reject traversal and unsafe symlinks, and do not follow symlinks. A record-supplied absolute path alone is not authority. Record deletion outcomes/retained paths; missing artifacts are idempotent success only under defined policy. Inventory separately records state, transcript, summary, logs, snapshot/worktree, patch/review payload and shared resources.

### 3.7 Reaping and recovery

Reaping MUST be durable and retryable:

1. Resolve canonical ID/revision; verify worker inactivity, eligibility and patch disposition.
2. CAS to `reaping`, persisting operation/idempotency key and intended artifact manifest before deletion.
3. Delete only authorized owned artifacts idempotently; persist per-artifact outcomes/errors.
4. On retry/restart, reconcile intent with actual artifact presence and continue safely. Missing snapshot alone does not prove completion.
5. CAS to `reaped` tombstone only when required cleanup completes or policy-approved retention is recorded. Failure remains observable and retryable; never erase the only recovery record.

Batch returns one structured outcome per requested ID and proceeds after independent errors. Patch disposition racing with reap is serialized by revision/CAS. Startup recovery SHOULD scan authoritative interrupted-reap records; orphan-artifact discovery is report-only unless separately authorized.

### 3.8 JSON versus SQLite criteria

JSON is reasonable with a clear single controller/service writer, modest record/query scale, dependable cross-process locking and a preference for inspectability/simple backup. It still needs schema/version rules, locking, revisions, recovery and validation. SQLite is favored by demonstrated multi-process transactional updates, indexed queries, cross-record consistency needs or write contention sufficient to justify operational cost. SQLite requires tested backup/restore, migration, journal/locking and filesystem policies. Compare measured concurrency, volume, query patterns, storage topology, backup requirements and failure recovery. Backend choice MUST NOT change lifecycle semantics; SQLite is not a substitute for defining ownership/reaping.

## 4. Acceptance tests for the desired contract

These are proposed additions, not claims of current coverage:

1. Schema: create/read current version; reject malformed/missing/unsupported version; migrate fixtures idempotently while preserving history and compatible fields; failed migration leaves prior data recoverable.
2. Authority/resolution: local and remote exact, unique-prefix, ambiguous and unknown resolution agree; orphan artifacts are not listed; no arbitrary selection.
3. Concurrency: same expected revision yields one transition winner and stale conflict; concurrent history appends are lossless; crash at write boundaries leaves valid old/new data, never truncated JSON.
4. Create: injected multi-file failure is recoverable and never reported complete; duplicate create cannot overwrite/resurrect a tombstoned ID.
5. Lifecycle: illegal transitions rejected; live worker cannot reap; unresolved submissions protect review artifacts; acknowledgement/rejection binds to submitted head and is auditable; child continuation preserves parent history/snapshot.
6. Tombstone: reap retains minimal record; retry is idempotent/already-reaped; ID reuse and retention/erasure policy are explicit.
7. Artifact safety: traversal, symlink, outside-root, shared project and externally owned review-artifact tests never delete unauthorized content and record outcomes.
8. Fault injection: crashes before intent, after intent, during deletion, after cleanup/before tombstone, and in batches resume safely without unrelated deletion or lost intent.
9. Remote parity: local/service outcomes match; transport failure never falsely acknowledges transition.
10. Backend parity: run identical contract suite against JSON and SQLite adapters if both exist; test backup/restore and supported filesystem assumptions.

## 5. Open questions

1. Canonical layout `agents/<id>/state/record.json` or incremental evolution of `<state_root>/<id>/metadata.json`? Where do logs, snapshots and reviews live?
2. Required record/history envelope fields and unknown-field compatibility policy?
3. Supported storage filesystems/process topology; can JSON use robust cross-process locks?
4. Exact reap-eligible states (especially halted vs stopped/failed), terminal and recoverable states?
5. Tombstone retention duration, privacy/erasure rules, and ID reuse?
6. Which patch dispositions authorize deletion of each artifact class; are review artifacts external to this store?
7. How represent reap operation IDs, retry ownership, stale intents and operator override?
8. History in record/transaction, separate JSON, JSONL or database table; size/retention limits?
9. What measured agent count, concurrency, query, backup/restore RPO/RTO and topology justify SQLite?
10. Required response taxonomy and logging for invalid/unknown/ambiguous/ineligible IDs across transports?

## 6. Meeting decisions to record

Record date, decision, owner, rationale and follow-up; unresolved items remain open. The architecture proposal’s agenda calls for decisions on:

- Canonical per-agent root and state-record location.
- Tombstone retention, privacy and ID-reuse policy.
- Patch dispositions authorizing deletion and independently retained artifacts.
- JSON-first with locking/revision and SQLite evaluation trigger, or SQLite based on workload/operations evidence.
- Local/remote topology and storage/locking guarantees.
- Schema/version/migration and history storage approach.
- Reap eligibility, recovery and batch-result taxonomy.

No meeting outcomes were supplied in the architecture proposal. These are decisions to make, not decisions already made.
