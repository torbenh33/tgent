# Agent orchestration lessons for future system prompts

This note records operational lessons from recent coding-agent runs. Use it when designing the agent/controller system prompts and lifecycle instructions. It is guidance for future prompt work, not a claim that the current implementation already enforces these behaviors.

## Task and snapshot discipline

- Give each agent one coherent, reviewable slice with explicit in-scope and out-of-scope work, concrete acceptance criteria, and the expected test commands.
- Before implementation, require the agent to inspect the current code, tests, and task brief. State the exact base commit and confirm that the snapshot contains the intended instructions and prerequisite changes.
- Treat an agent snapshot as fixed at its base. Later operator edits or commits are not visible unless the agent is explicitly continued/recovered with those changes or a fresh agent is started from the newer commit. If the task changes materially, communicate the delta explicitly or start a fresh agent from the desired base.
- Do not let exploration replace implementation. An agent should make reasonable decisions within the stated design, preserve useful work, and proceed to a tested result rather than stopping after describing possible architecture.

## Questions, blockers, and stopping

- Agents should ask the operator only when a specific unresolved decision materially blocks safe progress. Ask one concise, decision-oriented question, including the relevant code location/options and a recommended default when possible.
- When the question tool is available, use it to ask and wait; do not silently halt or report a generic blocker. If no question tool is available, finish the current safe slice and state the exact unresolved decision in the final report.
- Do not stop merely because tests fail or the task is larger than expected. Inspect the complete failure output, distinguish implementation defects from tests that are genuinely premature, fix defects within scope, and narrowly mark only intentionally deferred tests with an explanatory skip reason. Preserve deferred tests rather than deleting them.
- Halt only after a coherent patch is submitted, or when there is a specific blocker the operator must resolve. Report what is complete, what remains, exact test results, and the precise question/blocker.

## Blocking question tools and recovery

- A worker blocked inside an operator-question tool has an outstanding model function call. If the worker is stopped before the tool returns, its persisted conversation may contain a function call without a matching tool result; replaying that transcript can fail at the model API with “No tool output found for function call”.
- Until recovery is made robust, avoid stopping/restarting an agent while it is blocked in a question tool. Prefer answering the live call. If interrupted, recovery must reconcile the outstanding call and durably record/deliver the answer exactly once before replaying history; never replay an unmatched tool call as-is.
- Persist the pending question and operator response in agent state, make the pending question visible to the operator, and define idempotent answer/continue semantics. Add interruption/recovery tests before treating the question tool as restart-safe.

## Private agent repositories and review export

- Keep the agent’s private checkout distinct from the operator’s working repository. A commit made in the private agent checkout is not a commit to the operator repository and does not change the operator’s branch or files.
- The review exporter derives patches from committed history. Therefore, agents should commit coherent work in their private checkout when required to export a patch, then submit that patch for review. Explain this up front; do not tell agents “do not commit” if the supported review path requires a private commit.
- Never apply a patch to the operator checkout unless the operator has authorized applying it. Review feedback can request changes, approve application, or ask a question; interpret these distinctly. A request for changes is not approval to apply. If the operator says to act on review, clarify ambiguity or take the smallest reversible action consistent with the review.
- Verify patch submission through the tool’s explicit result. A local commit, a generated patch file, or an `awaiting_review` state alone is not proof that the review artifact was imported successfully.

## Test and handoff expectations

- Run focused tests for the changed code first and capture complete output. Report exact commands and pass/fail/skip counts. Run the full suite when feasible; if it fails, give concrete failure names and causes rather than an unexplained/truncated status.
- Every new behavior needs focused tests, especially lifecycle transitions, retries, failure paths, persistence, and recovery. Do not rely on unrelated passing tests as evidence for an untested feature.
- A review handoff should include the patch/commit identity, base, changed files, behavior, test results, skipped tests with reasons, and unresolved limitations. Ensure the submitted patch is non-empty and based on the expected commit.
- When a task depends on prior agent work, verify whether that work exists in the new snapshot. Do not assume one isolated agent can see another agent’s uncommitted changes or private repository.

## Operator/controller responsibilities

- Keep the design and phase boundaries stable while an agent works. If requirements change, update the brief and start from a snapshot containing it, or clearly deliver a concise authoritative delta.
- Distinguish incomplete implementation from a deliberate transitional state. For example, a legacy test may be expected to fail only when the corresponding flow is explicitly deferred; state that scope and retain a clear skip rather than allowing broad unexplained failures.
- Do not overstate progress. Check status and summarize the actual diff/test/submission state before reporting completion.
