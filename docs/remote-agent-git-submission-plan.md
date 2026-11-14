# Remote-agent Git submission migration plan

**Status:** proposal only; no runtime changes in this document's revision.

## Goal

Replace remote agents' mbox-based patch submission with Git-based submission as the first migration step. The proposed transport is a controlled Git fetch of the agent's submitted commit, not a bundle. The submission also carries the cover-letter message explicitly. This plan traces the existing lifecycle and identifies code and tests to change. It does not propose implementing the final review/ref-import behavior until the operator approves the design.

## Current lifecycle and storage boundaries

1. **Agent work and commit creation.** `agent_worker.py` exposes `submit_patch_queue`; it validates the submitted HEAD and delegates to the configured submit callback. In remote mode the service callback is `RemoteAgentService._submit_patch` (`remote_agent_service.py`). The agent worktree is a private clone created by `RemoteAgentService._start_agent`, with the remote project's bare mirror passed as a reference object store. `agent_sandbox.build_sandbox_command` mounts that bare mirror read-only.
2. **Submission metadata.** `_submit_patch` currently runs `git format-patch --stdout --thread=shallow --numbered --cover-letter <base>..<head>`, decorates the cover letter from commit subjects and cached agent history, sets `Reply-To`, then serializes the messages to mbox. It writes `<agent-id>.patch` in `config.patches_dir`, atomically, with a 16 MiB bound. The response and `_patches` metadata include base, submitted head, byte length, SHA-256, and artifact name. In the proposed flow, the agent instead submits its base, exact commit ID to submit, and an explicit cover-letter message; the service fetches the commit from the authenticated agent repository.
3. **Git fetch and submission metadata.** Replace `.patch` artifact transfer with a service-controlled Git remote/fetch path. The source repository and remote configuration must come from trusted agent/session state, not a URL or filesystem path supplied in submission content. The importer fetches the requested commit into a private staging ref, validates it, then publishes an immutable submission ref. Submission metadata includes agent/series identity, base, submitted commit, cover-letter message, and fetch/import status. There is no mbox or bundle artifact to rsync or retain; retain the submission metadata and provenance record instead.
4. **Review import.** `mcp_agent.review_remote_agent` and the bulk path currently call `mcp_tgit_review.import_patch_series`. That function checks size, digest, identities, Message-IDs, and base commit; applies all patch messages with `git am` in a temporary clone; stores the original mbox and revision/provenance in review state; and imports messages into the review Maildir. No operator working tree is checked out or changed. The existing host project repository is `_PROJECT_ROOT` in `mcp_agent.py`, passed as `repo_path`; it is the repository in which the declared base is currently resolved. Under the proposed Git flow, accepted submissions are first published to the remote project bare mirror for other agents to fetch. Any subsequent host-side review import is separate and must not change the operator's active checkout. The remote project bare mirror is under `project_root_dir`; unlike the current setup, it will also hold accepted submission refs.
5. **Review and follow-up.** Review records/manifests, retained mbox revisions and Maildir messages support the existing mail-based review/reply workflow in `mcp_tgit_review.py`. Preserve that workflow unless the operator separately approves changing it. The current worktree remains the operator's checkout and must never move as a side effect of import.

## Git repositories and terminology

There are three distinct Git repositories in this flow. “Staging ref” and “submission ref” below are refs inside a repository; neither is a separate repository.

1. **Agent work repository** — the private, per-agent clone/worktree created by `RemoteAgentService._start_agent`. The agent creates commits here, and the submitted commit ID names a commit in this repository. The trusted importer fetches from this repository using an authenticated, service-configured source. The agent has no direct access to the host project repository.
2. **Remote project bare mirror** — the bare mirror under `project_root_dir`, used as an object reference store by agent work repositories and mounted read-only inside agent sandboxes. This is where accepted submission commits and immutable submission refs must be published so other agents can fetch and use them. Agents retain read-only access; only the trusted submission importer may write submission objects and refs here.
3. **Host project repository** — the existing shared repository at `_PROJECT_ROOT` on the host. It is the operator's checkout and the repository used by host-side review tools. It is not the shared agent-visible submission store. Any review import into it is a separate, controlled operation; its branches, `HEAD`, index, and checked-out files must not be changed.

