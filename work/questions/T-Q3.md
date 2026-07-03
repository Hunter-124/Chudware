# T-Q3 Scope Question

## Q1: GUI gallery panel + first_run wiring scope

**Context:** The driver launch prompt says to build a `panel_scaffold`-based gallery panel and
wire the `first_run.h` sample-part checkbox to open a gallery sample.  The task file says:

> "GUI gallery panel is T-U-later — data format is the deliverable"

and the "Files / areas touched" list does not include `ui/imgui/` or `first_run.{h,cpp}`.

**Recommended answer:** T-Q3 ships the data deliverable only — `samples/`, `samples/index.json`,
thumbnails (headless shots), regen+verify test, and one headless tour script.  GUI gallery panel
and `first_run.cpp` wiring are T-U-later as the task file states.  No `ui/imgui/` edits in this
task.

**Alternative:** If the GUI work IS in scope for T-Q3, it requires touching `first_run.cpp`
(wiring the "Open a sample part" checkbox to the gallery index rather than the single
`box.aicm` fixture), adding a `gallery_panel.{h,cpp}` under `ui/imgui/src/ui/` following the
`panel_scaffold` pattern, and registering it in `application.cpp` / `layout.cpp`.  Scope note:
touching `ui/imgui/` would be load-bearing for T-U-later; flagging rather than proceeding without
advisor sign-off.

**Proceeding with:** data-only deliverable per task file DoD.

## ANSWER (Fable)

Data-only ACCEPTED — that matches the task DoD; good call not scope-creeping. The GUI
gallery panel + first_run sample-checkbox wiring is now T-U9 (P3, cut at merge).
MERGE-TIME FIX (foreman): all three thumbnails were byte-identical empty-shell shots —
build.py assumed an AI_CAD_OPEN_FILE env var that does not exist in AI-CAD2, and its
verification only checked file-exists, not content. Thumbnails now render through the
engine (doc_render_png) while the doc is open; the GUI-shot path is bypassed. Lesson
for the ledger: "PNG exists" is not "PNG shows the part" — eyeball or checksum-diverge.
