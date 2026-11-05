# Project Lore

This file records non-obvious technical discoveries: facts that caused surprising
failures, took significant time to debug, or would save a future developer 30+
minutes if known upfront.

Refer to `CLAUDE.md` and the `/project-lore` skill for recording guidelines.

---

## Build System

**ruff ignores `.gitignore` when the project directory is not a git repository.**
`respect-gitignore` only applies inside a git work tree, so scratch `.py` files in the gitignored `tmp/` are linted and fail `just verify`.
Fix: run `git init`, or delete scratch `.py` files from `tmp/` before running the gate.