In short: **agent work repository → fetch and validate → remote project bare mirror → agents fetch accepted submissions**. The host project repository remains distinct and is not the agent-visible publication target. Do not call all of these “the project repository” or “the remote repository”; use the three names above to identify them unambiguously.

## Actors and boundaries for the proposed Git flow

- **Agent worker:** submits a committed HEAD and its declared base; cannot write the shared host repository or the remote read-only bare mirror.
- **Remote service/controller:** authenticates/validates agent identity and bounds; accepts the submitted commit ID and cover-letter message. It must not accept an arbitrary fetch URL/path or gain host-project write access merely to facilitate submission.
- **Git fetch path:** uses a service-configured, authenticated remote for the agent's private repository. The trusted importer fetches the requested commit directly into a private staging ref in the remote project bare mirror, never by trusting a mutable branch name. It does not transfer a generated mbox or bundle artifact. This mirrors in reverse the existing agent setup, where the agent worktree uses the remote project's bare mirror as a read-only object reference.
- **Trusted submission importer:** resolves the authenticated agent's configured fetch source, validates fetched objects against the declared base, and publishes accepted objects and immutable submission refs to the remote project bare mirror. It is the sole writer for submission refs there. Host review tools may separately import accepted submissions into the host project repository without changing its active checkout.
- **Review store/Maildir:** keeps audit/review records and existing mail notifications/replies; Git submission should not silently discard review metadata or mail behavior.
- **Remote bare mirror:** remains a read-only object reference for agents; it is not the host shared repository and must not become a second review repository.

## Desired first-step flow

1. Agent submits an immutable commit identity (commit ID, base, and series/agent identity) plus an explicit cover-letter message through the existing submission operation. The service does not generate an mbox.
2. The trusted importer resolves the agent's fetch source from authenticated service state and fetches the requested commit directly into a private staging ref in the **remote project bare mirror**. The source endpoint is service-configured; submission data cannot select a URL, path, command, or destination ref. The fetch must obtain the exact commit and its required ancestry without relying on a mutable branch name. This is the reverse of the existing agent setup: agent worktrees read objects from the project's bare mirror, and the trusted importer fetches submitted objects from the agent's configured repository into that same mirror.
3. The importer validates the staged objects in place: connectivity, commit identity, declared base ancestry/range, and absence of unsupported gitlinks. It also bounds the cover-letter message and validates submission metadata. Failed validation removes the private staging ref and publishes no submission ref. Unreachable objects left by a failed fetch are harmless and can be collected by normal Git garbage collection.
4. After validation, the importer promotes the validated commit to an immutable, submission-scoped ref in the same **remote project bare mirror**. No separate temporary repository or new project/review repo is created. No branch or `HEAD` in the host project repository is updated. The published ref makes the accepted commit available to other agents through their configured mirror access.
5. Review manifest/provenance records agent, base, submitted commit, imported commit, cover-letter message, and ref. Preserve a mail/review summary sufficient for the existing review workflow. No Git bundle or mbox is required as the submission transport; an export format may be added later only for compatibility if needed.

## Invariants and non-goals

- No pushes to upstream remotes, branch updates, checkout/reset/merge/rebase of the operator worktree, or agent write access to the shared host repository.
- Validate before publishing refs; reject malformed, oversized, incomplete, unrelated, or mismatched submissions. Never trust a submitted fetch URL, artifact filename, remote branch name, or destination ref. Bound and safely store the cover-letter message.
- Idempotent retry for the same agent/submission/digest; conflicting content for an occupied immutable ref is an error. Revisions remain auditable.
- Keep the remote project bare mirror mounted read-only inside agent sandboxes. The trusted importer may publish accepted objects and refs to that mirror; agents themselves cannot write to it. Do not conflate this mirror with `_PROJECT_ROOT`.
- Preserve existing review mail/reply semantics and agent lifecycle acknowledgments unless separately changed by explicit decision.
- Not in scope for this first step: submodules/gitlinks, arbitrary uncommitted working-tree changes, changing local-agent patch submission, upstream publication, automatic rebase/merge, mirror garbage collection, and redesigning email review itself.

