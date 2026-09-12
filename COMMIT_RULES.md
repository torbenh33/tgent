# Commit Rules

- Keep commits focused on a single change.
- Try to keep commits as small as possible while keeping them bisectable. So prefer two commits, when 2 files change.
- Prefix the subject with the filename or module begin changed. Spearate it with a colon (for example: "module: Add new feature").
- Write commit messages in imperative mood (for example: "Add", "Fix", "Refactor").
- Explain why the change is needed in the commit body when it is not obvious.
- Do not commit secrets, credentials, or unrelated generated files.
- Do not commit whitespace noise. This can be stashed after commits have been made, so that repo is clean again.

## Commit message grounding

- Commit message body MUST be derived only from the staged diff and current commit subject.
- Do not include plans, alternatives, or discussion text from chat/context unless reflected in staged changes.
- Before commit, verify message-to-diff alignment:
  - every “what changed” claim maps to a visible hunk
  - no claim references absent hunks

## Scope control

- One commit, one intent.
- If one file contains multiple intents, use partial staging and separate commits.

## Agent execution defaults (token-efficient workflow)

- Default to action when user intent is clear: stage, write commit message, and commit without extra confirmation.
- If ambiguity exists, ask at most one concise clarification question.
- If a safe default exists, proceed with it and briefly state what was assumed.
- Generate commit subject/body directly from staged diff and current subject constraints.
- Batch related actions in one turn whenever possible (for example: stage + commit).

## Line-number selection for partial staging

- Prefer delta-rendered line numbers when they are clearly mapped to the target changed line.
- If mapping is ambiguous or resource output may be stale, derive line numbers from raw hunk coordinates via `git diff -U0` (`+N`/`-N`).
- Do not guess line numbers from unrelated visual numbering.
