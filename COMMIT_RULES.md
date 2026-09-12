# Commit Rules

- Keep commits focused on a single change.
- Try to keep commits as small as possible while keeping them bisectable. So prefer two commits, when 2 files change.
- Prefix the subject with the filename or module begin changed. Spearate it with a colon (for example: "module: Add new feature").
- Write commit messages in imperative mood (for example: "Add", "Fix", "Refactor").
- Explain why the change is needed in the commit body when it is not obvious.
- Do not commit secrets, credentials, or unrelated generated files.

## Commit message grounding

- Commit message body MUST be derived only from the staged diff and current commit subject.
- Do not include plans, alternatives, or discussion text from chat/context unless reflected in staged changes.

## Scope control

- One commit, one intent.
- If one file contains multiple intents, use partial staging and separate commits.

## Agent execution defaults (token-efficient workflow)

- Default to action when user intent is clear: stage, write commit message, and commit without extra confirmation.
- If ambiguity exists, ask at most one concise clarification question.
- If a safe default exists, proceed with it and briefly state what was assumed.
- Generate commit subject/body directly from staged diff and current subject constraints.

## Line-number selection for partial staging

- Prefer delta-rendered line numbers when they are clearly mapped to the target changed line.
- Do not guess line numbers from unrelated visual numbering.
