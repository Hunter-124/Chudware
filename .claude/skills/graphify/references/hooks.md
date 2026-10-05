# graphify reference: hooks and Claude integration

This repo already has curated graph guidance and PreToolUse hooks. A graph query, update or compatibility review must not install, uninstall or rewrite them, `.claude/settings.json`, or either `CLAUDE.md`.

## Post-commit hook

The installed CLI provides `graphify hook install`, `graphify hook uninstall` and `graphify hook status`. Only `status` is inspection. Install/uninstall mutate `.git/hooks`; leave them to an explicitly authorized integration task, never run them to fix a skill-version warning.

The hook schedules local AST rebuilding for code changes after commits; it is not a semantic LLM pass. The installer preserves an existing hook by appending its managed block. Document/media semantic changes still need the `/graphify <path> --update` host-agent flow in `references/update.md`. No new commit, staging, branch or dirty-tree procedure is implied by this skill.

## Claude integration and skill installation

`graphify claude install` writes a graphify section in `CLAUDE.md` and installs PreToolUse hooks in `.claude/settings.json`; `graphify claude uninstall` removes that managed integration. `graphify install` can replace SKILL.md, references and the version stamp with package assets. Do not use these commands here: they would discard the locally maintained safety rules or curated integration.

`graphify hook-guard` is the installed shell-independent Claude/Codebuddy guard: it consumes tool-call JSON on stdin and emits graph-orientation guidance. Its strict mode can deny an indexed source read once per session; it is not an ordinary graph query. `graphify hook-check` is a no-op compatibility hook for Codex Desktop, not an index freshness check. Neither command is a reason to replace this repo's existing hooks.

After a graphify package upgrade, review the CLI source and bundled skill/reference assets, update these local instructions and their compatibility stamp, and leave installation collateral unchanged.
