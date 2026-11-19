# Reviewing remote agent patches

Remote agents work in isolated Git snapshots. A submission publishes a versioned Git ref in the remote project bare mirror; that ref and its service-managed provenance are the source of truth. In remote mode, `fetch_pending_agent_mail` is the normal review workflow: the service generates a review mbox from each published ref and its cover-letter metadata, and the local MCP layer imports the generated messages into the existing review store and Maildir. The service creates an ephemeral mbox compatibility representation in the pending-mail response for local Maildir review; it retains no mbox artifact or transfer file.

## Before you start

- Configure and start the agent MCP server in **remote mode**, with access to the project repository and its `mcp_tgit_review` tools. `review_remote_agent` is not available in local mode.
- Configure the review mail setup described by `mcp_tgit_review.py` (including its private review state/Maildir and reply address) and make sure incoming replies are captured by the review server. Set `TGIT_REVIEW_REPLY_TO` to the address reviewers should reply to.
- Keep the project repository available locally. Import verifies the declared base commit and applies the mbox patches in a temporary clone; it does not apply them to your active checkout.

## Submit and import a review

Start an agent with the `start_agent` tool. Its request has this form:

```json
{"schema_version":1,"task":"Implement the requested change","name":"optional label"}
```

Use `list_remote_agents` and `get_agent_status(agent_id)` to monitor it. An agent that has completed a submission has a published versioned ref available for review. Ask the MCP client to call:

```text
fetch_pending_agent_mail()
```

The result reports each submission version's import status, series/revision and digest; the review store retains the generated mbox and remote provenance. Review and reply using the same mail-based workflow as other patch series. Repeated import of the same version is idempotent. If a submission is not yet available, fetch again after the agent submits. Applying a reviewed series remains a separate, explicit operation; it does not implicitly modify the operator's active checkout.

Import checks the generated mbox structure and Message-IDs, the declared base, and whether patches apply cleanly in a temporary clone. Generated review mboxes are bounded; failed validation returns an error and does not alter your working tree. The submitted cover letter is imported verbatim, so treat its text as agent-authored content.

The versioned Git ref is also available for manual recovery: fetch it from the remote project bare mirror and inspect/integrate it explicitly. Manual fetch is a recovery/transition path, not the normal review workflow. Do not expect remote `.patch` files to exist. Generated mbox content is returned only through the pending-mail operation and is not saved as an artifact or exposed through separate retrieval APIs.

## Read and reply to comments

Use the series ID returned by the import:

```text
list_review_series(repo_path="/path/to/project")
get_reviews(series_id="remote-a-123")
get_reviews(series_id="remote-a-123", unacknowledged_only=true)
get_review_replies(repo_path="/path/to/project", unacknowledged_only=true)
```

`get_review_replies` returns individual reply details across all series for a repository in one call (optionally only unacknowledged replies). In remote-agent mode, use `get_remote_review_replies(repo_path, unacknowledged_only)` to fetch that report through one remote request; it is read-only and does not acknowledge or resolve comments. `list_review_series` shows each series' revisions and counts of captured and unacknowledged replies. Reviews are matched to patches through email `In-Reply-To`/`References` Message-IDs. Reply to the patch email using your configured review mail workflow; captured replies then appear in `get_reviews`. Use `get_unmatched_reviews` if a reply was captured but could not be associated with a patch.

To send feedback to an agent that is still live, explicitly continue it with `continue_agent(agent_id, messages)`. For example:

```json
[
  {"role":"user","content":"Review feedback for revision 1, patch <patch-message-id>: please handle the null input case. Review ID: <review-id>."}
]
```

