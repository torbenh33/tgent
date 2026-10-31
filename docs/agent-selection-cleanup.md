# Agent ID selection and cleanup: specification

Status: review draft. This document specifies a consistent contract for local, remote, and batch lifecycle operations, using `docs/agent-management-architecture.md` as the architectural direction. It is not a claim that the contract is implemented.

## Scope and terminology

An **agent ID** is the canonical ID in an authoritative agent record. Callers may supply that full ID or a shortened hexadecimal prefix. A **candidate** is a canonical ID in the authoritative scope considered for resolution. A **submitted patch** remains protected review evidence until a recorded disposition authorizes cleanup.

The architecture proposes state-store resolution, canonical downstream IDs, explicit patch disposition, recoverable reap, and per-ID batch results. The requirements below refine those proposals; they do not prescribe a storage migration.

## Observed current behavior (inspection; not desired contract)

- Local IDs are minted with `secrets.token_hex(16)` in `agent_controller.py` (32 lowercase hex characters).
- `AgentController.resolve_agent_id` tries `store.get_metadata` for an exact ID first. If that fails, it validates hexadecimal spelling and scans real, non-symlink directories under `state_root` (excluding `logs`), reads metadata, and selects a case-insensitive unique name prefix. It raises on no match or ambiguity. It does not log candidates; the scan is filesystem-backed, not a canonical state listing.
- `test_agent_controller.py` covers exact ID, unique prefix via status/history, ambiguous prefix, unknown prefix, and invalid punctuation. Test IDs are shorter than production IDs.
- Local `reap_agents` resolves the entire input list before its per-agent loop, so one resolution error may fail the batch before per-item reporting. After resolution, duplicates yield `excluded/duplicate_id`; unresolved submitted patches and `awaiting_review` are excluded; only `stopped` or `failed` records are reaped. Other states are excluded. Per-item store/controller/OS exceptions are returned as `excluded` with exception text in `reason`.
- Applied acknowledgement and rejection require an inactive agent in allowed states, are tied to `submitted_head`, and do not remove submitted metadata/artifacts. The methods themselves obtain metadata by supplied ID without calling the prefix resolver.
- `RemoteAgentService` delegates lifecycle handlers to the same `AgentController`, including reap and batch reap; dispatch validates requests then wraps exceptions as protocol errors. This makes the controller the apparent resolver for delegated calls, but does not establish validation/order across every client entry point. Remote patch artifacts are separate `<agent-id>.patch` files; patch listing includes submitted-head and digest metadata. Batch stop/restart also resolve before their loop.

These are observations of inspected paths, not guarantees about every adapter or backend.

## Desired contract

### Resolution and canonical identity

1. Resolve only against canonical IDs in the authoritative state source for the requested scope. Artifact filenames/directories alone are never identity records. A candidate must be a valid/readable record with internally consistent canonical ID.
2. Validate requests as supported full IDs or non-empty hexadecimal prefixes. Match hex case-insensitively unless maintainers choose otherwise; return canonical stored spelling. Do not trim/reinterpret malformed input. Set a minimum prefix length (open decision below); do not imply one-character prefixes are safe.
3. Exact canonical full-ID match wins before prefix matching, even if it prefixes another ID. Otherwise one matching prefix resolves, zero is `unknown`, and multiple are `ambiguous`. Never choose by enumeration order.
4. Resolve at the authoritative local/remote service boundary before downstream full-ID validators and before mutation. Use the canonical ID for storage, runtime, artifacts, and audit. Remote resolution must use the authoritative remote scope.
5. Resolution failure is distinct from eligibility failure. Invalid, unknown, and ambiguous selection cause no mutation.

### Required selection logging

Emit a structured event for every resolution attempt, including failures: `operation`, `requested_id`, `scope` (controller/store identity, no secrets), `candidate_ids` actually examined in deterministic order, `match_type` (`exact`, `prefix`, `none`, `ambiguous`, `invalid`), `selected_id` (canonical ID or null), and request/correlation ID when available. Candidates are authoritative records, not directory names. Never log histories, patch content, credentials, or unrelated metadata. Batch events also identify input item/index.

### Cleanup eligibility and preservation

