# 2D Cutting-Process Architecture — Laser / Plasma / Waterjet

P4 core implementation is under `core/cam/cut/**`, with concrete `ProcessKind::Laser`,
`ProcessKind::Plasma`, and `ProcessKind::Waterjet` profiles and direct headless coverage
in `core/tests/test_cam_processes.cpp`. The implementation keeps process setpoints typed
and unit-bearing; no cut family stores power in spindle RPM.

Path ruling honored: production GUI only under `host/src/ui/manufacture/**`; cross-domain
service root `services/manufacturing/**`; domain engine under `core/cam/cut/**`; RPCs
`mfg_*`, events `mfg.*`; `host/src/ui/cam/**` extended only through one named seam.
(This supersedes the flat `core/cam/process_cut.*` names in an earlier draft; the
engine lives in `core/cam/cut/**` so it cannot collide with `core/cam/turn/**`.)

Implemented scope is bounded and offline: laser vector planning, GRBL laser-mode post and
independent re-parser; plasma pierce/arc/THC/corner/void/breakaway ledger replay; and exact-row
waterjet recipes plus LinuxCNC digital/analog-output post. Waterjet jet-lag and taper
compensation refuse by name. No family claims process physics, monitoring, transport,
execution, or safety integration; only levels backed by the implemented offline path are
declared pending gateway-stamped procedural evidence.

Gateway rules are cited by **stable rule id** only, never by gateway section number: the register that
resolves every id used below is **Appendix A.1**, and `test_mfg_contracts.cpp` plus every cut test key on
those ids. Review closure for **B10**, **B15**, **C9** and **C10** is recorded in **Appendix A**.

---

## 1. Measured baseline: what exists today, and why it is not three processes

All three "processes" are currently **one code path with one scalar**.

| Fact | Evidence |
|---|---|
| `MachineKind::{Laser,Plasma,Waterjet}` exist | `core/cam/model.h:105-115` |
| The *only* process field on `Machine` is `double kerf` | `core/cam/model.h:132-133` |
| `ToolType::{LaserHead,PlasmaTorch,WaterjetHead}` exist but `Tool` carries mill geometry only — `flutes`, `flute_length`, `holder_diameter = 32`, `shoulder_length` | `core/cam/model.h:150`, `core/cam/model.h:155-181` |
| One `OpType::Cut2D` for all three | `core/cam/model.h:237` |
| One shared parameter block: `passes`, `pierce_delay`, `pierce_height`, `cut_height`, `kerf_override`, `power`, `feed_rate` — with `power` documented as "0..1 laser power / **amperage scale**" | `core/cam/model.h:314-321` |
| One generator `gen_cut2d` for all three; `pb.set_spindle(0)`; `op.power` is **never read** by the generator | `core/cam/op_cut.cpp:43-175`, `:69` |
| Kerf comp keeps only `offs.front().outer` — island holes of a kerf-comped region are **silently dropped** | `core/cam/op_cut.cpp:89-96` |
| Open paths get **no** kerf comp and **no** lead-in/out | `core/cam/op_cut.cpp:84`, `:152-166` |
| `Move` has no process channel at all; `spindle_rpm` is the only power-shaped field | `core/cam/model.h:349-360` |
| `MoveKind` has no process on/off, no setpoint, no wait-for-condition, no probe | `core/cam/model.h:338-347` |
| The post's whole notion of a cutting head is `bool head = kind==Laser\|\|Plasma\|\|Waterjet` | `core/cam/post.cpp:401-402` |
| Power is emitted as an **S word rendered through the RPM formatter**: `"S" << fmt_num(op->power * c.power_max, g.c.rpm)` | `core/cam/post.cpp:460` |
| Beam gating is inferred from move kind: on at first `Plunge` (with S) or at any `Lead`/`Cut` (**without any S at all**), off at `Rapid`/`Retract` | `core/cam/post.cpp:454-470` |
| Only the `marlin` preset sets `laser_mode` / `torch_on` / `torch_off` / `power_max`; the other seven are `base_mill(...)` | `core/cam/post.cpp:120-131`, `:73-148` |
| Consequence: a Laser setup posted with `grbl`/`fanuc`/`haas`/… still takes the `head` branch and emits `c.torch_on`/`c.torch_off` defaults. `blk("")` returns immediately, so an empty default makes beam gating **vanish silently** | `core/cam/post.cpp:454-482`, `:207-208`; defaults live in `post.h`, outside read scope → `[INFERENCE]` on which of "silent no-op" vs "bare M3/M5 with no power" occurs |
| `post_dialects_json` advertises `"laser": c.laser_mode` (true only for marlin) yet nothing prevents posting a laser job with a mill dialect | `core/cam/post.cpp:169-175` |
| Unknown dialect id silently degrades to GRBL | `core/cam/post.cpp:158-162` |
| Footer suppresses `spindle_off` only for `Laser`; plasma and waterjet get an `M5` | `core/cam/post.cpp:487` |
| Simulation for a head machine is the **mill Z-dexel**: removal disc = `tool->effective_radius()`, field lowered to tip Z | `core/cam/stock_sim.cpp:325-326`, `:352-354` |
| But `cut_z = heights.top + cut_height` is **at or above the stock top** (`core/cam/op_cut.cpp:64`), so `zt < field.raw(i,j)` is essentially never true → `removed_volume_mm3 ≈ 0`, `machined_fraction ≈ 0`, `warnings` empty, `ok:true`. Kerf is never the removal width; a `LaserHead` would remove a 6 mm disc if any Z were below top (`model.h:159` default `diameter = 6.0`) | `core/cam/stock_sim.cpp:446-479` |
| Holder/shank collision for a torch uses `holder_diameter = 32` / `shoulder_length` mill defaults | `core/cam/stock_sim.cpp:331-334`, `:364-388` |
| Only `Lathe` + cylinder gets a dedicated non-dexel model; heads get nothing | `core/cam/stock_sim.cpp:192-245` |
| MCP accepts `machine="laser"\|"plasma"\|"waterjet"` and merges a **mill-only** profile (travel/rapids/rpm/feed/accel) | `services/mcp/cam_tools.py:481-485`, `:524-551` |
| MCP accepts `cam_add_tool(type="laser_head"\|"plasma_torch"\|"waterjet_head")` with mill-shaped args only — `diameter/flutes/corner_radius/tip_angle/thread_pitch`; there is no power, gas, pressure, orifice, or consumable parameter anywhere | `services/mcp/cam_tools.py:458-478` |
| `cam_add_operation` merges `params` verbatim (`op.update(params)`), so `power`/`pierce_delay` arrive untyped and unvalidated | `services/mcp/cam_tools.py:635-636` |
| `cam_simulate` docstring promises removal volume + gouge + holder checks for **any** setup | `services/mcp/cam_tools.py:744-750` |
| `cam_post`'s only release guard is the motion-line count; a laser job posted to `fanuc` counts motion lines and reports success | `services/mcp/cam_tools.py:782-801` |
| `feeds_speeds` tool vocabulary is `endmill_flat\|endmill_ball\|drill\|tap\|face_mill`; `cam_check_feeds_envelope` speaks only `rpm`/`feed` | `services/mcp/cam_tools.py:904-910`, `:937-975` |

Corroborating manufacturing-breadth research: laser is `NOW`
(vector + post + parser/emulator, hardware streaming labeled unsupported); plasma is
`NEXT` with an explicitly richer state machine (arc-ok, arc voltage, ohmic/float probe,
THC, breakaway, consumables — "not laser with another icon"); waterjet is `LATER` with
**weak open-emulator evidence** and process-specific pressure/abrasive/taper behavior.
Its abstraction rule #4 is exactly the rule this design enforces: *do not overload
spindle RPM as universal power*.

**Verdict on the baseline:** three regulated, physically unrelated processes share one
enum, one scalar, one generator, one S-word, and a removal model that reports
`machined_fraction ≈ 0` as success. That is a false-green chain and it is what this
design removes.

---

## 2. Separation principle

Three axes are split, and they are split *independently* — this is the part the current
code conflates:

1. **Geometry planning** (shared, honest to share): 2D vector contours, kerf
   compensation, lead placement, pierce placement, tabs, cut ordering. Lives once in
   `core/cam/cut/vector_plan.*`.
2. **Process state and setpoints** (never shared): what the head is commanded to do,
   in that process's own physical units, through its own typed channel.
3. **Controller binding** (never shared, never implicit): which G/M word carries which
   channel on which named dialect — declared, and a missing binding is a hard failure.

Enforced invariant, stated once here and asserted by a contract test:

> **No cut generator, post, recipe, or RPC ever writes `Move::spindle_rpm`, and no
> `ProcessSetpoint` is ever rendered through `PostConfig::rpm` (`NumFmt`) or an `S`
> word unless the selected dialect declares a `PowerBinding` for that exact channel and
> unit.** `power` as a bare 0..1 scalar is deleted from the API surface.

Channels are typed and unit-bearing, never interchangeable:

| Process | Channel | Unit | Never |
|---|---|---|---|
| Laser | `LaserPowerFraction` (+ `LaserPowerWatts` derived from the head rating) | dimensionless 0..1 / W | rev/min |
| Plasma | `PlasmaCurrentAmps` | A | rev/min, 0..1 |
| Waterjet | `WaterjetPressureMPa`, `AbrasiveFlowGPerMin` | MPa, g/min | rev/min, 0..1 |

---

## 3. Files

### 3.1 New engine — `core/cam/cut/**` (OCCT-free, JSON, headless-testable, matching the `core/cam` house style of `model.h:1-17`)

| File | Contents |
|---|---|
| `core/cam/cut/process.h` | `CutProcess`, `CutPowerChannel`, `ProcessSetpoint`, `CutHead`, `LaserProcess`, `PlasmaProcess`, `WaterjetProcess`, `CutProcessProfile`, `Provenance`, `CutFidelityManifest`, `CutCapability`. JSON round-trip for all. |
| `core/cam/cut/process.cpp` | (de)serialization, enum↔string tables, unit validation, profile/machine-kind consistency resolution. |
| `core/cam/cut/vector_plan.h/.cpp` | Shared 2D planning: multi-loop kerf comp (outer **and** islands), lead-in/out for open *and* closed paths, pierce placement, tab/micro-joint insertion, corner classification, cut ordering (holes before outer, thermal spacing for plasma), bounded 2-opt traverse ordering. |
| `core/cam/cut/op_laser.cpp` | `gen_cut_laser` |
| `core/cam/cut/op_plasma.cpp` | `gen_cut_plasma` (emits the process-state move sequence) |
| `core/cam/cut/op_waterjet.cpp` | `gen_cut_waterjet` |
| `core/cam/cut/recipe.h/.cpp` | `CutRecipe`, `CutRecipeKey`, `CutRecipeTable`, per-field `Provenance`, exact-row lookup with refusal (no interpolation, no extrapolation). |
| `core/cam/cut/plasma_state.h/.cpp` | `PlasmaProcessState`, `PlasmaSignalSet`, `PlasmaTransition`, `PlasmaFault`, `PlasmaReplayReport`; the offline state machine + replay driver. |
| `core/cam/cut/post_cut.h/.cpp` | `PowerBinding`, `CutDialect`, the four laser + one plasma + one waterjet dialect presets, and the head emission routine that replaces the `head` branch of `post_setup`. |
| `core/cam/cut/parse_gcode.h/.cpp` | `CutParseReport` — independent re-reader that reconstructs commanded state and asserts invariants. |
| `core/cam/cut/cut_sim.h/.cpp` | `CutSimResult` — kerf-through-cut 2D topology sim (severance, dropped parts, torch-over-opening, pierce/cut/traverse lengths, time model). |
| `core/cam/cut/emulator.h/.cpp` | `CutEmulatorReport`, tick-domain deterministic replay (E0/E1), out-of-process E2 adapter interface, E3 recorded-signal replay. |

