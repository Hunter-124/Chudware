# T-U7 questions

## Q1: "opens from doc tabs" interpretation

**Context:** The DoD says "each doc kind opens in its viewer from the doc tabs." The current `doc_tabs.cpp` `NewDocKind` enum only covers Part, Assembly, Drawing, Schematic, PCB. Piping/hydraulic/structural are not doc-tab kinds.

**Question:** Should T-U7 add Piping/Hydraulic/Structural to `NewDocKind` + `createNewDoc()` (with new-doc creation via worker RPC) — or is the DoD intent satisfied by the scenario/shot pattern (seed from worker RPC, open as floating panel, same as BOM/Parts)?

**Recommended answer:** Follow the panel pattern (floating closable windows) for v1, same as T-U2/U4. Full doc-tab integration (open-from-file, tab title, tab close) is a v2 concern. State this explicitly in the review check-in. Adding 3 doc kinds to `NewDocKind` requires coordinating with foreman/Erik since `doc_tabs.cpp` touches the main document management path.

**Status:** Proceeding with panel pattern (wantPiping/wantHydraulic/wantStructural one-shot shots). No doc-tab changes in this PR.

---

## Q2: T-U6 (sim_view.h) dependency

**Context:** DoD says "reuse T-U6 result rendering where shared." T-U6 is `status: doing` — not merged.

**Recommended answer:** Implement our own results side-list in the shared `net_canvas_panel` (right column). Mirror T-U6's approach when it merges. No dependency.

**Status:** Proceeding without T-U6 dependency.
