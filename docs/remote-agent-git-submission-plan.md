# Remote-agent Git submission migration plan

**Status:** proposal only; no runtime changes in this document's revision.

## Goal

Replace remote agents' mbox-based patch submission with Git-based submission as the first migration step. This plan traces the existing lifecycle and identifies code and tests to change. It does not propose implementing the final review/ref-import behavior until the operator approves the design.

## Current lifecycle and storage boundaries

1. **Agent work and commit creation.** `agent_worker.py` exposes `submit_patch_queue`; it validates the submitted HEAD and delegates to the configured submit callback. In remote mode the service callback is `RemoteAgentService._submit_patch` (`remote_agent_service.py`). The agent worktree is a private clone created by `RemoteAgentService._start_agent`, with the remote project's bare mirror passed as a reference object store. `agent_sandbox.build_sandbox_command` mounts that bare mirror read-only.
2. **Mbox generation and metadata.** `_submit_patch` runs `git format-patch --stdout --thread=shallow --numbered --cover-letter <base>..<head>`, decorates the cover letter from commit subjects and cached agent history, sets `Reply-To`, then serializes the messages to mbox. It writes `<agent-id>.patch` in `config.patches_dir`, atomically, with a 16 MiB bound. The response and `_patches` metadata include base, submitted head, byte length, SHA-256, and artifact name. This is generated mail, not a Git object/ref transfer.
3. **Transport and artifact storage.** `RemoteAgentAdapter.retrieve_patch` and batch `retrieve_patches` obtain metadata through `patches`/`pending_patches` then rsync the `.patch` artifact from the remote host. They verify size and digest and atomically retain it locally. Artifacts reside in the remote service's patches directory and an operator-chosen local artifact directory. `mcp_agent.review_remote_agent` handles one agent; the bulk review tool retrieves pending artifacts and imports each.
4. **Review import.** `mcp_agent.review_remote_agent` and the bulk path call `mcp_tgit_review.import_patch_series`. That function checks size, digest, identities, Message-IDs, and base commit; applies all patch messages with `git am` in a temporary clone; stores the original mbox and revision/provenance in review state; and imports messages into the review Maildir. No operator working tree is checked out or changed. The existing shared host project repository is `_PROJECT_ROOT` in `mcp_agent.py`, passed as `repo_path`; it is the repository in which the declared base is resolved. Existing code currently reconstructs commits in the temporary clone, and the submitted head may be a source agent SHA distinct from reconstructed `git am` commit IDs. The remote bare mirror is separate, lives under `project_root_dir`, and is only agent-side reference storage.
5. **Review and follow-up.** Review records/manifests, retained mbox revisions and Maildir messages support the existing mail-based review/reply workflow in `mcp_tgit_review.py`. Preserve that workflow unless the operator separately approves changing it. The current worktree remains the operator's checkout and must never move as a side effect of import.

## Actors and boundaries for the proposed Git flow

- **Agent worker:** submits a committed HEAD and its declared base; cannot write the shared host repository or the remote read-only bare mirror.
- **Remote service/controller:** authenticates/validates agent identity and bounds; can read the agent's private clone and remote mirror, but should not gain host-project write access merely to facilitate submission.
- **Transport adapter:** transfers bounded, verified Git data and metadata, not an executable command or a path supplied by project content.
- **Trusted host review importer:** validates submitted objects against the declared base and imports accepted objects into the existing host project repository. It is not the operator worktree. It remains the sole writer for submission refs.
- **Review store/Maildir:** keeps audit/review records and existing mail notifications/replies; Git submission should not silently discard review metadata or mail behavior.
- **Remote bare mirror:** remains a read-only object reference for agents; it is not the host shared repository and must not become a second review repository.

## Desired first-step flow

1. Agent submits an immutable commit identity (HEAD, base, and series/agent identity) through the existing submission operation, but asks the service to provide Git objects rather than generate an mbox.
2. Service creates a bounded Git bundle or equivalent self-contained Git object transfer for the exact submitted commit range. The transfer must include necessary objects without trusting a mutable branch name; artifact metadata includes format/version, base, head, byte length, and digest.
3. Adapter transfers that artifact with the existing verified, atomic-download pattern. Retry of the same content is idempotent.
4. Host importer validates the bundle in an isolated temporary repository: format, size, digest, object connectivity, commit identity, declared base ancestry/range, and no unsupported gitlinks unless explicitly supported. Only validated objects are imported.
5. Import creates immutable, submission-scoped refs in the **existing shared host project repository**, not in the remote mirror and not in a new project/review repo. The exact naming convention and whether refs name source or reconstructed commits need operator approval; no mutable branch/HEAD or active checkout is updated.
6. Review manifest/provenance records agent, base, submitted head, imported commit, artifact digest, and ref. Preserve a mail/review summary sufficient for the existing review workflow; retain the Git bundle as audit artifact. Mbox may be generated only as a compatibility/export artifact if required, not as the submission transport.

## Invariants and non-goals

