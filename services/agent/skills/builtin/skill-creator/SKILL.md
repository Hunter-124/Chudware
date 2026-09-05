---
name: skill-creator
description: Author a project-local copilot skill folder with valid frontmatter, precise triggers, progressive disclosure, and an activation checklist.
inject: manual
triggers:
  keywords: [create a skill, author skill, new skill, write a skill]
  tools: []
  phases: []
---

# Skill Creator

Skills are folders: `<root>/<name>/SKILL.md` with YAML frontmatter + markdown body.

## Locations (precedence)

1. Project: `.chudware/skills/<name>/SKILL.md` — the agent-writable surface.
2. User: `~/.local/share/Chudware/skills/<name>/SKILL.md` — managed by the
   Settings Skills editor, not by an agent tool.
3. Builtin: `services/agent/skills/builtin/<name>/SKILL.md` — shipped.

## Frontmatter (required)

```yaml
---
name: my-skill          # [a-z0-9][a-z0-9_-]*
description: One line  # shown in the always-on catalog (~1 line/skill)
inject: manual          # or auto
triggers:
  keywords: [optional, phrases]
  tools: [optional_tool_names]
  phases: [optional_phase_names]
roles: []               # optional allowlist
---
```

## Body rules

- Teach a **tool sequence**, not essays.
- Always include **verify-before-done** and **interface-first mating** when relevant.
- Keep body under ~150 lines; point to depth via `cad_skill("<name>", path="<file>")` or
  `cad_guide(...)`.
- Progressive disclosure: metadata always in context; full body only on activation.

## Save / test

1. Draft full `SKILL.md` text (frontmatter + body).
2. Write `.chudware/skills/<name>/SKILL.md` under the active project with
   `fs_write`; do not claim access to the Settings-only user skill RPCs.
3. Confirm `cad_skill("my-skill")` returns the body and the catalog includes the metadata.
4. For auto skills: fire a keyword once, confirm no double-inject until compaction.

## Checklist

- [ ] `name` + `description` present and valid
- [ ] Triggers only if `inject: auto`
- [ ] No secrets in the body
- [ ] References deeper docs instead of inlining catalogs
- [ ] Verify-before-done called out

## Deeper docs

- Contract: `contracts/skills.md`
- Fetch: `cad_skill("skill-creator")` (this file)
