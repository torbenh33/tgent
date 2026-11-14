# Phase 2: Generate review mbox on pending-submission retrieval

This document is the implementation brief for Phase 2 of `remote-agent-git-submission-plan.md`. Read the main plan for overall goals and repository terminology. Phase 2 consumes the Git submissions published by Phase 1 and generates mbox only when pending submissions are retrieved for the existing mail-based review workflow.

## Repository roles and source of truth

- **Remote project bare mirror:** authoritative store for accepted submission commits and immutable refs under the internal submission-ref namespace. Other agents can fetch these refs.
- **Host project repository (`_PROJECT_ROOT`):** used by the existing host review/import workflow. Do not change its active branch, `HEAD`, index, or checked-out files during pending retrieval/import.
- **Review store and Maildir:** continue to use the established mbox-based review records, notifications, and reply behavior.

Git refs and submission provenance in the remote project bare mirror are the source of truth. Generated mboxes are temporary compatibility/review representations, not submission transport or source of truth. Local-agent submissions and locally authored mbox-series review must remain unchanged.

## Required behavior

1. Update the existing single and bulk pending-submission retrieval flows to read published Git refs and associated submission metadata/provenance from the remote project bare mirror. Do not expect or download a `.patch` artifact for new Git submissions.
2. Generate an mbox at retrieval time for the existing review import path. Include the submitted commit series and the exact cover-letter message recorded at submission. Preserve enough agent, base, submitted commit, published ref, and identity provenance for review records and diagnostics.
3. Keep the existing review importer, Maildir, notification, reply, and agent lifecycle acknowledgement behavior where possible. Do not redesign mail events in this phase; that is a later decision/phase.
4. Handle missing refs and malformed/inconsistent submission metadata as explicit per-submission errors. Do not silently generate an incomplete series or mutate the host project checkout.
5. Make repeated retrieval safe and deterministic for a given published ref and metadata. Preserve current single/bulk partial-failure semantics and report which submissions succeeded or failed.
6. Leave local-agent submission and local mbox review behavior unchanged. Do not add a second Git publication or artifact-submission path.

## Tests and exit criteria

- Single and bulk pending-submission tests generate reviewable mboxes from published refs and preserve the cover-letter message and provenance.
- Cover missing refs, invalid/missing metadata, duplicate/repeated retrieval, and generation/import failures; verify failure handling does not change host branch, `HEAD`, index, or checked-out files.
- Existing local mbox review tests, including bulk report tests, continue to pass unchanged.
- Verify notification/reply and lifecycle acknowledgement semantics are preserved for remotely submitted Git refs.
- Run focused adapter, protocol, service, integration, importer, and review tests; then run the full test suite.
- **Exit criteria:** new remote submissions are reviewable through the existing single and bulk pending-submission flows, generated mboxes retain cover-letter content and provenance, and locally authored mbox review remains intact.

## Out of scope

- Replacing Git refs/provenance with mbox or bundle submission artifacts.
- Redesigning mail-based event generation or notifications; decide separately after Phase 2 works.
- Changing local-agent submission or local mbox review.
- Updating the operator's active checkout, branch, index, or checked-out files.
