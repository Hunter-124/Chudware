# Conveying Geometry to the LLM — Audit, Test Findings & Improvement Strategy

*Status: design writeup, 2026-06-26. No code changed — this is a review + proposal while the GUI rework is in flight.*

*Status update, 2026-07-01 (AI-experience lane):* Stages 0–2a of Part 3 had already shipped in earlier waves (`describe_part`/`describe_sketch`/`describe_scene` text channel, owner_map join, cad_guide('gsl')). This lane added the remaining deterministic layer: **write-path warnings** (`check_feature_overlap` + `add_features` `overlap_warnings`, core `eval_feature_overlap` over `CompileResult::tool_recs`), **boolean compare** (`eval_compare` RPC → match_pct / missing / extra / residual regions), **face wires** (`doc_face_wires` → doc-schema sketch entities, exposed as `convert_face_to_sketch`), and the **deterministic reverse-engineering pipeline** (`services/mcp/recon_tools.py` `reverse_engineer`: 5-pass recognition → checkpointed rebuild → eval_compare gating). Measured residual now exists: `RECOGNITION_RESIDUAL.md` — deterministic recon is sufficient (NO-GO for GNN) on turned/plate/pocketed/bladed families; adapter training justified (GO) only for ribbed_bracket, shelled_enclosure, swept_pipe, per the §3.4/§3.6 gate.

This document answers three questions:

1. **How does CAD geometry actually reach the model today?** (a complete channel map, code-grounded)
2. **Where does it break?** (live tests on a box, an organic vase, and a real imported impeller, plus edge cases)
3. **How should we improve it — including a custom "intermediate model" between the B-rep kernel and the LLM?** (staged proposal — see Part 3, added after the design pass)

The short version: **we feed the model the design *recipe* cleanly, but we feed it the resulting *geometry* as raw B-rep numerics with the semantics stripped out** — even though the kernel already computes those semantics internally and throws them away at the last step. The biggest wins are not ML; they're *emitting what we already know*, plus a thin deterministic "geometry digest" layer. A learned model earns its keep only in a few specific places (organic/imported feature recognition). Details below.

---

## Part 0 — The mental model: geometry reaches the LLM through 7 channels, not 1

| # | Channel | What the model gets | Quality |
|---|---------|---------------------|---------|
| 1 | `query_geometry` read-back | per-face/edge `{kind, surf, axis, centroid, area_or_len, owner_feature, hint_index}` | **Raw & lossy** |
| 2 | `get_part` feature tree | the parametric recipe (feature id/name/type/params) + `owner_map` raw topo ids | **Clean recipe, but unstable ids** |
| 3 | `add_features` return (most-traveled) | `{ok, revision, body_count, volume_mm3, per-feature ok/error/warning}` | **No semantic confirmation** |
| 4 | `render_part` vision | one 512×400 annotated isometric PNG | **Great prismatic, misleading organic** |
| 5 | analysis text (`measure_*`, `inspect_section`, `wall_thickness`, `draft_analysis`…) | rounded `summary` strings **+** raw tensors; spatial fields dead-end in a host `.aicm` file | **Mixed** |
| 6 | internal agent-loop feedback (`_feedback_ok`) | interpreted text: fill %, face histogram, "NOT hollow", "no round holes" + 6-view contact sheet | **Best — but not exposed to external LLM** |
| 7 | `cad_guide` / cheatsheets | the static *input* vocabulary (side words, surf, pick predicates) | **Good, but input-only** |

The asymmetry across these is the whole story: **input is rich plain-English; output is raw numerics.** The model speaks "the top face" but is answered in `axis:[0,0,1], centroid:[…,…,10.0]`.

---

## Part 1 — How geometry reaches the LLM today (code-grounded map)

### 1.1 The read-back channel — `query_geometry` → `make_selector`

`query_geometry` (`mcp_server/cad_tools.py:868`) → `doc_query` → `resolve_geom_query` (`core/doc/picks.cpp:598`) → **`make_selector`** (`core/doc/selectors.cpp:307`). Every matched face/edge is serialized as exactly these 7 fields:

