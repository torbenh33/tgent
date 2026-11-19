# Remote-agent Git submission migration: Phase 4

**Status:** implementation plan; no runtime changes in this document.

This document extracts Phase 4 from the [remote-agent Git submission migration plan](remote-agent-git-submission-plan.md). It covers final documentation and cleanup after Phases 1–3; it does not authorize removing code still used by local mbox review or compatibility paths.

## Goal

Finalize documentation and remove obsolete remote artifact and Message-ID handling only after the Git-submission and review workflows have replaced those paths. Keep the existing locally authored mbox-series review workflow intact.

## Implementation plan

1. Audit remote artifact and Message-ID code. Remove only code proven unused after Phases 1–3. Retain shared mbox support used for local review and any generated review-mbox path still required for compatibility.
2. Update `docs/remote-agent-review.md`, protocol documentation, and tests to describe the implemented contract and the manual-fetch transition if it remains relevant.
3. Run focused service, protocol, integration, importer, review, and sandbox tests, followed by the full pytest suite.

## Exit criteria

- There is one remote submission source of truth: published Git refs and provenance.
- No remote submission caller depends on transferred `.patch` artifacts.
- Local mbox review remains intact.
- The complete test suite passes.