## Phased implementation plan

Make each phase a focused, reviewable change that replaces one part of the old path rather than maintaining parallel implementations. Avoid changing local-agent submission or the shared local mbox-review support. The first phase deliberately stops at Git publication and manual retrieval; it does not attempt to automate operator review or mail events.

### Phase 1: Replace remote mbox submission with Git publication

- Change the remote submission request to carry the exact commit ID, declared base, and explicit cover-letter message, with strict schema and size validation.
- Replace remote mbox generation and `.patch` artifact storage with a trusted fetch from the authenticated agent work repository into a private staging ref in the remote project bare mirror.
- Validate connectivity, exact commit identity, declared base ancestry/range, and absence of unsupported gitlinks in the remote project bare mirror. Remove the staging ref on failure; after validation, publish an immutable, submission-scoped ref and record the commit/base/cover-letter metadata.
- Do not retain a parallel remote mbox submission path. For this interim phase, retire remote pending-patch artifact retrieval and automated remote mbox review; operators inspect submissions by manually fetching an accepted ref from the remote project bare mirror into the host project repository. Document a safe fetch procedure that does not change the host repository's active branch, `HEAD`, index, or checked-out files. Other agents can fetch the same published ref from the mirror. Local-agent submission and local mbox review remain unchanged.
- **Exit criteria:** service/protocol and integration tests prove fetch-source selection is service-controlled, invalid submissions publish no submission ref, valid refs are available from the mirror, retries are safe, cover-letter metadata is retained, and the host project repository is unchanged. The manual fetch procedure is documented, and obsolete remote `.patch` generation, storage, and artifact-retrieval code is removed rather than left as a parallel path.

### Phase 2: Generate review mbox on pending-submission retrieval

- Change the pending-submission retrieval path to read accepted Git refs and provenance from the remote project bare mirror, then generate the mbox needed by the existing mail-based review workflow at retrieval time.
- Include the submitted cover-letter message in the generated series. Keep generated mbox data as a compatibility/review representation, not as the submission transport or source of truth.
- Preserve the existing review import, Maildir, notification, and reply behavior where possible; keep locally authored mbox-series review unchanged.
- **Exit criteria:** single and bulk pending-submission flows generate reviewable mboxes from published refs, preserve cover-letter content and provenance, and retain existing mail/reply semantics. Tests cover missing refs, invalid metadata, retry/idempotence, and failure behavior.

### Phase 3: Add or adapt Git-based mail events, if needed

- Only after Phase 2 is working, decide whether mail events should be generated directly from Git submission metadata/ref state or continue to use the generated mbox review representation.
- Implement the chosen event behavior without restoring mbox generation to the submission operation or duplicating the submission lifecycle.
- **Exit criteria:** event behavior and notification/reply compatibility are explicit, covered by focused tests, and do not affect Git publication or the host project's active checkout.

### Phase 4: Finalize documentation and remove remaining obsolete code

- Remove only remote artifact/Message-ID code proven unused after Phases 1–3; retain shared mbox support used for local review and any generated review mbox path still required for compatibility.
- Update `docs/remote-agent-review.md`, protocol documentation, and tests to describe the implemented contract and the manual-fetch transition if it remains relevant.
- Run focused service, protocol, integration, importer, review, and sandbox tests, followed by full pytest.
- **Exit criteria:** there is one remote submission source of truth (Git refs and provenance), no remote submission caller depends on transferred `.patch` artifacts, local mbox review remains intact, and the complete test suite passes.

