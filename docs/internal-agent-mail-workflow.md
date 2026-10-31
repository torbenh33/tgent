# Internal Mail and Event Routing Proposal

This document specifies an internal message and notification system built on the existing Mutt/sendmail hook and Maildir spool used by `tgit_review`. It is not an external email integration: messages are local system events and commands carried in mail format. Authentication of external senders is out of scope. Stable object identity, reliable routing, causality, threading, and retry-safe delivery are in scope.

This proposal is for review. It does not prescribe an immediate implementation or replace the existing review-mail workflow without an explicit migration plan.

## Goals

- Allow a user to send a mail-formatted command to start an agent.
- Notify the user when agents start, stop, submit patches, or need input.
- Route review replies to the relevant agent or review object.
- Keep routine forwarding out of the main/supervisor agent's reasoning loop; the supervisor remains available to manage subagents.
- Allow a patch-integrator agent or service to collect and apply selected patches under explicit policy.
- Give addressable system objects stable synthetic mail addresses.
- Preserve the origin message that caused an object to exist, separately from its parent/manager object.
- Ensure state changes and notifications can be retried without losing events or generating uncontrolled duplicates.

## System model

The existing sendmail command hook deposits mail into a Maildir spool, and `tgit_review` already uses that path for review-related messages. The proposed system extends this internal transport to agent commands and lifecycle events. It does not send real Internet email and does not require user authentication in the external-email sense.

Addressable objects may include:

- A user or operator inbox
- A project
- An agent
- A patch/review series
- A controller or service
- A patch-integration job

Each object receives a stable synthetic address, for example:

```text
agent+<canonical-agent-id>@tgit.internal
review+<series-id>@tgit.internal
project+<project-id>@tgit.internal
operator@tgit.internal
```

These are internal routing identifiers, not necessarily valid or externally deliverable addresses. The mapping from address to canonical object ID must be deterministic and unambiguous. Addresses should not be recycled while messages referring to them may still exist.

## Object identity, causality, and threading

Each object should track distinct links rather than overloading one “last mail ID” field:

- **`origin_message_id`**: the incoming message that caused the object to be created. For an agent started by a user command, this is the user's start request. It remains immutable for the object's lifetime.
- **`parent_object_id`**: the object that created, supervises, or manages this object, where applicable. A child agent may have a supervisor agent or review job as its parent even though the user's command is its origin.
- **`last_received_message_id`**: the most recent incoming message that was accepted as input to this object.
- **`last_sent_message_id`**: the most recent outgoing message sent by this object.
- **`last_event_id`** (or equivalent): the last durable internal event processed/emitted by this object, for audit and delivery bookkeeping.

For every message, store durable routing and causal metadata in addition to standard mail headers:

- Unique internal event/message ID
- RFC-style `Message-ID`
- Sender object address and recipient object addresses
- `In-Reply-To` and `References`, where applicable
- Causation message/event ID
- Target object ID and event type
- Creation time and delivery state

Mail headers support Mutt threading and human navigation, but are not the sole source of truth. The durable event record is authoritative because messages may be imported, forwarded, or have incomplete threading headers.

### Threading policy

When responding directly to an incoming message, set `In-Reply-To` to that triggering message's `Message-ID` and extend `References` from its existing references. When an event is a continuation of an object's own conversation, use its recorded `last_sent_message_id` and the relevant reference chain. The causal relation should also be recorded explicitly, so a response can be traced even if headers are malformed or absent.

A child agent's origin is the exact message that requested its creation. Its `parent_object_id` independently identifies the supervisor/review object that spawned or manages it. The child can therefore reply in the user's originating thread while remaining manageable by its parent.

## Example lifecycle