```
kind          "face" | "edge" | "vertex"
surf          "plane"|"cylinder"|"cone"|"sphere"|"torus"|"bspline"|"other" | null
centroid      [x, y, z]            (mm, raw f64)
axis          [x, y, z] | null     (UNSIGNED surface/curve axis)
area_or_len   <number> | null      (mm² or mm, raw f64)
owner_feature <feature id> | null
hint_index    <int>                (internal tie-break ordinal)
```

What's wrong with this, concretely (all observed live, see Part 2):

- **No `side` label** — even though the tool's own docstring promises "Returns each matching face/edge with its shape (surf), **side**, centroid and size" (`cad_tools.py:873`). The engine computes the side internally (`parse_side`, `picks.cpp:507`) **only to score candidates**, then discards it. The model must re-derive top/bottom/left/right/front/back from an unsigned axis + a centroid sign.
- **`axis` is unsigned** — opposite faces (the +X and −X faces of a box) emit the *same* `axis:[1,0,0]`; only the centroid sign distinguishes them. A signed outward normal *is* computed (`rep_dir`, `selectors.cpp:111`) but is marked "NOT serialised" (`selectors.h:56`).
- **Float noise everywhere** — `area_or_len: 599.9999999999999` for a 600 mm² face; centroids like `-1.38e-17` instead of `0`; `6.45e-12` on imported parts. Nothing is rounded before serialization.
- **`hint_index` is internal plumbing** leaking into the model's context.
- **Rich semantics the engine *already computes* but never emits:** signed outward normal, the side word, `rep_dir` (outward direction for *every* face incl. curved), **cylinder concavity = bore/hole vs boss/fillet** (`cylinder_face_concave`, `picks.cpp:136`), and surface radius. The picker uses all of these; `make_selector` ships none of them.

### 1.2 The design-intent channel — `get_part` feature tree

`get_part` (`cad_tools.py:818`) returns the parametric feature DAG: `{id, name, type, params, refs, suppressed}` per feature, plus an `owner_map` of feature → `[body, topo_id]` topology. **This is the cleanest text channel** — it's semantic, editable, round-trips through the native `.step+`. Two problems:

- It describes the **recipe**, not the **resulting B-rep**. "box(40,30,20) then fillet r3" tells you the intent, not which faces/edges now exist or how they relate.
- `owner_map` exposes **raw `[body, topo_id]` pairs that are not stable across rebuilds** — a fillet re-owns and renumbers faces via TShape diffing. So the model cannot use them as durable handles.

### 1.3 The write-path channel — `add_features` return (the one the agent lives in)

Every build op returns `{ok, revision, body_count, volume_mm3, per-feature {ok, error, warning}}` (`cad_tools.py:788`). This is the **most-traveled** geometry signal — the agent builds far more than it queries.

*Tested live, with a useful surprise:* the engine **does** guard total no-ops. A primitive cut positioned to miss the body is rejected with `"primitive cut removed no material"` and the whole batch rolls back; a sketch+extrude-cut whose profile misses is rejected with `"extrude cut removed no material"`. That's a real strength worth keeping. **The gap is *intent-level* confirmation, not total no-ops:** an op that lands but in the *wrong place*, at the *wrong size*, or removes the *wrong* material still returns `ok:true` with no flag (a boss at the wrong height, a hole offset from where it was asked, a cut that bit a neighboring wall). The return tells the model *something changed* and *roughly how much volume*; it never asserts *the change matched the request* ("hole now centered on the top face, through-all, ⌀5"). The model must re-establish correctness from the query/vision channels.

### 1.4 The vision channel — `render_part`

`render_part` (`cad_tools.py:883`) → `doc_render_png` (`core/doc/session.cpp:4397`): a single **512×400 isometric** PNG, software-rasterized, with faces tinted by direction and the principal ones labelled TOP/BOTTOM/FRONT/BACK/RIGHT/LEFT, plus an XYZ gizmo. Mechanics that matter:

- Direction tint only applies when one normal component ≥ 0.92, else neutral grey. Good for boxes; **per-triangle on a curved face it splatters red/green blotches** (see the vase, Part 2.2).
- Labels only drawn for camera-facing faces (`dot(normal, cam) ≥ 0.15`); **back faces are unlabelled and may be occluded.**
- Single fixed viewpoint `d≈{1,1,0.8}`.
- **Capability asymmetry:** the *internal* agent loop renders a **6-view contact sheet** (`render_grid`, `loop.py`), but the MCP `render_part` tool an *external* LLM calls returns only the single isometric. An MCP-driven agent is strictly more blind than the in-loop one.

