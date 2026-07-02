---
id: T-G2
title: Restore doc-tab input, shrink Copilot header, PM button contrast
workstream: G
owner: sonnet-mira
status: doing
branch: task/T-G2-doc-tab-input-fix
day: 1
depends_on: [T-G1]
---

## Context

Three small diagnosed host-GUI defects left pending by the 2026-07-02 polish session
(`docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md` §PENDING), plus its live-verification debt. All
diagnoses are already written down — this task is mostly execution + live confirmation.

## Files / areas touched

- `host/src/ui/doc_tabs.cpp` (input fix)
- `host/src/ui/legacy_chat.cpp` (Copilot header sizing)
- `host/src/ui/property_manager.cpp` and/or `host/src/ui/pm_forms.cpp` ("Select with AI" button colors)

## Steps

1. Doc-tab strip input: remove `ImGuiWindowFlags_NoBringToFrontOnFocus` from the
   `##DocTabsOverlay` window (`doc_tabs.cpp:327`, in `dtFlags`) — same regression class as
   the FeatureManager fix in T-G1's set. If input is still dead, temporarily draw
   `GImGui->HoveredWindow->Name` to the foreground drawlist to find the stealing window.
2. Copilot header: in `drawCopilotHeader` (`legacy_chat.cpp` ~1238) drop the title from
   `st.fonts.h2` to a smaller font and shrink the logo to `GetTextLineHeight()`.
3. "Select with AI" PM button: dark-text-on-dark via `ImGuiCol_Button` — push
   translucent-glass button colors from `surf.tint` matching the FrameBg fix pattern in
   `property_manager.cpp` `drawBody`.
4. Live click-test the polish session's NEEDS-LIVE items: doc-tab "+" and "x", Copilot
   collapse-to-pill round trip, sketch overlay column-dodge with a large panned sketch,
   viewport toolbar clamp against both columns, Extrude form incl. End-Condition dropdown.
5. Rebuild with the flock'd build command; re-shoot `AI_CAD_SHOT=m1|sketch`.

## Definition of done

- [ ] Clicking doc-tab "+" and a tab "x" works in the live app
- [ ] Copilot header visibly narrower, no logo overlap at min column width
- [ ] "Select with AI" readable over light and dark viewports
- [ ] All NEEDS-LIVE items from the handoff confirmed or filed as new tasks
- [ ] Build green, screenshots taken, `graphify update .` run

## AI hints

- graphify does NOT index `host/src` — targeted grep/read is expected there.
- Build: `flock /tmp/host-build-linux.lock -c "cmake --build host/build-linux -j$(nproc)"`.
- Screenshot harness + scenario list: `docs/HANDOFF_SHOPCRAFT_GUI_POLISH.md` header.
