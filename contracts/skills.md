# Contract: Copilot Skills — v1

Append-only. Maintainer-owned. Producer: `services/agent/worker_rpc.py`.
Consumers: agent loop (context injection), chat slash-commands, AI-settings skills editor, MCP.

Skills are folders of instructions + resources the copilot (and humans) can invoke,
modeled on Anthropic Agent Skills / omp.sh progressive disclosure:
metadata always in context (~100 tokens/skill), full body injected on activation only.

- Locations (precedence): project `.chudware/skills/<name>/SKILL.md` > user
  `~/.local/share/Chudware/skills/` > builtin `services/agent/skills/builtin/<name>/SKILL.md`.
- Frontmatter (YAML): `name`, `description` (required); `triggers: {keywords: [..], tools: [..], phases: [..]}`,
  `inject: auto|manual` (default manual), `roles: [..]` (optional allowlist).
- Activation: (a) explicit `/skill:<name>` or `/<name>` slash command from user or agent;
  (b) auto-inject when `inject: auto` and a trigger fires (keyword match on the user prompt,
  tool name about to be used, or mission phase entered) — injected ONCE per session, re-injected
  after context compaction.
- `skill://<name>` and `skill://<name>/<path>` resolve to skill files (traversal-guarded).
- Worker RPCs: `skills_list {} -> {ok, skills:[{name, description, source, inject, triggers}]}`,
  `skills_get {name} -> {ok, body, files:[..]}`, `skills_save {name, body} -> {ok}` (user dir only),
  `skills_delete {name}` (user dir only).
- Builtin set (v1): edit-part, edit-assembly, edit-sketch, edit-feature, simulate,
  assess-model-state, source-parts, ecad-board, skill-creator,
  make-drawing, cam-job, sheet-metal, reverse-engineer, plm-workflow.
- Grounding rule: every builtin skill teaches verify-before-done (describe/measure/check_* after
  mutations) and single-source-of-truth sequencing (model the mating part FIRST, reference its
  dims — never parallel-guess mating interfaces).
- Builtin domain extensions: surface-modeling, weldment-frame, design-for-manufacturability.

## Current runtime clarification (supersedes compaction claim above)

The Python skill registry injects an automatic skill once per session. An explicit
`services.agent.skills.reset_session()` permits reinjection; there is no Python
compaction callback. Online context compaction is owned by Chudcode.
