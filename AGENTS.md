# AGENTS.md

## Repository Editing Rules

- Do not remove existing doctests from function docstrings unless the user explicitly asks for their removal.
- When changing behavior, update doctests to match the new behavior instead of dropping them.
- Prefer doctests that assert stable outcomes (for example `is None` or type checks) when environment-dependent output may vary.
- Keep subprocess and integration doctests non-noisy (no required warning print output unless explicitly part of behavior).
