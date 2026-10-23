# Remote-agent patch review: current workflow and proposal

## Scope and finding

This note compares the agent patch-queue path with `mcp_tgit_review.py` and proposes a practical way to review a **remote** agent using the same review lifecycle. This is an investigation and design proposal only; it does not change runtime behavior.

## Existing agent patch-queue flow

1. `agent_worker.py` advertises `submit_patch_queue(head_commit)` to the worker model. The worker runs in its own Git snapshot and forwards `patch.submit` with `AGENT_BASE_COMMIT` and the requested head commit to its controller IPC client. A successful submit puts the worker in `awaiting_review`; a later controller continuation (including `review_feedback`) resumes it with refreshed conversation history.
2. `AgentController` creates an independent snapshot and delegates submission to its configured `patch_submit_callback`; the callback is the policy boundary. In `mcp_agent.py`, `_submit_patch` loads `mcp_tgit_review.export_patch_series` and calls it with the snapshot base/head and snapshot repository path. Thus local agent submissions are review series produced from committed snapshot history, not an arbitrary diff of the operator's worktree.
3. `mcp_tgit_review.export_patch_series(base, head, repo_path, series_id?, cover_letter?)` resolves both commits, rejects ranges without non-merge commits, and writes a numbered cover-letter mbox revision. Its manifest records repo path, base/head, commit count and patch Message-IDs. Exported messages are also imported into its private review Maildir. Reusing `series_id` creates the next revision for that repository.
4. Review tools expose `list_review_series`, `get_reviews`, `get_unmatched_reviews`, `wait_for_mail` and `acknowledge_reviews`. Replies are associated with patches by `In-Reply-To` / `References` Message-IDs; acknowledgements mark messages seen, not comments resolved. `TGIT_REVIEW_REPLY_TO` controls the reply address.

## What remote mode adds today

`mcp_agent.retrieve_agent_patch(agent_id, destination)` is a remote-only controller tool. The remote worker/service uses the same `mcp_tgit_review.export_patch_series` callback against its snapshot, then separately runs `git format-patch --binary --stdout base..head` and stores a private, size-limited `.patch` artifact. The protocol operation `patches` returns availability, artifact name, size and submitted head; the adapter retrieves it with `rsync` and validates the reported size. Remote protocol operations include `patches` but have no review-series, mail, or acknowledgement operations. `continue_agent` can send feedback to a live remote worker, but current code does not connect review replies to that continuation.

The remote callback does invoke series export on the remote service, but that series and its Maildir reside in remote review state. The downloaded artifact is a separate raw format-patch stream; it does not carry the review-series manifest or establish a local series identity. Consequently, the existing local review tools cannot reliably discover and thread replies for a downloaded remote artifact as if it were a local review series. Reaping also removes agent snapshot/controller state; the proposal should not make review depend on retaining that snapshot.

## Proposed workflow

Keep the remote agent's isolated snapshot and existing bounded artifact-transfer path. Add a trusted **local review bridge** that converts a retrieved artifact into a normal local `mcp_tgit_review` series, then uses the existing review and feedback lifecycle:

1. On successful remote submission, persist immutable submission metadata: agent ID, submitted head, base commit, artifact identity/digest, and status. Make repeated retrieval/review calls idempotent for that submission.
2. The operator requests review for an agent. Retrieve the artifact into a private temporary location; validate size, digest, and expected agent/submission identity. Do not treat artifact filenames or paths supplied by a remote peer as paths to open.
3. Reconstruct the proposed commits against the recorded base in an isolated temporary Git worktree/clone. Verify the base exists and the patch applies cleanly; never apply it to the operator's active branch/worktree. Compare reconstructed range and submitted metadata, and fail closed on mismatch. Preserve the original patch artifact for audit.
4. Export the reconstructed `base..head` using `mcp_tgit_review.export_patch_series` against the **local** repository path, with a stable series ID mapped to agent ID plus submission (or revision). This creates the manifest and Message-ID-threaded mail in the local review store. Return the series ID and revision to the caller. Keep remote artifact receipt distinct from successful review-series creation.
5. The operator reads/responds through existing review tools. A local coordinator polls `get_reviews(series_id, unacknowledged_only=True)` and passes new comments to the corresponding live remote agent via `continue_agent` (structured messages with review IDs and patch/revision context). Acknowledge only after successful delivery; retain delivery state so retries do not lose or duplicate feedback. If the agent is stopped, expose comments for operator retrieval and deliver on explicit continuation rather than silently dropping them.
6. The worker continues its existing behavior: feedback resumes it; it makes changes in the snapshot and submits a new committed range. Map the next submission to the same series ID so `export_patch_series` creates a new revision and new patch Message-IDs. Keep per-revision base/head and artifact provenance. Do not report a comment as fixed merely because it was acknowledged; a later patch revision is the agent's response.

Prefer an orchestration layer over expanding the SSH protocol to expose arbitrary review-server calls. If remote-side review APIs are ever needed, add explicit allowlisted, versioned operations and bounded schemas; never tunnel arbitrary MCP names, filesystem paths, or shell commands through the remote protocol.

## Implementation sequence and acceptance checks

1. Define a durable mapping for remote agent/submission ↔ local review series/revision, including upload/retrieval/delivery states and idempotency semantics. Decide how to retain series after agent reaping.
2. Add a local bridge/controller API for `review_remote_agent` (retrieve, validate, reconstruct, export) and expose series/revision metadata. Reuse review export and mail threading rather than creating a second review store.
3. Add explicit synchronization from unacknowledged local reviews to `continue_agent`, with retry-safe delivery and operator visibility for unavailable agents.
4. Test valid and malformed artifacts, wrong base/head, conflicts, size limits, duplicate requests, reaping, missing/stopped agents, reply threading, acknowledgement only after delivery, and successive revisions. Exercise both local and remote mode, including the remote protocol's bounded/allowlisted behavior.

### Relevant source APIs inspected

- `agent_worker.py`: `submit_patch_queue`, `patch.submit`, `awaiting_review`, continuation handling.
- `agent_controller.py`: `PatchSubmitCallback`, snapshot lifecycle and agent review states.
- `mcp_agent.py`: `_submit_patch`, `retrieve_agent_patch`.
- `remote_agent_service.py`: `_submit_patch`, `_patches`; `remote_agent_adapter.py`: `patches`, `retrieve_patch`.
- `remote_agent_protocol.py`: version 1 allowlist includes `patches`; no review or acknowledgement RPC.
- `mcp_tgit_review.py`: `export_patch_series`, `list_review_series`, `get_reviews`, `wait_for_mail`, `acknowledge_reviews`; manifests and Message-ID association.
- Existing repository docs: `AGENTS.md` and `COMMIT_RULES.md`. `README.md` explicitly says not to change it, so this note is isolated under `docs/`.
