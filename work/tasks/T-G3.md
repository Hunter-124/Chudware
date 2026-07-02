---
id: T-G3
title: ShopCraft rename tier 1 - user-visible branding strings
workstream: G
owner: sonnet-brio
status: review
branch: task/T-G3-shopcraft-branding
day: 2
depends_on: [T-G2]
---

## Context

The project is being renamed AI-CAD → **ShopCraft** (Erik leaning "entire project").
The rename is tiered so tooling never breaks mid-flight: this task is tier 1 only —
strings a user sees on screen. Build targets, env vars, MCP ids, and appdata paths are
tier 2/3 and live in T-G10, deliberately sequenced AFTER the ui/imgui cutover so the
rename happens once in the surviving tree. Full tier breakdown:
`docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md` §PENDING BIG TASK.

## Files / areas touched

- `host/src/main.cpp` (~:164 window title), `host/src/app.cpp` (~:763 window title)
- `host/src/ui/legacy_chat.cpp` (~:1240 "AI-CAD Copilot" → "ShopCraft Copilot")
- Any other on-screen "AI-CAD": About/splash/ribbon/title-bar strings (grep for the
  user-visible subset only)
- `ui/imgui/src/**` equivalent visible strings if present

## Steps

1. Enumerate visible strings: grep `"AI-CAD` across `host/src` and `ui/imgui/src`,
   classify each hit visible-vs-infrastructure. Touch ONLY visible ones.
2. Replace with "ShopCraft" / "ShopCraft Copilot".
3. Do NOT touch: CMake project/target names, `AI_CAD_*` env vars, MCP server id
   "ai-cad", appdata dir "AI-CAD", file paths, scripts, docs build commands (all T-G10).
4. Rebuild (flock'd), screenshot `AI_CAD_SHOT=m1` + `chat`-adjacent scenario to confirm
   the new name renders and nothing truncates.

## Definition of done

- [ ] No user-visible "AI-CAD" remains in the running app (title bar, Copilot, About)
- [ ] Zero changes to build targets, env vars, MCP id, appdata paths
- [ ] Build green, screenshots confirm rendering, `graphify update .` run

## AI hints

- A blind global sed WILL break the build/MCP/paths — that's why tiers exist.
- Copilot header font shrink (T-G2) already landed; only the text changes here.

## Progress log
- 2026-07-02 18:50 UTC [sonnet-brio] Finisher (sonnet-pike2) closeout after sonnet-brio process-exit.

DoD:
- 20 ins / 20 del across 11 files — tier-1 user-visible strings only (window title, copilot header, about menu, quit modal, GLFW window title)
- Scope fence documented in work/questions/T-G3.md: file-filter labels, appdata paths, crash-dump filenames, exe/so name tooltips deferred to T-G10
- Coverage verified against T-G12 checklist: all user-visible chrome surfaces renamed; no T-G10-scoped strings touched
- Build: host AI-CAD (Linux) PASS; ui/imgui AI-CAD2 PASS
- Headless shot confirms title bar + copilot panel both show "ShopCraft"
- Gate: check.py FAIL-RECORDED (pre-existing), pytest 0-collected (pre-existing issue #6), all other stages PASS
- graphify update complete (22419 nodes, 48030 edges)
- Branch task/T-G3-shopcraft-branding pushed
