---
name: skill-creator
description: Author a new copilot skill folder — frontmatter, triggers, progressive disclosure, user-dir save via skills_save, and validation checklist.
inject: manual
triggers:
  keywords: [create a skill, author skill, new skill, write a skill]
  tools: []
  phases: []
---

# Skill Creator

Skills are folders: `<root>/<name>/SKILL.md` with YAML frontmatter + markdown body.

## Locations (precedence)

1. Project: `.billet/skills/<name>/SKILL.md`
2. User: `~/.local/share/AI-CAD/skills/<name>/SKILL.md` (writable via `skills_save`)
3. Builtin: `services/agent/skills/builtin/<name>/SKILL.md` (shipped)

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
- Keep body under ~150 lines; point to depth via `skill://name/path` or `cad_guide(...)`.
- Progressive disclosure: metadata always in context; full body only on activation.

## Save / test

1. Draft full `SKILL.md` text (frontmatter + body).
2. User-dir write: RPC `skills_save {name, body}` or write files under the user skills dir.
3. Confirm: `skills_list` shows it; `/skill:my-skill` activates; `skill://my-skill` resolves.
4. For auto skills: fire a keyword once, confirm no double-inject until compaction.

## Checklist

- [ ] `name` + `description` present and valid
- [ ] Triggers only if `inject: auto`
- [ ] No secrets in the body
- [ ] References deeper docs instead of inlining catalogs
- [ ] Verify-before-done called out

## Deeper docs

- Contract: `contracts/skills.md`
- Resolver: `skill://skill-creator` (this file)
