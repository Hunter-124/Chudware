# host/ — DEPRECATED (reference-only, end-of-life)

The legacy SolidWorks-clone ImGui desktop host is **deprecated**. Active GUI
development lives in [`ui/imgui/`](../ui/imgui/) over the framework-agnostic
seam in [`ui/abstract/`](../ui/abstract/).

## GUI ownership policy (T-G24, 2026-07-05)

- **`ui/imgui/` is the ONLY target for new GUI work.** Do not patch `host/` for any
  new feature or bug fix. If a change seems to require touching both trees,
  **stop — get explicit lead approval before double-patching.**
- `host/` is kept as **reference-only source** for the T-G23 Chrome-port work and
  is deleted in full by T-G11 (after Windows verify AND after T-G23 + T-G25 land).
- The repo-root `app` symlink (→ `ui/imgui`) has been removed (T-G24). Use
  `ui/imgui/` paths directly. Build output stays at `build/linux-app/`.

## Deletion criteria (T-G11 gate — depends on T-G23 + T-G25)
Do **not** delete this tree until ALL of the following are green:
1. `ui/imgui` builds standalone (no `host/build/_deps` FetchContent — vendored in-repo).
2. Feature-surface parity: every `cmd_id` in `ui/abstract/command_registry` routes to a handler.
3. Headless `--ui-screenshot` smoke passes on `ui/imgui` binary for m1/tree/ecad shots.
4. `scripts/feature_surface_audit.py` retargeted to `ui/imgui` passes.
5. T-G23 (Chrome port) and T-G25 have landed — `host/` is no longer needed as reference.
6. Human-verified Windows build recorded in T-G11.