1. The user sends a start-agent message to the controller's synthetic address.
2. The router ingests and records the message and its `Message-ID`.
3. The controller creates the agent record, setting `origin_message_id` to the request ID and `parent_object_id` as appropriate.
4. The service sends a started notification with a new `Message-ID`, replying to the start request.
5. The agent submits a patch queue through `tgit_review`; the patch notification is addressed/threaded in the original task conversation and references the agent/review object.
6. The user sends review comments as a reply addressed to the agent or review synthetic address.
7. The router records the incoming review message as the latest received message, then resumes the waiting agent or starts a continuation agent according to lifecycle policy.
8. The response notification names the newly started/continued agent and carries the relevant patch-queue reference.

```mermaid
sequenceDiagram
    actor User
    participant Mutt
    participant Spool as sendmail hook / Maildir
    participant Router as Internal mail router
    participant Agent
    participant Review as tgit_review

    User->>Mutt: Start-agent message
    Mutt->>Spool: Deposit message
    Spool->>Router: Deliver to controller address
    Router->>Agent: Create agent; persist origin Message-ID
    Agent->>Spool: Started notification (new Message-ID, replies to request)
    Spool-->>User: Mutt displays notification
    Agent->>Review: Submit patch queue
    Review->>Spool: Patch-queue message
    Spool-->>User: Patch notification in same thread
    User->>Mutt: Send review comments
    Mutt->>Spool: Deposit reply
    Spool->>Router: Route by synthetic agent/review address
    Router->>Agent: Persist review input; start or resume agent
    Agent->>Spool: Agent/patch response, threaded to review
```

## Event and notification model

The system should define a catalog of event types and recipients rather than sending informal mail from arbitrary code paths. Initial events could include:

- `agent.started`
- `agent.waiting_for_input`
- `agent.continued`
- `agent.stopped`
- `agent.failed`
- `agent.patch_submitted`
- `review.received`
- `patch_integration.started`
- `patch_integration.completed`
- `patch_integration.failed`

For each event, define the owning object, recipient objects, subject/body template, causal message/event, and whether operator notification is required. “Mail for every event” should mean every durable, externally meaningful event can produce a notification; internal high-frequency events (for example, each model token or transient progress update) should not automatically produce a user-facing message. Event granularity and notification policy need explicit review.

Notifications should be useful and bounded: identify the event, object name and canonical ID, outcome, relevant patch/review reference, and next action. Full agent transcripts should not be attached by default; provide a reference or a deliberate summary instead.

## Reliable delivery and idempotency

A state transition and its notification intent must not be separated by an unsafe crash window. Use a durable event/outbox pattern:

1. Persist the state transition and an outbox event together, or use an equivalent recoverable protocol.
2. Deliver the event to the Maildir spool.
3. Mark it delivered only after successful spool insertion.
4. On retry, use the same event ID and message ID so duplicate delivery can be detected or made idempotent.

If the current JSON store remains in use, specify how the state update and outbox write are made recoverable (for example, a journaled event file and atomic rename, with a reconciliation pass). If SQLite is selected later, a transaction can commit state and outbox rows together. The mail spool remains the delivery boundary in either design.

Inbound processing also needs idempotency. Record the incoming `Message-ID` (or stable spool-entry identity plus a content digest if headers are absent) before applying commands. Reprocessing the same message must not start duplicate agents, append the same review comments twice, or spawn repeated continuations. Define behavior for missing or duplicate `Message-ID` values.

## Commands and routing

Commands should be explicit and versioned, with a small set of accepted fields. A start-agent message should identify the project and task and may request bounded limits. The router validates the format and returns a clear error message for malformed or unsupported commands. Arbitrary attachments or message text must not be treated as privileged executable instructions. This is not an external authentication system, but deterministic parsing and safe validation remain necessary.

A review reply should route by its synthetic recipient and durable thread/series mapping, not by subject text alone. When headers provide conflicting object IDs, reject or quarantine the message with a diagnostic rather than guessing. If the addressed agent is no longer waiting, report that state and preserve the reply for operator action; do not silently discard it.

## Supervisor and patch integration roles

