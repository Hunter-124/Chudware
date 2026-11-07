# Developer documentation

Engineering conventions and subsystem notes for contributors. Start with
[`AGENTS.md`](../../AGENTS.md) and [`ARCHITECTURE.md`](../../ARCHITECTURE.md) for the
repository layout; reference material lives in [`docs/reference/`](../reference/).

| Document | Covers |
|---|---|
| [engineering-notes.md](engineering-notes.md) | Build/gate/smoke commands, testing and memory-cap conventions, engine, daemon, document, simulation and manufacturing invariants |
| [omp-integration.md](omp-integration.md) | Official OMP boundary, session lifecycle, CAD tools, events, and runtime pins |
| [gui-architecture.md](gui-architecture.md) | GUI subsystem map, shared files, `CHUDWARE_SHOT` scenario harness, ribbon/commands, viewport, sketcher and icons |
| [ux-quality.md](ux-quality.md) | Finite numeric, command and fillet audit inventory, behavioral acceptance and evidence protocol; not a product certification |
| [gui-theme-layout.md](gui-theme-layout.md) | Palette and glass, DPI metrics, `ui::kit`/`ui::anim` vocabularies, layout, and the `gui_lint.py` rules |
| [gui-workspaces.md](gui-workspaces.md) | Workspace tabs (AI, CAM/Manufacture, Reverse Engineering, Simulation, Firmware, ...) and auxiliary panels |
| [evaluate-tools.md](evaluate-tools.md) | Measurement, mass properties and interference tools shared by agents and the Evaluate tab |
| [engineering-checks.md](engineering-checks.md) | Agent DFM, stress, assemblability and harness checks, evidence and registration |
| [website-request-logging.md](website-request-logging.md) | chudware.com request logging: live tail, Analytics Engine dataset columns and queries |
| [SCAD file formats](../reference/cad-file-formats.md#openscad-source-and-native-documents) | SCAD import/rendering and native documents; [document operations contract](../../contracts/doc_ops.md#36-openscad-source-and-managed-conversion-append-only); agent guidance: `cad_guide('scad')` |
