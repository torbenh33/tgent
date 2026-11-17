# Remote-agent Git submission migration: Phase 3

**Status:** Phase 3A is implemented; Phase 3B is in progress.

This document extracts the Phase 3 review and integration plan from the main [remote-agent Git submission migration plan](remote-agent-git-submission-plan.md). Keep Git refs and per-version service metadata as the source of truth throughout. Generated mboxes are compatibility representations for mail clients, not the submission transport or source of truth. Preserve the existing locally authored mbox review workflow.

## Phase 3A: Email PR per submission version

Create a distinct email PR/thread for every submission version (`v1`, `v2`, etc.); never overwrite or conflate versions from the same agent. The submitted queue email itself is the submission notification—do not send a separate “queue submitted” notification.

Each PR email identifies agent, version, base, submitted commit, and Git ref. Preserve the submitted cover letter, use clear patch-series subjects, and thread patch messages/replies to the correct version. Persist enough version-specific review identity to associate replies and review state correctly.

The bulk mail-pump operation planned for Phase 3B is the normal way to materialize and deliver these PR emails into the operator's review Maildir. Single-version retrieval may remain a convenience using the same underlying generation/import logic.

**Exit criteria:** tests prove two versions from one agent produce independent, stable email threads; subjects, cover letters, Message-IDs, and reply association are correct; repeated retrieval/import does not duplicate a version's review record.

## Phase 3B: Unified pending-agent-mail pump and lifecycle event emails

Replace/rename the patch-only `fetch_pending_patches` operation (and corresponding MCP/API naming where appropriate) to a unified operation such as `fetch_pending_agent_mail`. One invocation should collect all unprocessed versioned submission PRs and queued lifecycle events, materialize the corresponding messages, and import/pump them into the operator's existing review Maildir. The name and response should reflect that it returns review submissions and lifecycle mail, not just patch files.

Define send/delivery timing explicitly: lifecycle actions (agent start, continuation, stop, question, stop without submission, and stopped-agent discovery after service restart) create event records with stable event IDs when observed. They do not independently send mail. The submitted queue is represented by its versioned PR record, not a separate “queue submitted” event. The service returns serialized email messages; the local MCP layer imports them into the existing operator-side review Maildir and acknowledges lifecycle materialization only after successful import. The next `fetch_pending_agent_mail` call materializes and delivers the currently available PRs/events into Maildir, then records that each event was materialized as an email (including its Message-ID and materialization time). If the service resets during delivery, duplicate email is acceptable; the recorded marker should prevent routine repeated fetches from regenerating already-materialized events.

Lifecycle emails cover starts initiated through `mcp_agent`, continuations, stops, stopping/exiting while asking the operator a question (including the pending question and reply/continue instructions), stops without a submission (state explicitly that no queue was submitted), and agents discovered stopped during service-restart reconciliation (identify the restart-discovered status). Include stable identity, event time, agent, state, and relevant version/review reference. Replies remain threaded to the corresponding version PR or question event.

Preserve per-item partial success: one malformed/unavailable PR or event must not block other mail from being pumped; report successes and failures with event/submission IDs so retry is safe.

**Exit criteria:** one unified fetch/pump operation delivers available versioned PRs and lifecycle messages and records their email materialization; tests cover timing, partial failures, restart discovery, and question/reply routing. Recovery may produce duplicates if reset interrupts delivery; elaborate exactly-once delivery guarantees are out of scope.

## Phase 3C: Integrate an approved version from its Git ref

Applying an approved submission uses its published version ref in an explicit Git merge/rebase or equivalent reviewed integration flow, not `git am` on the generated mbox. Preserve the original ref, version metadata, and email review record.

Keep review/import separate from integration. Never update the operator's active checkout implicitly; use an explicit integration branch/worktree or an equivalently safe controlled target, and make the action auditable.

Manual `git fetch` followed by explicit merge/rebase remains a recovery path, not the primary review workflow.

**Exit criteria:** tests demonstrate applying one approved version does not consume, overwrite, or conflate another version and does not silently alter the active checkout.

## Phase boundaries and invariants

- Phase 3A establishes distinct, version-specific email PR identity and threading. It does not replace Git refs or per-version metadata as the source of truth.
- Phase 3B adds a unified mail-pump operation and lifecycle event email materialization; it is not a standalone immediate-send path for each lifecycle action.
- Phase 3C integrates an explicitly approved Git ref through a controlled target. Review/import remains separate, and the operator's active checkout is never changed implicitly.
- Preserve existing review/reply behavior and locally authored mbox-series review except where a separately approved Phase 3 change deliberately updates it.
- Do not add a separate “queue submitted” lifecycle notification; the versioned PR is the submission notification.

