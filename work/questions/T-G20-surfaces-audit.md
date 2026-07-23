# T-G20 — Surfaces Tab Audit (verdict + evidence)

**Driver:** opus-vesper · **Date:** 2026-07-03 · **Branch:** task/T-G20-surfaces-audit

Erik (T-G12 round 1): *"the surfaces tab is broken + the icons don't reflect what the
actual operations do... you can figure that one out and do all the testing yourself, or
we can scrap it if you think it's not useful."*

This is the audit-with-a-verdict. **Bottom line: KEEP the tab — do NOT scrap.** It is far
more functional than "broken" implies: **11 of 15 tools work end-to-end, 1 works but is
geometrically fragile, and 3 have a single, well-isolated GUI-wiring mismatch** (edge
wells feeding an engine that wants sketch profiles). No tool is engine-dead. The icon
complaint is real and independently confirmed.

## Method (both levels exercised per tool — no all-skip)

1. **Engine level (load-bearing):** every one of the 15 `surf_*` ops was driven through
   the real kernel via the ai-cad MCP (`add_features`) on purpose-built parts, using the
   **exact ref formats the GUI's `buildOpFeature` emits** (region refs for profile wells,
   face-picks *with* `owner_feature` for body wells, edge selectors for axis wells). This
   isolates kernel breakage from GUI-wiring breakage — the whole point of the audit.
2. **Code audit:** traced ribbon (`host/src/ui/ribbon_def.cpp`) → PM form
   (`host/src/ui/pm_forms.cpp`, `property_manager.cpp::buildOpFeature`) → engine handlers
   (`core/doc/document.cpp` ~L5509–5960, `core/geom/surfacing.cpp`) and the pick→selector
   path (`core/doc/document.cpp::resolve_pick`, `core/doc/selectors.cpp::make_pick_selector`).
3. **GUI level:** headless screenshots via `CHUDWARE_SHOT=ribbon CHUDWARE_TAB=8` (Surfaces
   ribbon, **light + dark**) and `CHUDWARE_SHOT=pmform CHUDWARE_PMCMD=surf_*` (PM forms). Every
   PNG was inspected, not trusted by exit code. Artifacts in scratchpad `g20_shots/`.

## Verdict table

| # | Tool | Engine | GUI wiring | Verdict | Key evidence |
|---|------|--------|-----------|---------|--------------|
| 1 | surf_extrude | ✅ | ✅ region well | **WORKS** | vol=0, 4 side walls, no caps (correct open surface) |
| 2 | surf_revolve | ✅ | ❌ **axis well K_EDGE** | **IMPAIRED — GUI wiring** | engine accepts a datum axis (`axis.z`) *and* an edge-selector axis, but the form's axis well is `K_EDGE` (edge-only) not `K_EDGEAXIS` like solid revolve (`pm_forms.cpp` L228). `routeDatumAxis` requires `selKinds.axis`, so it can't fill this well — and on a profile-only doc there's no body edge to pick either. **You cannot pick the datum axis a revolve needs.** Cheap fix below. |
| 3 | surf_sweep | ✅ | ✅ | **WORKS** | open tube, vol=0 |
| 4 | surf_loft | ✅ | ✅ | **WORKS** | 4 bspline walls from 2 sections |
| 5 | surf_planar | ✅ | ✅ | **WORKS** | flat patch from closed region |
| 6 | surf_offset | ✅ | ✅ | **WORKS** | face refs resolve by geometry (no owner needed) |
| 7 | surf_ruled | ✅ *sketch refs* | ❌ **K_EDGE well** | **BROKEN — GUI wiring** | engine `surf_sketch_wire`+`require_sketch` needs a **sketch** ref; a body-edge pick → `"edge_a -> 'fN' is a solid body, not a sketch"`. Builds fine from sketch/region refs. |
| 8 | surf_boundary | ✅ *sketch refs* | ❌ **K_EDGE wells** | **BROKEN — GUI wiring** | same handler as fill; dir1/dir2 wells are body-edge pickers |
| 9 | surf_fill | ✅ *sketch refs* | ❌ **K_EDGE well** | **BROKEN — GUI wiring** | builds from a sketch loop (rect); body-edge pick rejected. (BRepFill also needs a valid bounded loop — a lone circle fails geometrically.) |
| 10 | surf_knit | ✅ | ✅ face-pick w/ owner | **WORKS** | two edge-sharing patches sewn into one surface body |
| 11 | surf_thicken | ✅ | ✅ | **WORKS** (loft-topology caveat) | body-ref and face-ref-**with-owner** both promote surface→solid (vol 0→24894); bare face ref w/o owner fails, but GUI picks carry owner |
| 12 | surf_trim | ✅ | ✅ | **WORKS** | surface owner + tool face-pick divide the surface |
| 13 | surf_untrim | ✅ | ✅ | **WORKS** *(was mis-documented as dead)* | handler exists (`document.cpp` L5747, "Bug S-1" fixed); creates untrimmed patch as a new body, source intact |
| 14 | surf_delete_face | ✅ | ✅ | **WORKS** | removes a face, opens solid→surface (vol 24000→0) |
| 15 | surf_replace_face | ⚠️ fragile | ✅ refs resolve | **WORKS but FRAGILE** | succeeds only when the replacement **actually splits** the solid (vol 24000→18000); a coincident/non-intersecting replacement → `"could not heal a solid"` on reasonable inputs |

