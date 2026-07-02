---
description: Audit and refresh the section context packs in work/context/ (source maps for the /ctx-* commands)
---
Available context packs:

!`ls work/context/ 2>/dev/null || echo "(none yet)"`

The /ctx-* slash commands feed these packs to sessions so nobody re-maps the codebase. Your job: bring them up to date. $ARGUMENTS

For each pack named in the arguments (or every pack that touches areas changed in recent commits — check `git log --stat -20`):
1. Orient with `graphify query`/`graphify explain` for that section (MANDATORY before reading raw files — this applies to any subagents you spawn too).
2. Verify every cited path/entry point still exists; fix moved/renamed ones; add new load-bearing entry points; update the debugging playbook with any new gotchas from docs/REFACTOR_LOG.md.
3. Keep each pack under ~90 lines, dense. Preserve the section format (Scope / Entry points / How it connects / Debugging playbook / Graphify starters / Related tasks-docs).
4. Also run `graphify update .` so the knowledge graph itself is current.
Commit the refreshed packs with message `docs(work): refresh context packs`.
