# Commit Rules

- Keep commits focused on a single change.
- Try to keep commits as small as possible while keeping them bisectable. So prefer two commits, when 2 files change.
- Prefix the subject with the filename or module begin changed. Spearate it with a colon (for example: "module: Add new feature").
- Write commit messages in imperative mood (for example: "Add", "Fix", "Refactor").
- Explain why the change is needed in the commit body when it is not obvious.
- Do not commit secrets, credentials, or unrelated generated files.
- Do not commit whitespace noise. This can be stashed after commits have been made, so that repo is clean again.