**Tally:** 10 WORKS · 1 impaired (revolve axis) · 1 works-fragile (replace_face) · 3 GUI-wiring-broken (ruled/fill/boundary) · 0 engine-dead.

### Emit-side proof for the ruled/fill/boundary verdict (not just engine reject-side)

The engine *rejecting* body-edge refs is only half the proof; the other half is that these
wells can **only** emit body-edge selectors. Verified in `property_manager.cpp`:
- The sketch-feature routing that produces `{kind:region,sketch:fN}` selectors is gated to
  specific well keys: **profile/sections** (`profileWell()`, L1255–1268) and **path/centerline**
  (`routePathFeature()`, L1283–1288). The `edge_a`/`edge_b`/`edges`/`dir1`/`dir2` wells match
  neither route.
- Their only input is the viewport geometry pick (`acceptPick`, L977–983: an `Edge` pick into
  an `.edge` well), which flows through `resolve_pick` → a **body-owned** edge selector.
So a ruled/fill/boundary well can hold nothing *but* a body-edge ref, which the engine's
`require_sketch` rejects. The `surf_ruled` PM-form screenshot ("Edge 1 … (pick in viewport)")
shows exactly this dead-end. (By contrast sweep's `path` and loft's `sections` route sketch
features via `routePathFeature`/`profileWell`, which is *why* they genuinely work.)

## The one real wiring bug (ruled / fill / boundary)

The entire "Curves & Patches" edge-fed group shares one root cause:

- The engine handlers for `surf_ruled` (`edge_a`/`edge_b`), `surf_fill` (`edges`), and
  `surf_boundary` (`dir1`/`dir2`) resolve their refs through
  `surf_sketch_wire()` → `require_sketch()` (`core/doc/document.cpp` L903–912). They build
  a wire from a **whole solved sketch** and *require the ref to point at a sketch feature*.
- The ribbon forms (`pm_forms.cpp` L706–720) give those slots **K_EDGE wells** ("Edge 1",
  "Edge 2", "Patch Boundary", "Direction 1 Curves"). The `surf_ruled` PM form screenshot
  literally prompts **"Edge 1 … (pick in viewport)"** — i.e. it invites the SolidWorks-style
  **body-edge** pick. A body-edge selector's owner is a *body*, so `require_sketch` throws
  `"… is a solid body, not a sketch"`.

Net: pick body edges (the natural interaction the form advertises) → the op is rejected.
This is almost certainly a big part of what Erik hit as "broken." **Engine is fine — the
form offers the wrong kind of selection.** Fix is a design call (see follow-ups) and is
**not** a cheap/safe in-scope change, so it is documented, not applied.

## Icons — confirmed misleading (truth-table handoff to T-G15)

Ribbon screenshots (light+dark) confirm Erik's icon complaint. Current glyph → issue:

| Tool | Current icon | Assessment | Suggested depiction |
|------|-------------|-----------|--------------------|
| surf_thicken | `extrude_boss` | ❌ **identical to the Extrude button** | offset-wall / two-parallel-sheets glyph |
| surf_ruled | `shell` | ❌ shell ≠ ruled strip between two edges | ruled/lofted-strip glyph |
| surf_fill | `boundary` | ❌ **duplicate of Boundary Surf.** | distinct filled-patch glyph |
| surf_extrude/revolve/sweep/loft/planar/offset/knit/trim/delete/replace | extrude_boss/revolve_boss/sweep/loft/ref_plane/offset_ent/combine/trim/subtract/wrap | acceptable | — |

Constraint for T-G15: icons must be existing atlas glyphs (68 in `host/assets/icons/atlas.json`);
`gui_lint --rule icons` must stay clean. If no distinct glyph exists, a new one is needed.

## Cheap wiring fixes

**Applied: none landed on this branch** (see reasoning below). **One is recommended with an
exact diff** so whoever picks it up lands *and verifies* it in a single pass.

