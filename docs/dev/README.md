# Developer documentation

Engineering conventions and subsystem notes for contributors. Start with
[`AGENTS.md`](../../AGENTS.md) and [`ARCHITECTURE.md`](../../ARCHITECTURE.md) for the
repository layout; reference material lives in [`docs/reference/`](../reference/).

| Document | Covers |
|---|---|
| [engineering-notes.md](engineering-notes.md) | Build/gate/smoke commands, testing and memory-cap conventions, engine, daemon, document, simulation and manufacturing invariants |
| [chudcode-integration.md](chudcode-integration.md) | The Chudware/Chudcode boundary, `chud --mode rpc` session lifecycle, host tools, events, and the runtime version pin |
| [gui-architecture.md](gui-architecture.md) | GUI subsystem map, shared files, `CHUDWARE_SHOT` scenario harness, ribbon/commands, viewport, sketcher and icons |
| [gui-theme-layout.md](gui-theme-layout.md) | Palette and glass, DPI metrics, `ui::kit`/`ui::anim` vocabularies, layout, and the `gui_lint.py` rules |
| [gui-workspaces.md](gui-workspaces.md) | Workspace tabs (AI, CAM/Manufacture, Reverse Engineering, Simulation, Firmware, ...) and auxiliary panels |
| [evaluate-tools.md](evaluate-tools.md) | Measurement, mass properties and interference tools shared by agents and the Evaluate tab |
