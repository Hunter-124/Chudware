# Manufacturing Turning Architecture — Honest Single-Spindle XZ Turning

Owner: `TurningArchitecture`. Domain id: **`cam.turn`**. P4 core implementation landed under
`core/cam/turn/**` with direct headless coverage in `core/tests/test_cam_processes.cpp`.

Evidence base: `core/cam/model.h`, `core/cam/op_turn.cpp`, `core/cam/post.cpp`, `core/cam/stock_sim.cpp`,
`services/mcp/cam_tools.py`, the CAM-kernel audit, the manufacturing-breadth research,
`00_decisions.md`, `01_definition_of_done.md`.

P4 implementation scope is deliberately offline: single-spindle XZ validation, declared
workholding and insert geometry, radius-internal/explicit diameter-or-radius NC semantics,
feed-per-revolution, CSS with an RPM clamp, degressive threading geometry, LinuxCNC-lathe
post output (`G18`, `G7`/`G8`, `G95`, `G96`, `G76`, and `M5`), and axisymmetric remnant
lineage through `stock_sim`. P4 uses an explicit lathe override on the existing LinuxCNC
dialect; the planned `linuxcnc_lathe` registry id remains unadvertised until interpreter
qualification exists. Capability rows remain declared until gateway-stamped procedural
evidence exists; parse, monitor, transport, execute, and safety integration are unsupported.

Project rulings honoured:
* Production GUI only under `host/src/ui/manufacture/**`; no `ui/mfg`, no new canvas. `host/src/ui/cam/**`
  remains offline-CAM owner and gains exactly one named seam.
* Cross-domain service root `services/manufacturing/**`; domain engine under `core/cam/turn/**`
  (confirmed non-colliding with `CuttingProcessArchitecture`'s `core/cam/cut/**`).
* Wire prefixes `mfg_*` / `mfg.*`. **User-facing MCP tool family is the single shared `manufacture`
  family, OFF by default in `agent.tool_families` until commissioning**, with per-tool toggles retained.
* Gateway's shared machinery is **composed, never re-declared** (`MFG-CAPTRUTH-ONE-DEFINITION`,
  `MFG-TIER0-ONE-REGISTER`, `MFG-SEQ-SOLE-SOURCE`, `MFG-PROV-GATEWAY-COMPUTED`; every gateway rule this
  document relies on is cited by stable rule id and resolved in **Appendix A.1**, never by gateway
  section number): `CapabilityTruth`/`CapabilityReport` from `core/device/capability.h`; the Tier-0 row
  shape from `core/manufacturing/tier0.h` registered through the single per-domain register; every
  durable row stamped by `mfg_event_append`. Turning mints **no** durable `seq` and, implementing no
  transport at all, can produce **no** `probed` evidence.
* No new normal-gate SKIP is requested by turning, and the batch-wide tally is zero (§14).
* Review closure for **B10**, **B15**, **C9** and **C10** is recorded in **Appendix A** (rule-id
  register, the shared `post.cpp:487` ownership statement, and the closure table).

Everything not directly observed in the five permitted files is marked `[INFERENCE]`.

---

## 0. Thesis

Turning already **generates** real XZ toolpaths (`core/cam/op_turn.cpp`, seven generators) and the post
already carries *conditional* lathe formatting (`post.cpp:229-237`, `:252-258`, `:403-410`). What is
missing is not "a turning feature" — it is **executable semantics for enums the schema already
advertises**: no lathe dialect exists, no plane word is ever emitted, CSS is destroyed one block after it
is set, a lathe program never stops the spindle, threading is claimed in a comment and never posted, ID
turning simulates as whole-diameter removal and reports success, and nothing in the codebase knows where
the chuck is.

So this is a **repair-and-close** plan. The deliverable is single spindle, single channel, single active
tool, XZ only, **offline only**: `plan`, `generate`, `post`, `parse`, `simulate`. `monitor`, `transport`,
`execute` and `safety_integration` are `unsupported` **with a why** (never `not_applicable`), and
`device_command_owner` is `null` with a why, for every turning subject. LinuxCNC appears only as an
out-of-process offline *interpreter*, never as a controller.

---

## 1. Verified defect register

Grounded in lines actually read. `Class` uses DoD v4 language: *false-green* = reports success while
producing a wrong artifact; *silent-substitution* = a plausible-but-wrong value injected without a word.

| id | Defect (verified) | Evidence | Class | Fix |
|---|---|---|---|---|
| D-T1 | **No lathe dialect exists.** All eight presets are built by `base_mill()`; not one sets `lathe` or `diameter_mode`, so `post_dialects_json` advertises `lathe:false` for every dialect. | `post.cpp:67-149`, `:169-175` | declared-not-implemented | §7 |
| D-T2 | **A lathe program gets `G17` and no plane word at all.** `if (!g.lathe) g.blk(c.plane_xy)` — the mill path emits G17 for a lathe setup; the lathe path emits no plane selection. `G18` appears nowhere in the post. | `post.cpp:361` | false-green | §6 |
| D-T3 | **CSS is clobbered by an RPM `S` word one block later.** `G50 S<clamp>` / `G96 S<css>` is emitted, then `if (!head && rpm>0 && rpm!=g.last_rpm) g.spindle(rpm,…)` emits `S<rpm> M3`. `last_rpm` was just forced to `-1` at the tool change, so this always fires; under G96 the control reads that `S` as surface speed. | `post.cpp:403-411`, `:389`, `:273-281` | false-green | §5 |
| D-T4 | **`css_off` (G97) is never emitted.** The field exists and is override-parsed but has zero emit sites, so a `TurnDrill`/`TurnThread` after a CSS op runs in CSS toward r→0. | `post.cpp:58` only; no emit site in `:328-519` | false-green (overspeed) | §5 |
| D-T5 | **Lathe programs never stop the spindle.** `if (!g.lathe && !(kind==Laser)) g.blk(spindle_off)`. | `post.cpp:487` | false-green (safety) | §5 |
| D-T6 | **CSS value silently substitutes RPM.** `op->css > 0 ? op->css : rpm` — `surface_speed_mode=="css"` with `css==0` emits the RPM number as m/min. | `post.cpp:409` | silent-substitution | §5 |
| D-T7 | **Threading is claimed and not implemented.** `op_turn.cpp:311` states "The post maps op metadata to G76". `post.cpp` has no G76/G33 path (`drill_cycle_code` has no thread entry), so the long-hand passes post as unsynchronised `G1 X… Z… F<pitch·rpm>`. | `op_turn.cpp:310-311`, `post.cpp:301-309`, `:448-483` | false claim + scrap/crash | §8 |
| D-T8 | **Threading X datum is the *global* profile max radius.** `od = prof_max_r(prof)`, then `thread_r = od - x_depth`. On a stepped shaft the passes are at the wrong diameter. | `op_turn.cpp:322-324`, `:352` | wrong geometry | §8 |
| D-T9 | **Threading Z overshoots to `z_lo - pitch`** with nothing bounding it toward the workholding. | `op_turn.cpp:357` | collision | §4, §8 |
| D-T10 | **Threading runs in whatever spindle mode is active** and falls back to `tf = op.feed` → `200 mm/min` when `rpm==0`, i.e. under CSS. | `op_turn.cpp:332-334` | silent-substitution | §5, §8 |
| D-T11 | **Lathe arcs emit `I`/`J`.** The lathe branch of `g.arc()` writes `X`/`Z` targets but offsets from `center.x-pos.x` and `center.y-pos.y`; on a lathe `y` is always 0, so it emits `J0` and never `K`. | `post.cpp:252-264`, `op_turn.cpp:82-95` | wrong geometry | §6 |
| D-T12 | **Generate and simulate disagree about stock OD by `off_radial`.** `stock_od()` returns `ctx.stock.radius`; the sim uses `st.radius + off_radial` for `CylinderOffset`. The first roughing pass starts *inside* material by `off_radial`, invisibly to the sim. | `op_turn.cpp:75-79` vs `stock_sim.cpp:169` | false-green (crash) | §9 |
| D-T13 | **`CylinderOffset` axial offset is applied asymmetrically.** `bottom = base.z`, `top = base.z + height + 2*off_axial`, while `model.h:82` documents `off_axial` as "per end" — the stock is shifted `+off_axial` axially. | `stock_sim.cpp:172-173` vs `model.h:82` | wrong geometry | §9 |
| D-T14 | **ID turning simulates as whole-diameter removal.** The axisymmetric model keeps one outer radius per slice; an ID/boring pass at small radius drives `rad[k]` down to the bore radius, so the sim reports the entire OD turned away — as a clean success. | `stock_sim.cpp:200`, `:224`, `:230-236` | **top-severity false-green** | §9 |
| D-T15 | **A `Lathe` setup with box or `FromSolid` stock falls through to the mill dexel path**, where XZ moves are read as XYZ mill moves with `y=0`, producing meaningless removal numbers that still report `ok`. | `stock_sim.cpp:164`, `:197`, `:247-282` | false-green | §9 |
| D-T16 | **`MillTurn` never reaches the turning sim** and lands in the same dexel path. | `stock_sim.cpp:197` | false-green | §9, §11 |
| D-T17 | **The turning branch returns before setting `res.remaining`, so rest-stock lineage is broken.** The `from_sim` chain converts a remnant field to `FromSolid`; for a lathe that field is default/empty. The audit also reports `lastRemaining` survives edits. | `stock_sim.cpp:244`, `:490`; CAM-kernel audit (`session.cpp:177-207`, `:291-327`) | false-green | §9 |
| D-T18 | **`geometry["stock_profile"]` has no producer.** `gen_turn_rough` reads it; `model.h:325-327`'s geometry-shape comment omits it, `cam_extract` does not emit it, `_GEOMETRY_SLOTS` lacks it. OD roughing therefore always uses the `stock_od()` fallback, which invents `prof_max_r + stl + 2.0`. | `op_turn.cpp:163`, `:75-79`; `cam_tools.py:307` | dead input / invented stock | §9 |
| D-T19 | **Nose radius is never compensated.** `gen_turn_finish` feeds the tool *reference point* exactly along the profile points; `gen_turn_rough` clips Z against the raw profile. Every taper, radius and corner is off by up to the insert nose radius. | `op_turn.cpp:255-260`, `:180-190` | wrong geometry | §3 |
| D-T20 | **Tool orientation, hand, insert width and lead angle do not exist.** `Tool` carries `diameter`/`corner_radius`/`flutes` and `effective_radius() = diameter*0.5`. A grooving or parting cut has no width; `PartingTool` kerf is unknown. | `model.h:155-181` | missing semantics | §3 |
| D-T21 | **Workholding does not exist anywhere.** `gen_turn_rough:189` comments "Z sweep toward chuck" while nothing in the model knows a chuck exists; roughing sweeps to the profile's `zlo` unbounded. | `op_turn.cpp:189`; no fixture type in `model.h` | collision | §4 |
| D-T22 | **The lathe holder check is not merely skipped — it would be nonsense.** The sim's holder model is coaxial cylinders about the **tool axis = Z** (`shank_r`, `holder_r`, `flute_len`), the wrong axis for a lathe; and the lathe branch returns before the rapid/holder/gouge checks. | `stock_sim.cpp:326-333`, `:389-472`, `:244` | must be labelled `unsupported` | §10 |
| D-T23 | **`WCS::z_dir` is documented as "tool axis"**; on a lathe Z is the *spindle* axis. `Machine::travel.y` is meaningless and `travel.x` is dimensionally ambiguous (radius vs diameter). | `model.h:96`, `:120` | ambiguous semantics | §2 |
| D-T24 | **The CSS clamp ignores the machine and the chuck.** Only `op->max_turn_rpm` is consulted; `machine.max_rpm`/`min_rpm` and any chuck rating are not. | `post.cpp:407-408` | unsafe | §5 |
| D-T25 | **Feed-per-revolution does not exist.** No `G94`/`G95` is emitted, no model field carries mm/rev, all turning feeds are mm/min with generator defaults (`100`, `50`, `30`), and cycle-time estimation assumes mm/min. | `op_turn.cpp:133`, `:287`, `:387`; `stock_sim.cpp:35-68` | missing semantics | §5 |
| D-T26 | **An unknown dialect silently degrades to GRBL.** A typo'd lathe dialect id posts mill GRBL code for a lathe. | `post.cpp:158-162` | silent-substitution | §7 |
| D-T27 | **`post_options.overrides` can flip `lathe`/`diameter_mode` on a mill dialect**, with no coherence check against `Machine::kind` — an undocumented back door into half-lathe output. | `post.cpp:339-340` | unsafe escape hatch | §7 |
| D-T28 | **`metric=false` emits `G20` over millimetre numbers.** The model is mm-only. | `post.cpp:359`; `model.h:16-17` | 25.4× error | §5 |
| D-T29 | **MCP converts `cylinder_offset` stock into a *box*** with box-style offsets, dropping `radius`/`height`/`base`. Every MCP-created turning setup therefore loses the axisymmetric sim path (which keys on `StockType::Cylinder*`) and lands in D-T15. | `cam_tools.py:333-359` vs `stock_sim.cpp:164` | **top-severity false-green** | §12 |
| D-T30 | **MCP's default stock is `box_offset` regardless of `machine="lathe"`.** | `cam_tools.py:498` | guaranteed-wrong default | §12 |
| D-T31 | **Turning profiles are unvalidated.** `read_profile` indexes `p[0]`/`p[1]` with no check of arity, `r >= 0`, ordering or monotonic Z; a malformed row surfaces as a generate exception. | `op_turn.cpp:18-24` | poor failure mode | §2 |
| D-T32 | **`comp_side`/`comp_in_control` are never read by any turning generator**, so control-side `G41/G42` comp is advertised by the schema and unimplemented for turning. | `model.h:289-290`; absent from `op_turn.cpp` | declared-not-implemented | §3 |
| D-T33 | **GRBL emits no tool-change stop** (`tool_change=""`, and the computed `multi` count is unused) — on a lathe, a turret index with no operator stop. | `post.cpp:81`, `:346-351`, `:382-386` | safety | §7 |

