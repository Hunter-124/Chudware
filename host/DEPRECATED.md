# host/ — DEPRECATED (end-of-life)

The legacy SolidWorks-clone ImGui desktop host is **deprecated**. Active GUI
development lives in [`ui/imgui/`](../ui/imgui/) over the framework-agnostic
seam in [`ui/abstract/`](../ui/abstract/).

## Deletion criteria (N4b gate)
Do **not** delete this tree until ALL of the following are green:
1. `ui/imgui` builds standalone (no `host/build/_deps` FetchContent — vendored in-repo).
2. Feature-surface parity: every `cmd_id` in `ui/abstract/command_registry` routes to a handler.
3. Headless `--ui-screenshot` smoke passes on `ui/imgui` binary for m1/tree/ecad shots.
4. `scripts/feature_surface_audit.py` retargeted to `ui/imgui` passes.

Until then, `host/` remains the production binary and supplies GL deps + panel
editors being rehosted workspace-by-workspace into `ui/imgui/`.