## Existing tests and assertions to revise

- `test_remote_agent_service.py::test_submit_patch_generates_and_stores_mbox_without_local_review_export`: currently asserts `.patch` artifact name, mbox framing/number of messages, `Reply-To`, decorated cover subject/blurb, count, and that `format-patch` was invoked. Replace these with exact submitted commit/base and cover-letter preservation, validation of cover-letter bounds, controlled fetch-source selection, fetch into a non-public staging ref, and absence of `format-patch`/mail mutation.
- `test_remote_agent_integration.py::test_patch_submission_and_retrieval_end_to_end`: replace artifact-download assertions with a fetch-based submission test that verifies the exact commit becomes available to the importer, the cover-letter message is preserved, and final refs are created only after validation; ensure `main`/current branch remains unchanged.
- `test_remote_agent_adapter.py` retrieval tests: retire `.patch` artifact retrieval coverage if the adapter no longer transfers submission artifacts. Add coverage for authenticated/configured fetch-source resolution and ensure submission input cannot supply arbitrary URLs, paths, or destination refs.
- `test_remote_agent_protocol.py`: update submission and `patches`/pending-patches schemas as appropriate to include commit/base/cover-letter metadata and remove artifact fields; retain bounds and reject extra/invalid fields.
- `mcp_agent.py` review tools have no located direct unit test for `import_patch_series`; add focused single and bulk integration coverage for fetch validation, ref/provenance, cover-letter preservation, retry/idempotence, and failure atomicity.
- `mcp_tgit_review.py` mail workflow tests (including `tests/test_review_bulk_report.py`) should continue to pass unchanged for locally authored mbox series. Separate local mbox review from remote Git submission rather than deleting shared mail support.
- `test_agent_sandbox.py::test_project_mirror_is_bound_read_only_at_alternates_path` asserts read-only mirror mounting; preserve it. Also assert Git submission cannot obtain a writable host-repository path.
- `test_remote_agent_upload.py` covers source snapshot upload, not submission artifacts; it should remain unchanged unless protocol reuse actually touches it.

## Acceptance criteria

- Remote submission uses a controlled Git fetch, not an mbox, bundle, or transferred artifact. Submission metadata includes the declared base, exact commit ID, and cover-letter message.
- The service resolves the authenticated agent's configured fetch source; single and batch submission handling cannot be redirected by caller-provided URLs, paths, or refs.
- Validation of objects fetched into a private staging ref in the remote project bare mirror rejects unavailable or incomplete objects, wrong-base, unrelated, or unsupported submissions without publishing submission refs; failed staging refs are cleaned up.
- Valid commits become reachable from submission-specific refs in the remote project bare mirror, with auditable manifest/provenance; duplicate retry is safe. The host project repository's branch, `HEAD`, index, and checked-out files remain unchanged. No second host review repository is created.
- Agents cannot write refs to the remote project bare mirror or the host project repository. Submission input cannot select an arbitrary fetch source or destination ref. The trusted importer alone publishes submission refs to the remote project bare mirror; any separate host-side review import is controlled independently.
- Existing review mail/reply flow for locally submitted mbox series remains passing, and remote operator notification behavior is explicitly preserved or deliberately retired.
- Focused service, adapter, protocol, integration, import, and sandbox tests plus full pytest pass.

## Open decision

The code supports one design question that materially affects compatibility: should review refs point to the agent's original commit IDs (preserving submitted SHA and commit metadata) or to commits reconstructed by the host importer? Current mbox `git am` reconstruction necessarily creates host-side commit IDs, while metadata retains the agent's submitted head. A fetch can preserve original IDs when it obtains the requested commit and required ancestry; the importer must verify that the fetched object is exactly the declared submission. Resolve this before implementation. Other choices still need agreement on authenticated fetch-source resolution and retry identity; the cover-letter message is part of the submission request and must be retained in review provenance.
