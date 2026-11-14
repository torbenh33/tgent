# Phase 2: Unify local submission and generate review mbox on retrieval

This document is the implementation brief for Phase 2 of `remote-agent-git-submission-plan.md`. Read the main plan for overall goals and repository terminology. This phase configures local-agent submission to use the same managed bare mirror and Git publication path as remote-agent submission, then generates mbox only when pending submissions are retrieved for the existing mail-based review workflow.

## Repository roles and source of truth

- **Local agent work repository:** the per-agent local clone/worktree that supplies the commit to submit.
- **Remote project bare mirror:** the shared managed bare mirror used by remote agents and local-agent submissions. It stores accepted submission commits and immutable refs; other agents can fetch these refs.
- **Host project repository (`_PROJECT_ROOT`):** used by the existing host review/import workflow. Do not change its active branch, `HEAD`, index, or checked-out files during pending retrieval/import.
- **Review store and Maildir:** continue to use the established mbox-based review records, notifications, and reply behavior.

Both local and remote agents must use one Git submission implementation: resolve the submitting work repository from service-managed agent/session state, fetch the submitted commit into the managed remote project bare mirror, and retain cover-letter/submission metadata in the existing service-managed submission metadata store. The Git ref is the source of truth for the submitted commit; the service metadata record is the source for cover-letter and provenance fields. Generated mboxes are temporary compatibility/review representations, not submission transport or source of truth. Locally authored mbox-series review remains unchanged.

## Required behavior

1. Configure local-agent sessions with the managed remote project bare mirror and the service-recorded work-repository path needed by submission. Remove the separate local mbox-export submission branch; both local and remote submission use the same Git fetch/publication implementation and submission metadata format.
2. Update the existing single and bulk pending-submission retrieval flows to read published Git refs from the remote project bare mirror and corresponding cover-letter/provenance records from the service-managed submission metadata store. Do not expect or download a `.patch` artifact for new Git submissions.
3. Generate an mbox at retrieval time for the existing review import path. Include the submitted commit series and the exact cover-letter message recorded at submission. Preserve enough agent, base, submitted commit, published ref, and identity provenance for review records and diagnostics.
4. Keep the existing review importer, Maildir, notification, reply, and agent lifecycle acknowledgement behavior where possible. Do not redesign mail events in this phase; that is a later decision/phase.
5. Handle missing refs and malformed/inconsistent submission metadata as explicit per-submission errors. Do not silently generate an incomplete series or mutate the host project checkout.
6. Make repeated retrieval safe and deterministic for a given published ref and metadata. Preserve current single/bulk partial-failure semantics and report which submissions succeeded or failed.
7. Keep locally authored mbox-series review unchanged. Do not add a second Git publication or artifact-submission path.

## Tests and exit criteria

- Local and remote submission tests prove both paths publish to the same managed mirror using the same service implementation and metadata format; no separate local mbox-export submission path remains.
- Single and bulk pending-submission tests generate reviewable mboxes from published refs and preserve the cover-letter message and provenance from service-managed metadata.
- Cover missing refs, invalid/missing metadata, duplicate/repeated retrieval, and generation/import failures; verify failure handling does not change host branch, `HEAD`, index, or checked-out files.
- Existing local mbox review tests, including bulk report tests, continue to pass unchanged.
- Verify notification/reply and lifecycle acknowledgement semantics are preserved for remotely submitted Git refs.
- Run focused adapter, protocol, service, integration, importer, and review tests; then run the full test suite.
- **Exit criteria:** local and remote submissions use one Git publication codepath and the same managed mirror; submissions are reviewable through the existing single and bulk pending-submission flows; generated mboxes retain cover-letter content/provenance; locally authored mbox review remains intact.

## Out of scope

- Replacing Git refs/provenance with mbox or bundle submission artifacts.
- Redesigning mail-based event generation or notifications; decide separately after Phase 3 works.
- Changing locally authored mbox review.
- Updating the operator's active checkout, branch, index, or checked-out files.
