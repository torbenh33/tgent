# git-bug Identity Policy for Agents

**Status:** Decision record for discussion and implementation planning.

## Decision

All agents operating on a project use one shared, project-scoped git-bug identity for automated issue and comment activity. Agents do not create or adopt a separate git-bug identity per agent.

The controller's canonical agent ID remains the system identity for an agent instance. It is recorded separately as provenance when an agent creates or updates an issue, adds a comment, or performs another git-bug action. The git-bug identity represents the automated project actor; the provenance identifies which agent performed the action.

## Rationale

- Avoids accumulating permanent identity refs for short-lived agents and continuations.
- Makes identity provisioning and synchronization simpler across local agents, remote agents, project mirrors, and clones.
- Keeps attribution stable when an agent is restarted or a continuation is represented by a child agent.
- Preserves per-agent accountability without conflating controller IDs with git-bug user identities.

A git-bug identity is author attribution stored in Git, not an access credential. It should not be treated as proof that a particular agent was authorized to perform an operation.

## Provisioning and use

The project/controller setup provisions the shared identity once, using an explicit display name and configured address (which may be a non-deliverable project identity). Its identity ref is published to the project’s canonical Git mirror alongside git-bug issue refs. Agent clones fetch the required `refs/identities/*` and `refs/bugs/*` data before querying or modifying issues.

The controller or MCP adapter ensures the shared identity is available and selected for git-bug operations. Workers should not independently create identities, choose arbitrary author names, or receive credentials for identity management. If the identity is missing or cannot be adopted, the operation should fail with an actionable error rather than create an implicit per-agent identity.

## Per-action provenance

Every agent-originated tracker mutation should preserve, where git-bug permits, the following provenance outside the shared author identity:

- Canonical controller `agent_id`
- Project ID
- Operation and issue ID
- Timestamp
- Originating task/event/message ID when available
- Relevant patch or review-series ID when applicable

Use structured metadata if supported by git-bug; otherwise use a consistent, clearly labeled comment/footer convention or a controller-side audit/event record. Do not rely on free-form text alone as the authoritative audit record. Avoid storing secrets or entire prompts in tracker metadata.

## Synchronization implications

- The project reference mirror is the authoritative shared Git remote for project-scoped git-bug data.
- The mirror must retain the shared identity ref and bug refs.
- `git clone --reference` shares Git objects but does not automatically make the mirror's refs visible in the agent clone; explicitly fetch/synchronize git-bug refs.
- Agent issue updates are published to the mirror through the approved Git-bug push/publish path. Other agents see them on an explicit refresh/pull.
- Shared identity creation is an administrative/project setup action, not an agent-start side effect.

## Open implementation questions

1. What stable identity name and non-deliverable address should the project use?
2. Should the identity be created during project registration, mirror initialization, or as an explicit operator command?
3. What is the supported git-bug mechanism for selecting/adopting an existing identity non-interactively in agent clones?
4. Which structured metadata mechanism best records `agent_id` and task/event provenance?
5. How should the controller handle concurrent issue edits and publish conflicts?
6. What is the policy for shared identity rotation or replacement, and how are its refs retained?

## Acceptance criteria

1. A newly initialized project has exactly one configured automation identity, shared by local and remote agents.
2. Starting an agent does not create a new git-bug identity ref.
3. An agent can list, create, and comment on issues after synchronizing bug and identity refs; authored changes use the shared identity.
4. Each automated mutation can be traced to its canonical controller agent ID and associated task/event through the agreed provenance mechanism.
5. Clones missing the shared identity fail clearly or recover through the documented fetch/adopt flow; they do not silently create identities.
6. Concurrent update/push failures are surfaced without losing issue changes or falsely reporting successful publication.