### Recommended cheap fix #1 — surf_revolve axis well (one line)
`host/src/ui/pm_forms.cpp` L681, the `surf_revolve` form's axis well:
```
-            Well  ("axis",    "Axis",    "Revolve", K_EDGE, false),
+            Well  ("axis",    "Axis",    "Revolve", K_EDGEAXIS, false),
```
This mirrors the solid `revolve` form (L228, already `K_EDGEAXIS`) so the axis well can accept
a datum axis (`axis.z`). **Evidence it's correct:** the engine already accepts a datum-axis ref
for `surf_revolve` (driven and confirmed via MCP), and `routeDatumAxis` (L1163) fills any
`selKinds.axis` well with `{kind:datum,feature:axis.z}`. **Not landed here** because: (a) it
changes selection *acceptance*, not layout, so a both-mode screenshot verifies nothing about it
(the parent's screenshot rule targets visual regressions); (b) it does not fully close the tool —
`readyForPreview` gates surf_revolve only on the `profile` well (L1815), so OK can still fire with
no axis → engine "axis required" error. Both should be fixed and verified together (see follow-up).

### Why the other candidates are not cheap-in-scope
- ruled/fill/boundary: needs a design decision + engine or form change + live-pick
  re-verification — a deep fix, not a safe one-liner. → follow-up issue.
- replace_face fragility: engine robustness / added validation. → follow-up issue.
- icons: T-G15's truth-table job (above), not this task's to change.

No test was weakened and no UI surface was removed (per the T-G12 kill-is-a-human-call rule).

## Follow-ups filed (deep work, not hand-waved)

Filed as in-repo backlog tasks (GitHub-issue creation is blocked for this agent by policy):
**`work/backlog/T-BL28.md`** (ruled/fill/boundary edge-well fix + stale cheatsheet) and
**`work/backlog/T-BL27.md`** (replace_face heal). The surf_revolve axis fix is captured as
cheap-fix #1 above (one-line diff ready). Detail:

1. **surf_ruled/fill/boundary edge-well ↔ sketch-wire mismatch** (T-BL28) — decide: teach the engine
   to build wires from picked **body edges** (SolidWorks-like, bigger engine work) **or**
   re-target the forms to **sketch/region** pickers (matches today's engine, unusual UX).
2. **surf_revolve axis well + readiness gate** — apply cheap fix #1 (`K_EDGE`→`K_EDGEAXIS`,
   pm_forms.cpp L681) AND gate `readyForPreview` on the axis well so OK can't fire without an
   axis; verify end-to-end with a live datum-axis pick. Small, but needs a GUI build to verify.
3. **surf_replace_face "could not heal"** — add pre-validation/guidance (replacement must
   split the solid) or make the BOP heal robust to coincident/non-intersecting replacements.
4. **Stale agent cheatsheet** `services/agent/prompts/shared/cheatsheets/surfacing.md` —
   claims `surf_untrim` has "NO compiler / ALWAYS errors" (false since Bug S-1 fix) and does
   not warn that ruled/fill/boundary want sketch refs. Refresh it.

## Recommendation for Erik — **KEEP**

Keep the Surfaces tab. It is genuinely useful and mostly works: surface-from-profile
(extrude/sweep/loft/planar), offset, knit, thicken (the marquee surface→solid step),
trim, untrim, delete-face all work end-to-end today. The breakage is **small and localised to
the form-selector layer, not the kernel**: three edge-fed patch tools (ruled/fill/boundary)
whose wells offer body-edge picks the engine won't take, one wrong `selKinds` constant on the
surf_revolve axis well (a one-line fix), a fragile replace_face heal, and **misleading icons**.
Every one is fixable without touching the geometry kernel. Scrapping would throw away a working
surface modeler over a handful of well-kind bugs and a couple of duplicate glyphs.

## Open questions for Erik (batched — non-blocking)

See `work/questions/T-G20.md`.

## ANSWER (Fable foreman, delegated by Hunter 2026-07-03)

**KEEP ratified.** The Surfaces tab stays. Follow-up disposition:
1. ruled/fill/boundary edge-wells → T-BL28 (in flight, opus-vela). Direction RULED: **fix the forms** — re-target to sketch/region pickers matching today's engine; teaching the engine body-edge wire-building is deferred as a possible future overhaul task.
2. surf_revolve axis one-liner (K_EDGE→K_EDGEAXIS, pm_forms.cpp L681) + `readyForPreview` axis gate → folded into T-BL28 scope by foreman note (same file, verify together with a live datum-axis pick).
3. replace_face heal → T-BL27 (in flight, sonnet-lira with foreman advisory).
4. Stale surfacing cheatsheet → already in T-BL28 scope.