---

## 2. Data model — exact types and fields

### 2.1 `core/cam/turn/turn_semantics.{h,cpp}` (new)

The **single** conversion point between the internal model and NC text. Invariant, already true of
`op_turn.cpp:2-3` and now made explicit and asserted: **internal `Move.to.x` is always a signed radius in
mm, `Move.to.y` is always 0, `Move.to.z` is the axial coordinate.** Diameter values exist *only* in
emitted NC.

```cpp
namespace chudware::cam::turn {

enum class RadialSense { Radius, Diameter };

struct AxisSemantics {
    RadialSense program_sense = RadialSense::Diameter; // what an X word means in the NC
    bool   front_tooling  = true;   // true: +X away from the centreline (operator side)
    double spindle_axis_r = 0.0;    // model radius of the spindle centreline (always 0; asserted)

    double to_program_x(double model_radius) const;
    double from_program_x(double x_word) const;
    double to_program_i(double model_radius_delta) const;  // ALWAYS a radius delta, never doubled
    json to_json() const; static AxisSemantics from_json(const json&);
};

// Lathe frame validation: Z must be the spindle axis, X the cross-slide.
struct LatheFrameCheck { bool ok = false; std::string error; };
LatheFrameCheck check_lathe_frame(const WCS&, const Machine&);
} // namespace
```

Closes D-T23 (`check_lathe_frame` refuses a `Lathe` setup whose `z_dir` is not the spindle axis and pins
the lathe reading of `Machine::travel` — `x` = radial cross-slide travel in **radius** mm, `y` unused and
required 0, `z` = axial travel; `model.h`'s comments on `travel` and `WCS::z_dir` gain that reading).
`to_program_i` closes half of D-T11: `I`/`K` are radius deltas even under `G7`.

### 2.2 `core/cam/turn/turn_profile.{h,cpp}` (new)

```cpp
struct TurnProfilePoint { double z = 0, r = 0; };

struct TurnProfile {
    std::vector<TurnProfilePoint> pts;   // ordered free end -> chuck end (decreasing Z)
    bool   closed_at_axis = false;       // final point on r == 0 (a faced or parted end)
    double z_free = 0, z_chuck = 0, r_min = 0, r_max = 0;   // computed
    std::string hash;                    // sha256 over the canonical point list + a units tag

    double r_at(double z) const;         // used by threading (§8) and the simulator (§9)
    struct Check { bool ok = false; std::string error; std::vector<std::string> notes; };
    static Check validate(const json& geometry, const char* key, TurnProfile& out);
    json to_json() const; static TurnProfile from_json(const json&);
};
```

`validate` replaces `read_profile`'s bare indexing (D-T31) and rejects arity ≠ 2, non-finite values,
`r < 0`, fewer than two points, non-monotonic Z, and self-crossing. Two points sharing a Z express a
vertical face — allowed and detected explicitly rather than tripping the monotonicity rule. `hash` is the
lineage key used by §9 and §13.

### 2.3 `core/cam/turn/workholding.{h,cpp}` and `turn_guard.{h,cpp}` (new) — closes D-T21

```cpp
enum class WorkholdKind { Chuck3Jaw, Chuck4Jaw, Collet, FaceDriver, BetweenCenters };
enum class JawStyle    { Hard, Soft, Pie };
enum class GripMode    { OD, ID };

struct Chuck {
    WorkholdKind kind = WorkholdKind::Chuck3Jaw;
    JawStyle jaws = JawStyle::Hard;
    GripMode grip = GripMode::OD;
    double body_diameter    = 0;  // chuck body OD (mm)
    double body_face_z      = 0;  // program Z of the chuck body face
    double jaw_outer_radius = 0;  // maximum jaw sweep radius (mm)
    double jaw_length       = 0;  // jaw length along +Z from body_face_z
    double grip_length      = 0;  // engaged jaw length on the part
    double grip_radius      = 0;  // radius at which the jaws contact the part
    double rated_max_rpm    = 0;  // 0 = unknown -> blocks the rpm clamp derivation (§5)
};
struct Tailstock {
    bool present = false, engaged = false;
    double tip_z = 0, tip_diameter = 0, body_diameter = 0, reach = 0;
};
struct SteadyRest { bool present = false; double z = 0, bore_radius = 0, body_radius = 0; };

struct Workholding {
    Chuck chuck; Tailstock tailstock; SteadyRest steady;
    double safety_mm = 3.0;
    bool   declared  = false;      // false => Tier-0 refusal for generate/post/sim/validate
    // computed by resolve():
    double min_safe_z = 0;         // no tool motion at Z below this
    double max_safe_z = 0;         // tailstock-side bound (+inf when absent)
    double max_part_radius = 0;    // jaw sweep + safety
    std::string hash;              // bound into every report (§14)
    void resolve();
    json to_json() const; static Workholding from_json(const json&);
};

struct TurnGuard {
    const Workholding* wh; const AxisSemantics* ax;
    struct Verdict { bool ok = false; std::string refusal_id, why; };
    Verdict check_point(double r, double z) const;
    Verdict check_segment(double r0, double z0, double r1, double z1) const;
};
```

`resolve()` is deterministic per workholding kind: OD gripping gives
`min_safe_z = body_face_z + jaw_length + safety_mm` (the jaw *tip* face); `Collet` gives
`body_face_z + safety_mm`; `BetweenCenters` takes both bounds from the driver and tailstock. One test per
kind. `Setup` gains `turn::Workholding workholding;`.

### 2.4 `core/cam/turn/turn_tool.{h,cpp}` (new) — closes D-T19, D-T20

```cpp
enum class TurnHand { Right, Left, Neutral };

struct TurnToolGeom {
    int      orientation     = 0;   // ISO / LinuxCNC tool-table code 1..9; 0 = undeclared -> refusal
    TurnHand hand            = TurnHand::Right;
    double   nose_radius     = 0;   // insert nose radius (mm); 0 on a turning insert -> refusal
    double   included_angle  = 0;   // insert included angle (deg)
    double   lead_angle      = 0;   // holder approach angle (deg)
    double   front_clearance = 0;   // deg
    double   back_clearance  = 0;   // deg
    double   insert_width    = 0;   // grooving/parting blade width (mm)
    double   max_doc         = 0;   // insert-rated max depth of cut; 0 = unknown -> no claim
    double   min_bore_radius = 0;   // BoringBarID: smallest bore it enters
    double   bar_length      = 0;   // BoringBarID overhang (mm)
    bool     declared        = false;
    std::string hash;
    json to_json() const; static TurnToolGeom from_json(const json&);
};

// Nose-radius compensation: part surface -> tool reference-point path.
struct NoseComp {
    struct Result { bool ok = false; std::string refusal_id, why; TurnProfile path; };
    static Result offset(const TurnProfile& part, const TurnToolGeom&, bool id_op,
                         double stock_to_leave);
};
```

`NoseComp::offset` offsets each segment by `nose_radius + stock_to_leave` along its outward normal
(outward = +r for OD, −r for ID), inserts a tangent arc at each convex corner, trims at each concave
corner, and **refuses** `turn_nose_radius_exceeds_fillet` when `nose_radius` exceeds the smallest concave
fillet — that case gouges and must never be silently rounded away. It also refuses
`turn_tool_orientation_incompatible` when the declared orientation and lead angle cannot reach a segment's
back angle (an undercut). Those two refusals are what make `orientation` executable rather than decorative.

`Tool` gains `std::optional<turn::TurnToolGeom> turn;`. `Tool::effective_radius()` is **not** used for
turning tools: the turning paths read `nose_radius`/`insert_width` explicitly, and a turning tool with
`turn == nullopt` is a Tier-0 refusal (§11) instead of a `diameter*0.5` guess.

### 2.5 `core/cam/model.h` — additions (appends only; no enum is reordered)

```cpp
enum class StockType { /*…existing…*/ FromTurnSim };   // appended

struct StockLineage {
    std::string parent_setup_id, parent_op_id_last, parent_job_revision, profile_hash;
    long long   parent_seq = 0;   // the gateway-stamped seq of the establishing sim result (§13.3)
    json to_json() const; static StockLineage from_json(const json&);
};

struct Stock {
    /*…existing…*/
    turn::TurnProfile turn_profile;   // FromTurnSim: outer remnant profile [[z,r]]
    turn::TurnProfile turn_bore;      // FromTurnSim: inner radius profile (empty = solid)
    StockLineage      lineage;
};

struct Setup { /*…existing…*/ turn::Workholding workholding; };
struct Tool  { /*…existing…*/ std::optional<turn::TurnToolGeom> turn; };

struct Operation {
    /*…existing…*/
    double feed_per_rev         = 0;   // mm/rev; > 0 selects G95 for this op
    double thread_peak_offset   = 0;   // G76 I; signed, negative for external
    double thread_degression    = 0;   // G76 R; 0 = none
    double thread_compound_ang  = 0;   // G76 Q (deg)
    int    thread_spring_passes = 0;   // G76 H
    double thread_taper         = 0;   // G76 E
    int    thread_taper_mode    = 0;   // G76 L
    bool   thread_internal      = false;
    double groove_width         = 0;   // override for TurnToolGeom::insert_width; 0 = use the tool
};

struct Toolpath {
    /*…existing…*/
    json turn_meta;                       // {nose_radius, insert_width, orientation, id_op,
                                          //  comp:"computer", profile_hash}
    struct Span { int begin = 0, end = 0; };
    Span sim_only;                        // moves the post must never emit (§8.2)
};
```

Existing turning fields are reused rather than duplicated: `thread_pitch` → `G76 P`,
`infeed` → `G76 J` (first cut depth), `thread_depth` → `G76 K`, `thread_passes` retained for the
simulation long-hand only, `id_op` unchanged.

`MachineKind`, `ToolType` and `OpType` are **not** extended. New turning enums live in
`chudware::cam::turn` and get `to_string`/`*_from_string` pairs in `core/cam/model.cpp` following the
existing table style `[INFERENCE: table shape from the CAM-kernel audit's citation of core/cam/model.cpp:100;
confirm at implementation]` — with one deliberate difference: **turning string lookups must not default.**
An unknown value returns an error, never element 0. This is precisely the defect class
`cam_tools.py:152-167` documents for `ToolType`, and it must not be reproduced in new code.

---

## 3. Tool orientation, nose radius and comp

1. `gen_turn_finish` and `gen_turn_rough` stop tracing the part profile. They drive
   `NoseComp::offset(part_profile, *tool.turn, op.id_op, op.stock_to_leave)`. (D-T19)
2. `gen_turn_rough`'s Z-extent clipping (`od_cut_z_range`, `op_turn.cpp:52-72`, and the ID mirror at
   `:207-222`) runs against the **comp'd** profile, so pass extents match the path actually cut.
3. `gen_turn_groove` and `gen_turn_part` require `insert_width > 0` (from the tool or `op.groove_width`),
   emit the blade-*centre* reference path, and record the width in `Toolpath::turn_meta` for the
   simulator (§9). A missing width is Tier-0 `turn_insert_width_undeclared`. (D-T20)
4. `comp_in_control == true` on any turning op is refused with `turn_control_comp_unsupported`, naming the
   missing level: control-side comp needs an exported machine tool table, which this wave does not
   produce. Computer comp is the only implemented mode. That closes D-T32 honestly instead of leaving
   `G41/G42` advertised and dead.