The mail router performs deterministic intake, event recording, notification generation, and routing. It should not ask the main/supervisor agent to forward every message. The supervisor agent is still useful for planning, inspecting agent state, deciding which subagents to start, and helping the operator manage work.

A patch integrator may collect submissions, verify their bases and dispositions, apply selected patches in an isolated integration branch/worktree, and run tests. It should report per-patch results and preserve original artifacts. Applying patches is distinct from merging/pushing to the project's main branch; any final merge or push should require an explicit operator policy/approval. Conflicts, stale bases, duplicate commits, and failed tests should be reported without silently dropping submissions.

## Suggested object/message storage fields

An object record may contain:

```text
object_id
object_type
synthetic_address
parent_object_id
origin_message_id
last_received_message_id
last_sent_message_id
last_event_id
lifecycle_state
revision
created_at
updated_at
```

An event/message record may contain:

```text
event_id
message_id
object_id
event_type
causation_id
sender_address
recipient_addresses
in_reply_to
references
payload_reference
created_at
delivery_state
attempt_count
last_error
```

This is illustrative, not a finalized schema. Large bodies or artifacts may remain in Maildir or artifact storage, with durable references in state.

## Acceptance criteria

Before implementation is considered complete, tests should establish that:

1. A start command creates exactly one agent when ingested once or retried with the same message identity.
2. The created agent retains the start request as `origin_message_id` and records its managing object independently as `parent_object_id`.
3. A started notification has a new `Message-ID`, replies to the start request, and appears in the same Mutt thread.
4. An agent patch notification is causally associated with the agent and review series and has the expected threading headers.
5. Review comments addressed to a synthetic agent/review address reach the correct object and are recorded as the latest received message.
6. A reply to a waiting agent resumes or continues the correct agent; a reply to a non-waiting agent is preserved and produces an actionable response.
7. Missing, duplicate, malformed, or conflicting message/thread identities do not cause guessed routing or duplicate side effects.
8. A crash between state transition and spool delivery results in a retryable outbox event, not a lost notification.
9. Reprocessing an outbox event does not create uncontrolled duplicate messages or duplicate agent operations.
10. Event recipients and notification contents follow the event catalog; routine routing does not require supervisor-agent forwarding.
11. Patch integration reports each selected patch's result, preserves source artifacts, and does not merge/push without the configured explicit authorization.

## Open questions for the meeting

1. What synthetic address format should be used, and are addresses derived directly from canonical IDs or looked up in an address registry?
2. Which objects receive addresses: agents and review series only, or also projects, integration jobs, and supervisors?
3. Which events generate user-visible mail, and which are recorded internally only?
4. Should a review reply continue the same agent or spawn a new continuation agent by default? Which state/patch conditions decide?
5. What is the canonical thread for a patch submission when a child agent was spawned by a review object but originated from a user command?
6. How are messages with missing or duplicate `Message-ID` headers identified idempotently?
7. Should the outbox live in the JSON state store, a journal, or a future database? What recovery process reconciles outbox and Maildir state?
8. What patch selection and application policy can an integrator perform automatically, and which actions require approval?
9. What retention policy applies to inbound messages, event records, Maildir entries, and reaped-object tombstones?
10. How should the system present command errors and replies that arrive after an agent has stopped or been reaped?

## Proposed next steps

1. Review and agree on object identity, `origin_message_id` versus `parent_object_id`, event granularity, and threading rules.
2. Inventory the current `tgit_review` sendmail hook, Maildir layout, Message-ID generation, reply capture, and existing tests.
3. Define a versioned event catalog and inbound command format.
4. Add protocol/contract tests using a temporary Maildir before routing lifecycle events through it.
5. Implement a minimal vertical slice: start-agent command, started notification, waiting-for-input notification, and threaded reply delivery.
6. Add outbox/idempotency and recovery before enabling notifications for every lifecycle event.
7. Introduce patch collection/integration as a separate capability with isolated worktrees and explicit approval boundaries.
