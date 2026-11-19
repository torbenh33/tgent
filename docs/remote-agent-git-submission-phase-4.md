# Remote-agent Git submission migration: Phase 4

**Status:** implemented. The remaining generated-mbox/single-submission compatibility path is documented below.

This document extracts Phase 4 from the [remote-agent Git submission migration plan](remote-agent-git-submission-plan.md). It covers final documentation and cleanup after Phases 1–3; it does not authorize removing code still used by local mbox review or compatibility paths.

## Goal

Finalize documentation and remove obsolete remote artifact and Message-ID handling only after the Git-submission and review workflows have replaced those paths. Keep the existing locally authored mbox-series review workflow intact.

## Implemented behavior and audit

- Published versioned Git refs plus service-managed provenance are the submission source of truth. The normal `fetch_pending_agent_mail` flow generates review mboxes from those refs on demand; mbox generation and Message-ID/thread construction remain required for Maildir review compatibility.
- Shared mbox parsing/import, Maildir delivery, Message-ID deduplication, and locally authored mbox review are retained. They are active review infrastructure, not obsolete remote submission transport.
- Legacy `.patch` retrieval/transfer APIs and the `patches` and `pending_patches` protocol operations are removed. Generated mbox content is ephemeral within `fetch_pending_agent_mail`; versioned Git refs and provenance are the only submission source of truth. Pre-versioned metadata enumeration is removed.
- `docs/remote-agent-review.md` now documents the normal mail pump, generated mbox semantics, and manual Git-fetch recovery transition. Runtime removal is limited to obsolete patch artifact APIs; local authored mbox review is preserved.

## Exit criteria

- Remote submission source of truth is published Git refs and provenance; generated mboxes are review compatibility representations.
- No normal remote submission/review flow depends on transferred `.patch` artifacts. Legacy retrieval compatibility is intentionally retained.
- Local mbox review remains intact.
- Focused and full pytest results are recorded with this implementation.