5. `Toolpath::turn_meta` is the single carrier that keeps generator, post and simulator on the same
   numbers — the class of drift that produced D-T12.

---

## 4. Workholding and the collision bound

* `Setup::workholding.declared == false` ⇒ **Tier-0 refusal** at every turning entry point (generate,
  post, simulate, offline validate). Not a warning, and never `not_applicable`.
* Every emitter in `op_turn.cpp` (`t_rapid`, `t_feed`, and the thread/groove/part loops) routes through
  `TurnGuard::check_segment`. A move crossing `min_safe_z`/`max_safe_z` or exceeding `max_part_radius`
  fails the operation with a named refusal — it is never clipped silently.
* `gen_turn_rough`'s "Z sweep toward chuck" (`op_turn.cpp:189`) becomes a sweep to
  `max(zlo, wh.min_safe_z)`; when that clip removes material the operation asked for, the op returns
  `ok=false, error="turn_workholding_blocks_pass"` naming the blocked Z. A refusal, not a quiet short pass. (D-T21)
* `gen_turn_thread`'s `z_lo - pitch` run-out (D-T9) and `gen_turn_part`'s parting Z are bounded the same way.
* Parting with `tailstock.engaged == true` is refused (`turn_parting_with_tailstock`).
* `gen_turn_drill` requires the tailstock absent or disengaged and bounds Z at `tailstock.tip_z`.
* `chuck.rated_max_rpm == 0` blocks the RPM clamp derivation in §5 rather than being ignored.

**Stated limitation, not a deferred fix:** this is a *bounding-envelope* check against the workholding and
the stock silhouette. It is **not** tool/holder/turret/carriage collision detection — see §10.

---

## 5. Spindle mode, feed mode, units — the modal plan

Computed per setup **before** any NC byte is emitted, in `core/cam/turn/post_turn.{h,cpp}`:

```cpp
enum class SpeedMode { Rpm, Css };
enum class FeedMode  { PerMin, PerRev };

struct TurnOpModal {
    SpeedMode speed = SpeedMode::Rpm;
    FeedMode  feed  = FeedMode::PerMin;
    double css_m_min = 0;      // G96 S value (metric m/min only)
    double rpm       = 0;      // G97 S value
    double clamp_rpm = 0;      // clamp value (dialect-specific placement)
    double feed_value= 0;      // F value in the selected feed mode
    bool   force_g97_before = false;
};
struct TurnModalPlan {
    std::vector<TurnOpModal> per_op;     // parallel to the setup's toolpaths
    std::vector<std::string> refusals;   // non-empty => post_setup returns ok=false
    double spindle_off_rpm = 0;          // G97 S value emitted before M5
    std::string hash;
    json to_json() const;
};
TurnModalPlan plan_turn_modals(const Setup&, const ToolLibrary&,
                               const std::vector<Toolpath>&, const PostConfig&);
```

| Rule | Closes |
|---|---|
| CSS engage is exactly `clamp` → `G96 S<css>` → `M3`/`M4` **with no S word**. `NcGen::spindle()` gains `bool with_speed_word` and is called with `false` while CSS is active. | **D-T3** |
| `clamp_rpm = min` over the non-zero values of `{op.max_turn_rpm, machine.max_rpm, chuck.rated_max_rpm}`; all zero ⇒ refusal `turn_css_clamp_unknown`. | D-T24 |
| `surface_speed_mode=="css" && css <= 0` ⇒ refusal `turn_css_missing`. No RPM substitution, ever. | D-T6 |
| `TurnThread`, `TurnDrill`, and any op whose comp'd path reaches below the CSS minimum radius set `force_g97_before` ⇒ `G97 S<rpm>` precedes the op; an op that *declares* CSS in those cases ⇒ refusal `turn_css_invalid_for_op`. | D-T4, D-T10 |
| Lathe footer: `G97 S<spindle_off_rpm>` → `coolant_off` → `spindle_off` (M5) → `program_end` → `program_stop`. The `!g.lathe` condition at `post.cpp:487` is deleted. | D-T4, **D-T5** |
| `feed_per_rev > 0` ⇒ `FeedMode::PerRev`: emit `c.feed_per_rev` (`G95`) on mode change and `F<mm/rev>`; a dialect with an empty per-rev word ⇒ refusal `turn_feed_per_rev_unsupported`. Returning to per-min emits `c.feed_per_min` (`G94`). | D-T25 |
| `opt.metric == false` on a lathe setup ⇒ refusal `turn_imperial_unsupported`. The model is mm-only; a `G20` header over mm numbers is a 25.4× error, and `G96 S` under `G20` is sfm, not m/min. | D-T28 |

**Cycle-time honesty.** `estimate_time`/`move_time_s` (`stock_sim.cpp:32-68`) assume mm/min. The estimator
gains a turning path using `f_mm_min = (PerRev) ? feed_per_rev * rpm_at(r) : feed_value`, with
`rpm_at(r) = min(clamp_rpm, 1000 * css_m_min / (2π·r))` under CSS and `rpm` otherwise, integrated over the
move's radius span. When `clamp_rpm` is unknown the estimate is reported as
`estimate_state:"unknown"` and the GUI renders unknown — never a number.

---

## 6. G18 and lathe arcs

`PostConfig` (in `core/cam/post.h`) gains, appended to the struct: `plane_xz` (`"G18"`), `diameter_on`
(`"G7"`), `radius_on` (`"G8"`), `feed_per_min` (`"G94"`), `feed_per_rev` (`"G95"`), `thread_cycle`
(`"G76"`), `thread_cycle_style` (`"linuxcnc_g76"` or empty), `turn_arc_ik` (bool). All are string/bool, so
`PostConfig::from_json`'s existing `S`/`B` helpers already cover them.

Lathe header, replacing `post.cpp:361`: emit `plane_xz`, then `diameter_on` **or** `radius_on`
**explicitly** — never relying on the control's default. LinuxCNC defaults to radius (`G8`), so shipping an
implicit assumption here would be exactly the silent-substitution class this plan exists to remove. (D-T2)

`NcGen::arc()`'s lathe branch (`post.cpp:252-264`) is rewritten: `X` via `AxisSemantics::to_program_x`,
`Z` plain, offsets **`I` = radius delta, `K` = axial delta** via `to_program_i`; `J` is never emitted on a
lathe. A lathe dialect with `arcs_ijk == false && r_for_arcs == true` may emit `R` only for sweeps under
180°, otherwise refusal `turn_arc_r_ambiguous`. (D-T11)

Note the ordering constraint: `op_turn.cpp` currently emits no `MoveKind::Arc` at all, so this path is
dead today and becomes live the moment `NoseComp::offset` inserts tangent corner arcs (§3). The `I`/`K`
fix must therefore land **in the same change as nose comp**, not after it.

---

## 7. Post — one qualified lathe dialect, fail-closed everywhere else

### 7.1 The dialect

Exactly **one** new preset, from a new `base_lathe()` sibling of `base_mill()` in `build_dialects()`:

```
id "linuxcnc_lathe"   name "LinuxCNC lathe"
description "LinuxCNC 2.9 lathe (G18, G7, G96/G97, G95, G76)"
  lathe = true; diameter_mode = true;
  plane_xz="G18"; diameter_on="G7"; radius_on="G8";
  feed_per_min="G94"; feed_per_rev="G95";
  css_on="G96"; css_off="G97"; css_clamp="";        // LinuxCNC clamps via the G96 D-word, not G50
  thread_cycle="G76"; thread_cycle_style="linuxcnc_g76";
  arcs_supported=true; arcs_ijk=true; turn_arc_ik=true; r_for_arcs=false;
  canned_drill=false;                                // TurnDrill long-hands (safer; §8)
  tool_change="M6"; tool_word_before_change=true; length_offset=false;
  work_offset=true; percent_wrap=true; dwell_seconds=true; dwell_word="P";
  comp_supported=false;                              // computer comp only (§3.4)
```

