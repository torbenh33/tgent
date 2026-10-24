# Reviewing remote agent patches

Remote agents work in isolated Git snapshots. When an agent submits its committed changes, the remote service saves a bounded patch-series artifact. In remote mode, `review_remote_agent` retrieves that artifact, validates it, and imports it into the local `mcp_tgit_review` store. You can then review and reply using the same mail-based workflow as other patch series.

## Before you start

- Configure and start the agent MCP server in **remote mode**, with access to the project repository and its `mcp_tgit_review` tools. `review_remote_agent` is not available in local mode.
- Configure the review mail setup described by `mcp_tgit_review.py` (including its private review state/Maildir and reply address) and make sure incoming replies are captured by the review server. Set `TGIT_REVIEW_REPLY_TO` to the address reviewers should reply to.
- Keep the project repository available locally. Import verifies the declared base commit and applies the mbox patches in a temporary clone; it does not apply them to your active checkout.

## Submit and import a review

Start an agent with the `start_agent` tool. Its request has this form:

```json
{"schema_version":1,"task":"Implement the requested change","name":"optional label"}
```

Use `list_remote_agents` and `get_agent_status(agent_id)` to monitor it. An agent that has completed a patch submission will have a patch artifact available. Ask the MCP client to call:

```text
review_remote_agent(agent_id="<agent-id>", artifact_directory="/path/to/kept-artifacts")
```

For example, if the agent ID is `a-123`, use `review_remote_agent("a-123", "/var/tmp/agent-review")`. The returned result includes the local `series_id`, revision and retained artifact path/digest. When applying a downloaded remote patch with `apply_agent_patch`, pass the originating `agent_id`; once Git successfully applies the complete patch series, the tool stops that remote agent through agent control. A failed apply does not stop it. Without an agent ID, application has no lifecycle side effect. The artifact directory is useful for audit; choose a directory writable by the MCP server. Repeating the import for an identical artifact is idempotent and reports the existing revision. If no patch is available, the result has status `unavailable`; retry after the agent submits.

Import checks the artifact digest, mbox structure and Message-IDs, the declared base, and whether patches apply cleanly. Artifacts larger than 16 MiB are rejected. Failed validation returns an error and does not alter your working tree. The remote cover letter is imported verbatim, so treat its text as agent-authored content.

There is also a lower-level `retrieve_agent_patch(agent_id, destination="/tmp")` tool that only downloads the remote artifact and reports its local path; it does **not** import the series. Its destination must be an existing directory, never a filename. The patch is saved inside that directory using a generated `<agent-id>.patch` filename. The default `/tmp` destination avoids creating or modifying files in the current Git repository; choose another directory explicitly if you want to retain the artifact elsewhere. Prefer `review_remote_agent` for normal review.

## Read and reply to comments

Use the series ID returned by the import:

```text
list_review_series(repo_path="/path/to/project")
get_reviews(series_id="remote-a-123")
get_reviews(series_id="remote-a-123", unacknowledged_only=true)
get_review_replies(repo_path="/path/to/project", unacknowledged_only=true)
```

`get_review_replies` returns individual reply details across all series for a repository in one call (optionally only unacknowledged replies); it is read-only and does not acknowledge or resolve comments. `list_review_series` shows each series' revisions and counts of captured and unacknowledged replies. Reviews are matched to patches through email `In-Reply-To`/`References` Message-IDs. Reply to the patch email using your configured review mail workflow; captured replies then appear in `get_reviews`. Use `get_unmatched_reviews` if a reply was captured but could not be associated with a patch.

To send feedback to an agent that is still live, explicitly continue it with `continue_agent(agent_id, messages)`. For example:

```json
[
  {"role":"user","content":"Review feedback for revision 1, patch <patch-message-id>: please handle the null input case. Review ID: <review-id>."}
]
```

The `messages` argument is a list of conversation message objects; include enough review and patch context for the agent to understand the comment. `continue_agent` resumes a live waiting agent. Review replies are **not** automatically forwarded to the agent, and `acknowledge_reviews` only marks replies as seen—it does not send feedback or mark comments fixed. Acknowledge IDs after you have handled them, for example:

```text
acknowledge_reviews(series_id="remote-a-123", review_ids=["<review-id>"])
```

If the agent is stopped or unavailable, keep the review visible and provide feedback when continuing an available agent; do not treat acknowledgement as resolution. The agent's subsequent committed submission can be imported as another revision under the same `remote-<agent-id>` series. Check the revision and patch IDs to see its response.

## Other useful review tools

- `get_agent_history(agent_id)` retrieves the agent conversation; `get_agent_log(agent_id)` retrieves a bounded log tail.
- `stop_agent(agent_id)` requests shutdown. An `awaiting_review` agent cannot be reaped directly: call `stop_agent(agent_id)` first, then `reap_agent(agent_id)`. This uses agent control to terminate its review-waiting worker and makes the lifecycle explicit. Other eligible terminal states are `stopped`, `halted`, and `failed`. Reaping removes snapshot/controller state, not the separately stored submitted patch artifact. Import and preserve that artifact before reaping if needed. Fetching or importing an artifact does not prove it was applied to the target checkout.
- `wait_for_mail(timeout_seconds=300)` waits for new mail addressed to the configured `TGIT_REVIEW_REPLY_TO` address.

Remote patch transfer and local review storage are separate: the import creates the local series and manifest, retains remote provenance, and threads comments against the imported patch Message-IDs. No arbitrary review-server operations are sent over the remote-agent protocol.

## Voluntary context child tasks

A parent worker may call `delegate_context_task` with a bounded task. It runs synchronously: the parent waits while a one-level child makes model/tool calls and then receives only the child's plain-text final response as the tool result. The child starts from a deep copy of the parent's current transcript plus the task, and its own tool exchanges remain local. The parent alone persists its history; no child-history adoption or automatic compression occurs. Thus this feature supports delegated summarization but does not compact history already retained by the parent. Safe compaction would require a separate, explicit parent-owned operation that reviews a proposed summary and deliberately changes its own retained transcript; that is not implemented here.

Children cannot invoke the child tool, structurally preventing recursion. They retain the parent's normal sandbox tools, including project MCP tools. Those tools can cause external side effects; isolation protects parent-agent internals, not outside systems. Child model calls are bounded. Errors and exhausted limits are returned through the normal tool-result error response.

## Scope and finding

## Conversation history

For routine status and review, call `summarize_agent_history(agent_id)` to send the agent conversation to the configured LLM and receive a concise summary. This does not change the saved conversation. Call `get_agent_history(agent_id)` when full messages and tool exchanges are needed for complicated debugging; complete history retrieval remains available with its existing behavior.