### 3.2 Existing files — append-only edits at named seams

`TurningArchitecture` edits the same four C++ files. The seams below are disjoint from its
lathe/CSS/diameter seams by line region and by branch **with exactly one exception this document
previously denied**: the `core/cam/post.cpp` footer line `:487` is genuinely shared, so the earlier flat
claim that these seams are "**disjoint**" from turning's was false as written. That line is owned
**exclusively by `cam.turn`** for this wave and cutting edits it not at all; the binding ownership
statement is **Appendix A.2**, recorded identically in
`17_turning.md` (Appendix A.2). Every other seam below is disjoint exactly as stated.

**`core/cam/model.h`** — appends only. No field reordered, renamed, retyped, or removed
(ABI + `.cam+` + C-ABI JSON are append-only, per DoD §16 and `model.h:14-16`).

- P4 keeps the persisted `OpType::Cut2D` geometry carrier for compatibility; concrete
  family identity is mandatory in `cut::Profile`/`cut::ProcessKind`, so no new persisted
  operation id can silently default to laser.
- Process transitions live in `cut::PlanMove` with an optional unit-bearing
  `ProcessSetpoint`; they are deliberately not represented by `Move::spindle_rpm`.
- Head kerf and process settings live in the concrete profile. `MachineKind` remains the
  compatibility check at the existing `gen_cut2d` seam.

**Legacy boundary:** `OpType::Cut2D` remains the persisted geometry carrier and the legacy
generic CAM generator still resolves the head from `MachineKind`. New process planning
never reads the legacy unitless `Operation::power`; callers must construct a typed profile,
so a plasma current or waterjet pressure cannot be inferred from a laser fraction.

**Posting boundary:** process-aware NC is emitted by `cut::post_laser_grbl` and
`cut::post_waterjet_linuxcnc`, not by overloading the generic spindle words. Both consume
`VectorPlan` process transitions. Plasma deliberately has no post capability until a
qualified controller dialect exists. The shared generic post footer no longer emits a
spindle-off word for any cut-head machine; each process post owns its process-off sequence.

**`core/cam/stock_sim.cpp`** — one guard, inserted immediately after the zero-extent check
(`stock_sim.cpp:188-191`) and **before** the lathe branch (`:197`):

```
if (is_cut_head(setup.machine.kind)) {
    res.ok = false;
    res.error = "2D cutting head: the top-down Z-dexel removal model does not "
                "represent a kerf-through cut — it reports ~0 mm^3 removed for a "
                "correct program. Use mfg_cut_emulate_run / cut_sim instead.";
    return res;
}
```

This converts today's measured false green (`ok:true`, `machined_fraction ≈ 0`, empty
warnings) into an honest refusal. It is the highest-value single line in this design.

**`core/cam/op_cut.cpp`** — `gen_cut2d` (`:43-175`) becomes a thin dispatcher: resolve
machine kind → `gen_cut_laser` / `gen_cut_plasma` / `gen_cut_waterjet`; refuse when the
setup machine kind and the operation's process profile disagree, and refuse a cut op on a
non-head machine. The current body moves into `vector_plan.cpp` with the island and
open-path defects fixed. `gen_probe` (`:191-253`) is **not touched** — probe/metrology is
another workstream's surface.

### 3.3 Service — `services/manufacturing/cut/**`

| File | Contents |
|---|---|
| `services/manufacturing/cut/__init__.py` | package |
| `services/manufacturing/cut/mcp_tools.py` | the ten MCP tools of §5, all read-only/offline |
| `services/manufacturing/cut/profiles.py` | process/head/dialect catalog projection + capability truth table |
| `services/manufacturing/cut/recipes.py` | recipe table loading, provenance enforcement, refusal messages |
| `services/manufacturing/cut/emulator/runner.py` | E0/E1 driver (gate-resident, dependency-free); E2 out-of-process launcher behind `CHUDWARE_GRBL_SIM_BIN`, invoked only by the opt-in script below |
| `scripts/smoke_cut_offline.sh` | gate-resident, dependency-free: plan→post→parse→E0/E1 emulate per process |
| `scripts/qualify_cut_e2.sh` | opt-in only, **never called by `gate.sh`** — drives E2 against a user-supplied `grbl-sim` |
| `services/manufacturing/cut/replay.py` | plasma signal-ledger loader (`FreshValue` envelopes) |
| `services/manufacturing/cut/tier0.py` | the declarative Tier-0 row table for `domain_id="cam.cut"` (`MFG-TIER0-ONE-REGISTER`) |
| `services/mcp/cam_tools.py` | three guard/docstring edits (§5.2) + registration call |

### 3.4 GUI — `host/src/ui/manufacture/**`

| File | Contents |
|---|---|
| `host/src/ui/manufacture/cut_process_panel.h/.cpp` | process + head + recipe editor (unit-labeled fields, never an "S/RPM" field), capability truth table with the fidelity manifest rendered beside every result, emulator scrub, plasma state timeline with fault markers, Tier-0 refusal rendered at the action entry point |
| `host/src/ui/cam/**` | **one** seam only: a registration call `ui::cam::registerCutProcessSeam()`. No new canvas, no new dockspace, no `ui/mfg`. |

### 3.5 Fixtures — `fixtures/mfg/cut/**`

```
fixtures/mfg/cut/geometry/plate_100x60_3holes.json      shared planning input
fixtures/mfg/cut/geometry/open_path_bracket.json        open-path kerf/lead case
fixtures/mfg/cut/recipes/laser_co2_ss_3mm.json          measured/vendor provenance rows
fixtures/mfg/cut/recipes/plasma_45a_ms_6mm.json
fixtures/mfg/cut/recipes/waterjet_abrasive_al_12mm.json
fixtures/mfg/cut/programs/laser_grbl_expected.nc        post golden
fixtures/mfg/cut/programs/laser_power_on_traverse.nc    MUTATED — parser must flag
fixtures/mfg/cut/plasma/nominal_run.jsonl               recorded signal ledger
fixtures/mfg/cut/plasma/arc_lost_midcut.jsonl
fixtures/mfg/cut/plasma/probe_no_contact.jsonl
fixtures/mfg/cut/plasma/breakaway_trip.jsonl
fixtures/mfg/cut/plasma/thc_void_lock.jsonl
fixtures/mfg/cut/plasma/stale_arc_ok.jsonl              FreshValue state != ok
```

Recorded plasma ledgers are **data**, structurally modeled on the documented QtPlasmaC
signal set (manufacturing-breadth research). No QtPlasmaC (GPLv2) or grbl-sim (GPLv3)
source is copied, vendored, or linked; see §10.

---

## 4. Types

### 4.1 Process profiles

```cpp
enum class CutProcess { Laser, Plasma, Waterjet };

enum class CutPowerChannel {
    LaserPowerFraction, LaserPowerWatts,
    PlasmaCurrentAmps,
    WaterjetPressureMPa, AbrasiveFlowGPerMin,
};

enum class Provenance { Measured, Vendor, User, Inferred, LegacyMigrated };

struct ProcessSetpoint {          // the ONLY power channel in the move stream
    CutPowerChannel channel;
    double value = 0;
    std::string unit;             // "fraction" | "W" | "A" | "MPa" | "g/min"
    Provenance provenance = Provenance::User;
};

struct CutHead {                  // replaces the mill-shaped Tool for heads
    std::string id, vendor, model;
    CutProcess process;
    double kerf_mm = 0;
    Provenance kerf_source = Provenance::Inferred;
    double standoff_mm = 0;
    Vec3 mount_offset{0,0,0};
    double max_power_value = 0;    // in the channel's unit, not RPM
    CutPowerChannel channel;
};
```

**Laser**

```cpp
struct AssistGas { enum class Kind { None, Air, N2, O2 } kind; double pressure_bar; Provenance p; };

struct LaserProcess {
    double power_rated_w = 0;
    double power_fraction = 0;     // 0..1 commanded
    double duty_cycle = 1.0;       // diode PWM
    int    pwm_hz = 0;
    bool   speed_scaled_power = false;   // dynamic-power mode (Grbl M4)
    double focus_offset_mm = 0;
    double nozzle_standoff_mm = 0;
    AssistGas assist;
    int    pierce_pulses = 0;
    double pierce_power_fraction = 0, pierce_dwell_s = 0;
    double corner_power_scale = 1.0;     // ProcessSet at classified corners
    double kerf_mm = 0; Provenance kerf_source = Provenance::Inferred;
};
```

**Plasma**

```cpp
enum class ThcMode { Off, ArcVoltage, ManualHeight };
enum class PlasmaProbeMode { None, Ohmic, FloatSwitch };

struct GasMix { std::string plasma_gas, shield_gas; double plasma_bar, shield_bar; Provenance p; };
struct Consumable {
    std::string nozzle_id, electrode_id, swirl_ring_id, shield_id;
    double rated_current_a = 0;
    int    pierce_count = 0, arc_starts = 0;   // life counters, informational
    std::string cut_chart_id;                  // OUR row id; no vendor chart copied
};
struct ThcSettings {
    ThcMode mode = ThcMode::Off;               // never defaulted silently — Tier-0 requires explicit
    double target_voltage_v = 0, sample_delay_s = 0;
    double vad_threshold_pct = 0;              // velocity-anti-dive
    bool   corner_lock = true, void_lock = true;
    double max_correction_mm_s = 0;
};
struct ProbeSettings {
    PlasmaProbeMode mode = PlasmaProbeMode::None;   // explicit or refuse
    double probe_feed_mm_min = 0, probe_travel_mm = 0;
    double ohmic_offset_mm = 0, float_switch_travel_mm = 0;
};
struct PlasmaProcess {
    double current_a = 0;                      // NOT a 0..1 scale, NOT rev/min
    GasMix gas; Consumable consumable;
    double pierce_height_mm = 0, pierce_delay_s = 0;
    double transfer_height_mm = 0, cut_height_mm = 0;
    double puddle_jump_height_mm = 0;
    double kerf_mm = 0; Provenance kerf_source = Provenance::Inferred;
    ThcSettings thc; ProbeSettings probe;
    double breakaway_retract_mm = 0;
    double arc_ok_timeout_s = 0, arc_lost_retry_count = 0;
};
```