`[INFERENCE]` — the LinuxCNC clamp form (`G96 S… D<max rpm>` rather than Fanuc's `G50 S…`) and the
`G76 P… Z… I… J… K… R… Q… H… E… L…` argument set come from the LinuxCNC lathe documentation cited by
the manufacturing-breadth research. **Neither is interpreter-verified in this design.** The E2 interpreter run
(§8.3, §14) is the acceptance evidence, and until it passes and its trace is checked in, this dialect's
`post` level is not advertised. `css_clamp` is deliberately left empty so the generic Fanuc `G50` block is
not emitted into a control that does not use it.

**No `fanuc_lathe`, `haas_lathe` or `grbl_lathe` in this wave.** Shipping a "generic ISO lathe" preset
repeats D-T1/D-T26: a lathe dialect is worth its name only once an interpreter has read its output. Those
ids do not exist, so asking for one is an error rather than a degradation.

### 7.2 Fail-closed resolution (D-T26)

`post_dialect()` is **deleted** and replaced — clean cutover, no shim:

```cpp
struct DialectResolve { bool ok = false; std::string error; const PostConfig* cfg = nullptr; };
DialectResolve post_dialect_resolve(const std::string& id);
```

Callers to migrate: `post.cpp:331` (`post_setup`), `post.cpp:551`/`:553` (`setup_sheet`), and the
`cam_post` / `cam_setup_sheet` / `cam_audit` paths in `core/cam/session.cpp`.
`post_dialects_json()` additionally emits each dialect's turning capability row (§13.4), so
`cam_post_dialects` stops implying every dialect is lathe-capable-if-you-override-it.

### 7.3 Coherence, and closing the override back door (D-T27, D-T33)

* `post_setup` refuses `machine.kind == Lathe && !c.lathe` (`turn_dialect_not_lathe`) and
  `c.lathe && kind != Lathe` (`turn_dialect_lathe_only`).
* `opt.overrides` may no longer set `lathe`. `diameter_mode` stays overridable **only** inside a dialect
  that already has `lathe == true`. `post.cpp:339-340` is replaced accordingly.
* `MachineKind::MillTurn` with any `Turn*` op ⇒ refusal `turn_machine_kind_unsupported`, naming the
  missing level `generate` (multi-channel and live tooling are out of scope, §19).
* A dialect with `tool_change.empty()` and more than one distinct tool number in a turning setup ⇒
  refusal `turn_tool_change_unsupported`. With §7.1 this means GRBL simply cannot post a lathe program,
  which is the turning-side closure of the audit's highest-risk GRBL gap.

---

## 8. Threading

### 8.1 Posted motion

`post_setup` gains a threading branch on `op->type == OpType::TurnThread` that emits the dialect's
`thread_cycle` and **never** long-hand moves:

```
(preceded by G97 S<rpm> per §5, then a G0 to the drive-line start X/Z)
G76 P<pitch> Z<z_end> I<peak_offset> J<first_depth> K<full_depth>
    [R<degression>] [Q<compound_angle>] [H<spring_passes>] [E<taper>] [L<taper_mode>]
```

`I` is signed and negative for an external thread; `Z` is the thread end; the drive line is the current
position, so the generator's approach move defines the major diameter. `P` = `thread_pitch`,
`J` = `infeed`, `K` = `thread_depth`, `H` = `thread_spring_passes`.

An empty `thread_cycle` ⇒ refusal `turn_thread_cycle_unsupported`. **There is no long-hand threading
fallback, ever** — an unsynchronised `G1` thread is defect D-T7, not a degraded mode.

### 8.2 Simulation long-hand

`gen_turn_thread` still produces degressive pass geometry, but:
* those moves are marked via `Toolpath::sim_only`, so the post cannot emit them even accidentally, and the
  false comment at `op_turn.cpp:310-311` is corrected;
* `od` is replaced by the **local** major radius from `TurnProfile::r_at(z)` sampled over the thread's Z
  span, refusing `turn_thread_profile_ambiguous` when the major radius is not constant there (D-T8);
* the Z run-out is bounded by `TurnGuard` (D-T9);
* the synthetic `tf` feed is deleted — the simulation consumes `pitch × rpm` from the modal plan, and
  threading under CSS is refused upstream (D-T10).

### 8.3 Acceptance

The `G76` block is accepted only by the E2 interpreter run (§14): zero interpreter errors, and the canon
trace's threading motion envelope matching the simulated pass geometry within tolerance. Until then
threading is not part of the advertised `post` level.

---

## 9. Stock, simulation and lineage

### 9.1 `core/cam/stock_sim.h` additions

```cpp
struct TurnRemnant {
    double z0 = 0, dz = 0;               // first slice centre, slice pitch
    std::vector<double> r_out, r_in;     // per-slice annulus (r_in == 0 => solid)
    bool   valid = false;
    std::string profile_hash;
    turn::TurnProfile to_outer_profile() const;   // -> geometry["stock_profile"]
    turn::TurnProfile to_bore_profile()  const;
    json to_json() const; static TurnRemnant from_json(const json&);
};

struct SimResult {
    /*…existing…*/
    TurnRemnant turn_remnant;
    std::string sim_model;     // "dexel_topdown" | "axisymmetric_annulus"
    json        sim_fidelity;  // §10
};
```

### 9.2 Rewrite of `stock_sim.cpp:192-245`

| Change | Closes |
|---|---|
| `kind == Lathe` **requires** `Cylinder`/`CylinderOffset`/`FromTurnSim`; box or `FromSolid` ⇒ `ok=false, error="turn_sim_requires_axisymmetric_stock"`. No fall-through to the dexel path. | D-T15 |
| `kind == MillTurn` ⇒ `ok=false, error="turn_sim_machine_kind_unsupported"`. | D-T16 |
| Two arrays `r_out`, `r_in`. `id_op == false` may only **lower** `r_out`; `id_op == true` may only **raise** `r_in`. An ID pass whose radius exceeds `r_out[k]` records `turn_id_pass_outside_stock` and does not touch `r_out`. | **D-T14** |
| Volume `Σ π (r_out[k]² − r_in[k]²) · dz`. | D-T14 |
| `CylinderOffset` axial resolution becomes per-end: `bottom = base.z − off_axial`, `top = base.z + height + off_axial`, matching `model.h:82`. | D-T13 |
| `stock_od()` (`op_turn.cpp:75-79`) is replaced by a shared `turn::resolve_stock_od(const Stock&)` used by **both** generator and simulator, which adds `off_radial` for `CylinderOffset` and **refuses** rather than inventing `prof_max_r + stl + 2.0`. | D-T12, D-T18 |
| Grooving/parting radial plunges remove a slot of `insert_width` centred on the blade reference Z (from `Toolpath::turn_meta`), not one `res_mm` cell. | D-T20 |
| The swept shape is the **nose circle**, not the reference point: each cutting segment sweeps radius `nose_radius`, so the simulated surface matches the comp'd path. | D-T19 |
| `TurnGuard` intrusion is evaluated over **every** move including rapids, reported as `turn_workholding_collision` with the offending `(r,z)`. The dexel path checks only cutting moves; turning rapids at `od+clr` are the ones that hit jaws. | D-T21, and the audit's "holder checks skip rapids" gap |
| `res.turn_remnant` is populated and `res.remaining` is deliberately **left empty**, with `sim_model` set, so no caller can mistake an axisymmetric result for a dexel field. | D-T17 |
| The early `return` before the check battery is gone; the turning branch runs its own checks and then returns. | D-T22 partial |

### 9.3 Lineage (D-T17, and the stale-`lastRemaining` hazard)

* `StockType::FromTurnSim` carries `turn_profile`/`turn_bore` plus `StockLineage`.
* `core/cam/session.cpp`'s `from_sim` path gains a turning branch: when
  `sim_model == "axisymmetric_annulus"` it materialises `FromTurnSim`, **not** `FromSolid`.
* It **refuses** `stock_lineage_stale` when `parent_job_revision` differs from the current job revision,
  and publishes `mfg.turn.invalidated {class:"falsified", reason:"stock_lineage_stale", why,
  affected:[setup_id], at_seq}` — pushed by the producer, never discovered by polling, and applied only to
  rows with `seq <= at_seq`. `at_seq` is the gateway-stamped seq (§13.3); turning mints none.
* Re-running a sim on unchanged inputs publishes `{class:"superseded", superseded_by:<new report id>,
  at_seq}` — retained, forward-pointing, no alarm.
* `mfg_turn_profile_extract{source:"sim_remnant"}` emits `geometry["stock_profile"]` from
  `TurnRemnant::to_outer_profile()`, finally giving `gen_turn_rough`'s dead input a producer, and
  `_GEOMETRY_SLOTS` (`cam_tools.py:307`) gains `"stock_profile"`. (D-T18)

---

## 10. Collision — what is and is not modelled

`SimResult::sim_fidelity` for turning is emitted verbatim into every report and rendered in the GUI:

```json
{
  "model": "axisymmetric_annulus",
  "models": ["material_removal_od", "material_removal_id", "volume_accounting",
             "nose_radius_swept_surface", "insert_width_slot",
             "workholding_bounding_envelope", "stock_silhouette_intrusion"],
  "does_not_model": ["tool_holder_geometry", "turret_and_carriage_bodies",
                     "cross_slide_and_saddle_envelope", "tailstock_body_vs_tool",
                     "non_axisymmetric_features", "live_tooling_and_c_axis",
                     "chip_evacuation", "cutting_forces_and_deflection",
                     "workpiece_ejection", "thermal_growth", "coolant"],
  "collision_truth": "unsupported",
  "collision_why": "the holder/shank model is coaxial about the tool axis (core/cam/stock_sim.cpp:326-333), which is the wrong axis for a lathe"
}
```

`collision_truth` is **`unsupported`** with a why — not `not_applicable`. Per DoD v4 §5 it could become
`not_applicable` only if independently corroborated for the same subject and config epoch by fresh
evidence, which no turning check can supply. The GUI renders it as an explicit "not modelled" row; the
report never renders a blank or a tick there. A clean turning simulation is therefore **never** evidence
of clearance, and it says so in the same object that carries the volumes.

---

## 11. Broad-enum repair policy and the Tier-0 register

The rule for every declared-but-unimplemented turning enum value: **either wire executable semantics or
refuse at the action entry point, naming the missing level.** No value degrades silently, and no
`*_from_string` in the turning namespace defaults.

Rows use Gateway's shape from `core/manufacturing/tier0.h` —
`{action, prerequisite, class:"PREREQ", artifact_dependent, applies_when{artifact_kind[], machine_class[]},
why, missing_level?, remedy[]}`. `class` is always `PREREQ`: a whole action prerequisite can never be
`not_applicable`, missing evidence fails, and the refusal renders at the action entry point before any
proposal exists. Gateway owns the row type, the totality check over
`action × artifact_kind × machine_class`, and the rendering contract.

**Registration — one authoring site, one register.** Every action in this table is gated inside the
daemon (the turning generate/post/simulate paths are C++, and `mfg_turn_validate_offline` is a daemon RPC
that dispatches to the Python service). So all 18 rows are authored **once** in
`core/cam/turn/turn_tier0.cpp` as a static table and registered with
`tier0::registerRows("cam.turn", rows)` at engine init. Turning therefore ships **no**
`services/manufacturing/turning/tier0.py` and never calls `mfg_tier0_register`: per
`MFG-TIER0-ONE-REGISTER` that RPC is a thin wrapper over the same in-daemon register for out-of-process
domains, a second *entry point* is fine, and a domain-private second *register* is not. The Python
service reads the rows back over the bridge for rendering and never re-declares them, which is what
`fixtures/mfg/turn/tier0/turn_tier0_rows_expected.json` (§16.3) pins.

| id | Action | Prerequisite | Refusal id | `missing_level` |
|---|---|---|---|---|
| T0-T1 | turning generate / post / simulate / validate | `Setup::workholding.declared` | `turn_workholding_undeclared` | plan |
| T0-T2 | turning generate | `Tool::turn` present, `orientation != 0`, `nose_radius > 0` | `turn_tool_orientation_undeclared` | plan |
| T0-T3 | turning generate | `TurnProfile::validate` ok on `geometry["profile"]` | `turn_profile_invalid` | plan |
| T0-T4 | turning generate (groove / part) | `insert_width > 0` | `turn_insert_width_undeclared` | plan |
| T0-T5 | turning generate | `machine.kind == Lathe` | `turn_machine_kind_unsupported` | generate |
| T0-T6 | turning generate | `check_lathe_frame` ok | `turn_frame_not_lathe` | plan |
| T0-T7 | turning generate | `comp_in_control == false` | `turn_control_comp_unsupported` | post |
| T0-T8 | turning post | resolved dialect exists | `post_dialect_unknown` | post |
| T0-T9 | turning post | `dialect.lathe == true` | `turn_dialect_not_lathe` | post |
| T0-T10 | turning post | one distinct tool, or non-empty `tool_change` | `turn_tool_change_unsupported` | post |
| T0-T11 | turning post (TurnThread) | non-empty `dialect.thread_cycle` | `turn_thread_cycle_unsupported` | post |
| T0-T12 | turning post | `opt.metric == true` | `turn_imperial_unsupported` | post |
| T0-T13 | turning post (CSS) | `css > 0` | `turn_css_missing` | post |
| T0-T14 | turning post (CSS) | a non-zero clamp source exists | `turn_css_clamp_unknown` | post |
| T0-T15 | turning post (feed/rev) | non-empty `dialect.feed_per_rev` | `turn_feed_per_rev_unsupported` | post |
| T0-T16 | turning simulate | axisymmetric stock | `turn_sim_requires_axisymmetric_stock` | simulate |
| T0-T17 | `from_sim` stock | `parent_job_revision == current` | `stock_lineage_stale` | simulate |
| T0-T18 | any turning transport / execute request | — (no path exists) | `turn_no_command_owner` | execute |

`applies_when.machine_class` is `{lathe}` for T0-T1..T0-T17 and `{*}` for T0-T18. Every row ships
`remedy[]` naming the concrete action (e.g. T0-T1 → "call `set_turning_workholding`"). A guard that later
catches one of these is defence-in-depth, never a substitute (DoD §6): the refusal renders at the entry
point *and* the guard stays.

---

## 12. MCP surface (`services/mcp/cam_tools.py`)

### 12.1 Repairs to existing tools

* `_materialize_offset_stock` (`:321-363`) — `cylinder_offset` no longer becomes a box. It resolves the
  part bbox into `{"type":"cylinder", "base":[cx,cy,zmin-off_axial],
  "radius":max_half_extent+off_radial, "height":(zmax-zmin)+2*off_axial}`, keeping the type axisymmetric.
  The `kind not in (...)` early return leaves `box_offset` behaviour untouched. (**D-T29**)
* `cam_add_setup` (`:498`) — the default stock is chosen by machine kind: `lathe` ⇒
  `{"type":"cylinder_offset","off_radial":1.0,"off_axial":1.0}`; everything else keeps
  `{"type":"box_offset","off_ztop":1.0}`. (**D-T30**)
* `cam_add_setup` — a `lathe` setup with no workholding returns the T0-T1 refusal naming
  `set_turning_workholding`, instead of creating a setup that cannot generate.
* `cam_add_tool` — a turning `type` (`turning_general`, `grooving_tool`, `threading_tool`,
  `parting_tool`, `boring_bar_id`) requires `orientation` and `nose_radius` (plus `insert_width` for
  groove/part), rejected in the same style and for the same documented reason as the existing
  `_resolve_tool_type` guard: the engine would otherwise fill defaults and every derived number would be wrong.
* `_GEOMETRY_SLOTS` (`:307`) gains `"stock_profile"`.
* `cam_simulate` — a lathe response echoes `sim_model` and `sim_fidelity`, and the `changes` summary
  states `collision: unsupported` explicitly so a reader cannot infer clearance.
* `cam_post` — the existing zero-motion false-green guard (`:784-797`) stays; a lathe post additionally
  fails when the program lacks `G18`, lacks an explicit radial-mode word before first motion, or lacks a
  spindle-off block. That is a cheap independent re-read of our own output; the full battery is §14.

### 12.2 New tools — family `manufacture`, OFF by default

Registered as `view(mcp, "manufacture")`, the single shared manufacturing family, OFF by default in
`agent.tool_families` until commissioning, with per-tool toggles retained for granularity.

| Tool | Risk | Purpose |
|---|---|---|
| `describe_turning_capability(setup_id?)` | READ | `CapabilityReport` for `cam.turn` (§13.4) |
| `extract_turning_profile(source, setup_id?, z_range?)` | READ | validated `TurnProfile` + hash, from the part or a sim remnant |
| `set_turning_workholding(setup_id, workholding)` | WRITE (document only) | sets `Setup::workholding`, returns resolved `min_safe_z`/`max_safe_z`/`max_part_radius` |
| `get_turning_sim_remnant(setup_id)` | READ | `TurnRemnant` + `StockLineage` |
| `validate_turning_program_offline(setup_id, dialect_id, stage)` | READ | `TurnValidationReport` (§14) |

Turning contributes **no** `RISK_MOTION` tool, no safety-class tool, and no `request_mfg_*` tool. There is
no `connect`, `upload`, `stream`, `run`, `jog`, `home`, `zero`, `override`, `unlock` or interlock path in
this surface — absence, not denial — and none will be added by this track.

---

## 13. RPCs, events, freshness, capability

### 13.1 RPCs

Engine RPC table plus `services/manufacturing/turning/**`. **No `mfg_*` symbol is added to the public
`libchudware` C ABI.** Non-overlapping with `mfg_device_*`/`mfg_run_*`/`mfg_approval_*`/`mfg_capability_*`/
`mfg_event_*`/`mfg_policy_*` (gateway), `mfg_monitor_*`, `mfg_cut_*`, `mfg_plasma_*`, `mfg_pnp_*`, `mfg_robot_*`.

| RPC | Payload → result |
|---|---|
| `mfg_turn_capability_report` | `{setup_id?}` → `FreshValue<CapabilityReport>` |
| `mfg_turn_profile_extract` | `{source:"part"\|"sim_remnant", setup_id, z_range?}` → `FreshValue<TurnProfile>` |
| `mfg_turn_workholding_set` | `{setup_id, workholding}` → `{revision, resolved}` |
| `mfg_turn_sim_remnant` | `{setup_id}` → `FreshValue<TurnRemnant>` + `StockLineage` |
| `mfg_turn_post_preview` | `{setup_id, dialect_id}` → `{nc, modal_plan, refusals[]}` (no file write) |
| `mfg_turn_validate_offline` | `{setup_id, dialect_id, stage:"A"\|"A'"\|"B"}` → `FreshValue<TurnValidationReport>` |

### 13.2 Events

`mfg.turn.profile_changed`, `mfg.turn.workholding_changed`, `mfg.turn.validation_complete`,
`mfg.turn.capability_changed`, `mfg.turn.invalidated`.

### 13.3 Freshness and provenance

The sole external envelope is the shared
`FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}`. Turning defines no parallel shape.
`v` is null unless `state=="ok"`; `last_ok` is display-only; a sequence gap forces `unknown`.

Per `MFG-SEQ-SOLE-SOURCE`, **`mfg_event_append` is the only way turning obtains a stamped `seq`** — the journal
assigns it and turning supplies content only; minting one is a contract-test failure. The Python service
reaches the journal exclusively through the gateway-owned helper
`services/manufacturing/rpc/journal_client.py` (`append` / `invalidate` / row read-back); that helper is
**not** copied into `services/manufacturing/turning/`. Offline durable seq comes from
`chudware-mfgd --journal-only`, which opens no transport, registers no device, and adds no gate SKIP.

`src` and `admission` are computed by the gateway and never accepted from a caller
(`MFG-PROV-GATEWAY-COMPUTED`). Turning implements **no transport at all** — not `ITransport`, not even
the read-only `IReadTransport` of `MFG-READ-TRANSPORT-SPLIT` that `MonitoringArchitecture` now owns — so
it is *structurally* incapable of producing `probed` evidence.
Its sim and Stage-A/A' results are `simulated`; its dialect/profile/workholding/tool declarations are
`declared`. That is why turning can never pass a Stage-1 execute-side admission (§15), and why its five
reachable levels are flipped by `procedural_fact` instead (§14).

`derived_from[]` is supplied on every append so the gateway computes the transitive closure of
`affected[]` on `mfg_result_invalidate` (`MFG-CLOSURE-GATEWAY-OWNED`) — turning names the root subject
only and never guesses an affected set. Turning uses no
`retain_as_last_ok` row: with no probed lane there is nothing whose value must clear while a display-only
last-good is retained.

### 13.4 Capability — composed, never re-declared

Turning composes Gateway's `core/device/capability.h` types verbatim:

```cpp
// domain_id "cam.turn"; levels{} holds one CapabilityTruth per DoD level name.
struct TurnCapabilityView {                     // a thin view, NOT a new level vocabulary
    CapabilityReport report;                    // {domain_id, levels{}, max_advertised,
                                                //  emulator_stage_passed}
    std::string device_command_owner_why;       // owner itself is null
    json dialects;                              // per dialect: {lathe, thread_cycle, feed_per_rev,
                                                //               css, verdict}
    json sim_fidelity;                          // §10
    json bindings;                              // job_revision, profile_hash, workholding_hash,
                                                // tool_hash, modal_plan_hash, nc_sha256, dialect_id
    std::string verdict;                        // computed; "unknown" on any edit
};
```

Every level is `declared` until its `evidence_ref` names a seq-stamped journal row, at which point it
becomes `verified`. `assumed` is never emitted for turning — it would require naming a missing level we
depend on, and turning depends on none. `monitor`, `transport`, `execute` and `safety_integration` are
`unsupported` **with a why**. `device_command_owner` is `null` with a why. `max_advertised` may never
exceed the `emulator_stage_passed` ceiling.

### 13.5 Durable result kinds

Six kinds, each registered in the contract's durable-result-kind table with **both** mandatory columns
(`MFG-KIND-TWO-COLUMNS`): an epoch policy *and* runtime invalidation triggers. Declaring only one is a schema
rejection. Turning owns no probed transport, so every kind is `epoch_invariant` — its durable state
survives a gateway restart intact — while still being falsifiable mid-epoch by a source-hash change. Ids
are namespaced on the domain id, matching `cam.cut.*`.

| `result_kind` | Epoch policy | Runtime invalidation triggers | `derived_from[]` | `supersede_with_parent` |
|---|---|---|---|---|
| `cam.turn.profile` | `epoch_invariant` | part geometry hash change, extract source change → **falsified**; re-extract with identical hash → **superseded** | — | n/a (root) |
| `cam.turn.workholding` | `epoch_invariant` | new declaration for the same setup → **superseded**; setup deletion → **falsified** | — | n/a (root) |
| `cam.turn.sim_remnant` | `epoch_invariant` | `job_revision`, `profile_hash`, `workholding_hash` or `tool_hash` change → **falsified**; identical-input re-run → **superseded** | `cam.turn.profile`, `cam.turn.workholding` | **true** — a remnant is a derivation of one profile and one workholding; once either is superseded this remnant is no longer the current derivation, and saying so is not an accusation |
| `cam.turn.procedural_fact` | `epoch_invariant` | the cited `artifact_hash`, `config_epoch` or `snapshot_id` changing → **falsified** | `cam.turn.profile`, `cam.turn.workholding` | **true** — REQUIRED by `MFG-PFACT-FOLLOWS-PARENT`: a procedural fact is what flips a level to `verified`, so a superseded parent leaving the old fact standing would keep a level verified after the check it cites was re-run |
| `cam.turn.validation_report` | `epoch_invariant` | any of `nc_sha256`, `dialect_id`, `profile_hash`, `workholding_hash`, `tool_hash`, `modal_plan_hash` changing → **falsified**; re-run on identical bindings → **superseded** | `cam.turn.profile`, `cam.turn.workholding`, `cam.turn.procedural_fact` | **true** — the report is the collated view of its facts; when they move forward so does it |
| `cam.turn.capability` | `epoch_invariant` | `emulator_stage_passed` change, dialect-table change, or level `evidence_ref` change → **superseded** by a freshly re-derived row; any cited evidence row being falsified → **falsified** via the closure, and the affected level drops out of `verified` | every row the report cites: `cam.turn.validation_report`, `cam.turn.procedural_fact` | **false** — FORBIDDEN by `MFG-CAPABILITY-NO-INHERIT`: a capability row re-derives from its own evidence and never inherits |

`falsified` always propagates along `derived_from`, and the gateway computes the closure. Supersession
follows the per-kind declaration above: turning's derived artifacts do follow their parents, because
"no longer the current derivation" is the honest statement for a remnant, a fact or a report whose profile
or workholding has moved on — while `cam.turn.capability` must re-derive so a level can never stay
`verified` on inherited paperwork. Every notice carries the gateway's `at_seq` and applies only to rows
with `seq <= at_seq`. No turning kind uses `retain_as_last_ok`: with no probed lane there is nothing whose
value must clear while a display-only last-good is retained.

`cam.turn.capability` exists because of `MFG-CAPABILITY-DURABLE-KIND`: turning's panel paints a
`CapabilityReport`, and DoD 10 requires a gateway-stamped durable row behind **every** durable result a UI
may paint, not only telemetry. Turning advertises five levels above `unsupported`, so registration
without this kind is refused `mfg_capability_requires_durable_kind`. When any cited evidence row is
falsified or superseded, turning re-derives and appends a fresh capability row; the closure makes the
previous one non-current, so no level can outlive the evidence beneath it.

**W1.1 registration of these kinds — closes review finding B10.** Per `MFG-KIND-MUST-BE-REGISTERED`,
until a kind is in the contract's durable-result-kind table every append of it is refused
`mfg_unregistered_result_kind` and turning's durable surface does nothing; per
`MFG-CAPABILITY-DURABLE-KIND` a domain advertising any level above `unsupported` without a registered
`<domain>.capability` kind is refused `mfg_capability_requires_durable_kind` outright. Turning therefore
**submits all six rows above — `cam.turn.{profile,workholding,sim_remnant,procedural_fact,validation_report,capability}`,
the sixth of which *is* the capability kind — for the W1.1 accepted durable-result-kind set**, each
carrying both `MFG-KIND-TWO-COLUMNS` columns plus its `supersede_with_parent` declaration and its
`derived_from[]`, together with turning's permitted subject namespaces `{art:, job:, res:}` (§13.6). This
is an append-only, integrator-owned contract edit landing in **W1.1**, and it is a hard predecessor of
every later turning slice: no turning append, and no turning capability advertisement above
`unsupported`, is admissible before it lands. `cam.turn.capability` is registered under
`MFG-CAPABILITY-DURABLE-KIND` as turning's `<domain>.capability` kind, with
`supersede_with_parent:false` mandated by `MFG-CAPABILITY-NO-INHERIT`, so W1.1's kind table is total over
`cam.turn` in both columns the moment it closes.

### 13.6 Subject namespaces and guard acceptance

Turning declares its permitted subject namespaces at registration (`MFG-SUBJECT-NS-DECLARED`) as exactly
`{art:, job:, res:}` — an NC artifact, a job/setup, a turning result. It declares **neither `dev:` nor
`src:`**, so an append outside that set is refused `mfg_subject_namespace_not_permitted` and turning
*cannot* write into the device or monitor lanes even by mistake.

Turning's Stage-2 guards declare `accepts{subject_ns:[art:,job:,res:], admission:["procedural_fact"]}`
(`MFG-GUARD-ACCEPTS-DECLARED`). `gates::evaluate` refuses anything outside that set by name — including a fresh `probed` `src:`
observation, which turning has no basis to interpret. None of turning's guards is approval-bound to a
device, since turning binds no machine identity and owns no device lease.

---

## 14. LinuxCNC-backed offline validation

New: `services/manufacturing/turning/{__init__,capability,profile,offline_validate,linuxcnc_sidecar,report}.py`.

### Stage A — hermetic NC re-read (E1, always runs, dependency-free)

`offline_validate.py` re-parses **our own** emitted NC and asserts, per check with a named id:

1. `plane_selected` — exactly one `G18` before the first motion block; no `G17`/`G19` anywhere.
2. `radial_mode_explicit` — `G7` or `G8` before the first motion block.
3. `css_clamped` — every CSS engage carries a clamp source.
4. `css_not_clobbered` — no bare `S` word between a `G96` and the next `G97`; no `S` on the `M3`/`M4`
   following `G96`. *(direct D-T3 regression)*
5. `css_cancelled` — a `G97` precedes every threading and drilling block, and precedes `M5`.
6. `spindle_stopped` — a spindle-off block exists before program end. *(D-T5)*
7. `feed_mode_consistent` — every `F` value's mode matches the modal plan.
8. `radial_words_match_model` — every `X` word equals `AxisSemantics::to_program_x(model radius)` for its
   `Move`, within format tolerance. *(catches every future radius/diameter slip)*
9. `arc_offsets_ik` — lathe arc blocks carry `I`/`K`, never `J`. *(D-T11)*
10. `thread_cycle_used` — a `TurnThread` op emits `thread_cycle`, and no `G1` block lies inside its span. *(D-T7)*
11. `workholding_respected` — no motion block below `min_safe_z` or above `max_safe_z`. *(D-T21)*
12. `units_metric` — `G21` present, `G20` absent. *(D-T28)*

### Stage A' — recorded canon replay (E1, gate-resident)

Stage-B canon traces are checked in as fixtures and replayed hermetically, so the *comparison logic* is
gate-covered even where no binary exists. Each trace carries `{linuxcnc_version, date, binary_sha256}`;
a trace whose recorded version no longer matches the documented pin reports `state:"stale"`, not `ok`.

### Stage B — LinuxCNC standalone interpreter (E2, opt-in, never called by the gate)

`linuxcnc_sidecar.py` invokes the installed LinuxCNC standalone interpreter **out of process** on the
posted file, captures the canon call trace, and asserts zero interpreter errors, a tool-tip path matching
our `Toolpath` moves within tolerance (this is what actually validates the `G7` reading and the `G76`
argument set), and a threading canon envelope matching the simulated pass geometry.

Discovery follows the established idiom (`CHUDWARE_NGSPICE`, `OPENMVS_BIN`, `CHUDWARE_SLICER_BIN`):
`CHUDWARE_LINUXCNC_BIN`. It runs **only** from the opt-in `scripts/qualify_turn_e2.sh`, which
`scripts/gate.sh` never calls — with a test asserting that. Per the project ruling, **turning requests no new
normal-gate SKIP**; Stage A and Stage A' are hardware-free and dependency-free, so a missing binary
degrades fixture richness, not coverage.

Guardrails: an executable allowlist (`rs274`, `rs274ngc`) resolved from the env var; no HAL, no config, no
device node, no serial or network argument is ever passed; refused if the resolved path is not a regular
executable. It is an interpreter, not a controller — there is no device to own, and
`device_command_owner` stays `null`.

Licensing: LinuxCNC is GPL. It is **invoked**, never linked, never vendored, never redistributed. A
hermetic test asserts that no `linuxcnc`/`rs274` header include or link appears anywhere under `core/**`
or `host/**`, so the rule holds without editing the integrator-owned fast gate; appending the same rule
there is a one-line belt-and-braces follow-up for the integrator.

### Stage C — LinuxCNC sim lathe config (E3, manual)

Task-level run in a LinuxCNC sim configuration, hardware-free. Not in CI; recorded as evidence only.

### Emulator ladder and the ceiling

`E0` unit → `E1` Stage A + Stage A' (gate-resident) → `E2` live interpreter (opt-in) → `E3` sim config
(manual). `E4`–`E6` are not attempted. Gateway's ceiling rule (`E0-E3 → monitor`, `E4-E5 → transport`,
`E6 → execute`, and `safety_integration` unlocked by no stage) leaves turning's ceiling at `monitor`;
turning deliberately stops **one rung lower, at `simulate`**, because it produces no observations. `post`
for `linuxcnc_lathe` is advertised only once E2 has passed and its trace is checked in.

