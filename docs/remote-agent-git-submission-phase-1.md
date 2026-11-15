# Phase 1: Replace remote mbox submission with Git publication

This document is the implementation brief for Phase 1 of `remote-agent-git-submission-plan.md`. It is intentionally limited to the first step: publish an agent's submitted commit to the remote project bare mirror and allow manual retrieval. Do not implement later phases in this change.

## Repository roles

- **Agent work repository:** per-agent repository created and recorded by `RemoteAgentService`; this is the fetch source. Resolve its path from service-managed session state, never from submission fields.
- **Remote project bare mirror:** under `project_root_dir`; this is the publication destination. The importer may write submission objects and refs; agent sandboxes continue to have read-only access. Other agents must be able to fetch published submission refs from this mirror.
- **Host project repository:** `_PROJECT_ROOT`; do not change its branches, `HEAD`, index, or checked-out files during submission.

The flow is **agent work repository → Git file-transport fetch → remote project bare mirror → agents/manual operator fetch**. A submission ref is a ref within the remote project bare mirror, not a separate repository.

## Required behavior

1. Change the remote submission request to carry the exact commit ID, declared base, and explicit cover-letter message. Apply strict request schema and field-size bounds. The request must not choose a source URL/path, command, artifact name, or destination ref.
2. Resolve the agent work repository path from the service-managed state for the submitting agent/session. Fetch the requested commit using Git's local file transport directly into an internally generated, immutable, versioned submission ref (`refs/agent-submissions/<agent-id>/v1`, `/v2`, and so on) in the remote project bare mirror. The service assigns versions; agents cannot select a version or ref. Do not use a bundle or generate an mbox during submission.
3. Phase 1 performs no additional checks of the submitted commit, ancestry, base, connectivity, or gitlinks beyond rejecting an empty queue (`head_commit == base_commit`). A successful fetch is the submission operation. A failed fetch must not leave a published submission ref or metadata.
4. Store submission metadata/provenance: agent and submission identity, assigned version, supplied commit ID and base, cover-letter message, and published ref. Retrying the latest submission with the same commit/base/cover-letter is idempotent and reuses its version; a distinct submission receives the next version. Conflicting metadata for an occupied immutable ref is an error.
5. Keep the existing pending-patch retrieval APIs, adapter methods, and review callers unchanged. Since submission no longer creates `.patch` artifacts, these existing flows are expected not to retrieve/review new Git submissions until Phase 2. Do not remove these APIs or add a replacement retrieval path in this phase.
6. Route local and remote controller submissions through the same Git-fetch publication path into the managed bare mirror; do not preserve a separate local mbox submission path. Local setup without a managed bare mirror may fail to submit until it is provisioned with one. Submission must not modify the host project repository.

## Manual inspection and agent access

To inspect a published submission, fetch only its advertised ref from the remote project bare mirror into a submission-scoped ref in the host project repository, for example: `git -C /path/to/host-project fetch /path/to/project-bare-mirror refs/agent-submissions/AGENT:refs/remotes/agent-submissions/AGENT`. Do not checkout, merge, reset, or update a branch. This fetch updates only the explicitly named remote-tracking ref; it leaves `HEAD`, the active branch, index, and checked-out files untouched. The host project checkout is not a publication target.

## Tests and exit criteria

- Protocol/service tests cover the request contract, strict bounds, service-resolved fetch source, internally generated destination ref, successful publication, failed-fetch atomicity, metadata retention, and idempotent/conflicting retries. Assert no `format-patch` or mail mutation occurs during submission. Do not require commit/history validation in this phase.
- Integration tests verify a submitted commit becomes fetchable from its published mirror ref and that the host project repository's branch, `HEAD`, index, and checked-out files remain unchanged.
- Existing adapter retrieval APIs/tests remain present and unchanged; they are not expected to retrieve a `.patch` for a new Git submission.
- Sandbox tests continue to prove agents have read-only access to the remote project bare mirror and no writable host-project path.
- Focused tests pass. No Phase 2 mbox-on-retrieval work, mail-event redesign, local-agent changes, or local mbox-review changes are included.