**Waterjet**

```cpp
enum class PumpClass { Intensifier, DirectDrive };
enum class WaterjetPierceMode { Stationary, Dynamic, Circle, Wiggle };

struct AbrasiveSpec { std::string material; int mesh = 0; double flow_g_min = 0; Provenance p; };
struct WaterjetProcess {
    double pressure_mpa = 0;                   // declared operating pressure
    PumpClass pump = PumpClass::Intensifier;
    double orifice_mm = 0, mixing_tube_mm = 0, mixing_tube_length_mm = 0;
    AbrasiveSpec abrasive;
    int    quality_level = 3;                  // 1..5, recipe-table lookup ONLY
    WaterjetPierceMode pierce_mode = WaterjetPierceMode::Stationary;
    double pierce_delay_s = 0, standoff_mm = 0;
    double kerf_mm = 0; Provenance kerf_source = Provenance::Inferred;
    double taper_deg_declared = 0;             // ECHOED vendor/user value
    Provenance taper_provenance = Provenance::Vendor;
    bool   corner_slowdown_enabled = true;
    double min_corner_feed_scale = 1.0;        // recipe scalar, not a jet-lag model
};

struct CutProcessProfile {                     // tagged; exactly one payload is set
    CutProcess kind;
    std::string profile_id, revision;
    std::optional<LaserProcess>    laser;
    std::optional<PlasmaProcess>   plasma;
    std::optional<WaterjetProcess> waterjet;
    CutCapability capability;
    CutFidelityManifest fidelity;
};
```

### 4.2 Recipe

```cpp
struct CutRecipeKey { CutProcess process; std::string head_id, material_id; double thickness_mm; int quality; };
struct CutRecipeField { double value; std::string unit; Provenance provenance; std::string source, source_rev; };
struct CutRecipe {
    CutRecipeKey key; std::string row_id, revision;
    CutRecipeField power, feed, pierce_delay, pierce_height, cut_height, kerf;
    std::map<std::string, CutRecipeField> extra;   // gas bar, abrasive flow, current, ...
};
```