### How the five reachable levels become `verified`

Turning's evidence is procedural by construction: a named check ran against a specific artifact, config
epoch and snapshot. Each Stage-A/A'/B check therefore appends a
`ProceduralFact{check_name, artifact_hash, config_epoch, snapshot_id?, at_seq, outcome}` tagged
`admission:"procedural_fact"` as a `cam.turn.procedural_fact` row (§13.5, which per
`MFG-PFACT-FOLLOWS-PARENT` must and does declare `supersede_with_parent:true`), and that is the **only**
route by which a turning level moves from `declared` to `verified`. `MFG-PFACT-FLIP-SCOPE` scopes
`procedural_fact` to exactly `plan | generate | post | parse | simulate` — which is precisely turning's
reachable set — and forbids it from ever flipping
`monitor`, `transport`, `execute` or `safety_integration`. Those four require fresh `probed` evidence plus
the emulator-stage ceiling, and turning has no transport to produce it, so they are permanently
`unsupported` **by construction, not by policy**.

### `TurnValidationReport`

```cpp
struct TurnValidationReport {
    std::string id, stage;          // "A" | "A'" | "B" | "C"
    std::string emulator_stage;     // E1 | E2 | E3
    json checks;                    // id -> {result:"pass"|"fail"|"omitted", why}
    std::string verdict;            // "pass" | "fail" | "unknown"
    json bindings;                  // job_revision, setup_id, nc_sha256, dialect_id, profile_hash,
                                    // workholding_hash, tool_hash, modal_plan_hash, sim_model
    std::vector<std::string> waivers;  // durable {by, reason, at_seq}
};
```