### 1.5 The analysis channels — the good pattern, half-applied

`measure_part`, `measure_between`, `inspect_section`, `check_symmetry`, `wall_thickness`, `draft_analysis` each emit a **clean, rounded, unit-labelled `summary` string** — e.g. `"thinnest wall = 0.2895 mm (mean 18.69 mm)"`, `"area = 12820.8 mm^2"`. **This is exactly the pattern the read-back channel lacks.** But the same payloads also dump raw inertia/section tensors with `e-14` noise and arbitrary-sign principal axes, and — critically — **spatial field data dead-ends at the host**: `wall_thickness`/`draft_analysis` write *where* the thin wall / failing draft is to a `.aicm` file the LLM cannot read; only the scalar min/mean/max reach the model. It knows a wall is 0.29 mm *somewhere* but cannot locate it.

### 1.6 The internal-loop feedback — we already prototyped the intermediate layer

`_feedback_ok` (`loop.py:543`) is the most interesting thing in the codebase for this discussion. It already does light **semantic interpretation**:

```
Build OK. Bounding box: 80.0 x 60.0 x 6.0 mm | 28800 mm^3. Triangles: 4321.
Solid material fills 95% of the bounding box. If this is meant to be a hollow
enclosure/case, it is NOT hollow - subtract the interior volume.
Faces: 26 total, 20 planar, 6 cylindrical.
```

and the zero-cylindrical-faces heuristic: *"Zero cylindrical faces = this part has NO round holes or bosses … they do not exist yet - their cutters/solids missed the body."* (`loop.py:557`). **This is an embryonic intermediate model** — exactly the kind of translation we want — but it lives inside the Python loop, is global/coarse (a decorative boss masks a missing required bore), and is **not exposed on the MCP surface.**

---

## Part 2 — Live test findings (box, organic vase, imported impeller)

I drove the real engine through the MCP tools. Verbatim observations:

### 2.1 Prismatic box (40×30×20) — the happy path

`query_geometry({"of":"face"})` → 6 faces, each `surf:"plane"`, e.g. the +X and −X faces **both** report `axis:[1,0,0]`, distinguished only by `centroid` x = ±20; areas come back as `599.9999999999999` / `1199.9999999999998`; centroids carry `e-16` dust. No `side`. The **render is excellent**: clean isometric, TOP (blue) / RIGHT (red) / BACK (green) labelled, gizmo correct. *For boxes the system works — and that's most of the QA corpus, which is why this hasn't surfaced as pain.*

### 2.2 Organic vase (sketch + 360° revolve) — the text channel collapses

