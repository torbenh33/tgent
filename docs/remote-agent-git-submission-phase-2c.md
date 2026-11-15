# Phase 2C: Versioned submission queues

This brief defines the versioning work for remote-agent Git submissions. Read `remote-agent-git-submission-plan.md` for repository roles and overall design, and the Phase 2A/2B briefs for shared publication and retrieval behavior. Phase 2C adds successive immutable submissions per agent; it must preserve the shared local/remote publication path and Phase 2B review-mbox flow.

## Goal and storage contract

Each agent may submit multiple distinct patch queues over its lifetime. Each accepted queue is an immutable version, addressed by an internally generated Git ref:

```text
refs/agent-submissions/<agent-id>/v1
refs/agent-submissions/<agent-id>/v2
...
```

- The service assigns monotonically increasing versions. Submission input cannot supply a version, ref, path, or source.
- The first accepted non-empty queue is `v1`.
- A retry matching the latest version's commit ID, declared base, and cover-letter message is idempotent and returns that version without advancing the counter.
- A distinct valid queue gets the next version. Existing versions and refs are never overwritten or deleted as part of submission.
- Empty queues (`head_commit == base_commit`) are rejected before creating a ref or metadata, with actionable feedback that the agent should make and commit intended changes in its private repository. Committing there does not change the operator checkout.
- Git commit objects and versioned refs live in the managed remote project bare mirror. Per-version provenance, including the cover-letter message, remains in service-managed submission metadata. Do not move provenance into the mirror in this phase.

## Publication behavior

1. Use the existing unified local/remote submission implementation. Resolve the agent work repository and managed mirror from service-managed session metadata; never accept either from the submission request.
2. Validate the submitted request schema and field bounds, reject an empty queue, and determine whether it exactly retries the latest version or is a distinct submission.
3. For a new version, allocate the next internal version and fetch the requested commit from the registered agent work repository into the corresponding versioned ref in the bare mirror. A failed fetch must not create metadata or a published ref for that version. Do not overwrite any existing versioned ref.
4. After successful publication, persist a per-version metadata record containing at least agent ID, version, base, submitted commit ID, cover-letter message, and published ref. Keep earlier records intact.
5. Return the assigned version and ref. Retries must return the same version; conflicting or inconsistent metadata for an occupied version must fail explicitly.

## Retrieval and review behavior

- Pending-submission enumeration must return all unretrieved/unreviewed versions, not just the latest submission for each agent.
- Single-item retrieval must identify a submission by agent plus version (or an equivalent stable submission ID). Batch retrieval must preserve those identities and report independent per-version outcomes.
- Generate each review mbox from its version's published commit/ref and service-managed metadata, preserving the exact cover-letter message and provenance. Do not collapse versions into one series or overwrite earlier review records.
- Preserve Phase 2B's existing Maildir, notification/reply, partial-failure, idempotency, and host-checkout invariants. Locally authored mbox-series review remains unchanged.
- If legacy records use the pre-versioned `refs/agent-submissions/<agent-id>` layout, handle them through an explicit compatibility path or migration; never silently treat them as `v1` unless that mapping is unambiguous and tested.

## Tests and exit criteria

- First non-empty submission publishes `v1`; a distinct second submission publishes `v2`, and both refs and metadata records remain available.
- Identical retry of the latest submission returns the existing version and creates no extra ref/record. Conflicting metadata for an occupied version fails without changing it.
- Empty submission is rejected before Git fetch, ref creation, or metadata persistence, and returns actionable private-repository commit guidance.
- Fetch failure leaves no published ref or metadata for the attempted version; the next valid submission receives the expected version.
- Single and bulk pending retrieval enumerate and review multiple versions independently, preserve each cover letter, and report per-version errors without losing successful siblings.
- Verify repeated retrieval is idempotent and the host project repository's branch, `HEAD`, index, and checked-out files remain unchanged.
- Run focused controller, service, protocol, adapter, integration, importer, and review tests; then run the full suite when feasible.
- **Exit criteria:** agents can submit `v1`, `v2`, etc. safely; all versions remain immutable and reviewable; retries and empty submissions have clear correct behavior; existing local mbox review remains intact.

## Out of scope

- Changing the commit-object publication transport or moving metadata into Git refs/the bare mirror.
- Redesigning mail events or notification semantics.
- Updating, checking out, merging, resetting, or otherwise changing the host project's active checkout.
- Rewriting or deleting historical submission refs as part of normal submission.
