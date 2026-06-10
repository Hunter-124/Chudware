# Session Cards — build-out work orders

Each card is a self-contained prompt for one agent session (Sonnet/Opus) or one human-driven
Claude Code session. Cards reference the frozen contracts in `docs/contracts/` — read them, never
edit them. The orchestrator dispatches cards in waves and owns integration + commits.

## Conventions (every card inherits these)

- **Repo root**: `C:\Users\nigga\Desktop\AI-opencad`. Work from there.
- **Python**: `%APPDATA%\AI-CAD\runtime\venv\Scripts\python.exe` (py 3.11). Run tests as
  `& $py -m <package>.<test_module>` from repo root. Tests are **plain `main()` runners with
  asserts** (see `engine/test_kernel_aliases.py` for the house style) — NOT pytest.
- **C++ build**: `cmake -S host -B <BUILD_DIR> -G "Visual Studio 17 2022" -A x64` then
  `cmake --build <BUILD_DIR> --config Release`. Use the BUILD_DIR your card assigns (parallel
  agents must not share build dirs). MSVC is not on PATH — cmake finds it via the generator.
- **Ownership**: write ONLY inside your card's "Owned paths". If you need a change in a shared
  file (`engine/worker.py`, `engine/doc/__init__.py`, `host/CMakeLists.txt`,
  `host/src/ui/ribbon_def.cpp`), DO NOT edit it — list the exact change in your final report.
- **No git**: never commit/stage; leave changes in the working tree. The orchestrator commits.
- **No contract edits**: `docs/contracts/*` and `docs/PLAN.md` are read-only. If a contract is
  wrong or blocks you, STOP that part and report the conflict instead of improvising around it.
- **Units**: canonical mm/degrees/grams everywhere (doc_schema.md §0). Equations trig = degrees.
- **Code style**: match the surrounding code. Python: type hints, no heavy deps beyond the venv.
  C++: C++17, no exceptions across module boundaries, follow existing file structure.
- **Continuation criteria** (ported from the runtime orchestrator design): if the SAME error
  defeats you 3 times in a row, or you're about to exceed your card's scope, stop and report
  what you tried — a stuck honest report beats a flailing workaround.
- **Report back** (your final message): what you built (file list), test output (verbatim tail),
  shared-file registrations needed, deviations from the card, and anything the next card should
  know. Your final message goes to the orchestrator, not the user — raw facts, no salesmanship.

## Deck index

| card | title | model | wave | depends |
|---|---|---|---|---|
| C01 | Doc compiler core + primitive/script/datum features | opus | 0 | — |
| C02 | AICM tessellation writer | sonnet | 0 | — |
| C03 | Selector build + resolve | sonnet | 0 | — |
| C04 | gui_app.cpp mechanical split | opus | 0 | — |
| C05 | Icon pipeline (atlases) | sonnet | 0 | — |
| C06 | nsolve numeric core | opus | 0 | — |
| C07 | CFD: D3Q19 LBM wind tunnel | sonnet | 0 | — |
| C08 | FEA pack: mesher + static + modal + sim worker | sonnet | 0 | wheels installed |
| C09 | Dress + extrude/revolve feature compilers | sonnet | 1 | C01, C03 |
| C10 | Sketch constraint solver | sonnet | 1 | C06 |
| C11 | Doc sessions + worker doc_* RPC + minimal .step+ | opus | 1 | C01–C03, C09 |
| C12 | Evaluate compute layer | sonnet | 1 | C02 |
| C13 | Theme roles + SolidWorks-light + runtime switch | sonnet | 2 | C04 |
| C14 | Ribbon widgets + command table | sonnet | 2 | C04, C05, C13 |
| C15 | AICM loader + ID-buffer picking | opus | 2 | C02, C04 |
| C16 | Feature tree + doc mirror + selection round-trip | sonnet | 2 | C11, C15 |
| C17 | PropertyManager + core feature forms + M1 integration | opus | 2 | C14, C16 |
| C18 | Sketch mode UI | opus | later | C10, C17 |
| C19 | Simulation + flow panels | sonnet | later | C08, C07, C17 |
| C20 | Assembly: mates + ops + panels | opus | later | C06, C17 |
| C21 | Formats finalization (.step+ full, import/export matrix) | sonnet | later | C11 |
| C22 | AI orchestrator + Activity UI + prompt editor | opus | LAST | all |
| C23 | Hardening, wheelhouse, release | sonnet | LAST | C22 |
