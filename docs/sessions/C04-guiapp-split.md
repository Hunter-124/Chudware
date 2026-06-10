# C04 — gui_app.cpp mechanical split (behavior-preserving)

Model: **opus** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
Split the ~1.9k-line `host/src/gui_app.cpp` monolith into modules WITHOUT changing behavior.
This gates every other C++ workstream. Zero new features, zero styling changes.

## Read first
1. `host/src/gui_app.cpp` (all of it), `host/src/main.cpp`, `host/CMakeLists.txt`
2. `docs/PLAN.md` §3.1 "Host modularization" (target layout)

## Owned paths
`host/src/**` (moves + new files) · `host/CMakeLists.txt` (EXCEPTION: you may edit it — you are
the only C++ agent in this wave) · `scratch/split_baseline/**`

## Procedure (order matters)
1. **Baseline FIRST**: build unmodified (`cmake -S host -B host/build -G "Visual Studio 17 2022"
   -A x64; cmake --build host/build --config Release`), then capture every headless screenshot
   mode into `scratch/split_baseline/`:
   for MODE in (default, chat, assembly, plan, settings):
   `$env:AI_CAD_SHOT=MODE; .\host\build\Release\AI-CAD.exe --ui-screenshot scratch\split_baseline\<mode>.png`
   (unset AI_CAD_SHOT for the default one). Note: the exe needs `AI_CAD_REPO` +
   `AI_CAD_PYTHON` env vars for worker features, but screenshot modes render without a live
   worker — if a mode hangs, kill it and note which.
2. Split into:
   - `host/src/core/app_state.h` — shared structs (Settings, JobState, ChatMsg, AgentStep,
     AgentEvent, PlanInfo, PartInfo, FitReport, …) + the app-state aggregate
   - `host/src/core/jobs.cpp/.h` — launchJob, event drain helpers, worker lifecycle calls
   - `host/src/ui/legacy_chat.cpp/.h` — the Design panel (prompt, conversation, iteration
     cards, plan card, build-code section)
   - `host/src/ui/settings_modal.cpp/.h`
   - `host/src/ui/console_panel.cpp/.h`
   - `host/src/ui/title_bar.cpp/.h` — menu bar + window buttons + chrome::HitLayout publishing
   - `host/src/ui/status_bar.cpp/.h`
   - `host/src/ui/viewport_panel.cpp/.h` — viewport draw + parts list + fit readout + pills
   - `host/src/app.cpp` — run_gui(): GLFW/ImGui init, dockspace, panel calls, frame loop,
     screenshot modes
   - `gui_app.cpp` shrinks to nothing and is DELETED (update CMake source list)
3. Keep function/struct names; prefer moving code verbatim over refactoring. Internal linkage
   (`static`/anon-namespace) becomes either member of the moved file or a small shared header —
   choose the least-churn option each time.
4. Rebuild + regenerate the same screenshots into `scratch/split_after/`; byte-compare with
   `fc.exe /b`. If any differ, diff visually (load both, describe) and either fix or justify
   (e.g. embedded timestamp) in your report. The goal is BYTE-IDENTICAL.

## Constraints
- `main.cpp` entry points (`run_gui`, CLI modes) must keep their exact signatures.
- No header reorganization beyond what the split forces. No clang-format pass (noise).
- Do not touch `renderer.*`, `mesh.*`, `worker_client.*`, `bootstrap.*`, `win_chrome.*`,
  `ui_theme.*` beyond #include path fixes.

## Verification
Release build green; all 5 screenshot modes byte-identical (or justified); report the new file
list with line counts.