The `messages` argument is a list of conversation message objects; include enough review and patch context for the agent to understand the comment. `continue_agent` sends feedback to a live waiting agent, or (for eligible inactive `halted`/`stopped` agents without a submitted patch) creates a new child from saved history and snapshot. It does not resume an inactive `awaiting_review`/`review_received` worker. See [Continuation and recovery by lifecycle state](#continuation-and-recovery-by-lifecycle-state) below for the full state matrix, restrictions, and what a child restart preserves or resets. Review replies are **not** automatically forwarded to the agent, and `acknowledge_reviews` only marks replies as seen—it does not send feedback or mark comments fixed. Acknowledge IDs after you have handled them, for example:

```text
acknowledge_reviews(series_id="remote-a-123", review_ids=["<review-id>"])
```

If the agent is stopped or unavailable, keep the review visible and provide feedback when continuing an available agent; do not treat acknowledgement as resolution. The agent's subsequent committed submission can be imported as another revision under the same `remote-<agent-id>` series. Check the revision and patch IDs to see its response.

## Continuation and recovery by lifecycle state

`continue_agent(agent_id, messages)` has two distinct modes: it queues messages for a worker process that is still alive, or it starts a **new child agent** for an inactive `halted`/`stopped` record. It does not resurrect the old process. The controller first requires the recorded state to be one of `halted`, `stopped`, `awaiting_review`, or `review_received`; process liveness then determines which mode is possible. Use the table for both local and remote controllers:

| Recorded state | Can `continue_agent` proceed? | Reason and operator action |
| --- | --- | --- |
| `starting` | No | Not an accepted continuation state. Let startup complete and inspect status/logs; if startup failed, the worker will settle to `failed`. Start a fresh agent if it cannot be recovered. |
| `running` | No | The agent is already working; continuation is only for a waiting worker, not a way to interrupt/add a concurrent turn. Wait, inspect status/history/log, or `stop_agent` if intentionally abandoning it. After a stop, an eligible unsubmitted `stopped` agent can be continued as a child. |
| `stopping` | No | Shutdown is in progress and is not an accepted state. Wait for the terminal state (`stopped`, `halted`, or `failed`) before choosing an action. |
| `halted` | Yes, if inactive; also queues feedback if the worker is still alive | A natural model turn ended without tool calls, or the worker is awaiting an operator command (`stop_reason=awaiting_command`). Inactive, unsubmitted work starts a child. For a still-live waiting process, messages are queued to that same worker. Use `get_agent_history` to inspect its retained transcript first. |
| `stopped` | Yes, if inactive and no `submitted_head` | Operator stop or controller restart ended the process. It starts a child, not the old process. A submitted patch blocks restart; finish patch review/disposition instead. `restart_agents` is a convenience for this state only. |
| `awaiting_review` | Only if a worker process is still alive | This normally means a patch was submitted and the worker is waiting for review feedback. With a live worker, messages are queued to it. If inactive, restart is explicitly refused (`Only inactive halted or stopped agents can be restarted`); review the patch, then stop/acknowledge or reject as appropriate. Never treat a submitted patch as restartable. |
| `review_received` | Only if a worker process is still alive | Feedback has already been queued and the worker may be processing it. Another live call queues more messages. If the worker is inactive, this state is not a restart state; the controller reports that only inactive halted/stopped agents can be restarted. Check status/history and wait for its resulting state. |
| `failed` | No | A worker error/nonzero exit is terminal for continuation; neither `continue_agent` nor `restart_agents` accepts it. Inspect `get_agent_log` and `get_agent_history`; use a fresh `start_agent` for recovery. If useful, provide the old history explicitly as start-request `history` (and include a new task/message); this starts from the current configured project, not the failed agent's snapshot. Resolve or preserve any submitted patch before cleanup. |
| `reaped` | No; normally the ID is no longer found | Reaping removes the controller record, saved history and managed snapshot. There is no in-controller recovery path. Start a new agent and reconstruct context from independently retained artifacts/notes. |

A record with a missing/unrecognized state is also rejected by the accepted-state check (`Agent cannot continue from state ...`); inspect status/storage and use a fresh `start_agent` rather than guessing a transition. `reaped` records are normally absent altogether. These state rules do not override other continuation checks. Inactive restarts require a valid, extant snapshot project under the managed snapshot root, no submitted patch, and history-plus-new-messages within configured size limits. Invalid/missing snapshots and oversized transcripts are rejected rather than silently starting from another checkout. A live worker cannot be synced to another checkout; supplying `sync_checkout` in that case fails.

### What a child restart keeps and what it does not

For an inactive halted/stopped agent, `continue_agent` reads its complete persisted conversation and starts a new agent with that history followed by the supplied messages. It uses the recorded snapshot project as the new source checkout; for mirror-backed projects it carries the mirror/base-commit/project identity options forward. It also carries the name, model and reasoning effort unless explicitly overridden. The new metadata records `parent_agent_id`; the original record/history/snapshot are not consumed or rewritten by making the child.

This is a fresh worker with a fresh agent ID, process, runtime counters/budgets, tool/session state and new managed snapshot/log; it is not a process checkpoint or provider-side model-session resume. Only the saved transcript and project contents/Git state in the source snapshot provide continuity. Restart creates a new snapshot from that source, so subsequent child edits do not modify the parent's snapshot. Tool allowlists and other in-memory controller state are initialized anew, not copied from the parent. `restart_agents` supplies the generic user message “Resume the previous task from the saved history.”; use `continue_agent` directly when specific instructions or review feedback are needed.

A failed agent is deliberately not in the restart set, although its saved history/snapshot may still exist: diagnose first and explicitly start a replacement, optionally seeding history. A submitted patch is a separate protection boundary: an **inactive** submitted-patch agent cannot be restarted even if its state is otherwise `halted`/`stopped`; retain/import/review the patch, then record an explicit applied acknowledgement or rejection before eligible reaping. A still-live `awaiting_review` worker can receive feedback, but that is continuation of the live worker—not permission to discard or bypass its submitted patch.

## Other useful review tools

- `get_agent_history(agent_id)` retrieves the agent conversation; `get_agent_log(agent_id)` retrieves a bounded log tail.
- `stop_agent(agent_id)` requests shutdown. `reap_agents(agent_ids)` removes eligible agent snapshots/controller state and reports per-agent exclusions. Agents with a submitted head remain protected until the patch is reviewed or explicitly acknowledged as already applied.
- `acknowledge_agent_patch_applied(agent_id)` is an auditable local or remote operator assertion for a patch applied outside the patch-application workflow. It does not apply or verify anything: inspect/apply the patch first. It requires an inactive agent (`awaiting_review`, `stopped`, `halted`, or `failed`), records the acknowledged head and UTC time, and retains `submitted_head` plus review/patch artifacts. It is idempotent for that head. For `awaiting_review`, call `stop_agent` first; live workers are never acknowledged. A later different head needs a new acknowledgement. Once acknowledged, reap removes controller state and snapshot only; independently stored review artifacts remain. For an operator decision not to apply a submission, use `nack_agent_patch(agent_id, reason=None)`. It records `patch_rejection` with submitted head, UTC rejection time, and optional reason, without setting the applied acknowledgement. It is limited to inactive submitted work; repeated rejection of the same head is idempotent. Rejection permits reaping controller state/snapshot while leaving independently retained patch/review artifacts intact.
- `reap_agents(agent_ids)` bulk-reaps only stopped/failed agents without unresolved submitted patches and returns a per-agent result, including exclusions. The legacy single-item remote protocol operation is only a compatibility wrapper around this bulk API; invalid remote agent-ID behavior is being investigated separately, so this change does not alter remote production state or its validation. Acknowledged or nacked submitted patches no longer require operator attention and may be reaped when stopped/failed. Bulk reaping remains limited to stopped/failed agents.
- `list_agents_requiring_attention()` surfaces halted and awaiting-review agents, agents awaiting a command, and any agent with a submitted patch (which is explicitly marked in `attention_reasons`).
- `wait_for_mail(timeout_seconds=300)` waits for new mail addressed to the configured `TGIT_REVIEW_REPLY_TO` address.

Remote patch transfer and local review storage are separate: the import creates the local series and manifest, retains remote provenance, and threads comments against the imported patch Message-IDs. No arbitrary review-server operations are sent over the remote-agent protocol.

## Voluntary context child tasks

A parent worker may call `delegate_context_task` with a bounded task. It runs synchronously: the parent waits while a one-level child makes model/tool calls and then receives only the child's plain-text final response as the tool result. The child starts from a deep copy of the parent's current transcript plus the task, and its own tool exchanges remain local. The parent alone persists its history; no child-history adoption or automatic compression occurs. Thus this feature supports delegated summarization but does not compact history already retained by the parent. Safe compaction would require a separate, explicit parent-owned operation that reviews a proposed summary and deliberately changes its own retained transcript; that is not implemented here.

Children cannot invoke the child tool, structurally preventing recursion. They retain the parent's normal sandbox tools, including project MCP tools. Those tools can cause external side effects; isolation protects parent-agent internals, not outside systems. Child model calls are bounded. Errors and exhausted limits are returned through the normal tool-result error response.

## Streamlining the operator workflow (design proposal)

Current MCP calls are intentionally granular, so routine work incurs repeated list/status/review/decision/apply/stop/reap round trips. The common sequences are: (1) `list_agents_requiring_attention` → `get_agent_status` → `review_remote_agent` → review-series/replies calls → explicit apply → `stop_agent` → `reap_agents`; (2) for already-applied patches, status → explicit `acknowledge_agent_patch_applied` → reap; (3) halted remote work: list/status → `review_halted_agents` (which retrieves/imports available patches) → inspect/import result → stop/reap; and (4) ordinary cleanup: list/status → stop → reap. A halted agent with no submitted patch may also require `get_agent_history` or `summarize_agent_history` before choosing continue/reap.

Recommended incremental additions, not a replacement of the existing primitives:

- Add a read-only `get_agent_operator_brief(agent_id)` (and optionally a batch variant) combining status, attention reasons, history summary, and patch availability/reference. Return explicit per-section errors; do not fetch unbounded history or call an LLM unless requested. This combines status/list follow-ups and remains safe without confirmation.
- Add `review_agent_patch(agent_id, artifact_directory)` that dispatches local/remote retrieval and import behind one API, returning the retained artifact path, digest, series/revision, submitted head, and next safe actions. Import is an auditable state change but must not apply. Keep the existing `review_remote_agent` alias for compatibility.
- Add `apply_reviewed_agent_patch(series_id, revision, confirmation)` only if the existing patch application API can be called with explicit revision selection and dry-run result. Preview/validation is read-only; actual application must require an explicit confirmation token/argument, report exact commits/files and result, and never implicitly stop/reap. Keep direct `apply_agent_patch` supported.
- Add `finalize_agent(agent_id, action, confirmation)` as an orchestration command for stop/reap only after checking inactive state, submitted head, review artifacts, and acknowledgement. Require explicit action (`stop`, `reap`, or `acknowledge_then_reap`), return a plan before destructive execution, and require confirmation for snapshot/state deletion. Never infer that a patch is applied from Git state. Existing stop/reap/acknowledgement tools remain the compatibility primitives.
- Extend halted-agent retrieval to accept a batch of IDs and return one structured result per agent (available/imported/unavailable/error), retained paths and digests, and notification outcomes. The current `review_halted_agents` helper already batches some work; retain it, make failures explicit per ID, and avoid hidden applying, stopping, or reaping.

Migration: introduce these as additive MCP tools, document the old-to-new call mapping, and keep existing tool names/response contracts. Implement orchestration by composing existing controller/service operations, not by duplicating lifecycle rules; use idempotency keys for imports/acknowledgements and preserve artifacts. Confirmation must guard only irreversible effects (patch application, snapshot/state deletion), while review retrieval/import and status remain safe and repeatable. Do not bundle apply with stop/reap: operators need to inspect the actual apply result and preserve a recoverable state on failure.

## Scope and finding

## Conversation history

For routine status and review, call `summarize_agent_history(agent_id)` to send the agent conversation to the configured LLM and receive a concise summary. This does not change the saved conversation. Call `get_agent_history(agent_id)` when full messages and tool exchanges are needed for complicated debugging; complete history retrieval remains available with its existing behavior.
