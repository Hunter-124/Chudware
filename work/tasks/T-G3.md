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