- `query_geometry({"of":"face"})` → **`count: 1`**: the entire wall+base is **one face**, `surf:"other"`, `axis:null`, `centroid:[~0, ~0, 25.83]` (the wall's area-centroid — meaningless as a handle). `query_geometry("top")` returns **that same blob**.
- `query_geometry({"of":"edge"})` → 3 edges, **two of them degenerate zero-length edges at `[0,0,0]`** (`area_or_len: 0.0`) plus one seam. **There is no addressable rim, lip, or opening.**
- The **render is actively misleading**: the curved body is mostly grey (correct) but smeared with **red/green blotches** where local triangles happen to face ±X/±Y, with a single floating **"RIGHT"** label mid-body (meaningless on a surface of revolution).

**Takeaway:** the plain-word vocabulary — and the text read-back entirely — **degenerates to noise on exactly the organic bodies the cheatsheet tells the model to build** (revolve/loft/sweep). On organic parts the model is flying on vision alone, and the vision is degraded too.

### 2.3 Imported centrifugal impeller (real STEP) — token blow-up + zero structure

- `query_geometry({"of":"face"})` → **68 faces (~13,000 characters)**: a flat, unlabelled list of `cylinder`/`cone`/`plane` blobs with `e-12` float dust and `owner_feature:null` (no provenance on imports).
- The data is **full of exploitable structure the model must reverse-engineer by hand**: `area_or_len` values `355.536`, `218.265`, `123.75`, `88.196`, `52.63`, `42.97`, `19.14`, `13.21` each **repeat ~5–6×** — that repetition *is* the blade count and the rotational symmetry. Nothing groups them. No "hub + shroud + 6 blades (instanced)". The model gets 68 anonymous primitives and must infer it's an impeller.
- The good channels still work: `measure_between(largest flat face)` → `"area = 12820.8 mm^2"`; `wall_thickness` → `"thinnest wall = 0.2895 mm"`; `inspect_section("front")` → `"cross-section area … = 2742.99 mm^2"`. These rounded summaries are the model to emulate.

### 2.4 Edge-case behaviors worth noting

- **Ambiguity guard is good:** `_resolve_one_selector` (`cad_tools.py:484`) *refuses* to measure when a query ties (e.g. "the vertical edges" → 4 matches) unless a tie-break predicate is present — "no silent wrong geometry." This is the right instinct and should be preserved.
- **Degenerate edges leak:** zero-length seam/pole edges are emitted as if addressable (vase) — a footgun for "fillet an edge".
- **front = −Y** (`picks.cpp:513`) is a non-standard convention never reconciled with the render's "FRONT" label or the model's spatial prior.

---

## The 5 highest-leverage weaknesses (the scoreboard for any fix)

- **W1 — No persistent, stable, round-trippable face/edge identity.** Output is unstable topo ids or fuzzy centroid fingerprints; input is English predicates. The model can't name a face once and refer to it again. *Root cause of editing fragility — dwarfs the rest.*
- **W2 — The write-path confirms *change*, not *intent*.** The engine guards total no-ops well (tested: `"extrude cut removed no material"` rolls the batch back), but a feature that lands in the wrong place / wrong size / removing the wrong material returns `ok:true` with no flag. There is no positive "the op matched the request" assertion.
- **W3 — No topological adjacency / feature-face provenance is ever serialized.** "the edge between the top and front face," "the faces this pocket cut into" are inexpressible and unverifiable.
- **W4 — Spatial field data dead-ends at the host.** Thickness/draft/curvature *locations* live in a `.aicm` file the model can't read; only scalars escape.
- **W5 — Organic/curved geometry degenerates to a single `"other"` blob** with no addressable sub-features, and the vision tint misleads on curved faces; front=−Y contradicts priors.

A recurring meta-finding sits behind all five: **the kernel already computes side, signed normal, rep_dir, bore-vs-boss concavity, radius, and adjacency (OCCT `TopExp`) — and strips them out one function before the model sees anything.** A large fraction of the fix is *stop throwing away what we compute.*

---

## Part 3 — The recommended intermediate-model architecture

*Produced by a 5-architect design panel (deterministic digest, learned B-rep GNN, structured multi-view vision, geometry-language-server, hybrid-staged) → a 3-judge panel (pragmatic-shipping, correctness-fidelity, long-horizon). All three judges independently converged on the same ranking and the same Stage 0, and verified that Stage 0 is real in source.*

### 3.0 The verdict in one paragraph

Build a **deterministic Geometry-Semantics Layer (GSL)** that sits between the OCCT kernel and every place we feed the model (`query_geometry`, `get_part`, `add_features` return). Feed it from the semantics the kernel **already computes and currently deletes**. Expose it not as one fat dump but through a **progressive-pull, "language-server"-style surface** (outline → hover → diff) so token cost decouples from face count. Fold in two cheap render fixes so the *picture* and the *text* address the **same numbered entity**. A **learned model is explicitly deferred** — it earns its keep only on freeform/scanned imports, and only after we measure deterministic recognition actually failing there. The first PR is ~1–2 days and is almost pure deletion-of-deletions.

> The panel's framing that stuck: **the highest-leverage "ML" win on this codebase requires zero ML — it requires not throwing data away.** The kernel computes a signed outward normal, the side word, `rep_dir` for every face, bore-vs-boss concavity, radius, and owner-provenance — and discards all of it at four named source lines (`core/doc/picks.cpp:927-938` copies only `kind/surf/centroid/axis/measure` out of the `QCand` that already holds `rep_dir`/`concave`/`radius`; `core/doc/selectors.cpp:307` `make_selector` emits the internal `hint_index` but none of the semantics).

### 3.1 What the intermediate layer is (and isn't)

**It is** a *semantic aggregation + stable-naming + progressive-disclosure* layer. Three responsibilities:

1. **Promote** per-face kernel facts into a named feature hierarchy (holes, bores, bosses, fillets, walls, caps, blades) with **symmetry/instance collapse** ("6 blades, circular pattern about +Z") instead of a flat list of anonymous primitives.
2. **Name** every addressable entity with a **stable, round-trippable ID** that survives a rebuild (the W1 fix) and is accepted back as an input selector.
3. **Serve it progressively** — an outline by default; details, adjacency, fields, and the raw face list are *pulled on demand* — so a 68-face impeller and a 500-face turbine cost about the same.

**It is not** a learned "B-rep tokenizer," and it is not a vision model. Vision stays an *annotator of the digest*, never the addressing source of truth (pixels are lossy, occluded, non-metric).

### 3.2 Target representation — what the model should receive

Today the impeller is 68 anonymous faces / ~13k chars and the vase is one `surf:"other"` blob. The GSL turns them into this (≈900 and ≈400 chars respectively):

**Impeller** (deterministic, from the feature tree + symmetry detection):
```
Part "impeller"  bbox 120×120×46 mm  | 1 body | 68 faces
Symmetry: 6-fold rotational about +Z (source: feature tree → BladeRing)
- hub:   boss cylinder ⌀40×22  +  bore_1 through-hole ⌀12 on +Z (CONCAVE)
- shroud: conical cap, opening +Z
- blade ×6  [INSTANCED, 60° circular pattern about +Z]
    each = pressure face + suction face + tip plane + root fillet r1.5
    address any as blade[0..5] or "all blades"; representative = blade[0]
addressable: hub.bore, blade[n].pressure, blade[n].root_fillet, shroud
```
**Vase** (the case where determinism is weakest — note the *synthesized* handles and the explicit warning):
```
Part "vase"  bbox 80×80×150 mm  | revolve about +Z (axisymmetric)
- wall_1:  revolved bspline wall; profile r18→40 over h0→150; min wall 0.29 mm @ z≈95
- rim_1:   top opening circle ⌀36 @ z=150 (+Z)   ← addressable (the lip you shell/fillet)
- base_1:  planar bottom cap ⌀40 (−Z)
NOTE: the wall is ONE bspline face — do NOT address it with side:top.
      Use rim_1 / base_1 / wall_1. (The two [0,0,0] seam edges are construction seams — ignore.)
```
Two things to notice: the impeller's repeated areas (`355.5`/`218.3`/`88.2` ×6) that the model used to reverse-engineer by hand are **grouped and named once**; and the vase gains an **addressable rim** that the raw read-back never exposes, plus an explicit *don't-do-this* guard that prevents confident mis-addressing rather than just degrading.

### 3.3 Architecture — the layer + the "geometry language server" surface

**The layer** (`core/doc/digest.{h,cpp}`, sibling to `selectors.cpp`/`picks.cpp`, computed at the same point the describers already run — fold it into the recompile the GUI rework already has open):

- **Stage 0 — stop deleting data.** Patch `picks.cpp:927-938` + `make_selector` (`selectors.cpp:307`) to emit `side` (from `rep_dir` vs the `parse_side` table, `picks.cpp:507`), the **signed** `normal` (replacing the unsigned `axis`), `concavity` (bore/boss), and `radius`; round all floats to `~1e-6·bbox_diag` (kills `599.9999…` and the `e-16` dust); **drop `hint_index`**. De-lies the docstring, fixes the unsigned-axis ambiguity. **~1–2 days, zero risk.**
- **Stage 1 — adjacency + write-diff + stable IDs.** Build the `TopExp` face↔edge↔face adjacency graph (one O(E) pass, the primitive `dress.cpp` already uses) with per-edge convex/concave dihedral → **W3**. Diff the face-set+volume+bbox before/after each `add_features` op → a *semantic* write-confirmation ("created bore ⌀6 on +Z" / "no new face — cutter missed") → **W2**. Mint a **per-feature-scoped** content-hash ID `hash(owner_feature, quantized invariants)`, persisted in `document.json`, re-bound on rebuild by the **existing `resolve()` scorer** → **W1**. (Per-feature scoping, not a global content hash, is the design that survives the impeller's six identical blades — symmetry defeats naive content-hashing; instances are addressed as a group + orbit ordinal.)
- **Stage 2 — feature/symmetry grouping.** *2a:* for authored parts, grouping is nearly free — the feature tree *already declares* `circular_pattern{count:6, axis:z}`; read `owner_map` + the pattern params. *2b:* for imports (no tree), rule-based rotational/mirror detection on the face-invariant multiset (cluster by quantized {area, radius, surf}, fit a common axis, verify centroids land on a uniform orbit), emitted **with a confidence** and never allowed to suppress the raw list.

**The surface** (the part that controls tokens — adopt the language-server "pull" model, not a fat dump). New `gls_*` RPCs behind thin MCP tools:

| Verb | Returns | Closes |
|---|---|---|
| `list_symbols(scope)` | the outline: feature tree enriched with named faces + symmetry orbits (~400–600 tok for the impeller, regardless of 68 faces) | token blow-up |
| `hover(name)` | one entity's semantic card (surf, side, concavity, radius, area, owner, adjacency) — pulled only for the 2–3 entities an edit touches | — |
| `references(name)` / `adjacency(name)` | what depends on it / its shared-edge neighbors | **W3** |
| `diff(since_rev)` | named faces added/removed/split + volume delta + intent verdict | **W2** |
| `field(name, kind)` | the worst-N thickness/draft/curvature points **bound to named entities + locations**, lifted out of the host-only `.aicm` | **W4** |

`add_features` returns `list_symbols`-summary + `diff` (~400 tok), not the 68-face list; the model expands a specific group only if it needs to. The agent loop's reviewer/fixer verify via `diff()` instead of re-dumping whole geometry.

**Two render fixes folded in from the vision proposal** (independently shippable, high value/line):
- **Per-face tint fix** (`session.cpp` render path, ~line 4263): the tessellation already carries `FaceRec{face_id, tri range, surf}` (`aicm.h`); the renderer ignores it and tints **per-triangle normal** → the curved-face blotch you saw on the vase/impeller. Tint by the **B-rep face's** `rep_dir` instead → one clean tint per face. *This is a rendering bug whose fix doubles as a representation upgrade.*
- **Tag == ID == selector isomorphism:** the balloon number drawn on a face in the render **equals** the digest's stable ID **equals** the input selector token. "Point at #7" becomes a reliable editing primitive, and vision + text address the same entity with no translation. Also: **ship the internal 6-view contact sheet to the external `render_part`** (closes the capability asymmetry where the MCP-driven LLM is blinder than the in-loop one) and add an **auto-section** view so organic internals the text can't carry are *seen*.

### 3.4 Where a learned/custom model genuinely earns its keep

**Deferred, and gated — not in the near-term roadmap.** The panel was unanimous and blunt about this:

- For **authored parts and SolidWorks imports** (the actual workload), the feature tree *is* the ground truth — deterministic grouping wins, and a GNN that hallucinates a pattern is strictly *worse* than a flat list.
- The **only honest case** for a learned encoder is **novel scanned/freeform organic imports** where there is no feature tree and `surf:"other"` dominates (the vase class). Build it **only after** Stages 0–2 are live and you have a **measured residual** proving determinism fails there.
- When that day comes, the clean design is a small **GNN over the face-adjacency graph** (UV-Net/BRepNet-style), featurized almost entirely from the semantics Stage 0 already surfaces, with a **deterministic templated caption head** (never free-form — no hallucinated numbers) that degrades to the raw descriptor on low confidence. **Bootstrap it for free** from a *synthetic-label factory*: the feature op that created a face *labels* that face — zero hand-labeling. **Do not** try to train from `corpus/` (it's 64 sketch JSONs — confirmed; it cannot train a B-rep model). **Drop the soft-token/adapter head** entirely unless a single local LLM becomes the committed target.

Net: the "custom model in between" you intuited is real and worth building — but the deterministic version of it captures ~90% of the value first, and the learned version is a *gated enrichment for one hard case*, not the foundation.

### 3.5 Staged roadmap (most first-try value, least build, riding the GUI rework)

| Stage | What | Effort | Closes | Ship? |
|---|---|---|---|---|
| **0** | Emit discarded semantics (side/signed-normal/concavity/radius) + round floats + drop `hint_index`; per-face tint fix | ~1–2 days | W5 (partial), de-noise, de-lies docstring | **First PR — now, while recompile path is open** |
| **1a** | Write-diff confirmation on `add_features` | ~2 days | **W2** | Yes |
| **1b** | Adjacency graph + owner provenance serialization | ~days | **W3** | Yes |
| **1c** | Per-feature stable IDs + re-bind via `resolve()`; accept `{"of":"feature","name":…}` on input | ~1–2 wks | **W1** (the long-horizon core) | Yes — **essential** |
| **2a** | Feature/symmetry grouping from the feature tree | ~few days | W5 (authored) | Yes |
| **GLS** | `list_symbols`/`hover`/`diff`/`field` pull API; reviewer verifies via `diff()` | ~1–2 wks | token scaling, **W4** | Yes — the token-cost win |
| **2b** | Rule-based symmetry detection for imports | ~1 wk | W5 (imports) | When imports are a real workload |
| **3** | Learned GNN encoder + caption + vision adapter | weeks | W5 (scanned/freeform only) | **Only after a measured residual** |

**Essential trio:** Stage 0, the write-diff (1a), and stable IDs (1c). **Overkill until proven:** the Stage 3 learned encoder.

### 3.6 What NOT to build

- **A learned encoder before determinism is measured to fail.** Its Phase-0/1 is just the deterministic work relabeled; its ML delta is a *correctness liability* on authored parts.
- **The soft-token / vision-adapter head** — brittle, model-specific, un-debuggable; the text caption is always the source of truth.
- **An inbound MCP "pixel" tool** — `render_part` returns an `Image` outward; nothing ingests pixels, and nothing needs to. (This same constraint shapes the intake plan's photo handshake — see the companion doc.)
- **The full 4-up annotated "plate" as the *primary* channel** — pixels are lossy/occluded/non-metric and its image-token profile is the worst in the set. Vision *annotates* the digest; it doesn't replace it.
- **Don't let any heuristic (symmetry detection, recognizers) suppress the raw face list** — always emit confidence and keep the raw path available.

### 3.7 Honest risks

- **Stable IDs are the riskiest piece.** Fully symmetric instances have identical invariants by construction (symmetry defeats content-hashing) → address them as *group + orbit ordinal*, and accept that an individual instance isn't stably addressable across a topology change that breaks the symmetry.
- **Float-rounding tolerance** must scale with bbox and stay *below* the kernel's own merge tolerance, or it merges genuinely-distinct close faces.
- **Stage 2a trusts the feature tree;** a tree that lies (suppressed features, instances later merged by a boolean) yields stale grouping — cross-check against the same geometric verification 2b uses and downgrade confidence on mismatch.
- **The `.aicm` field plumbing (W4) couples the digest to the host mesh format** — if the GUI rework changes `.aicm`, gate the field-location feature behind a flag.

---

## Appendix — how the two writeups connect

This doc fixes **how geometry leaves the engine for the model** (perception + addressing). The companion **the intake/measurement/DfM plan (doc pruned)** fixes **how real-world requirements and fit constraints enter** (measurement + manufacturing intent). They share two structural decisions worth stating once:

1. **One shared engine subsystem, persisted in the document, two thin surfaces (MCP + built-in agent) over it** — the GSL digest and the intake artifacts both ride in `.step+` (cloned from the `studies` machinery) so a part built via MCP reopens in-app with everything intact.
2. **The model's vision is the transport, not a server-side upload** — `render_part` returns an `Image` outward; the intake plan's photos arrive through the LLM's own vision the same way. Neither side builds an inbound pixel tool.

Build order suggestion: **GSL Stage 0 is the single cheapest, highest-leverage change in either document** (~1–2 days, de-noises every channel, de-lies the docstring) — do it first, then the intake subsystem's Gate 0 spine (shared schema + pause/resume), since both downstream efforts lean on clean, named, stable geometry handles.
