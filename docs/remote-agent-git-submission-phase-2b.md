# Phase 2B: Generate review mbox during pending-submission retrieval

This brief covers only retrieval and review compatibility. It assumes Phase 2A has configured local-agent sessions to publish through the same Git path as remote agents. Read `remote-agent-git-submission-plan.md` for repository terminology and overall goals, and `remote-agent-git-submission-phase-2.md` for the preceding Phase 2A publication work.

## Scope and source of truth

- **Remote project bare mirror:** source of submitted commits and immutable versioned refs `refs/agent-submissions/<agent-id>/v1`, `/v2`, and so on.
- **Service-managed submission metadata:** source of agent/submission identity, assigned version, declared base, submitted commit ID, cover-letter message, and published ref. Keep this metadata where Phase 1 stores it; do not move it into the mirror as part of this phase.
- **Host project repository:** existing review/import repository. Retrieval and review import must not change its active branch, `HEAD`, index, or checked-out files.
- **Review store and Maildir:** retain the current mbox-based review representation and mail/reply behavior.

Git refs plus service-managed provenance are the submission source of truth. An mbox is generated only as a compatibility representation for the existing review workflow; it is not submission transport or source of truth. Local-agent publication is already unified by Phase 2A. Locally authored mbox-series review remains unchanged.

## Required behavior

1. Update single and bulk pending-submission flows to enumerate all pending version records and resolve each published versioned ref in the remote project bare mirror. Report agent and version distinctly; do not collapse multiple revisions of one agent into one item. Do not rely on an mbox generated during submission or on a pre-existing `.patch` artifact.
2. At retrieval time, generate the review mbox for the submitted commit series. Preserve the submitted cover-letter message exactly, along with agent identity, declared base, submitted commit, and published ref in review provenance/diagnostics.
3. Keep the existing retrieval and review APIs usable where practical. The existing adapter may continue downloading a generated mbox artifact if that is the least disruptive interface, but the service must generate it from the published ref and managed metadata on demand. Do not add a second submission transport or a separate source of truth.
4. Preserve existing single/bulk partial-failure semantics, review import behavior, Maildir insertion, notifications/replies, and agent lifecycle acknowledgements. Do not redesign mail events; that is a later phase.
5. Missing refs, missing or malformed metadata, and mbox-generation failures must produce explicit per-submission errors. Do not silently omit a submission or report it as successfully fetched. Retrieval/import must not mutate the host project checkout.
6. Repeated retrieval of unchanged ref+metadata must produce a stable review result and remain idempotent through the existing review importer. Distinct versions for one agent must remain separately retrievable and auditable. If generated artifact files are retained for compatibility, define their lifecycle and avoid treating stale files as authoritative.

## Tests and exit criteria

- Single and bulk retrieval generate reviewable mboxes from published refs and service-managed metadata, including exact cover-letter content and provenance.
- Cover missing refs, missing/invalid metadata, generation failures, repeated retrieval, and partial batch failure.
- Assert host project branch, `HEAD`, index, and checked-out files are unchanged through retrieval and review import.
- Preserve notification/reply and lifecycle acknowledgement behavior. Existing locally authored mbox-review tests, including bulk-report tests, continue to pass unchanged.
- Run focused adapter, protocol, service, integration, importer, and review tests, then full pytest when feasible. Report exact commands and complete failure details.
- **Exit criteria:** local and remote Git submissions are both reviewable through existing single and bulk pending-submission flows; generated mboxes preserve cover-letter/provenance; locally authored mbox review remains intact.

## Out of scope

- Changes to Git publication, local/remote submission unification, or submission metadata placement (Phase 2A).
- Mail-event redesign or changing notification/reply semantics (later phase).
- Changes to locally authored mbox-series review.
- Updating, checking out, merging, resetting, or otherwise changing the host project repository's active checkout.