- No pushes to upstream remotes, branch updates, checkout/reset/merge/rebase of the operator worktree, or agent write access to the shared host repository.
- Validate before publishing refs; reject malformed, oversized, incomplete, unrelated, or mismatched submissions. Never trust artifact filenames or remote branch names as destinations.
- Idempotent retry for the same agent/submission/digest; conflicting content for an occupied immutable ref is an error. Revisions remain auditable.
- Keep current remote mirror synchronization and its read-only sandbox mount unchanged. Submission import targets the pre-existing `_PROJECT_ROOT` repository only.
- Preserve existing review mail/reply semantics and agent lifecycle acknowledgments unless separately changed by explicit decision.
- Not in scope for this first step: submodules/gitlinks, arbitrary uncommitted working-tree changes, changing local-agent patch submission, upstream publication, automatic rebase/merge, mirror garbage collection, and redesigning email review itself.

## Staged implementation sequence

1. **Approval and compatibility contract:** settle bundle format, head/base/ref semantics, retention policy, bounds, and review-mail compatibility. This plan makes no code change.
2. **Git artifact production:** replace `RemoteAgentService._submit_patch` mbox formatting/header decoration with deterministic bounded bundle creation for the declared base/head; keep response identity and idempotent artifact storage. Update protocol metadata validation.
3. **Transport:** adapt `RemoteAgentAdapter` single/batch retrieval to Git-artifact naming and metadata. Reuse strict size/digest checks and atomic replacement; do not weaken them.
4. **Host validation/import:** replace `import_patch_series` calls in `mcp_agent.review_remote_agent` and bulk review with a dedicated bundle importer. Validate in isolation, then publish only submission refs to `_PROJECT_ROOT`; write provenance and retained artifact. Keep working tree and branches unchanged.
5. **Review integration:** preserve operator-visible notifications and mail workflow by producing a summary/review record from validated commits; remove mbox-specific storage/Message-ID assumptions only after compatibility behavior is agreed.
6. **Cleanup and docs:** remove obsolete remote mbox generation/import code only once no caller depends on it; update `docs/remote-agent-review.md` and related protocol docs. Run full pytest.

## Existing tests and assertions to revise

- `test_remote_agent_service.py::test_submit_patch_generates_and_stores_mbox_without_local_review_export`: currently asserts `.patch` artifact name, mbox framing/number of messages, `Reply-To`, decorated cover subject/blurb, count, and that `format-patch` was invoked. Replace these with bundle format/type, exact base/head, bounded artifact storage, digest/size metadata, deterministic or equivalent content, and absence of `format-patch`/mail mutation in submission.
- `test_remote_agent_integration.py::test_patch_submission_and_retrieval_end_to_end`: currently asserts `.patch` name and that downloaded text contains commit subject and before/after patch context. Replace with byte-identical verified Git artifact retrieval, bundle verification, and expected commit/object availability; later extend through host import/ref creation and ensure `main`/current branch unchanged.
- `test_remote_agent_adapter.py` retrieval tests: assert `.patch` naming, size/digest rejection, atomic replacement, batch transfer, and unavailable/partial metadata. Keep these security guarantees; update filenames and content to bundle artifacts.
- `test_remote_agent_protocol.py`: update `patches`/pending-patches response schema and validation if artifact kind or bundle metadata changes; retain bounds and reject extra/invalid fields.
- `mcp_agent.py` review tools have no located direct unit test for `import_patch_series`; add focused single and bulk integration coverage, replacing mbox Message-ID/import expectations with bundle validation, ref/provenance, retry/idempotence, and failure atomicity.
- `mcp_tgit_review.py` mail workflow tests (including `tests/test_review_bulk_report.py`) should continue to pass unchanged for locally authored mbox series. Separate local mbox review from remote Git submission rather than deleting shared mail support.
- `test_agent_sandbox.py::test_project_mirror_is_bound_read_only_at_alternates_path` asserts read-only mirror mounting; preserve it. Also assert Git submission cannot obtain a writable host-repository path.
- `test_remote_agent_upload.py` covers source snapshot upload, not submission artifacts; it should remain unchanged unless protocol reuse actually touches it.

## Acceptance criteria

- Remote submission transports Git objects, not an mbox; artifact name/type and metadata explicitly identify the bundle format, base, head, byte count, and digest.
- Adapter verifies and retrieves single and batch submissions atomically, rejecting mismatched or over-limit artifacts.
- Host-side isolated validation rejects corrupt, truncated, wrong-base, unrelated, unsupported, or digest-mismatched bundles without creating refs or changing the worktree.
- Valid commits become reachable from submission-specific refs in the existing `_PROJECT_ROOT` repository, with auditable manifest/provenance; duplicate retry is safe. No branch, HEAD, index, or checked-out files change. No second host review repository is created.
- Agent cannot write host refs or the read-only remote mirror; transport cannot name arbitrary destination refs.
- Existing review mail/reply flow for locally submitted mbox series remains passing, and remote operator notification behavior is explicitly preserved or deliberately retired.
- Focused service, adapter, protocol, integration, import, and sandbox tests plus full pytest pass.

## Open decision

The code supports one design question that materially affects compatibility: should review refs point to the agent's original commit IDs (preserving submitted SHA and commit metadata) or to commits reconstructed by the host importer? Current mbox `git am` reconstruction necessarily creates host-side commit IDs, while metadata retains the agent's submitted head. A Git bundle can preserve original IDs, but only if the chosen bundle strategy includes and validates the required ancestry. Resolve this before implementation; other choices above have straightforward defaults: reuse the existing artifact transport, retain bundles for audit, and keep host refs immutable and namespaced.