Lookup is **exact-row**. A miss returns a refusal naming the missing key — never an
interpolated or nearest-neighbour value. A recipe is process data, never an authorization
(`00_decisions.md`, abstraction #5 of the manufacturing-breadth research).

### 4.3 Plasma process state

```cpp
enum class PlasmaState {
    Idle, Positioning, ProbeStart, ProbeTouch, ProbeRetractToPierce,
    TorchOn, AwaitArcOk, PierceDelay, TransferToCutHeight,
    Cutting, ThcLockedCorner, ThcLockedVoid, PuddleJump,
    TorchOff, Retract, Fault,
};
enum class PlasmaFault {
    None, ArcOkTimeout, ArcLost, ProbeNoContact, ProbeOverTravel,
    BreakawayTripped, ThcOutOfRange, ConsumableExpired, SignalUnknownOrStale,
};
struct PlasmaSignalSet {                       // consumed ONLY as FreshValue<T>
    FreshValue<bool>   arc_ok, ohmic_contact, float_switch, breakaway, torch_on_feedback;
    FreshValue<double> arc_voltage_v, thc_correction_mm;
};
struct PlasmaTransition { PlasmaState from, to; double t_s; PlasmaFault fault; std::string why; };
struct PlasmaReplayReport {
    std::vector<PlasmaTransition> transitions;
    PlasmaFault terminal_fault; bool completed;
    int pierces, arc_losses, thc_corrections, unexplained_transitions;
    std::vector<PlasmaState> states_covered; std::vector<PlasmaFault> faults_covered;
    CutFidelityManifest fidelity;
    bool protective_function = false;          // asserted false, always, by contract test
};
```

### 4.4 Reports and capability

```cpp
// Gateway owns both shared types (`MFG-CAPTRUTH-ONE-DEFINITION`; ratified in
// `10_gateway.md`).
// This workstream COMPOSES them and never re-declares the level list or the row shape.
//   core/device/capability.h    CapabilityTruth{level, truth, evidence_ref, emulator_stage,
//                                              missing_level?, why?}, CapabilityReport
//   core/manufacturing/tier0.h  Tier-0 register row + tier0::registerRows(domain_id, rows)
struct CutCapability {
    CapabilityReport report;                   // domain_id="cam.cut"; levels{} keyed by the
                                               // nine gateway level names — not redefined here
    std::optional<std::string> device_command_owner;  // ALWAYS null: no device path exists
    std::string command_owner_why =
        "no transport, no device session, and no execute capability in this domain";
    std::vector<std::string> qualified_dialects;
    std::vector<std::string> missing_for_execute;
};

struct CutFidelityManifest {                   // rendered beside EVERY result
    bool parser, controller_state_machine, kinematics, collision;
    bool dynamics = false, process_physics = false, sensor_faults;
    std::string kinematics_model, collision_model, timing_model, notes;
};

struct CutPlanReport {
    int pierces, closed_loops, open_paths, tabs;
    double cut_length_mm, traverse_length_mm, est_time_s;
    std::vector<std::string> refusals, warnings;
    CutFidelityManifest fidelity;
};
struct CutParseReport {
    bool ok; std::vector<ParseViolation> violations;   // typed, with line numbers
    int motion_lines, power_on_segments; double max_power_seen, max_feed_seen;
    std::vector<std::string> operator_prerequisites;   // e.g. "$32=1"
};
struct CutSimResult {
    int severed_regions, dropped_parts_untabbed, torch_over_opening_events, pierces;
    double cut_length_mm, traverse_length_mm, est_time_s;
    std::vector<std::string> warnings; CutFidelityManifest fidelity;
};
struct CutEmulatorReport {
    int stage; bool deterministic; uint64_t ticks;
    std::vector<CutFrame> frames;              // pos, power state+value+unit, feed, state
    std::optional<PlasmaReplayReport> plasma;
    CutFidelityManifest fidelity; std::string artifact_hash, profile_epoch;
};
```

`FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}` is used **as-is**
(DoD §4) for every externally sourced signal. It is not re-declared here. The emulator is
tick-domain internally; the publisher stamps the external envelope, and the Gateway owns
durable `seq` (DoD §10).

---

## 5. RPCs, events, MCP tools

### 5.1 RPCs (`mfg_*`, never in the `libchudware` C ABI — DoD §16)

| RPC | Direction | Contract |
|---|---|---|
| `mfg_cut_profile_list` | read | processes, heads, dialects, capability truth table, fidelity manifests |
| `mfg_cut_profile_describe` | read | one `CutProcessProfile` + its `CutCapability` + `missing_for_execute` |
| `mfg_cut_head_set` | write (document) | attach a typed `CutHead` to a setup; replaces `cam_add_tool` for heads |
| `mfg_cut_recipe_resolve` | read | exact-row `CutRecipe` with per-field provenance, or a refusal naming the key |
| `mfg_cut_plan` | compute | vector plan + `CutPlanReport`; Tier-0 refusals rendered here |
| `mfg_cut_post` | compute | NC + the `PowerBinding` actually used; **fails** on missing binding / mill dialect / unknown dialect |
| `mfg_cut_parse_verify` | compute | `CutParseReport` over our own or a supplied program (read-only) |
| `mfg_cut_emulate_run` | compute | E0/E1 (and E2 when the optional binary exists) → `CutEmulatorReport` |
| `mfg_cut_emulate_replay` | compute | re-run a stored report deterministically for the same ticks |
| `mfg_plasma_state_replay` | compute | recorded signal ledger + plan → `PlasmaReplayReport` |
| `mfg_cut_capability_report` | read | the two-stage gate evidence bundle for one artifact hash |

No `transport`, `connect`, `stream`, `run`, `fire`, `pump`, `gas`, `jog`, `home`, or
`reset` RPC exists in this surface, by design and by test (§8, T19). `mfg_tier0_*`,
`mfg_event_*`, and `mfg_result_*` are **gateway-owned and merely called** from here, never
claimed or reimplemented; offline durable `seq` comes from `chudware-mfgd --journal-only`,
which opens no transport, registers no device, and adds no gate SKIP.

### 5.2 Events (`mfg.*`, gateway-stamped durable `seq`)

`mfg.cut.plan_ready`, `mfg.cut.post_ready`, `mfg.cut.parse_result`,
`mfg.cut.recipe_resolved`, `mfg.cut.emulate_frame`, `mfg.cut.emulate_done`,
`mfg.cut.capability_changed`, `mfg.cut.invalidated`, `mfg.plasma.process_state`,
`mfg.plasma.arc_event`.

Publication and invalidation go through the gateway journal (`MFG-SEQ-SOLE-SOURCE`), never through a
locally minted `seq`:

- Every durable row a UI may paint is appended with **`mfg_event_append{domain_id:"cam.cut",
  kind, subject, result_kind, body, raw?, admission?}`**; the journal assigns `seq` and
  `epoch` and returns `ref`. This domain supplies content only. Minting a `seq` is a
  contract-test failure (§8, T27).
- Invalidation is **`mfg_result_invalidate{domain_id, class, reason, why, affected[],
  superseded_by?}` → `{at_seq}`**. Per `MFG-CLOSURE-GATEWAY-OWNED` the gateway computes the **transitive
  closure over `derived_from[]`**, so `affected[]` is its closure and not this domain's best
  guess: we name the root subject only. `falsified` propagates along `derived_from`;
  `superseded` does not, except for rows declaring `supersede_with_parent`. The DoD §9
  envelope applies only to rows with `seq <= at_seq`, producer-pushed, never poll-driven.
- `admission` and `src` are **computed by the gateway and never accepted from this domain**
  (`MFG-PROV-GATEWAY-COMPUTED`). `probed` is settable only by daemon transport code holding an open
  `ITransport` — and per `MFG-READ-TRANSPORT-SPLIT` the read-only collector interface `IReadTransport`
  has no `write()` at all. This domain implements **neither**: it opens no transport of
  either kind, so it can produce no probed evidence and never takes or contends for a
  command lease.

Durable `result_kind` ids this domain appends, each of which must already exist in the
contract's durable-result-kind table or the append is refused `mfg_unregistered_result_kind`
(`MFG-KIND-MUST-BE-REGISTERED`). Per the **mandatory two-column rule** (`MFG-KIND-TWO-COLUMNS`) every kind
declares *both* an epoch policy (`falsify | reestablish | epoch_invariant`) *and* its runtime
invalidation triggers — they are different axes, and declaring only one is a schema
rejection. Cutting owns **no probed transport**, so every kind below is `epoch_invariant`:
its durable state survives a gateway restart intact and is only ever invalidated mid-epoch by
a content change. Registering these is an append-only, maintainer-owned contract edit
requested as part of Wave 1. Permitted subject namespaces for `domain_id="cam.cut"` are
declared at registration as **`art:`, `job:`, `res:`** — never `dev:` and never `src:`
(`MFG-SUBJECT-NS-DECLARED`); an out-of-set append is refused `mfg_subject_namespace_not_permitted`, which
mechanizes the subject-binding rule that a cut row can never be about a device or a monitor
source.

| `result_kind` | Payload | Epoch policy | Runtime invalidation triggers | `derived_from[]` | `supersede_with_parent` + declared reason (`MFG-SUPERSESSION-DECLARED`) |
|---|---|---|---|---|---|
| `cam.cut.recipe` | resolved `CutRecipe` + provenance | `epoch_invariant` | recipe row value change → `falsified`; revision bump with byte-identical values → `superseded` | — (root) | n/a — root |
| `cam.cut.plan` | `CutPlanReport` | `epoch_invariant` | head record / `kerf_mm` / `kerf_source` / machine-profile / setpoint / geometry edit → `falsified`; latest-per-(setup, op) wins | `cam.cut.recipe` | **yes** — a plan built from a superseded recipe revision is no longer the current derivation |
| `cam.cut.post` | NC hash + `PowerBinding` used | `epoch_invariant` | dialect `PowerBinding` change → `falsified` | `cam.cut.plan` | **yes** — the NC is a pure derivation of the plan, so it cannot be current when its plan is not |
| `cam.cut.parse` | `CutParseReport` | `epoch_invariant` | bound to the posted artifact hash; parser rule-set revision → `falsified` | `cam.cut.post` | **yes** — a parse report describes one NC artifact and follows it |
| `cam.cut.sim` | `CutSimResult` | `epoch_invariant` | `cut_sim` model revision → `falsified` | `cam.cut.plan` | **yes** — the severance topology is a derivation of the plan geometry |
| `cam.cut.emulate` | `CutEmulatorReport` | `epoch_invariant` | bound to `artifact_hash` + `profile_epoch`; higher stage for the same hash → `superseded` | `cam.cut.post` | **yes** — a replay of a superseded program is no longer the current replay |
| `cam.plasma.replay` | `PlasmaReplayReport` | `epoch_invariant` | ledger hash change → `falsified`; re-run of the same ledger → `superseded` | `cam.cut.plan` | **yes** — the replay is scored against the planned state sequence |
| `cam.cut.capability` | `CapabilityReport` | `epoch_invariant` | latest-per-domain; **any cited `evidence_ref` row falsified or superseded → re-derive and append a fresh capability row**, and the closure makes the prior one non-current, so a level cannot outlive the evidence under it (`MFG-CAPABILITY-DURABLE-KIND`) | `cam.cut.procedural_fact`, `cam.cut.emulate` | **no** — mandatory FALSE for `*capability` kinds (`MFG-CAPABILITY-NO-INHERIT`): a capability row re-derives from its own evidence and never inherits |
| `cam.cut.procedural_fact` | `ProceduralFact` | `epoch_invariant` | falsification of the cited artifact propagates (falsify always propagates), which correctly un-verifies the level; no independent trigger | the cited artifact row — `cam.cut.plan` \| `post` \| `parse` \| `sim` \| `emulate`, whichever `check_name` ran against | **yes** — mandatory for `*procedural_fact` kinds (`MFG-PFACT-FOLLOWS-PARENT`): a fact left standing over a superseded parent would keep a level `verified` after the check it cites was re-run |

Two rulings changed this table from the first submission and both are now general rules:
`cam.cut.procedural_fact` was a parentless root with `supersede_with_parent` unset and
described as "never falsified". Per `MFG-PFACT-FOLLOWS-PARENT` it now **declares the artifact it cites as its
parent and sets `supersede_with_parent:true`** — registration refuses otherwise
(`mfg_procedural_fact_must_follow_parent`) — and since falsification always propagates, the
fact is falsified with its cited artifact, which is the behaviour that stops a fabricated
`verified`. Conversely `cam.cut.capability` must **not** set it; the doc reached that
independently and `MFG-CAPABILITY-NO-INHERIT` makes that FALSE mandatory. Per `MFG-SUPERSESSION-DECLARED`, supersession of every derived artifact
row above is this domain's declared choice with its one-line reason, not a claim that other
domains must choose the same.

Per `MFG-CAPABILITY-DURABLE-KIND` this kind is **required, not optional**: every panel paints the `CapabilityReport`,
and DoD §10 requires a gateway-stamped durable row for every durable result a UI may paint,
not only telemetry. Registration enforces it — a domain advertising any level above
`unsupported` without a registered `<domain>.capability` kind is refused
`mfg_capability_requires_durable_kind`. Cutting advertises five levels above `unsupported`, so
the requirement binds; the id follows this domain's own dotted convention,
`cam.cut.capability`.

**W1.1 registration of these kinds — B10 closure for `cam.cut`.** All nine rows above are in the
**accepted W1.1 durable-result-kind set**:
`cam.cut.{recipe,plan,post,parse,sim,emulate,procedural_fact,capability}` plus `cam.plasma.replay`,
with `cam.cut.capability` as this domain's `MFG-CAPABILITY-DURABLE-KIND` capability kind and
`{art:, job:, res:}` as its `MFG-SUBJECT-NS-DECLARED` namespace set. B10 found four domains missing from
that table; `cam.cut` is not one of them, and this paragraph exists so that completeness cannot regress
silently. The consequences of a gap are total, not cosmetic: per `MFG-KIND-MUST-BE-REGISTERED` an append
of any unlisted kind is refused `mfg_unregistered_result_kind` and this domain's durable surface does
nothing, and per `MFG-CAPABILITY-DURABLE-KIND` advertising any level above `unsupported` without the
registered capability kind is refused `mfg_capability_requires_durable_kind` outright. Registration is the
append-only, maintainer-owned contract edit in **wave 1** (§12 step 1) and is a hard predecessor of every
later cut slice. Each row carries both `MFG-KIND-TWO-COLUMNS` columns, its `derived_from[]`, and its
`supersede_with_parent` declaration with the one-line reason `MFG-SUPERSESSION-DECLARED` requires —
`true` for `cam.cut.procedural_fact` (`MFG-PFACT-FOLLOWS-PARENT`), **false** for `cam.cut.capability`
(`MFG-CAPABILITY-NO-INHERIT`).

Root-subject → class mapping (the gateway expands each to its closure):

| Change | Class | Rationale |
|---|---|---|
| head record, `kerf_mm`, `kerf_source`, machine profile, or process setpoint edited | `falsified` | every downstream plan/post/parse/emulate result is now wrong; clear it |
| recipe row values changed | `falsified` | the program's power/feed/pierce no longer match the authority |
| recipe/profile revision bumped with byte-identical values | `superseded` | retain, point forward, no alarm |
| a new dialect gains a `PowerBinding` | `superseded` | old refusals remain historically true |
| an emulator stage passes for the first time | `mfg.cut.capability_changed` | capability ceiling may rise, never above E3 here; a `procedural_fact` may flip only `plan\|generate\|post\|parse\|simulate` (`MFG-PFACT-FLIP-SCOPE`) |

### 5.3 MCP tools

`services/manufacturing/cut/mcp_tools.py`. Tool family is the single shared
**`manufacture`** family for the entire overhaul (the final project ruling — the user-facing and
config family is the existing product term *Manufacture*, not the `mfg` wire prefix). The
family is **OFF by default in `agent.tool_families` until commissioning**, so `enable_tools`
cannot silently self-add manufacturing surface; per-tool Settings toggles retain granularity.
`mfg_*` / `mfg.*` remain the RPC/event wire prefixes. All ten tools are read-only/offline
risk class; none is `RISK_MOTION`, and this workstream contributes no motion-class or
safety-class tool at all — absence, not denial.

Registration idiom, matching the gateway: `view(mcp, "manufacture")` in
`services/manufacturing/cut/mcp_tools.py`, plus the one registration call added to
`services/mcp/cam_tools.py`.

`list_cut_processes`, `describe_cut_process`, `set_cut_head`, `resolve_cut_recipe`,
`plan_cut_job`, `post_cut_program`, `verify_cut_program`, `emulate_cut_program`,
`replay_plasma_signals`, `cut_capability_report`.

Guards, written in the same measured-defect style the existing file already uses
(`cam_tools.py:152-168`, `:263-284`, `:612-627`, `:784-797`) — each one exists because the
current code has the exact failure it prevents:

1. Unknown process id → **reject**, name the vocabulary. Never default to laser.
2. `power` on a plasma or waterjet op → **reject**, name `current_a` / `pressure_mpa` and
   their units. (Today `model.h:320` literally calls one field "laser power / amperage
   scale".)
3. `rpm`, `spindle_rpm`, `spindle_dir`, `css`, or a bare `S` value on any cut op →
   **reject**, name the process's channel and unit. (Today `post.cpp:460` renders power
   through the RPM formatter.)
4. Cut op on a non-head machine, or a process profile disagreeing with the machine kind →
   **reject** (Tier-0, at the action entry point).
5. Head setup + mill dialect, or unknown dialect → **reject** with the qualified dialect
   list. (Today: `post.cpp:158-162` degrades to GRBL and the `head` branch emits empty
   torch words.)
6. `resolve_cut_recipe` miss → **reject** naming the missing key; never interpolate.
7. Any `inferred` provenance in the power/feed/pierce set → surfaced in the response and
   fails Stage-2 check S2-5 unless a durable named waiver exists.

Amendments to `services/mcp/cam_tools.py` (three surgical edits, no behavior overlap with
turning):

- `cam_add_tool` (`:458-478`): `laser_head|plasma_torch|waterjet_head` become **rejected**
  values pointing at `set_cut_head` — a torch has no flutes, no corner radius, and no
  32 mm holder, and letting one in produces a mill-shaped record the sim then uses as a
  collision body (`stock_sim.cpp:331-334`).
- `cam_add_operation` (`:593-650`): `type="cut2d"` **rejected**, pointing at
  `cut_laser|cut_plasma|cut_waterjet`; the free-form `op.update(params)` merge (`:636`)
  rejects the cut-scalar keys listed above.
- `cam_simulate` (`:744-759`): docstring states it is a milling/turning removal model, and
  a head setup returns the engine's new refusal pointing at `emulate_cut_program`.

---

## 6. Per-process capability, planning, post, and fidelity

### 6.1 Capability truth table (DoD §2; `assumed` disables actions exactly like absent)

| Level | Laser | Plasma | Waterjet |
|---|---|---|---|
| `plan` | verified | verified | verified |
| `generate` | verified | verified | verified |
| `post` | verified — `grbl_laser`, `grbl_laser_constant`, `marlin_laser`, `linuxcnc_laser`, `smoothie_laser` only | verified — `linuxcnc_plasma` only | verified — `linuxcnc_waterjet` only |
| `parse` | verified | verified | verified |
| `simulate` | verified (kerf topology + tick replay) | verified (kerf topology + recorded-signal state replay) | verified (**kerf topology only; `process_physics: false`**) |
| `monitor` | unsupported — `why`: "no live source; offline replay is not monitoring" | unsupported — same `why` | unsupported — same `why` |
| `transport` | unsupported — `why`: "no device session; E4-E5 never reached" | same | same |
| `execute` | unsupported — `why`: "E6 pilot out of scope; no approval binding" | same | same |
| `safety_integration` | unsupported — `why`: "hardware/integrator-owned; no stage unlocks it" | same | same |
| `device_command_owner` | null + `why` | null + `why` | null + `why` |
| `emulator_stage_passed` | E1 in gates; E2 only after `scripts/qualify_cut_e2.sh` journals its evidence row | E3 (recorded ledgers) | E1 |

Truth vocabulary is the gateway's — `verified | declared | assumed | unsupported |
not_applicable`. The **first five levels only** (`plan`, `generate`, `post`, `parse`,
`simulate`) read `declared` until their `evidence_ref` names a journal row the gateway
stamped, then flip to `verified`; the table states the post-evidence target. That evidence is
a **`ProceduralFact{check_name, artifact_hash, config_epoch, snapshot_id?, at_seq, outcome}`**
appended with `admission:"procedural_fact"` — exactly what DoD §14 permits: a gate on the
procedural fact that a *named* check ran against a *specific* artifact, config, and snapshot.

Per `MFG-PFACT-FLIP-SCOPE` a `procedural_fact` row may flip **only those five**.
`monitor`/`transport`/`execute`/`safety_integration` require fresh **probed** evidence plus
the emulator-stage ceiling, and can never be flipped by `procedural_fact`, `simulated`,
`declared`, `advisory`, or `assumed`. Since this domain opens no transport of either kind,
those four are structurally unreachable here and are reported `unsupported` **with a `why`** —
never `not_applicable`. `assumed` disables an action exactly like absent and must name its
`missing_level` (DoD §2).

Ceiling (`MFG-STAGE-CEILING`, contract-tested): `max_advertised` may never exceed the ceiling of
`emulator_stage_passed` — E0-E3 unlocks at most `monitor`, E4-E5 `transport`, E6 `execute`,
and **no stage ever unlocks `safety_integration`**. At E3 the ceiling would permit
`monitor`; this workstream deliberately stops one rung lower at `simulate` because no live
source exists to monitor. `missing_for_execute` enumerates the gap: pilot hardware, a named
controller+firmware+option set, integrator safety sign-off, and an arbiter approval binding.

`src` and `admission` are never self-asserted: the gateway derives them from typed transport
provenance (`MFG-PROV-GATEWAY-COMPUTED`). Our emulator and plasma-replay evidence is
therefore `src=simulated`, profile/recipe values are `src=declared`, and no path in this
domain can reach `probed` — neither can pass Stage-1 admission for an execute-side subject,
by construction rather than by convention. The E2 `grbl-sim` path is **also capped at
`simulated`**: a subprocess reading a config file is not a probed transport, so opt-in
qualification buys auditability, never admissibility.

### 6.2 Laser — vector planning / post / parser / emulator-first

**Plan** (`gen_cut_laser` + `vector_plan`):
- multi-loop kerf comp: outer offset outward, every island offset inward, all loops
  retained (fixes `op_cut.cpp:89-96` island loss);
- lead-in/out for closed **and** open paths (fixes `op_cut.cpp:152-166`), pierce placed at
  the lead entry so the pierce scar stays off the finished edge (the intent already
  documented at `op_cut.cpp:113-115`, now applied to open paths too);
- tabs/micro-joints via `tab_count`/`tab_width` so small severed parts do not drop;
- ordering: islands before outers, then bounded 2-opt traverse minimization;
- move stream: `ProcessOn{LaserPowerFraction}` → `Cut/Arc` (+ `ProcessSet` at classified
  corners when `corner_power_scale != 1`) → `ProcessOff` before every traverse. `Plunge`
  is used only for genuine standoff/focus changes and never doubles as the beam gate
  (today the post infers the gate from `Plunge`, `post.cpp:457-465`).

**Post** (`post_cut.cpp`): four laser dialects, each declaring a `PowerBinding`.

| Dialect | Power word | Mode | Operator prerequisite |
|---|---|---|---|
| `grbl_laser` | `S 0..power_max` with `M4` | speed-scaled dynamic power | `$32=1` (laser mode) — surfaced as a Tier-0 operator prerequisite, and re-asserted by the parser |
| `grbl_laser_constant` | `S` with `M3` | constant power | `$32=1` |
| `marlin_laser` | `S 0..255` with `M3/M5` | constant | (re-scoped from the existing `marlin` preset, `post.cpp:120-131`) |
| `linuxcnc_laser` | analog out `M67 E<n> Q<v>` / `M68` | synchronized analog | named HAL analog pin mapping |
| `smoothie_laser` | `S` fraction | constant | — |

A missing binding for the op's channel is a **post failure**, not an empty `blk("")`.

**Parser** (`parse_gcode.cpp`, truth level `parse`): independently re-reads the emitted
text and reconstructs motion mode, units, absolute/incremental, feed, power state, power
value, and laser-mode requirement, then asserts:
- no power-on state across a traverse move (the classic burn-through / fire risk);
- every `ProcessOn` has a matching `ProcessOff` before program end;
- power never exceeds the recipe's max, feed never exceeds `Machine::max_feed`;
- no `S` word is emitted while a spindle `M3` is active in a non-laser dialect;
- the required controller settings (`$32=1`) are listed as operator prerequisites.
It also accepts an externally supplied program, read-only.

**Emulator-first** — the E-ladder (DoD §18):

| Stage | What runs | Gate placement |
|---|---|---|
| **E0** | our own parser + commanded-state machine, in-process, no vendor code | normal gates |
| **E1** | deterministic fixed-integer-tick replay → frames (pos, power state/value/unit, feed, elapsed), snapshot/restore | normal gates |
| **E2** | `grbl-sim` (GPLv3) as an **optional out-of-process binary** over a pty, discovered via `CHUDWARE_GRBL_SIM_BIN` (same idiom as `CHUDWARE_SLICER_BIN` / `CHUDWARE_NGSPICE` / `OPENMVS_BIN`) | **outside `gate.sh`** — opt-in `scripts/qualify_cut_e2.sh`, so DoD §1's SKIP list gains no new class |
| **E3** | recorded-signal replay (plasma; see §6.3) | normal gates |
| **E6** | pilot hardware | **out of scope — never claimed** |

### 6.3 Plasma — dedicated arc / probe / THC / breakaway state + offline replay

The plasma move stream is *not* laser's with a different power number. It carries the
process state explicitly:

```
Positioning → ProbeTouch (ohmic or float; MoveKind::ProbeTouch, never Plunge)
            → ProbeRetractToPierce (pierce_height)
            → TorchOn (ProcessOn{PlasmaCurrentAmps})
            → AwaitArcOk (MoveKind::ProcessWait, arc_ok, arc_ok_timeout_s)
            → PierceDelay → TransferToCutHeight (transfer_height → cut_height)
            → Cutting  [THC ArcVoltage | ManualHeight | Off]
                 ├─ ThcLockedCorner  (corner_lock, VAD threshold)
                 ├─ ThcLockedVoid    (void_lock over a prior opening)
                 └─ PuddleJump       (crossing a severed kerf)
            → TorchOff (ProcessOff) → Retract → Idle
Fault ← { ArcOkTimeout, ArcLost, ProbeNoContact, ProbeOverTravel,
          BreakawayTripped, ThcOutOfRange, ConsumableExpired, SignalUnknownOrStale }
```

Rules that make this honest:

- Every consumed signal is a `FreshValue<T>`. `state != ok`, a sequence gap, or an
  exceeded `ttl_ms` transitions to `Fault{SignalUnknownOrStale}` — **fails closed**
  (DoD hard rule: missing/stale/unknown telemetry fails closed). It never assumes arc-ok.
- `ThcMode` and `PlasmaProbeMode` have **no silent default**. Tier-0 refuses a plan whose
  THC mode or probe mode is unset. (Contrast today's single `pierce_height` scalar,
  `model.h:317`.)
- Breakaway and arc-ok are **hardware interlocks**. This state machine *models* them to
  author programs and to explain recorded runs. `PlasmaReplayReport::protective_function`
  is `false` by construction and asserted by a contract test. A state-machine "stop" is
  not a protective function (ISO 13849 / ISO 17916 exposure noted in
  the manufacturing-breadth research).
- **Offline replay** (`mfg_plasma_state_replay`): a recorded ledger + the planned program
  produce per-transition timing, faults, THC corrections, unexplained transitions, and
  **coverage** (`states_covered`, `faults_covered`). Coverage is the DoD §8 guard-liveness
  proof: every fault branch must be reachable by at least one fixture, and every guard
  must be demonstrably able to pass *and* to fail.
- Post: `linuxcnc_plasma` only — current via analog out, arc-ok/ohmic/float/breakaway as
  named digital inputs with `M66` waits, torch as a named digital output. Emitting a
  plasma program on `grbl`/`fanuc`/`haas` is a **failure**, not an `M3`.

### 6.4 Waterjet — bounded, no false physics or execution claims

Delivered: vector plan, kerf comp, lead/pierce (`Stationary|Dynamic|Circle|Wiggle`),
corner slowdown as a **recipe feed scalar**, exact-row recipes with provenance,
`linuxcnc_waterjet` post (pump and abrasive as named digital outputs `M62-M65`, pressure
as analog `M67`), parser, and kerf-topology sim.

Explicitly **not** delivered, and stated in every response's fidelity manifest:

- no jet-lag or taper compensation math — `taper_deg_declared` is an **echoed** vendor/user
  number with `taper_provenance`, never computed;
- no pressure, pump, orifice-wear, mixing-tube-wear, abrasive-flow, or garnet dynamics;
- no cut-quality prediction beyond the recipe row that was actually looked up;
- `grbl_waterjet` is **refused**: `M8` is a coolant word, not a pump command owner, and
  binding a pump to it would invent a device command owner this workstream does not have.
- Out-of-table request → refusal, never extrapolation. The manufacturing-breadth research
  found no mature open waterjet controller/emulator; the design therefore stops at E1 and
  claims nothing beyond it.

---

## 7. Simulation fidelity

The mill Z-dexel is structurally wrong for a kerf-through cut (`stock_sim.cpp:1-3`,
`:325-326`, `:352-354`), and today it silently reports success with ~0 mm³ removed. The
replacement is a **2D severance topology** model, with its limits declared in-band.

`cut_sim.cpp` computes, per setup:
- the kerf swath as a Clipper2 boolean against the sheet outline;
- `severed_regions`; `dropped_parts_untabbed` (a fully severed region with `tab_count == 0`);
- `torch_over_opening_events` (a later traverse or cut crossing an already-severed opening
  — the real plasma/waterjet head-crash and slat-hit mode);
- `pierces`, `cut_length_mm`, `traverse_length_mm`;
- `est_time_s` from feed + pierce delays + a constant-accel trapezoid (reusing the existing
  `move_time_s` accel convention, `stock_sim.cpp:32-68`).

`CutFidelityManifest` values actually published:

| Field | Laser | Plasma | Waterjet |
|---|---|---|---|
| `parser` | true | true | true |
| `controller_state_machine` | false | **true (recorded-signal replay)** | false |
| `kinematics` | true — `"XY constant-accel trapezoid; no controller lookahead or junction deviation"` | same | same |
| `collision` | `"2D severance topology only: head vs already-cut opening"` | same | same |
| `dynamics` | false | false | false |
| `process_physics` | **false** | **false** | **false** |
| `sensor_faults` | false | **replay-only** | false |
| `timing_model` | `"feed + pierce delay + accel; no S-curve, no controller planner"` | same | same |

Not modeled, stated as such: beam/arc/jet physics, HAZ, dross, taper, melt, kerf-width
variation with speed, thermal distortion, fume/exhaust, pressure transients, abrasive
behavior, slat interaction, sheet lift, controller lookahead, and every safety function.
Per DoD §14, simulated conclusions are **context only** and never satisfy an execute-side
gate.

---

## 8. Tests

Every test below defends an observable contract and fails on a plausible bug — most of
them fail on the bug that is in the tree *today*. C++ tests go in the existing `core/cam`
unit-test target alongside the other CAM tests (`[INFERENCE]` — the test directory was
outside read scope); Python tests alongside the existing MCP tool tests.

**Power-channel integrity**
1. `power_channel_never_spindle` — for all three processes, every generated `Move` has
   `spindle_rpm == 0` and every `ProcessSetpoint` carries the right channel + unit.
   *Fails on:* any regression to S-word power.
2. `post_missing_power_binding_fails` — laser op + `fanuc` → `ok:false` naming the missing
   binding. *Fails today:* the `head` branch emits torch defaults or nothing
   (`post.cpp:454-482`).
3. `post_unknown_dialect_fails_closed` — `post_dialect_or_error("nope")` errors.
   *Fails today:* silent GRBL (`post.cpp:158-162`).
4. `plasma_rejects_legacy_power_fraction` — legacy `power:0.5` on a Plasma setup → refusal
   naming `current_a` in amps.
5. `laser_legacy_power_migrates_with_provenance` — same value on a Laser setup migrates
   with `Provenance::LegacyMigrated` and a `superseded` notice.

**Planning geometry**
6. `kerf_offset_preserves_islands` — a region with 3 holes yields 4 kerf-comped loops.
   *Fails today:* `offs.front().outer` drops the islands (`op_cut.cpp:89-96`).
7. `open_path_gets_kerf_and_leads` — an open cut receives side comp and lead-out.
   *Fails today:* `op_cut.cpp:152-166`.
8. `tabs_prevent_dropped_part` — `tab_count > 0` clears `dropped_parts_untabbed`.
9. `cut_ordering_islands_before_outer`.

**Post + parser**
10. `grbl_laser_declares_dollar32_prerequisite` — the prerequisite appears in
    `CutParseReport::operator_prerequisites` and is rendered at the action entry point.
11. `parse_flags_power_on_traverse` — feed the mutated fixture
    `laser_power_on_traverse.nc`; the parser flags it. This is the guard-liveness proof
    (DoD §8): the guard demonstrably fails for some input.
12. `parse_roundtrip_clean_for_own_post` — the guard demonstrably passes for our output.
13. `plasma_post_refuses_mill_dialect`.
14. `waterjet_post_refuses_grbl_pump_binding`.

**Plasma state machine**
15. `plasma_state_transitions_table` — fixture-driven; asserts every `PlasmaState` and
    every `PlasmaFault` is reached by at least one fixture (coverage assertion).
16. `plasma_stale_signal_fails_closed` — `stale_arc_ok.jsonl` (`state != ok`) ends in
    `Fault{SignalUnknownOrStale}`, never `Cutting`.
17. `plasma_probe_no_contact_fails_closed`.
18. `plasma_thc_locks_in_corner_and_void`.
19. `plasma_breakaway_never_claims_protective_function` — asserts
    `protective_function == false`.

**Simulation**
20. `dexel_sim_refuses_cut_head` — `simulate_setup` on a Laser setup returns `ok:false`
    with the pointer. *Fails today:* `ok:true`, `machined_fraction ≈ 0`, no warnings.
21. `cut_sim_detects_dropped_part` and `cut_sim_detects_torch_over_opening`.
22. `emulator_replay_is_deterministic` — identical ticks → byte-identical report.
23. `e2_is_absent_from_the_gate` — assert `gate.sh` never invokes `scripts/qualify_cut_e2.sh`
    and that E0/E1 need no external binary, so the DoD §1 SKIP list is unchanged. E2's own
    qualification test lives in the opt-in script and journals its `evidence_ref` there.

**Contract / policy boundaries (DoD §19)**
24. `capability_never_exceeds_emulator_stage`.
25. `no_transport_or_execute_rpc_exists` — enumerate the `mfg_cut_*` surface and assert no
    connect/stream/run/fire/pump/gas/jog/home/reset method.
26. `no_mfg_cut_symbols_in_c_abi` — extends the existing ABI-symbol expectation (DoD §16).
27. `no_locally_minted_seq_or_admission` — every durable row this domain publishes arrives
    via `mfg_event_append`, and no code path writes a `seq`, `src`, or `admission` field.
    Also asserts `invalidation_seq_scope`: a kerf edit falsifies only rows with
    `seq <= at_seq` and a revision bump with identical values supersedes without alarm.
28. `tier0_totality_zero_uncovered` — `mfg_tier0_register` reports no uncovered
    `(action, artifact_kind, machine_class)` tuple for the three head classes, every row is
    `class:"PREREQ"`, registration is idempotent on re-run, and each prerequisite renders at
    its action entry point with the guard behind it as defense-in-depth (DoD §6).
29. `unregistered_result_kind_is_refused` — appending an unlisted `result_kind` returns
    `mfg_unregistered_result_kind` rather than creating an unreconciled durable row.
30. `every_result_kind_declares_both_columns` — all nine kinds declare an epoch policy **and**
    runtime triggers (`MFG-KIND-TWO-COLUMNS`); dropping either column is a schema rejection, and all
    nine are `epoch_invariant` because this domain owns no probed transport, so durable cut
    state survives a gateway restart intact.
31. `falsify_closure_follows_derived_from` — falsifying a `cam.cut.recipe` root reaches plan →
    post → parse/sim/emulate/replay through the gateway's transitive closure while this domain
    names only the root subject; a no-op revision bump supersedes the same chain via
    `supersede_with_parent` **without** touching `cam.cut.capability`, which is excluded so a
    capability row is always re-derived from its own evidence rather than inherited.
32. `procedural_fact_flips_only_the_first_five` — a `ProceduralFact` row bound to the same
    `artifact_hash` + `config_epoch` can flip `plan|generate|post|parse|simulate` to
    `verified` and **cannot** flip `monitor|transport|execute|safety_integration`
    (`MFG-PFACT-FLIP-SCOPE`); `simulated`, `declared`, `advisory`, and `assumed` rows flip nothing, and no path
    in this domain implements `ITransport` or `IReadTransport`, so `probed` is unreachable and
    no command lease is ever taken or contended.
33. `fidelity_manifest_accompanies_every_result` — plan/post/parse/sim/emulate responses
    all carry it, `process_physics == false` in all three processes.
34. `procedural_fact_follows_its_cited_artifact` (`MFG-PFACT-FOLLOWS-PARENT`) — registering
    `cam.cut.procedural_fact` without `supersede_with_parent:true` is refused
    `mfg_procedural_fact_must_follow_parent`; superseding the cited artifact supersedes the
    fact, and falsifying the artifact falsifies the fact, so a level cannot stay `verified`
    after the check it cites was re-run. Mirror assertion: `cam.cut.capability` must **not**
    set the flag and re-derives from its own evidence.
35. `subject_namespace_is_enforced` (`MFG-SUBJECT-NS-DECLARED`) — `cam.cut` registers only `art:`, `job:`,
    `res:`; appending a `dev:` or `src:` subject is refused
    `mfg_subject_namespace_not_permitted`.
36. `guards_refuse_undeclared_evidence_by_name` (`MFG-GUARD-ACCEPTS-DECLARED`) — feed every S2 guard every
    namespace and admission class it did not declare, **including a fresh probed `src:`
    monitor observation**, and require a named refusal; no cut guard is approval-bound, so
    none can be satisfied by a device snapshot or a monitor row at any freshness.
37. `every_supersession_choice_is_declared` (`MFG-SUPERSESSION-DECLARED`) — each derived-artifact kind
    carries a one-line reason for its `supersede_with_parent` value; the test asserts the four
    invariants (falsify always propagates, `*procedural_fact` always follows,
    `*capability` never follows, artifact identity by direct hash comparison) and that the
    choice is declared — not that it matches another domain's choice.
38. `no_level_outlives_its_evidence` (`MFG-CAPABILITY-DURABLE-KIND`, the per-domain test) — falsify one cited
    `evidence_ref` row (a `cam.cut.procedural_fact` or `cam.cut.emulate`) and require **both**
    that a fresh `cam.cut.capability` row is appended and that the affected level drops off
    `verified`. There must be no path where a level stays `verified` on falsified evidence.
    Also asserts registration refuses `mfg_capability_requires_durable_kind` when any level
    above `unsupported` is advertised without the registered kind, and that
    `supersede_with_parent` is **false** for `cam.cut.capability` — a well-meaning
    "make it consistent with the rest of the chain" refactor is exactly how this regresses.

**Smokes** (run the thing, not a test file)
- `plate_100x60_3holes` through plan → post → parse → emulate, once per process:
  asserts non-zero motion lines, non-zero pierces, 4 severed regions, and cut length
  matching the plan within tolerance.
- One plasma replay smoke over `nominal_run.jsonl` plus one over `arc_lost_midcut.jsonl`,
  asserting opposite terminal states.

---

## 9. Safety gates

### 9.1 Tier-0 construction refusals (DoD §6 — before a proposal or preflight exists, rendered at the action entry point, artifact-dependent)

Rows live in **one** declarative table, `services/manufacturing/cut/tier0.py`, registered at
domain init through the gateway-owned helper `services/manufacturing/rpc/journal_client.py`
(`register_tier0`) calling **`mfg_tier0_register{domain_id:"cam.cut", schema_version, rows[]}`**
(`MFG-TIER0-ONE-REGISTER`). `mfg_tier0_register` is a thin wrapper over the in-daemon
`tier0::registerRows` — same register, same totality check, same refusal text — so two entry
points are fine but a domain-private second register is not. Cutting's rows are
Python-authored and therefore go through the RPC only; the earlier engine-private
`tier0::registerRows` call is deleted, and the C++ entry points read the rows registered by
that one table, so the Python and C++ refusal text cannot drift.
Registration is atomic and idempotent per `(domain_id, action, prerequisite,
hash(applies_when))`; a partially invalid batch registers nothing and returns per-row
reasons; a `class` other than `PREREQ` is rejected. The row shape is the gateway's:
`{action, prerequisite, class:"PREREQ", artifact_dependent, applies_when{artifact_kind[],
machine_class[]}, why, missing_level?, remedy[]}`. `class` is **always `PREREQ`**: a whole
action prerequisite can never be `not_applicable`, missing evidence fails, and the refusal
renders at the action entry point before any proposal exists. Gateway owns the totality check
over `action × artifact_kind × machine_class` and returns uncovered tuples, which this
workstream must drive to zero for the three head machine classes.

| Row | `action` | `prerequisite` | `artifact_dependent` | `applies_when` | `remedy[]` |
|---|---|---|---|---|---|
| T0-C1 | `mfg_cut_plan` | typed `CutHead` attached and head process == machine kind | yes | kind ∈ {laser, plasma, waterjet} | `mfg_cut_head_set` |
| T0-C2 | `mfg_cut_plan` | exact `CutRecipe` row for (process, head, material, thickness, quality) | yes | all three head classes | `mfg_cut_recipe_resolve`, author the row |
| T0-C3 | `mfg_cut_post` | dialect declares a `PowerBinding` for the op's channel; dialect known; dialect not a mill dialect | yes | all three head classes | pick a qualified dialect from `mfg_cut_profile_list` |
| T0-C4 | `mfg_cut_plan` | `ThcMode` set, `PlasmaProbeMode` set, consumable record present, cut-chart row present | yes | kind == plasma | set THC/probe mode explicitly; attach consumable |
| T0-C5 | `mfg_cut_plan` | pump class, orifice, mixing tube, abrasive spec present | yes | kind == waterjet | complete the `WaterjetProcess` record |
| T0-C6 | every entry point | `transport`/`execute` advertised for this domain | no | all three head classes | none — `missing_level` = `transport`/`execute`; message names "emulator_stage_passed E3; execute requires E6" |
| T0-C7 | `cam_simulate` | setup machine is not a 2D cutting head | yes | kind ∈ {laser, plasma, waterjet} | `mfg_cut_emulate_run` |

Missing evidence **fails**; it is never `not_applicable` (DoD §6). A guard catching one of
these later is defense-in-depth, never the primary refusal.

### 9.2 Stage 1 (DoD §5)

Stage 1 admits only fresh `state==ok && src==probed` evidence plus verified execute
capability. **No cut process has any probed device evidence and none advertises `execute`,
so all three are Stage-1 ineligible by construction.** This is recorded as a deliberate
absent-guard exemption with its reason (DoD §8), not as a silent gap. Advisory, simulated,
declared, and `assumed` inputs fail closed, as does anything the plasma replay produced.

### 9.3 Stage 2 (DoD §7 — `pass|fail|not_applicable`, no unknown) — release of an NC artifact for **offline/manual transfer only**

Per `MFG-GUARD-ACCEPTS-DECLARED` every guard declares `accepts{subject_ns, admission}` and
`gates::evaluate` refuses out-of-set evidence **by name**. Cutting's guards accept only
`art:`/`job:`/`res:` subjects and only `procedural_fact` or `simulated` admission — none
accepts `dev:` or `src:`, and none is approval-bound, so no cut guard can be satisfied by a
device snapshot or a monitor observation however fresh or probed it is:

| Check | Fails when | `accepts{subject_ns, admission}` | `not_applicable` allowed? |
|---|---|---|---|
| S2-1 plan non-empty | 0 pierces or 0 cut segments | `{art:, job:}`, `{procedural_fact}` | no |
| S2-2 parse clean | any power-on-traverse, unmatched on/off, over-max power or feed | `{art:}`, `{procedural_fact}` | no |
| S2-3 emulator completed | E0/E1 faulted; for plasma, no replay fixture matched the planned state sequence | `{art:, res:}`, `{procedural_fact, simulated}` | no |
| S2-4 severance safe | an un-tabbed dropped part lies under a later traverse or cut | `{art:, res:}`, `{procedural_fact, simulated}` | no |
| S2-5 recipe provenance | any `inferred` value among power/feed/pierce | `{res:}`, `{procedural_fact}` | no — waivable only by a durable named waiver `{by,reason,at_seq}` |
| S2-6 controller prerequisite acknowledged | laser-mode setting (`$32=1`) unacknowledged | `{art:, job:}`, `{procedural_fact}` | **yes**, only for dialects with no laser-mode setting, corroborated by the dialect record |

Machine-class-irrelevant mill checks (holder/shank collision, gouge-below-part, tool
length offset) are **omitted from the required set** rather than marked
`not_applicable` — `not_applicable` is only ever a missing corroborating sub-condition
(DoD §7). Aggregate waiver rates are visible; gates waived on effectively every recent run
are reported (DoD §8).

### 9.4 Verdict discipline (DoD §3)

The word "verified" is emitted only when S2-1…S2-6 all ran against the *same*
`artifact_hash` + `profile_epoch`. Any edit to head, recipe, kerf, profile, or geometry
flips the verdict to unknown via `mfg.cut.invalidated{class:"falsified"}`. Live mode, if it
ever exists, reads a frozen approval binding, never editable planning state — and this
workstream creates no approval and no binding.

### 9.5 Authority

`device_command_owner` is **null** for all three processes, with the `why` recorded in
`CutCapability::command_owner_why` — there is no device path to own (DoD §15). Approval
validation lives only in the deterministic arbiter, never here and never in an MCP guard.
Durable `seq` is stamped by the gateway journal at append; this workstream mints no `seq`
for anything a UI may paint, and every `falsified`/`superseded` notice carries `at_seq`
applied only to rows with `seq <= at_seq`. Software commands never substitute for E-stop,
interlocks, enclosure, exhaust, or a breakaway circuit. Model output proposes; deterministic
code and hardware own authorization.

---

## 10. Licensing posture

- `grbl-sim` (GPLv3) and QtPlasmaC (GPLv2) are **never linked, vendored, or copied**.
  E2 launches an optional out-of-process binary the user supplies via
  `CHUDWARE_GRBL_SIM_BIN`, from an opt-in script outside `gate.sh` (no new SKIP class).
  E3 replays **recorded data**
  whose schema is modeled on QtPlasmaC's publicly documented signal set.
- No vendor cut chart, post kernel, or process model is copied. Recipe rows are ours, with
  provenance fields naming their source, matching the clean-room stance already asserted at
  `post.cpp:1-3`.
- Fast gate rejects any in-process coupling to those projects, consistent with DoD §17.

---

## 11. Non-goals

**Process scope**
- No raster/photo/dithered engraving (data volume and scan planning are a different
  problem, per the manufacturing-breadth research); no 3D/bevel/5-axis head; no tube, pipe, or
  profile cutting; no nesting optimizer beyond within-operation ordering.

**Execution**
- No execution, streaming, transport, jogging, homing, zeroing, unlocking, overriding,
  torch firing, pump start, gas control, or abrasive control. No device command owner is
  defined; there is none. No resume after fault of any kind.

**Physics**
- No beam, arc, or jet physics. No thermal, HAZ, dross, melt, kerf-width-vs-speed, taper,
  jet-lag, distortion, fume, exhaust, pressure-transient, or abrasive-dynamics model.
  Waterjet taper is an echoed declared number; corner slowdown is a recipe scalar.

**Safety**
- No safety function whatsoever. Enclosure, interlocks, E-stop, breakaway, arc-ok, ohmic
  sense, lid, fire detection, exhaust, and optical containment are hardware- and
  integrator-owned. The plasma state machine is authoring/explanation only and reports
  `protective_function: false`.

**Regulatory**
- No claim under ISO 11553-1 / IEC 60825 / FDA 21 CFR 1000-1040 (laser), ISO 17916
  (thermal cutting), ISO 12100 / ISO 13849-1, or UHP pressure-system safety (waterjet).
  These obligations are unaddressed and explicitly unclaimed.

**Vendor compatibility**
- No Ruida, Trocen, Hypertherm, OMAX, or Intelli-MAX dialect; no proprietary cut chart; no
  controller certification. Compatibility is contracted only at
  `(controller, firmware, options, dialect revision)` granularity for the six named
  dialects.

**Owned elsewhere**
- Probe/metrology results and `gen_probe` (`op_cut.cpp:191-253`) — untouched.
- `ObservationRecord` / live monitoring — `MonitoringArchitecture`, which per
  `MFG-READ-TRANSPORT-SPLIT` owns `IReadTransport` collectors and can therefore produce `src=probed` observations.
  Cutting consumes them only as read-only `FreshValue<ObservationRecord>` for display and
  authoring context. The reason no cut gate, verdict, or capability level depends on a monitor
  lane is **subject binding, not provenance**: a probed observation is admissible for the
  subject it directly measures — a machine's state — and a cut plan, NC artifact, or parse
  report is not that subject. So a monitor observation never corroborates a cut result, and
  cutting never widens one past what it measured.
- Turning, workholding, lathe posts, and the axisymmetric sim — `TurningArchitecture`.
- Durable `seq` stamping, approval binding, nonce burn, arbiter — `GatewayDesignRecovery`.
- Mill/router planning, the Z-dexel sim, and the eight existing mill dialects — unchanged
  except for the one head-refusal guard and the one head-emission call site.

---

## 12. Wave order

1. `core/cam/cut/process.*` + `recipe.*` + `model.h` appends + the `stock_sim.cpp`
   refusal guard + the append-only `result_kind` rows (both policy columns, `derived_from[]`,
   `supersede_with_parent` with its declared reason, permitted subject namespaces) in the
   durable-result-kind contract table (§5.2) — all nine kinds, including the
   `MFG-CAPABILITY-DURABLE-KIND`-mandated `cam.cut.capability`, registered before any cut
   append or capability advertisement (§5.2 "W1.1 registration") + tests 1-5, 20, 24-27, 29,
   30, 31, 34, 37, 38.
   *(This alone deletes the false green.)*
2. `vector_plan.*` + `op_laser.cpp` + `post_cut.cpp` laser dialects + `parse_gcode.*` +
   `emulator.*` E0/E1 + tests 6-12, 22, 33 + the laser smoke.
3. `plasma_state.*` + `op_plasma.cpp` + `linuxcnc_plasma` + fixtures + tests 13, 15-19 +
   the two plasma replay smokes.
4. `op_waterjet.cpp` + `linuxcnc_waterjet` + `cut_sim.*` + tests 14, 21 + the waterjet
   smoke.
5. `services/manufacturing/cut/**` including `tier0.py` registered through the gateway
   `journal_client.py` + the three `cam_tools.py` guard edits + tests 23, 28, 32, 35, 36 +
   the MCP guard tests.
6. `host/src/ui/manufacture/cut_process_panel.*` + the single `host/src/ui/cam/**` seam.
7. As-built map (`docs/manufacturing/README.md`) and developer-doc updates in the same green commit
   (DoD §20).

---

## Appendix A — Review closure (B10, B15, C9, C10)

Closes the architecture plan review findings assigned to this document. No product scope changes:
every cutting deliverable, non-goal,
capability claim and fidelity disclaimer above stands exactly as accepted.

### A.1 Gateway rule-id register (replaces every gateway section-number cross-reference — closes C10)

C10 found that this document cited the most load-bearing gateway rule — the `procedural_fact` flip scope,
which is what stops a level being flipped to `verified` by an offline check — as **§24.4**, which is in
fact the robot kind table; and cited the read-only transport split as **§24.1**, which is the two-column
rule. Renumbering would only move the hazard, since the gateway's contract tests are specified by
section. So this document now cites **stable rule ids** only; `test_mfg_contracts.cpp` and cut tests
26-38 key on these ids, never on a section number. Each row restates an already-accepted gateway
obligation verbatim in substance — nothing here is new scope.

| rule id | obligation (accepted gateway text, restated) | previously cited here as |
|---|---|---|
| `MFG-SEQ-SOLE-SOURCE` | `mfg_event_append` is the only way any domain obtains a stamped durable `seq`; the caller supplies content only, and minting, deriving or carrying a seq is a contract-test failure. | "gateway §24" (§5.2) |
| `MFG-KIND-MUST-BE-REGISTERED` | A `result_kind` absent from the contract's durable-result-kind table is refused `mfg_unregistered_result_kind`; table presence is a hard W1.1 prerequisite, not a nicety. | "gateway §24 rule 2" (§5.2) |
| `MFG-PROV-GATEWAY-COMPUTED` | `src`/`admission` are computed by the gateway and never accepted from a caller; `probed` is settable only by transport code inside the daemon holding an open `ITransport`. | "gateway §24 rule 3", "§23 rule 4" (§5.2, §6.1) |
| `MFG-TIER0-ONE-REGISTER` | `mfg_tier0_register` is a thin wrapper over the in-daemon `tier0::registerRows` — same register, same totality check, same refusal text. Two entry points are permitted; a domain-private second register is not. | "gateway §24", **§24.4 — wrong** (§3.3, §9.1) |
| `MFG-KIND-TWO-COLUMNS` | Every registered kind declares **both** (a) an epoch policy (`falsify \| reestablish \| epoch_invariant`) and (b) its runtime invalidation triggers; declaring only one is a schema rejection. | §24.1 (§5.2, test 30) |
| `MFG-CLOSURE-GATEWAY-OWNED` | `mfg_result_invalidate` computes the transitive closure over `derived_from[]`; `affected[]` is the gateway's closure and the caller names only the root subject. `falsified` always propagates; `superseded` propagates only for kinds declaring `supersede_with_parent`. | §24.2 — wrong (§5.2) |
| `MFG-PFACT-FLIP-SCOPE` | A `procedural_fact` row may flip only `plan \| generate \| post \| parse \| simulate`; `monitor \| transport \| execute \| safety_integration` require fresh `probed` evidence plus the emulator-stage ceiling and can never be flipped by `procedural_fact`, `simulated`, `declared`, `advisory` or `assumed`. | **§24.4 — wrong** (§5.2, §6.1, test 32) |
| `MFG-PFACT-FOLLOWS-PARENT` | Every kind whose id ends in `procedural_fact` MUST declare `supersede_with_parent:true`; registration without it is refused `mfg_procedural_fact_must_follow_parent`. | §24.7 (§5.2, test 34) |
| `MFG-SUPERSESSION-DECLARED` | Supersession of a derived-artifact row is the owning domain's choice, valid only when declared in its kind table with a one-line reason. | §24.9 (§5.2, test 37) |
| `MFG-CAPABILITY-DURABLE-KIND` | Any domain whose `CapabilityReport` can reach a UI MUST register a `<domain>.capability` durable kind; advertising any level above `unsupported` without one is refused `mfg_capability_requires_durable_kind`. | §24.11 (§5.2, §12, test 38) |
| `MFG-CAPABILITY-NO-INHERIT` | `*capability` kinds MUST NOT set `supersede_with_parent`; a capability row re-derives from its own evidence, and its `derived_from[]` is the evidence rows it cites. | §24.7 converse / §24.11 (§5.2) |
| `MFG-SUBJECT-NS-DECLARED` | A domain declares its permitted subject namespaces at registration; an append outside that set is refused `mfg_subject_namespace_not_permitted`. `dev:` belongs to the gateway device lane and `src:` to the monitor lane. | §24.8 (§5.2, test 35) |
| `MFG-GUARD-ACCEPTS-DECLARED` | Every Stage-2 guard declares `accepts{subject_ns, admission}` and `gates::evaluate` refuses out-of-set evidence **by name**, recorded in the gate report rather than skipped. | §24.8 (§9.3, test 36) |
| `MFG-READ-TRANSPORT-SPLIT` | `IReadTransport` has no `write()`, so a monitor-lane collector can produce `probed` observations without ever being able to command. | **§24.1 — wrong** (§5.2, §11) |
| `MFG-CAPTRUTH-ONE-DEFINITION` | `CapabilityTruth`/`CapabilityReport` are consumed verbatim from `core/device/capability.h`; a second definition is a contract-test failure, and `probed` is an observation `src`, never a truth value. | §23 (§4.4, §6.1) |
| `MFG-FRESHVALUE-ONE-DEFINITION` | The sole external freshness shape is `FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}`; `v` is null unless `state=="ok"`, `last_ok` is display-only, and a sequence gap forces `unknown`. | DoD §4 (§3.3, §6.3) |
| `MFG-STAGE-CEILING` | `max_advertised` may never exceed the ceiling of `emulator_stage_passed`: E0-E3 unlocks at most `monitor`, E4-E5 `transport`, E6 `execute`, and no stage unlocks `safety_integration`. | §23 (§6.1) |
| `MFG-FAMILY-OFF-BY-DEFAULT` | The single shared MCP family `manufacture` is OFF by default in `agent.tool_families` until commissioning, so `enable_tools` cannot silently self-add manufacturing surface. | §17 (§5.3) |

Cutting cites no other gateway rule. Where the text above names a DoD criterion (DoD §2, §3, §5, §6, §7,
§8, §9, §10, §14, §16, §19, §20) that is the authoritative document itself, not a gateway section, and
stays as written.

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
| **B10** (P0) — the W1.1 durable-result-kind table must be total over every domain that appends, or the append is refused `mfg_unregistered_result_kind` and the capability registration `mfg_capability_requires_durable_kind` | Extend the accepted W1.1 set; W1.1 is not green until the table is total. `cam.cut`'s nine kinds plus `cam.cut.capability` were already accepted, so cutting is not one of the four missing domains | Registration is now stated explicitly rather than implied: all nine kinds — `cam.cut.{recipe,plan,post,parse,sim,emulate,procedural_fact,capability}` and `cam.plasma.replay` — are named as the accepted W1.1 set with `cam.cut.capability` identified as this domain's `MFG-CAPABILITY-DURABLE-KIND` kind (`supersede_with_parent:false` per `MFG-CAPABILITY-NO-INHERIT`) and `{art:, job:, res:}` as its `MFG-SUBJECT-NS-DECLARED` set, registered in wave 1 as a hard predecessor of every later cut slice | §5.2 "W1.1 registration"; §12 step 1 |
| **B15** (P1) — `post.cpp:487` requires a joint edit by two owners, and this document's "disjoint" claim is false as written | Assign the footer line to exactly one owner for the wave (turning, since it deletes a condition cut only adjusts), have cut consume the result, record it in the wave ownership table, and reconcile the false "disjoint" claim | The false claim is **withdrawn in place** in §3.2 and the line removed from cutting's `post.cpp` edit set in §3.2's post bullet; cutting makes no edit to `:487` and consumes the resulting footer behaviour, pinned from the cut side by `fixtures/mfg/cut/programs/laser_grbl_expected.nc`. Ownership is recorded in A.2, identical to the turning document | §3.2; A.2 |
| **C9** (P3) — turning's fixtures diverge from the ratified `fixtures/mfg/**` root | `fixtures/mfg/turn/**` | No change required here: cutting's fixtures were already rooted at `fixtures/mfg/cut/**` and remain so, which is what made turning's divergence visible. Recorded so the compliance cannot be silently reverted, and so the cross-document footer golden cited in A.2 keeps a stable path | §3.5 (unchanged) |
| **C10** (P3) — cross-reference drift on the two most load-bearing §24 rules (cutting cited the `procedural_fact` flip scope as §24.4 and the read-only transport split as §24.1; both point at unrelated rules) | Normalize cross-references and have the contract tests key on stable rule ids rather than section numbers | Every gateway section-number citation replaced by a stable rule id from the A.1 register, which restates the obligation in place so a later gateway renumbering cannot invalidate it; the wrong citations are corrected to `MFG-PFACT-FLIP-SCOPE`, `MFG-READ-TRANSPORT-SPLIT`, `MFG-CLOSURE-GATEWAY-OWNED` and `MFG-TIER0-ONE-REGISTER` | A.1; §3.3; §4.4; §5.2; §6.1; §8 tests 30-38; §9.1; §9.3; §11 |

Unchanged by this appendix, and asserted here so the closure cannot be read as scope drift: all domains
stay in scope; the shared MCP family remains `manufacture`, off by default (`MFG-FAMILY-OFF-BY-DEFAULT`);
wire names remain `mfg_*`/`mfg.*`; cutting adds **zero** new normal-gate SKIPs (`CHUDWARE_GRBL_SIM_BIN`
stays outside `gate.sh` behind `scripts/qualify_cut_e2.sh`); the sole freshness schema remains
`FreshValue<T>` (`MFG-FRESHVALUE-ONE-DEFINITION`); the gateway journal owns all durable `seq` and all
invalidation closure (`MFG-SEQ-SOLE-SOURCE`, `MFG-CLOSURE-GATEWAY-OWNED`); and cutting — a domain with a
UI capability truth table — registers the durable capability kind `cam.cut.capability`
(`MFG-CAPABILITY-DURABLE-KIND`).