- Cleanup eligibility is checked against current canonical state immediately before deletion. Worker inactivity alone is insufficient; live workers are never reaped. Current local behavior permits only `stopped` and `failed`; the final supported states must be explicit and race-safe.
- `awaiting_review`, or any submitted patch without a matching explicit disposition, is ineligible. Applied acknowledgement or explicit rejection tied to the current submitted head may permit later cleanup but does not itself delete patch, review series, submitted metadata, or evidence. A newer head invalidates an older disposition. Rejection is not application.
- Preserve submitted patches/review evidence by default. Specify per artifact whether an authorized reap removes controller-owned snapshot/state while retaining reviewable evidence; never erase the sole reviewable copy. Never remove a shared project repository/mirror as agent-owned data.
- Remove only paths proven owned by the canonical agent, with symlink and containment checks. Define treatment of state, history, snapshot/worktree, logs, patch bytes, sockets, and temporary files. Record cleanup intent/results for safe retry; partial failure is not success.

### Failures and batch reports

Single-item success identifies the canonical ID. Typed failures distinguish invalid, unknown, ambiguous, ineligible, and operational cleanup failure. Never report a resolution failure as ineligibility or a failed cleanup as success.

Batch operations process each input independently and return one ordered result per input, including malformed, unknown, and ambiguous inputs. One resolution failure cannot prevent unrelated attempts. Report duplicates explicitly and never reap a canonical ID twice. Distinguish `reaped`, `excluded` (stable eligibility reason), and `error` (operation failure); include requested and canonical IDs when known. Report partial success at top level. Exact API labels remain a decision, semantics do not.

## Acceptance-test cases

### Resolver

1. Exact full ID wins even where another ID begins with it.
2. Unique prefix resolves; mixed-case hex follows the chosen case policy.
3. Multi-match prefix is ambiguous and lists all matches; shuffled enumeration has identical outcome.
4. No-match is unknown; empty, non-hex, overlong, and below-minimum forms are invalid (or agreed typed equivalent) without mutation.
5. Orphan artifact/directory is not a candidate. Corrupt/inconsistent record behavior is explicit.
6. Exact, prefix, invalid, unknown, and ambiguous attempts log required fields and selected canonical ID/null.
7. Remote prefixes resolve at service boundary before full-ID-only validation, and canonical IDs drive state/artifact paths.

### Cleanup and batches

8. `stopped`/`failed` without unresolved submission are eligible; active, waiting, halted, awaiting-review, and other states follow finalized eligibility table.
9. Unresolved submission is preserved/excluded despite inactivity. Matching applied acknowledgement or rejection permits only documented cleanup and preserves review evidence. Stale-head disposition does not unlock a newer head.
10. Traversal, symlink, outside-root, and shared project/mirror paths are never removed.
11. Injected failures at every cleanup stage do not report reaped, preserve retry state, and retry idempotently reports artifact outcomes.
12. Mixed batch of valid, invalid, unknown, ambiguous, ineligible, duplicate, and failing IDs returns ordered per-input results and proceeds with independent eligible IDs.
13. Concurrent state/worker changes cannot cause deletion after eligibility changes; concurrent reap is safe.
14. Local/remote single and batch APIs have equivalent resolution, eligibility, canonical reporting, preservation, and failure semantics.

## Uncertainties and meeting decisions required

1. **ID schema:** Local generation is 32 hex characters; architecture notes IDs are not necessarily SHA-1. Is 32 the durable schema, or must legacy lengths remain?
2. **Prefix policy:** Decide minimum prefix length, case sensitivity, and exact full-ID case policy. Architecture does not specify these.
3. **Authoritative scope:** Confirm candidate records, tombstone/reaped inclusion, and corrupt-record treatment. Architecture proposes a state store; current resolver scans directories.
4. **Eligibility:** Current reap permits stopped/failed only. Decide whether halted may be reaped and whether rejection/application allows snapshot/state removal.
5. **Retention:** Decide locations/periods for patch bytes, review import, logs, history, snapshot, and tombstone. Architecture proposes a reaped tombstone; current reap artifact details need further verification.
6. **Batch shape:** Decide stable error codes, top-level partial-failure indicator, and representation of input validation errors. Current pre-loop resolution obstructs full per-ID reporting.
7. **Remote parity:** Verify protocol schemas and client validation for all local/remote single/batch operations; service delegation alone is insufficient evidence.
8. **Logs:** Confirm privacy/retention and access controls for full candidate IDs.

Meeting outcome: **pending**. The open items are not settled contract details; obtain maintainer decisions before implementation or record approved defaults in a revision.