Stage-2 semantics: `pass | fail | omitted`. There is **no** `unknown` in a completed report, and **no**
`not_applicable` for any whole prerequisite. `omitted` covers only checks irrelevant to the job's shape —
`thread_cycle_used` when there is no `TurnThread` op, `arc_offsets_ik` when the comp'd path produced no
arcs — and an omitted check never counts as a pass (DoD §7). `verdict` is computed, bound to `bindings`,
and flips `unknown` the instant a bound input hash changes, at which point
`mfg.turn.invalidated {class:"falsified", …, at_seq}` is pushed with the gateway-stamped `at_seq`.

---

## 15. Capability claims — staged, exact

| Level | Claim at the end of this wave | Basis |
|---|---|---|
| `plan` | `declared` → `verified` once its evidence_ref names a seq-stamped row | profile/workholding/tool schemas, validators, Tier-0 register (§2, §4, §11) |
| `generate` | `verified`, scoped: single spindle, single channel, one active tool, XZ only, computer comp only | `op_turn.cpp` hardened per §3, §4, §8.2 |
| `post` | `verified` **for `linuxcnc_lathe` only**, and only after E2 passes once. Every other dialect: `unsupported` for turning, with a why. | §7, §14 |
| `parse` | `verified` at E1 (Stage A + Stage A'); E2 is opt-in corroboration | §14 |
| `simulate` | `verified` for axisymmetric OD+ID removal, volume, remnant, lineage and cycle time (with `estimate_state:"unknown"` when the CSS clamp is unknown). Sub-level `collision`: `unsupported` with a why. | §9, §10 |
| `monitor` | `unsupported` + why: turning produces no observations; `ObservationRecord` is `MonitoringArchitecture`'s and turning consumes none of it | — |
| `transport` | `unsupported` + why: no transport exists in this track | — |
| `execute` | `unsupported` + why: no controller I/O; `device_command_owner` null + why | — |
| `safety_integration` | `unsupported` + why: ISO 23125 hazards are guarding, interlock and commissioning concerns that no software here addresses | — |

Never claimed, in any wording, anywhere in the product surface: "lathe supported", "turning ready",
"machine-executable", "collision-checked", "verified NC", "Fanuc/Haas compatible".

### Interaction with the shared two-stage gate

Turning contributes **no** Stage-1 evidence. Stage 1 admits only fresh `state=="ok" && src=="probed"`
evidence plus verified execute capability; every turning result is `simulated` or `declared` by typed
provenance, and DoD §14 makes simulated conclusions context only. Turning's own release gate is a
Stage-2 artifact gate over `TurnValidationReport.checks`, and passing it authorises exactly one thing:
handing an operator an NC file with an attached, hash-bound evidence report. Guard liveness (DoD §8) is
satisfied per guard by the paired pass/fail tests in §16.

---

## 16. Tests and fixtures — exact files

### 16.1 C++ — `core/tests/test_cam_turn.cpp` (new)

Registered by appending to `core/CMakeLists.txt`, mirroring the existing pattern at
`core/CMakeLists.txt:516-519`: `add_executable(test_cam_turn tests/test_cam_turn.cpp)`,
`target_include_directories(test_cam_turn PRIVATE ${doctest_SOURCE_DIR})`,
`target_link_libraries(test_cam_turn PRIVATE chudware_cam)`, plus the new `core/cam/turn/*.cpp` sources
appended to the `chudware_cam` source list. Both are pure appends; the integrator owns the merge.

| # | Test | Defends |
|---|---|---|
| 1 | `turn_semantics_radius_diameter_roundtrip` | §2.1; `to_program_i` never doubles |
| 2 | `turn_post_emits_g18_and_explicit_radial_mode` | D-T2 |
| 3 | `turn_post_css_not_clobbered_by_rpm_word` | **D-T3** |
| 4 | `turn_post_emits_g97_before_thread_and_drill` | D-T4 |
| 5 | `turn_post_emits_spindle_off_in_lathe_footer` | **D-T5** |
| 6 | `turn_post_refuses_css_mode_without_css_value` | D-T6 |
| 7 | `turn_post_refuses_css_without_clamp_source` | D-T24 |
| 8 | `turn_post_emits_g76_and_never_longhand_thread` | **D-T7** |
| 9 | `turn_post_refuses_thread_on_dialect_without_thread_cycle` | D-T7 |
| 10 | `turn_thread_uses_local_major_radius_on_stepped_shaft` | D-T8 |
| 11 | `turn_thread_runout_bounded_by_workholding` | D-T9 |
| 12 | `turn_lathe_arc_emits_ik_never_j` | D-T11 |
| 13 | `post_dialect_resolve_fails_closed_on_unknown_id` | D-T26 |
| 14 | `post_refuses_mill_dialect_for_lathe_setup` + converse | D-T27 |
| 15 | `post_refuses_lathe_override_on_mill_dialect` | D-T27 |
| 16 | `post_refuses_multi_tool_turning_without_tool_change` | D-T33 |
| 17 | `post_refuses_imperial_turning_output` | D-T28 |
| 18 | `turn_feed_per_rev_emits_g95_and_reverts_to_g94` | D-T25 |
| 19 | `turn_cycle_time_integrates_css_rpm_clamp` + `..._reports_unknown_without_clamp` | §5 |
| 20 | `turn_nose_comp_offsets_taper_by_nose_radius` (uncomp'd control case fails) | **D-T19** |
| 21 | `turn_nose_comp_refuses_radius_larger_than_concave_fillet` | D-T19 |
| 22 | `turn_generate_refuses_without_declared_workholding` | D-T21 / T0-T1 |
| 23 | `turn_generate_refuses_rather_than_silently_clipping_at_jaws` | D-T21 |
| 24 | `turn_generate_refuses_undeclared_tool_orientation` | D-T20 / T0-T2 |
| 25 | `turn_groove_and_part_require_insert_width` | D-T20 / T0-T4 |
| 26 | `turn_stock_od_identical_in_generate_and_sim_with_off_radial` | **D-T12** |
| 27 | `turn_sim_cylinder_offset_axial_is_per_end` | D-T13 |
| 28 | `turn_sim_id_pass_does_not_remove_outer_diameter` | **D-T14** |
| 29 | `turn_sim_parting_removes_insert_width_slot` | D-T20 |
| 30 | `turn_sim_refuses_box_stock_on_lathe` | D-T15 |
| 31 | `turn_sim_refuses_millturn` | D-T16 / T0-T5 |
| 32 | `turn_sim_populates_remnant_and_leaves_dexel_field_empty` | D-T17 |
| 33 | `turn_from_sim_refuses_stale_job_revision` | D-T17 / T0-T17 |
| 34 | `turn_sim_reports_collision_unsupported_with_why_not_na` | D-T22, §10 |
| 35 | `turn_profile_validate_rejects_negative_radius_and_bad_arity` | D-T31 |
| 36 | `turn_generate_refuses_control_side_comp` | D-T32 / T0-T7 |
| 37 | `turn_frame_check_refuses_non_spindle_z` | D-T23 / T0-T6 |
| 38 | `turn_capability_composes_gateway_report_without_redeclaring_levels` | §13.4 |
| 39 | `turn_capability_last_four_levels_unsupported_with_why_and_owner_null` | §15 |
| 40 | `turn_capability_max_advertised_never_exceeds_emulator_stage_passed` | Gateway ceiling |
| 41 | `turn_enum_from_string_never_defaults` | §11 |
| 42 | `turn_tier0_rows_are_all_class_prereq_with_remedy` | §11 |

Guard liveness: tests 2–17 and 22–37 each ship a **pass** case and a **fail** case, so no guard is
vacuously satisfiable and none is unsatisfiable (DoD §8).

### 16.2 Python

* `services/mcp/test_cam_tools.py` (append) — `test_cylinder_offset_stock_stays_cylindrical` (**D-T29**),
  `test_lathe_setup_default_stock_is_cylinder_offset` (**D-T30**),
  `test_lathe_setup_without_workholding_is_refused`,
  `test_turning_tool_requires_orientation_and_nose_radius`,
  `test_cam_post_lathe_rejects_program_without_g18_or_spindle_off`,
  `test_cam_simulate_lathe_echoes_collision_unsupported`.
* `services/manufacturing/turning/test_offline_validate.py` (new) — one mutation test per Stage-A check
  (drop `G18`; insert an `S` between `G96` and `G97`; delete the `G97` before threading; delete the
  spindle-off; emit `J` on a lathe arc; emit a `G1` inside a thread span; place a motion block below
  `min_safe_z`; flip `G21`→`G20`; perturb one `X` word), each asserting the specific check id fails while
  the others still pass.
* `services/manufacturing/turning/test_capability.py` (new) — `FreshValue` shape and the null-`v` rule;
  seq-scoped invalidation (`falsified` clears, `superseded` retains and points forward); verdict flipping
  `unknown` on a bound-input hash change; `device_command_owner` null with a why; the emulator ceiling;
  and an assertion that turning **mints no seq** of its own but obtains one from `mfg_event_append`.
* `services/manufacturing/turning/test_result_kinds.py` (new) — every turning `result_kind` declares
  **both** mandatory columns (epoch policy and triggers) and is rejected when either is dropped; every
  kind is `epoch_invariant`; `falsified` propagates along `derived_from`; `cam.turn.procedural_fact`
  declares `supersede_with_parent:true` and registration is refused
  `mfg_procedural_fact_must_follow_parent` without it; `cam.turn.capability` declares it **false** and is
  refused if set true; every derived kind declares a supersession choice with a reason
  (`MFG-SUPERSESSION-DECLARED`); no kind
  uses `retain_as_last_ok`; and `affected[]` comes from the gateway closure, not the caller.
* `services/manufacturing/turning/test_capability_durable_kind.py` (new, `MFG-CAPABILITY-DURABLE-KIND`) — turning registers
  `cam.turn.capability`; a registration advertising any level above `unsupported` without it is refused
  `mfg_capability_requires_durable_kind`; falsifying **one** cited evidence row produces a **new**
  capability row **and** drops the affected level out of `verified` (asserted per level, so there is no
  path where a level survives on falsified evidence); and the panel reads only the current row, never a
  superseded one.
* `services/manufacturing/turning/test_subject_namespace.py` (new) — turning registers exactly
  `{art:, job:, res:}`; an append with a `dev:` or `src:` subject is refused
  `mfg_subject_namespace_not_permitted`; every turning Stage-2 guard refuses by name each namespace and
  admission class it did not declare, including a fresh `probed` `src:` observation; and turning declares
  no approval-bound device guard.
* `services/manufacturing/turning/test_procedural_fact.py` (new) — a `procedural_fact` row flips
  `plan/generate/post/parse/simulate` to `verified`, and **cannot** flip
  `monitor/transport/execute/safety_integration` (each of the four asserted individually); turning
  registers no transport class, so no code path can construct `probed` evidence for it.
* `services/manufacturing/turning/test_tier0_single_register.py` (new) — the 18 rows come from the
  in-daemon `tier0::registerRows("cam.turn", …)` table, match
  `fixtures/mfg/turn/tier0/turn_tier0_rows_expected.json`, and no turning module defines a second register or ships
  a `tier0.py`; a `class` other than `PREREQ` is rejected; the gateway totality check reports no uncovered
  `(action, artifact_kind, machine_class)` tuple for `cam.turn`.
* `services/manufacturing/turning/test_canon_replay.py` (new) — Stage A' replay of the recorded traces,
  plus `test_gate_never_invokes_turn_e2` asserting `scripts/gate.sh` contains no reference to
  `qualify_turn_e2.sh` or `CHUDWARE_LINUXCNC_BIN`. The live Stage-B test is `skipif` on the env var.
* `services/manufacturing/turning/test_tool_family.py` (new) — the five turning tools register under
  family `manufacture`, the family is OFF by default in `agent.tool_families`, and none of them is
  `RISK_MOTION` or safety-class.

### 16.3 Fixtures — `fixtures/mfg/turn/**` (closes C9)

Under the ratified top-level fixture root, not the service tree: `fixtures/mfg/**` is the recorded
path/naming ruling, and six sibling slices (`fixtures/mfg/{gateway,twin,cut,pnp,robot,monitor}/**`)
create the directory in W1.x regardless, so turning's earlier "no `fixtures/` directory exists in this
repo" rationale no longer holds. Every turning fixture path below, and every reference to one elsewhere
in this document (§11's Tier-0 pin, §16.1, §16.2), is rooted at `fixtures/mfg/turn/`.

```
fixtures/mfg/turn/jobs/stepped_shaft.json    cylinder_offset stock, 3-jaw chuck, face + OD rough + finish + part
fixtures/mfg/turn/jobs/bored_sleeve.json     ID case: centre drill + bore rough/finish + OD finish
fixtures/mfg/turn/jobs/threaded_stub.json    external M20x1.5 thread on a turned-down diameter
fixtures/mfg/turn/jobs/grooved_pulley.json   grooving with a declared insert width
fixtures/mfg/turn/nc/linuxcnc_lathe_stepped_shaft.nc
fixtures/mfg/turn/nc/linuxcnc_lathe_bored_sleeve.nc
fixtures/mfg/turn/nc/linuxcnc_lathe_threaded_stub.nc
fixtures/mfg/turn/nc/linuxcnc_lathe_grooved_pulley.nc
fixtures/mfg/turn/canon/rs274_stepped_shaft.trace.json   + .meta.json  (linuxcnc_version, date, binary_sha256)
fixtures/mfg/turn/canon/rs274_threaded_stub.trace.json   + .meta.json
fixtures/mfg/turn/sim/stepped_shaft_remnant.json         expected TurnRemnant (r_out / r_in per slice)
fixtures/mfg/turn/sim/bored_sleeve_remnant.json
fixtures/mfg/turn/capability/turn_capability_expected.json
fixtures/mfg/turn/tier0/turn_tier0_rows_expected.json
```

---

## 17. GUI

`host/src/ui/manufacture/turning_panel.{h,cpp}` (new), entry point
`ui::manufacture::drawTurningPanel(...)`. It consumes only `FreshValue<CapabilityReport>` and
`FreshValue<TurnValidationReport>` and renders:

1. a workholding editor (writing via `mfg_turn_workholding_set`) showing the resolved
   `min_safe_z`/`max_safe_z`;
2. a turning-tool editor — orientation 1..9 picker, nose radius, insert width, hand, lead angle;
3. the profile with the **comp'd reference path drawn separately** from the part surface, so nose comp is
   visible rather than implied;
4. the axisymmetric remnant with the bore drawn as a distinct annulus, which makes D-T14's failure class
   visually impossible to miss;
5. a capability strip: `plan/generate/post/parse/simulate` with their truth values, and
   `monitor/transport/execute/safety_integration` as an explicit **"unsupported — no machine control in
   this product"** row carrying each `why`;
6. a `collision: unsupported` banner from `sim_fidelity`, always visible on a turning setup;
7. the Stage A / A' / B check list with pass/fail/omitted plus the bound hashes.

`FreshValue.state != "ok"` renders "unknown" with `why`; `last_ok` renders greyed with its age and is never
presented as current truth. No new canvas, no `ui/mfg`.

**The one seam into existing CAM GUI:** `host/src/ui/cam/**` gains a single `Turning…` entry calling
`ui::manufacture::drawTurningPanel`. Nothing else there changes.

---

## 18. Files touched — complete list

**Modified (existing, extended through named seams):**
`core/cam/model.h`, `core/cam/model.cpp`, `core/cam/op_common.h`
`[INFERENCE: OpContext gains workholding + AxisSemantics; its exact current members beyond
stock/tool/machine and the resolved_op/op_num helpers were not read for this design]`,
`core/cam/op_turn.cpp`, `core/cam/post.h`, `core/cam/post.cpp`, `core/cam/stock_sim.h`,
`core/cam/stock_sim.cpp`, `core/cam/session.cpp` (turning `from_sim` branch + `mfg_turn_*` registration),
`services/mcp/cam_tools.py`, and `core/tests/test_cam.cpp` **only if** an existing case encodes the
`CylinderOffset` asymmetric behaviour — if so it is asserting D-T13 and is updated with a comment
recording that.

**Appended (append-only, integrator-owned):** `core/CMakeLists.txt` (new sources + `test_cam_turn`
target); the **six** `cam.turn.*` rows of §13.5 in the contract's durable-result-kind table — one of
which is the `cam.turn.capability` kind mandated by `MFG-CAPABILITY-DURABLE-KIND` — each with both
mandatory columns plus its `supersede_with_parent` declaration and its `derived_from[]`, and turning's
permitted subject namespaces
`{art:, job:, res:}` (§13.5, §13.6, submitted for W1.1 per Appendix A.3/B10); optionally one fast-gate rule forbidding LinuxCNC includes/links. **No**
`scripts/gate.sh` edit and **no** new SKIP category — the batch-wide tally is zero new SKIPs, and the
documented list stays the DoD-1 five. **No** C ABI symbol, **no** top-level CMake change, and no
`contracts/**` edit beyond the append-only result-kind rows.

**New:** `core/cam/turn/{turn_semantics,turn_profile,workholding,turn_guard,turn_tool,turn_capability,post_turn,turn_tier0}.{h,cpp}`;
`core/tests/test_cam_turn.cpp`;
`services/manufacturing/turning/{__init__,capability,profile,offline_validate,linuxcnc_sidecar,report}.py`
plus the nine test modules; the `fixtures/mfg/turn/**` tree of §16.3; `scripts/qualify_turn_e2.sh` (opt-in, never
called by the gate); `host/src/ui/manufacture/turning_panel.{h,cpp}`.

**Deliberately NOT created:** `services/manufacturing/turning/tier0.py` (one authoring site, §11) and any
per-domain copy of `services/manufacturing/rpc/journal_client.py` (gateway-owned, §13.3).

**Coordination agreed with the twin and cutting workstreams:** `TwinDesignRecovery` extracts only the milling sweep helpers
(`stock_sim.cpp:24-30`, `:339-388`) into `core/cam/stock_step.{h,cpp}` and refuses `MachineKind::Lathe`
outright, so the axisymmetric branch (`:192-245`) plus the timing and frame code belong to turning and are uncontested.
`CuttingProcessArchitecture` owns `core/cam/cut/**` (disjoint from `core/cam/turn/**`) and the head branch
of `post.cpp` (`:401-402`, `:454-470`, `:482`). The footer line `post.cpp:487` is **turning's exclusively for
this wave** and is no longer a joint edit — the binding statement is **Appendix A.2**, which is the wave
ownership record for that line and is recorded identically in
`18_cutting.md` (Appendix A.2).
`GatewayDesignRecovery` owns `CapabilityTruth`/`CapabilityReport` and the Tier-0 row type;
`MonitoringArchitecture` owns `ObservationRecord`, of which turning consumes none.

---

## 19. Non-goals (explicit)

1. **Any machine control.** No connect/upload/stream/run/jog/home/zero/override/unlock/reset; no serial,
   network or device node. `device_command_owner` null. LinuxCNC is an interpreter subprocess, never a controller.
2. **Multi-channel, multi-spindle, mill-turn, live tooling, C-axis, polar interpolation, sub-spindle, bar
   feeder, part catcher, synchronised hand-off.** `MachineKind::MillTurn` refuses at Tier-0 (T0-T5) with
   the missing level named. The channel/spindle/barrier/transfer schema that the manufacturing-breadth research
   recommends designing ahead is deliberately **not** added: an unused schema is the same
   declared-not-implemented defect this plan exists to close.
3. **Tool/holder/turret/carriage collision detection.** `collision: unsupported` (§10).
4. **Non-axisymmetric turning features** — off-centre work, milled flats, cross-drilling.
5. **Vendor controller compatibility.** No `fanuc_lathe`/`haas_lathe`/`grbl_lathe` exists. Generic
   ISO-family output is not a compatibility statement, and a lathe dialect no interpreter has read is not
   a dialect.
6. **Control-side cutter comp (`G41/G42`), machine tool-table export, tool-offset writeback, touch-off,
   lathe probing.** Probing belongs to its own track.
7. **Safety functions of any kind.** ISO 23125 hazards — chuck and workpiece ejection, rotating
   entanglement, tool/turret collision, chips and coolant — are guarding, interlock and commissioning
   concerns. Nothing here reduces them; `safety_integration` is `unsupported`.
8. **Cutting forces, deflection, chatter, chip control, thermal growth, tool wear.**
9. **Imperial output.** Refused (T0-T12) rather than silently wrong.
10. **Monitoring.** Turning defines no observation type and consumes none.

---

## 20. Risks

* **The `CylinderOffset` axial fix (D-T13) changes existing mill-with-cylinder-stock results** by
  `off_axial`. `model.h:82`'s documented meaning ("per end") is the correct target, so the fix lands and
  any test encoding the old asymmetry is updated with a comment naming it as the bug. This is the only
  change in the plan that alters a non-turning result — flagged for the integrator.
* **`post.cpp:487` is the one genuinely shared line in the batch, and it is now singly owned.** Turning's change
  deletes the `!g.lathe` condition so lathe programs stop the spindle; cutting's laser-condition
  adjustment is subsumed by that deletion and lands with it. One owner, one edit — see **Appendix A.2**.
  Residual risk is behavioural rather than textual: the footer is now reached by lathe setups too, so
  `turn_post_emits_spindle_off_in_lathe_footer` and cutting's golden NC both assert it.
* **The LinuxCNC `G76` argument set and `G96` clamp form are documentation-derived, not
  interpreter-verified** (§7.1, §8.1). Largest correctness risk, and contained: the `post` level for
  `linuxcnc_lathe` is not advertised until E2 passes and its canon trace is checked in.
* **`core/cam/op_common.h` was not read for this design**, so the exact shape of the `OpContext` extension is
  `[INFERENCE]`. It affects how workholding and axis semantics reach the generators, not what they do.
* **Stage A' trace freshness** depends on a documented refresh procedure tied to a pinned LinuxCNC
  version; a stale trace reports `state:"stale"` rather than `ok`, so the failure mode is visible rather
  than silent. Per the project ruling this stays opt-in qualification and adds no gate SKIP.

---

## Appendix A — Review closure (B10, B15, C9, C10)

Closes the architecture plan review findings assigned to this document. No product scope changes:
every turning deliverable, non-goal and
capability claim above stands exactly as accepted.

### A.1 Gateway rule-id register (replaces every gateway section-number cross-reference — closes C10)

C10 found that this document cited the two most load-bearing gateway rules by the wrong section number
(the `procedural_fact` flip scope as §24.6 and subject namespacing as §24.10, which are in fact the
monitor kind table and the `monitor_condition` relabel). Renumbering would only move the hazard, since
gateway §20's contract tests are specified by section. So this document now cites **stable rule ids**
only; `test_mfg_contracts.cpp` and every turning test key on these ids, never on a section number. Each
row restates an already-accepted gateway obligation verbatim in substance — nothing here is new scope.

| rule id | obligation (accepted gateway text, restated) | previously cited here as |
|---|---|---|
| `MFG-SEQ-SOLE-SOURCE` | `mfg_event_append` is the only way any domain obtains a stamped durable `seq`; the caller supplies content only, and minting, deriving or carrying a seq is a contract-test failure. | "Gateway §24" (§13.3) |
| `MFG-KIND-MUST-BE-REGISTERED` | A `result_kind` absent from the contract's durable-result-kind table is refused `mfg_unregistered_result_kind`; table presence is a hard W1.1 prerequisite, not a nicety. | — (implicit) |
| `MFG-PROV-GATEWAY-COMPUTED` | `src`/`admission` are computed by the gateway and never accepted from a caller; `probed` is settable only by transport code inside the daemon holding an open `ITransport`. | "Gateway §23/§24" (header, §13.3) |
| `MFG-TIER0-ONE-REGISTER` | `mfg_tier0_register` is a thin wrapper over the in-daemon `tier0::registerRows` — same register, same totality check, same refusal text. Two entry points are permitted; a domain-private second register is not. | "Gateway §24" (§11) |
| `MFG-KIND-TWO-COLUMNS` | Every registered kind declares **both** (a) an epoch policy (`falsify \| reestablish \| epoch_invariant`) and (b) its runtime invalidation triggers; declaring only one is a schema rejection. | §24.1 (§13.5) |
| `MFG-CLOSURE-GATEWAY-OWNED` | `mfg_result_invalidate` computes the transitive closure over `derived_from[]`; `affected[]` is the gateway's closure and the caller names only the root subject. `falsified` always propagates; `superseded` propagates only for kinds declaring `supersede_with_parent`. | §24.1 (§13.3, §13.5) |
| `MFG-PFACT-FLIP-SCOPE` | A `procedural_fact` row may flip only `plan \| generate \| post \| parse \| simulate`; `monitor \| transport \| execute \| safety_integration` require fresh `probed` evidence plus the emulator-stage ceiling and can never be flipped by `procedural_fact`, `simulated`, `declared`, `advisory` or `assumed`. | **§24.6 — wrong** (§14) |
| `MFG-PFACT-FOLLOWS-PARENT` | Every kind whose id ends in `procedural_fact` MUST declare `supersede_with_parent:true`; registration without it is refused `mfg_procedural_fact_must_follow_parent`. | §24.8 — wrong (§13.5, §14) |
| `MFG-SUPERSESSION-DECLARED` | Supersession of a derived-artifact row is the owning domain's choice, valid only when declared in its kind table with a one-line reason. | §24.9 (§16.2) |
| `MFG-CAPABILITY-DURABLE-KIND` | Any domain whose `CapabilityReport` can reach a UI MUST register a `<domain>.capability` durable kind; advertising any level above `unsupported` without one is refused `mfg_capability_requires_durable_kind`. | §24.11 (§13.5, §16.2) |
| `MFG-CAPABILITY-NO-INHERIT` | `*capability` kinds MUST NOT set `supersede_with_parent`; a capability row re-derives from its own evidence, and its `derived_from[]` is the evidence rows it cites. | §24.8/§24.11 (§13.5) |
| `MFG-SUBJECT-NS-DECLARED` | A domain declares its permitted subject namespaces at registration; an append outside that set is refused `mfg_subject_namespace_not_permitted`. `dev:` belongs to the gateway device lane and `src:` to the monitor lane. | **§24.10 — wrong** (§13.6) |
| `MFG-GUARD-ACCEPTS-DECLARED` | Every Stage-2 guard declares `accepts{subject_ns, admission}` and `gates::evaluate` refuses out-of-set evidence **by name**, recorded in the gate report rather than skipped. | §24.8 (§13.6) |
| `MFG-READ-TRANSPORT-SPLIT` | `IReadTransport` has no `write()`, so a monitor-lane collector can produce `probed` observations without ever being able to command. | §24 rule 3 / §24.5 (§13.3) |
| `MFG-CAPTRUTH-ONE-DEFINITION` | `CapabilityTruth`/`CapabilityReport` are consumed verbatim from `core/device/capability.h`; a second definition is a contract-test failure, and `probed` is an observation `src`, never a truth value. | §23/§23.1 (header, §13.4) |
| `MFG-FRESHVALUE-ONE-DEFINITION` | The sole external freshness shape is `FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}`; `v` is null unless `state=="ok"`, `last_ok` is display-only, and a sequence gap forces `unknown`. | §3.2 / DoD 4 (§13.3, §17) |
| `MFG-STAGE-CEILING` | `max_advertised` may never exceed the ceiling of `emulator_stage_passed`: E0-E3 unlocks at most `monitor`, E4-E5 `transport`, E6 `execute`, and no stage unlocks `safety_integration`. | §18 / DoD 18 (§14, §15) |
| `MFG-FAMILY-OFF-BY-DEFAULT` | The single shared MCP family `manufacture` is OFF by default in `agent.tool_families` until commissioning, so `enable_tools` cannot silently self-add manufacturing surface. | §17 (header, §12.2) |

Turning cites no other gateway rule. Where the text above still names a DoD criterion (DoD 2, 7, 9, 10,
14, 18) that is the authoritative document itself, not a gateway section, and stays as written.

### A.2 Shared-line ownership — `core/cam/post.cpp:487` (closes B15)

**Wave ownership of the shared `core/cam/post.cpp` footer line (`:487`).** For this wave the footer line
`core/cam/post.cpp:487` — today `if (!g.lathe && !(kind==Laser)) g.blk(spindle_off)` — is owned
**exclusively by `TurningArchitecture`** (domain `cam.turn`). Turning *deletes* the line's `!g.lathe`
condition so a lathe program finally stops its spindle (defect D-T5), while cutting only *adjusts* the
same line's laser condition, whose job its head branch moves into the `post_cut.cpp` call: the deletion
subsumes the adjustment, so one owner lands both effects and the joint edit both documents feared does not
exist. `CuttingProcessArchitecture` (domain `cam.cut`) makes **no** edit to `:487`. It consumes the
resulting footer behaviour — `spindle_off` reached by every mill and lathe setup, and the process-off word
for a cut head emitted by `post_cut.cpp` instead of by an inline condition on this line — and asserts it
from the cut side by golden NC (`fixtures/mfg/cut/programs/laser_grbl_expected.nc`, whose footer must
match what the `post_cut.cpp` call emits rather than what the deleted inline condition used to emit),
while turning asserts it by `turn_post_emits_spindle_off_in_lathe_footer` in
`core/tests/test_cam_turn.cpp`. Every other region of the file keeps the owner both documents already
agreed: `cam.cut` owns the head branch (`post.cpp:401-402`, `:454-470`, `:482`), the GRBL-degradation
seam (`:158-162`) and the power-through-RPM render it deletes (`:460`); `cam.turn` owns the lathe/CSS
region (`:403-410`) and the diameter-mode formatting (`:229-232`, `:252-254`). `post_dialects_json`
(`:169-175`) stays the shared **append** point both documents already declared — cutting adds `process`
and `power_bindings`, turning adds the per-dialect turning capability row — and this reconciliation does
not change its ownership. This paragraph is the wave ownership record for the line, recorded identically
in `17_turning.md` and
`18_cutting.md`; it supersedes turning's earlier "that one line
needs a joint edit" note and cutting's earlier claim that its `post.cpp` seams are **disjoint** from
turning's, which was false as written for `:487`.

### A.3 Closure table

| finding | required correction (review) | how this document closes it | where |
|---|---|---|---|
| **B10** (P0) — turning's durable-result kinds are absent from the W1.1 table, so every durable append is refused `mfg_unregistered_result_kind` and the capability registration is refused `mfg_capability_requires_durable_kind` | Extend the accepted W1.1 set with turning's six `cam.turn.*` kinds plus `cam.turn.capability`; W1.1 is not green until the kind table is total over every domain that appends | The submitted set is the **six rows of §13.5**, one of which already *is* `cam.turn.capability` (the review counted it separately; §13.5's table always contained it, so "six plus capability" and "six including capability" name the same set and the registered total is six). All six are **submitted for the W1.1 accepted durable-result-kind set** as an append-only integrator contract edit, each with both `MFG-KIND-TWO-COLUMNS` columns, its `supersede_with_parent` declaration (`false` for `cam.turn.capability` per `MFG-CAPABILITY-NO-INHERIT`, `true` for `cam.turn.procedural_fact` per `MFG-PFACT-FOLLOWS-PARENT`) and its `derived_from[]`, plus the permitted subject namespaces `{art:, job:, res:}`. Registration is a hard predecessor of every later turning slice; gateway-side acceptance of the extended set is `GatewayDesignRecovery`'s edit | §13.5 "W1.1 registration"; §13.6; §18 |
| **B15** (P1) — `post.cpp:487` requires a joint edit by two owners | Assign the footer line to exactly one owner for the wave (turning, since it deletes a condition cut only adjusts), have cut consume the result, and record it in the wave ownership table rather than one document's coordination note | Line assigned **exclusively to `cam.turn`**; cutting makes no edit and consumes the footer behaviour. Recorded as the wave ownership record in A.2, identical in both documents; §18's coordination note and §20's risk bullet now point at it instead of asserting a joint edit | A.2; §18; §20 |
| **C9** (P3) — turning's fixtures diverge from the ratified `fixtures/mfg/**` root | `fixtures/mfg/turn/**` | Fixture root moved from `services/manufacturing/turning/fixtures/` to `fixtures/mfg/turn/**`, with the stale "no `fixtures/` directory exists" rationale deleted and every in-document reference re-rooted (§11's Tier-0 pin, §16.2's Tier-0 test, §18's New list) | §16.3; §11; §16.2; §18 |
| **C10** (P3) — cross-reference drift on the two most load-bearing §24 rules (turning cited the flip scope as §24.6 and subject namespacing as §24.10; both point at unrelated rules) | Normalize cross-references and have the contract tests key on stable rule ids rather than section numbers | Every gateway section-number citation replaced by a stable rule id from the A.1 register, which restates the obligation in place so a later gateway renumbering cannot invalidate it; the two wrong citations are corrected to `MFG-PFACT-FLIP-SCOPE` and `MFG-SUBJECT-NS-DECLARED`, and the mis-attributed §24.8 uses are corrected to `MFG-PFACT-FOLLOWS-PARENT` / `MFG-CAPABILITY-NO-INHERIT` | A.1; §11; §13.3-§13.6; §14; §16.2 |

Unchanged by this appendix, and asserted here so the closure cannot be read as scope drift: all domains
stay in scope; the shared MCP family remains `manufacture`, off by default (`MFG-FAMILY-OFF-BY-DEFAULT`);
wire names remain `mfg_*`/`mfg.*`; turning adds **zero** new normal-gate SKIPs; the sole freshness schema
remains `FreshValue<T>` (`MFG-FRESHVALUE-ONE-DEFINITION`); the gateway journal owns all durable `seq` and
all invalidation closure (`MFG-SEQ-SOLE-SOURCE`, `MFG-CLOSURE-GATEWAY-OWNED`); and turning — a domain with
a UI capability strip — registers the durable capability kind `cam.turn.capability`
(`MFG-CAPABILITY-DURABLE-KIND`).
