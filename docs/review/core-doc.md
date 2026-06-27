# Phase 1 Review — core-doc track

**Branch:** feat/gui-rewrite  
**Ownership:** `core/doc/**`, `core/tests/test_selectors.cpp`, `core/tests/test_picks.cpp`, `core/tests/test_document*`  
**Focus:** selectors / picks / session active changes, render tint, `doc_adjacency` / `doc_set_meta` / `doc_get_meta`, contract alignment (read-only `docs/contracts/selectors.md`).

---

## Fixed

1. **`side_word` oblique-face bug** (`selectors.cpp`) — Dominance threshold was `0.7` with `m == az` float equality, so ~45° normals (e.g. `|z| ≈ 0.707`) incorrectly received a `side` label instead of `null` per contract §1. Raised threshold to `0.92` (matching `orient_tint`) and replaced equality with max-component tie-break.

2. **`doc_adjacency` face ordinal mismatch** (`session.cpp`) — Face indices for `ord` / `between` were derived from `TopExp::MapShapes`, while `describe_faces`, `doc_query`, and face selectors use `TopExp_Explorer` order. Rebuilt the face index map with `TopExp_Explorer` so adjacency ordinals align with query-match positions.

3. **`doc_adjacency` incomplete edge/face payload** (`session.cpp`) — Added missing `surf` on shared edges (line/circle/ellipse/bspline) and `concavity` on faces (via `make_selector`), matching the `session.h` RPC shape.

4. **Stale hint-fallback documentation** (`picks.h`) — Header still described the dress.py `0.01` gate; implementation correctly uses `0.35` (resolve acceptance bound). Comment updated.

5. **Stale concavity comment** (`picks.cpp`) — QCand comment still referenced `TopAbs_REVERSED`; code uses geometry-based `cylinder_face_concave()`. Comment updated.

**Note:** `doc_adjacency`, `doc_set_meta`, and `doc_get_meta` were already implemented in `session.cpp` (not missing). No stub removal needed.

**Render tint:** Reviewed per-face averaged-normal tint path (`render_iso_png`). Logic is correct for the stated goal (planar FaceRec only; curved faces neutral; mesh fallback). No code change — see Flagged for edge cases.

---

## Flagged

| Item | Location | Notes |
|------|----------|-------|
| **`sid` field not in contract** | `selectors.cpp` `make_selector` | Emits `sid` (GSL W1 stable handle). Not in `docs/contracts/selectors.md` §1. Informational only; resolve ignores it. |
| **resolve() extensions beyond §3** | `selectors.cpp` `resolve()` | Signed-normal tie-break (F5) and coaxial area tie-break go beyond normative contract algorithm. Intentional pragmatic fixes; contract / torture suite unchanged for axis-less cases. |
| **Line-edge scoring extension** | `selectors.cpp` `score_candidate` | Perpendicular-distance scoring for line-vs-line not specified in contract §3. Matches fillet-preview trimming behavior. |
| **`doc_adjacency` concavity often null** | `session.cpp` | `describe_faces` does not set `TopoInfo::concave`; only geom-query path sets it via `cylinder_face_concave`. Adjacency `concavity` field present but usually `null` until describers enrich cylinders. |
| **`doc_set_meta` / `doc_get_meta` semantics** | `session.cpp` | No undo step, no revision bump, no empty-key validation. Matches “opaque metadata” intent but differs from mutating ops that push undo. Product decision. |
| **Face pick id space assumption** | `selectors.cpp`, `document.cpp` | `make_pick_selector` (faces) uses `describe_faces` / Explorer order; `face_edges` uses `MapShapes`. Code assumes Explorer == MapShapes for faces on simple solids. **Cross-track:** verify against tessellator AICM face ids. |
| **Render tint on reversed planar faces** | `session.cpp` `render_iso_png` | Averaged triangle normals on a reversed planar FaceRec could partially cancel if tessellation winding is inconsistent; rare. Monitor in Phase 2 vision review. |
| **Query vs describer plane axis sign** | `picks.cpp` | Geom query keeps unsigned geometric plane axis for scoring; `rep_dir` is signed. Documented intentional; F5 score path unchanged. |

---

## Tests added

| Test | File | CMakeLists |
|------|------|------------|
| **B11** — oblique face `side` is `null` when `rep_dir` is 45° off-axis | `core/tests/test_selectors.cpp` | Existing `test_selectors` target — **no CMakeLists edit** |

Existing torture coverage retained: A1–A8 (pure resolve), B7–B10 (describers + end-to-end), extensive geom-query suite in `test_picks.cpp`. Phase 2 should run `test_selectors` + `test_picks` binaries.

**Suggested Phase 2 additions (not implemented):** session-level `doc_adjacency` / `doc_set_meta` round-trip; render PNG tint regression on curved vs planar body.

---

## Files touched

| File | Change |
|------|--------|
| `core/doc/selectors.cpp` | Fix `side_word` threshold / tie-break |
| `core/doc/picks.h` | Fix stale hint-fallback comment |
| `core/doc/picks.cpp` | Fix stale concavity comment (prior diff: GSL Stage 0 query read-back fields) |
| `core/doc/session.cpp` | Fix `doc_adjacency` indexing + payload; render tint + meta/adjacency already present in branch |
| `core/tests/test_selectors.cpp` | Add B11; prior diff: A8, B10 GSL assertions |
| `docs/review/core-doc.md` | This report |

**Read-only reviewed (no edits):** `selectors.h`, `picks.h` (API), `session.h`, `docs/contracts/selectors.md`, `test_picks.cpp`, `test_document.cpp`, other `core/doc/*` sources.

---

## Summary counts

| Metric | Count |
|--------|------:|
| **Fixes** | 5 |
| **Flags** | 8 |
| **Tests added** | 1 (B11) |
| **Files touched** | 6 |
