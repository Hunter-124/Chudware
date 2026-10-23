# Pick-and-Place (OpenPnP virtual / job-interchange) — Implementation-Ready Architecture

Track owner: `PnpArchitecture`. Design-only.
Sources read: `services/mcp/server.py`, `services/mcp/tool_registry.py`, `host/src/ui/cam/cam_panel.h`,
`core/cam/model.h`, `contracts/worker_rpc.md`, the manufacturing-breadth research,
`00_decisions.md`, `01_definition_of_done.md`.

Path ruling adopted: production GUI only under `host/src/ui/manufacture/**`; cross-domain
service root `services/manufacturing/**`; RPC/events `mfg_*` / `mfg.*`; `host/src/ui/cam/**` untouched.

---

## Citation convention — stable gateway rule ids

Gateway rules are cited here by **stable rule id**, never by gateway section number. The review found
three domains already drifted onto stale `§24.x` numbers pointing at unrelated rules, and the gateway
numbering moved again during the amendment pass, so a section-number citation is not a durable
reference. Each id below is bound to the rule's *content* and, where the gateway has one, to the
wire-level refusal code — a refusal code cannot drift because the daemon returns it verbatim.
Contract tests key on these ids, not on section numbers.

| id | rule as cited in this document |
|---|---|
| `GW-SEQ-1` | the Gateway journal is the sole durable-`seq` authority; `mfg_event_append` stamps every `seq` a UI may paint, and a domain-minted, guessed, derived or copied `seq` is a contract-test failure |
| `GW-KIND-2` | a `result_kind` must already be registered in the durable-result-kind table or the append is refused `mfg_unregistered_result_kind` |
| `GW-KIND-COLUMNS` | every registered kind declares **both** epoch policy **and** runtime invalidation triggers; declaring one column alone is a schema rejection |
| `GW-KIND-REASON` | `supersede_with_parent` on a derived-artifact kind is the owning domain's call **provided the kind table carries a one-line declared reason** |
| `GW-PF-FOLLOWS-PARENT` | a kind whose id ends in `procedural_fact` MUST set `supersede_with_parent:true`, else `mfg_procedural_fact_must_follow_parent` |
| `GW-CAP-NO-INHERIT` | a `*capability` kind MUST NOT set `supersede_with_parent`; a capability row re-derives from its own evidence and never inherits a parent's status |
| `GW-SRC-3` | `src` and `admission` are computed by the gateway and never accepted from a caller; `probed` is settable only by transport code inside the daemon holding an open `ITransport` |
| `GW-ADMISSION-CLASSES` | Stage 1 has exactly two admission classes — `probed_evidence` (`state=="ok" && src=="probed"`) and `procedural_fact` (a seq-stamped `ProceduralFact` row bound to an `art:`/`job:`/`res:` subject) — and `procedural_fact` can never satisfy an execute-side subject |
| `GW-PF-SCOPE-4` | a `procedural_fact` may flip only `plan` \| `generate` \| `post` \| `parse` \| `simulate` to `verified`; `monitor` \| `transport` \| `execute` \| `safety_integration` require fresh probed evidence plus the stage ceiling |
| `GW-CLIENT-5` | `services/manufacturing/rpc/journal_client.py` is gateway-owned and is imported, never vendored or re-implemented per domain |
| `GW-JOURNAL-ONLY-6` | `chudware-mfgd --journal-only` gives an offline domain a real `seq`; it opens no transport, registers no device, and adds no gate SKIP because it is hardware-free |
| `GW-NO-ABI-7` | gateway calls cross the bridge the domain already calls; no new `libchudware` C ABI symbol and no new native build surface |
| `GW-CLOSURE` | `mfg_result_invalidate` computes the transitive closure over the `derived_from[]` edges declared at append; the caller names only the root subject and never populates `affected[]` |
| `GW-SUBJECT-NS` | a domain declares its permitted subject namespaces at registration; an out-of-set append is refused `mfg_subject_namespace_not_permitted` |
| `GW-GUARD-ACCEPTS` | every Stage-2 guard declares `accepts{subject_ns:[…], admission:[…]}` and `gates::evaluate` refuses out-of-set evidence **by name** |
| `GW-TYPES-OWNED` | `CapabilityTruth` / `CapabilityReport` (`core/device/capability.h`) and the Tier-0 `PREREQ` register row (`core/manufacturing/tier0.h`) are gateway-owned; domains compose them and re-declare nothing |
| `GW-TIER0-REGISTER` | `mfg_tier0_register` is atomic and idempotent per `(domain_id, action, prerequisite, hash(applies_when))`, runs the totality check over `action × artifact_kind × machine_class`, and owns the rendering contract |
| `GW-CAP-KIND-REQUIRED` | a domain advertising any level above `unsupported` MUST have a registered `<domain>.capability` durable kind, else registration is refused `mfg_capability_requires_durable_kind`; an all-`unsupported` domain is exempt |
| `GW-CAP-DERIVED-FROM-EVIDENCE` | a capability row's `derived_from[]` is **every evidence row the report cites**, so no level can outlive the evidence under it |
| `GW-FRESHVALUE-PUBLISH` | `FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}` is the sole external freshness shape, and **every** field a UI paints crosses wrapped in it — including a durable authored row, which is journaled first and then published wrapped with `state:"ok"` and `ttl_ms:null` when it does not decay |
| `GW-EMULATOR-CEILING` | no advertised level may exceed what the highest stage actually passed unlocks: E0–E3 → `monitor`, E4–E5 → `transport`, E6 → `execute`; `safety_integration` is never unlocked by any stage |
| `GW-FAMILY-MANUFACTURE` | one shared MCP family id `"manufacture"` covers the whole overhaul and is OFF by default; the enabling mechanism (`DEFAULT_TOOL_FAMILIES` + `DEFAULT_OFF_TOOL_FAMILIES` in `services/agent/config.py`) is Gateway-owned in W1.1 and is a hard predecessor of every domain's MCP-registration wave |
| `GW-RISK-TABLE-OWNER` | Gateway is the sole owner of the `services/mcp/tool_registry.py` risk-class edit for the batch, in one wave; every other domain's rows land as **data** the owner applies |
| `GW-ZERO-NEW-SKIP` | the batch adds **zero** new normal-gate SKIPs; an absent optional prerequisite pins the level at `assumed` with the missing level named, or is excluded from `gate.sh` behind an opt-in qualification script — never reported as a SKIP and never read as a pass |

---

## 0. One-paragraph position

PnP enters the product as an **offline authoring, interchange and deterministic-emulation track with no
device path at all**. There is no serial, USB, socket, or subprocess route to a placement machine; the
only subprocess this track can ever spawn is a *user-supplied, user-installed* OpenPnP binary used
strictly as an out-of-process **conformance checker for a file we wrote**, gated behind an explicit
environment variable and never invoked by `scripts/gate.sh`. Consequently the track advertises
`monitor`, `transport`, `execute`, and `safety_integration` as `unsupported` at every level, and the
DoD's "one command owner per device" rule is satisfied vacuously and stated as such rather than left
implicit. No OpenPnP source, jar, JVM binding, or derived code enters the repository.

---

## 1. Licensing boundary (decided first, because it constrains every other choice)

OpenPnP is GPLv3 (breadth audit, `openpnp/LICENSE.txt`). Per the project license policy, the permissively licensed core links no GPL code, via a
**two-lane split**, and the lanes never merge:

### Lane A — native interchange (always available, zero third-party dependency)

We implement our own board/panel/placement/resource model and our own readers and writers. Reading and
writing a *file format* is not a derivative work of the program that also reads it. Lane A ships
unconditionally, is fully gate-tested, and is the only lane any user-visible capability depends on.

### Lane B — external qualification harness (opt-in, user-supplied, out-of-process)

The user points `CHUDWARE_OPENPNP_BIN` at *their own* OpenPnP install. We spawn it as a separate OS
process against a temp config directory we generated, and read back only its exit status, stdout/stderr
text, and any files it wrote. This is the `Q1` qualification stage (§7). It is a *verification*
mechanism, never a runtime dependency, never redistributed, never linked.

### Hard boundary rules (each is a test in §11)

| Rule | Enforcement |
|---|---|
| No OpenPnP source, jar, class file, or vendored copy in the repo | `test_licensing_boundary.py` + append patterns to the existing DoD §17 fast gate (integrator-applied): reject `**/openpnp*.jar`, `vendor/**/openpnp/**`, any `*.jar` under the repo, `import org.openpnp`, `org.openpnp.` string references in build files |
| No JVM in-process | No JNI, no JPype, no `jpype`/`py4j` dependency; the only permitted interaction is `subprocess` with an absolute path from env |
| The OpenPnP-format writer contains no OpenPnP code | Writer is schema-driven from a hand-authored profile (§6.3); the golden expected output is *configuration data*, not program text |
| Distribution artifacts unaffected | PnP adds no bundled binary; `scripts/qualify_pnp_q1.sh` is a developer/integrator script, excluded from packaging |
| The boundary is documented at the user surface | `describe_pnp_capabilities` and the GUI capability table both state "OpenPnP is not included; Lane B requires your own GPLv3 install" |

**Deliberate deferral, stated in the product surface:** embedding OpenPnP, linking it, or shipping a
plugin built against it is *out of scope permanently in this wave* and requires counsel sign-off on the
exact boundary plus a source-offer process before it could ever be revisited (breadth audit founder
decision 3, decisions brief "Copyleft components remain out-of-process behind coarse documented
contracts").

---

## 2. Where the code lives

**Deliberate choice: PnP is pure Python. There is no `core/pnp/**` C++ engine.**

Rationale, in order: (a) DoD #16 forbids `mfg_*` symbols in the public `libchudware` C ABI, and a Python
track cannot accidentally violate it; (b) the PnP math is 2D affine/similarity least-squares fitting and
step-and-repeat array generation — no OCCT, no heightfields, none of the reasons `core/cam` is C++;
(c) the interchange work is text-dialect munging, which iterates far faster in Python; (d) it adds zero
native build surface, so no top-level CMake change is needed for the engine at all. The only C++ is a
read-only GUI panel that consumes JSON already crossing the existing worker-RPC seam.

```
services/manufacturing/pnp/
  __init__.py
  model.py                # canonical dataclasses + JSON round-trip (chudware.pnp/1)
  units.py                # unit + rotation-convention resolution with provenance
  centroid.py             # dialect-detecting centroid readers (KiCad .pos / Altium / generic CSV)
  bom.py                  # BOM join: refdes -> part -> package
  package_library.py      # Package/NozzleTip compatibility library
  panel.py                # step-and-repeat panelisation, skips, per-instance transforms
  fiducial.py             # 2D fits (translation/rigid/similarity/affine) + residual acceptance
  resources.py            # Feeder / Nozzle / Head / NozzleTip resolution and assignment
  job.py                  # PnpJob construction, step ordering, artifact hashing
  virtual_machine.py      # deterministic integer-tick VirtualPnpMachine (stages E1-E2)
  faults.py               # declared fault injection catalogue
  fidelity.py             # PnpFidelityDeclaration (what the emulator does and does not model)
  tier0.py                # Tier-0 construction-refusal register
  gates.py                # two-stage gate definitions + evaluation
  invalidation.py         # falsified / superseded producers
  evidence.py             # canonical run/evidence record emission
  export_canonical.py     # chudware.pnp.job/1 writer
  export_csv.py           # round-trippable CSV centroid writer
  export_openpnp.py       # schema-profile-driven OpenPnP config-bundle writer (Lane A)
  openpnp_schema.py       # loads + validates a version-pinned OpenPnP schema profile
  openpnp_harness.py      # Lane B: probe / spawn / classify a user-supplied binary
  capability.py           # PnP capability truth-level report
  rpc.py                  # WORKER_METHODS = {...} — the mfg_pnp_* surface
  mcp_tools.py            # register_pnp_tools(view) — tools join the shared family "manufacture"
  test_*.py               # §11
fixtures/mfg/pnp/**        # §10
host/src/ui/manufacture/pnp_panel.{h,cpp}
scripts/smoke_pnp_offline.sh
scripts/qualify_pnp_q1.sh
contracts/mfg_pnp.md       # NEW contract file (new file, so not an append-only violation)
```

### Integrator-owned append-only edits (not made by the PnP track; each is exactly one appended block)

1. `contracts/worker_rpc.md` — append `### Manufacturing — Pick & Place (mfg_pnp_*, append-only, <date>)`
   pointing at `contracts/mfg_pnp.md` as the normative schema, in the style of §5/§6 of that file.
2. `services/mcp/server.py` — append, in the shared manufacturing registration block the tracks agreed
   to place after the existing CAM registration (`register_cam_audit_tools`, currently line 365):
   ```python
   from services.manufacturing.pnp.mcp_tools import register_pnp_tools
   register_pnp_tools(_reg.view(mcp, "manufacture"))
   ```
3. `services/mcp/tool_registry.py` — **not appended by PnP.** Gateway is the single owner of that
   file's risk-class edit for the whole batch, in one wave (`GW-RISK-TABLE-OWNER`); PnP's three
   `_RISK_OVERRIDES` rows (§9.2) are handed over as data for that owner to apply, and
   `test_mfg_tools.py`'s "`classify_risk` unchanged for every pre-existing tool" assertion is the
   shared acceptance check.
4. Host CMake source list — append `host/src/ui/manufacture/pnp_panel.cpp` (host-level list, not the
   top-level `CMakeLists.txt`).
5. DoD §17 fast-gate pattern list — append the four Lane-B rejection patterns from §1.
6. Gateway's durable-result-kind table — append the **eleven** `pnp.*` kinds from §4.2 with both
   policy columns, `derived_from`, and the declared `supersede_with_parent` reason for each, plus
   PnP's permitted subject namespaces (`art:` `job:` `res:`). Without this, every `mfg_event_append`
   from PnP is correctly refused `mfg_unregistered_result_kind`, so this item is a hard prerequisite
   for the track functioning at all, not a nicety.

PnP creates no file under `services/manufacturing/rpc/`: `journal_client.py` is gateway-owned and is
imported, never vendored.

No existing contract clause, C ABI symbol, top-level CMake statement, or gate assertion is modified.

---

## 3. Canonical data model — `chudware.pnp/1`

All lengths mm, angles degrees CCW-positive, times seconds unless a field says otherwise — matching the
`core/cam/model.h` convention verbatim. Every type has `to_json()` / `from_json()` and is round-trip
tested. `source` fields everywhere record *how we know* a value, because the DoD forbids a value whose
provenance is unstated from ever satisfying a Stage-1 gate.

```python
Provenance = Literal["probed", "declared", "simulated", "assumed", "inferred", "unknown"]
```

### 3.1 Board and placement

| Type | Fields |
|---|---|
| `BoardRevision` | `id, name, revision, source_files:[SourceFile], units_source:UnitsResolution, rotation_convention:RotationConvention, origin_convention, outline_mm:[[x,y]], thickness_mm, sides:Literal["single","double"], placements:[Placement], fiducials:[Fiducial], seq, artifact_hash` |
| `SourceFile` | `path, sha256, kind:Literal["kicad_pos","altium_pickplace","csv_centroid","bom_csv","outline_dxf"], dialect_id, imported_at, row_count, rejected_rows:[RejectedRow]` |
| `UnitsResolution` | `units:Literal["mm","inch"], source:Provenance, evidence:str` — `source=="assumed"` is a **hard import refusal**, never a default |
| `RotationConvention` | `convention:Literal["ipc7351","eda_native","unknown"], source:Provenance, per_package_overrides:{package_id: deg}` |
| `Placement` | `id, ref_des, part_id, x_mm, y_mm, rotation_deg, side:Literal["top","bottom"], kind:Literal["placement","fiducial"], enabled:bool, status:Literal["planned","skipped","placed_virtual"], skip_reason?` |
| `Fiducial` | `id, ref_des?, nominal_mm:[x,y], side, diameter_mm, clearance_mm, scope:Literal["board","panel","local"], template_id?` |
| `RejectedRow` | `line_no, raw, reason` — importers never silently drop a row |

### 3.2 Parts and packages

| Type | Fields |
|---|---|
| `Package` | `id, name, body_mm:[w,h], height_mm, lead_pitch_mm?, lead_count?, kind:Literal["chip","gullwing","jlead","bga","qfn","tht","odd"], pick_offset_mm:[x,y], pick_rotation_deg, compatible_tip_ids:[str], vision_settle_ms, source:Provenance` |
| `Part` | `id, package_id, value, manufacturer_pn?, height_mm?, polarized:bool, notes?` |
| `PackageLibrary` | `packages:{id: Package}, revision, seq` |

### 3.3 Panel

| Type | Fields |
|---|---|
| `PanelDefinition` | `id, board_id, array:{cols, rows, step_x_mm, step_y_mm, rotation_deg}, skips:[[col,row]], panel_fiducials:[Fiducial], rail_mm:{left,right,top,bottom}, instances:[PanelInstance], revision, seq` |
| `PanelInstance` | `index, col, row, transform:Affine2, fit:FiducialFitResult \| None, enabled` |
| `Affine2` | `a,b,c,d,tx,ty` (row-major 2×3) + derived read-only `rotation_deg, scale_x, scale_y, shear_deg` |

### 3.4 Machine resources (all virtual in this wave)

| Type | Fields |
|---|---|
| `NozzleTip` | `id, name, outer_d_mm, inner_d_mm, min_part_mm, max_part_mm, compatible_package_ids:[str], in_changer_slot?` |
| `Nozzle` | `id, head_id, tip_id?, z_travel_mm, has_vacuum_sensor:bool, changer_slots:[int]` |
| `Head` | `id, nozzles:[Nozzle], down_camera_id?, offsets_mm:[x,y,z]` |
| `Feeder` | `id, kind:Literal["tape","tray","tube","stick","bulk","loose"], part_id?, pick_mm:[x,y,z], pick_rotation_deg, pitch_mm, tape_width_mm?, remaining_count?, remaining_source:Provenance, enabled, bank_id, actuation:Literal["manual"]` |
| `PnpCamera` | `id, kind:Literal["up","down","fixed"], units_per_pixel_mm:[x,y], resolution_px:[w,h], offsets_mm:[x,y,z], simulated:bool, calibration:CalibrationRecord \| None` |
| `CalibrationRecord` | `id, subject_id, kind:Literal["camera_upp","nozzle_offset","head_offset","backlash","tip_runout"], method, method_version, performed_at, performed_by, source:Provenance, uncertainty_mm?, expires_at?, config_epoch, evidence_refs:[str]` |
| `PnpMachineProfile` | `id, name, heads:[Head], feeders:[Feeder], cameras:[PnpCamera], nozzle_tips:[NozzleTip], envelope_mm:{min:[x,y,z],max:[x,y,z]}, park_mm:[x,y,z], driver:Literal["virtual_null"], kinematics:{vmax_mm_s:[x,y,z], accel_mm_s2:[x,y,z]}, capability:CapabilityTruth, revision, seq` |

`Feeder.actuation` and `PnpMachineProfile.driver` are single-value literals on purpose: the type system
itself refuses a driven feeder or a physical driver, so the deferral cannot be defeated by a config file.

### 3.5 Job, simulation, evidence

| Type | Fields |
|---|---|
| `PnpJob` | `id, subject:{kind:"board"\|"panel", id, revision}, machine_profile_id, machine_profile_revision, steps:[JobStep], sequence_policy:Literal["by_feeder_bank","by_nozzle","by_travel","source_order"], inputs_digest, artifact_hash, built_at, seq` |
| `JobStep` | `seq, kind:Literal["fiducial_check","nozzle_change","feeder_pick","up_vision","place","discard","park"], placement_id?, feeder_id?, nozzle_id?, tip_id?, target:Affine2?, expected_ms, preconditions:[str]` |
| `PnpSimEvent` | `tick, t_ns, kind, step_seq?, subject_id?, detail:{}, outcome:Literal["ok","fault"], fault_id?` |
| `PnpSimResult` | `job_artifact_hash, machine_profile_revision, fidelity:PnpFidelityDeclaration, fault_set:[str], tick_count, events:[PnpSimEvent], counts:{placed,skipped,faulted}, deterministic_digest, seq` |
| `PnpFidelityDeclaration` | see §5.3 |
| `PnpEvidenceRecord` | `id, run_id, kind, subject_id, subject_revision, artifact_hash?, inputs_digest, source:Provenance, produced_at, seq, payload:{}` |

`deterministic_digest` is a SHA-256 over the serialized event stream. Two runs of the same job on the
same profile with the same fault set MUST produce an identical digest; this is asserted directly (§11).

---

## 4. Freshness, sequencing, invalidation

### 4.1 `FreshValue<T>` scope — two axes, deliberately kept apart

The sole external freshness shape is
`FreshValue<T>{v,state,t_mono_ns,seq,age_ms,ttl_ms,src,why,last_ok?}` with `v` null unless
`state=="ok"` (`GW-FRESHVALUE-PUBLISH`). PnP defines no second envelope **and no unwrapped painting
path beside it**: every field the PnP GUI paints crosses the seam wrapped.

Two axes are kept apart, because collapsing them in *either* direction is a defect:

- **Decay** — does the value's truth expire with wall time? Only these PnP values decay, and they are
  the only PnP rows carrying a non-null `ttl_ms`:
  - `mfg_pnp_harness_status` → `FreshValue<HarnessState>` (an external OS process's liveness).
  - Live simulation progress the GUI paints while a run is in flight → `FreshValue<SimProgress>`.
  - Any `CalibrationRecord` rendered as "current" → `FreshValue<CalibrationRecord>` with `ttl_ms` from
    `expires_at`; an expired record renders `state=="stale"` and `v==null`.
- **Persistence identity** — what names the row, and what decides whether it is still the current one?
  For every durable authored artifact (board revision, panel, machine profile, feeder assignment,
  fiducial fit, job, sim result, gate row, export artifact, Q1 procedural fact, capability report)
  that is `seq` + `revision` + `artifact_hash`, and it stays that way. The envelope is never the
  identity: nothing keys, joins, supersedes or invalidates on `age_ms`, `t_mono_ns` or `ttl_ms`, and
  §4.3's notices are scoped by `seq <= at_seq` only.

**The ruling (closes review C8).** A durable authored row that a UI paints is **journaled first and
published wrapped**. `mfg_event_append` stamps its `seq` (`GW-SEQ-1`); the publisher then emits it as
a `FreshValue<T>` carrying `state:"ok"`, `v` populated, `seq` = the journal's stamped `seq`, `src`
gateway-derived (`GW-SRC-3`), and **`ttl_ms:null`** — the explicit encoding of *"this row does not
decay; it is superseded or falsified, never aged out"*. `age_ms` is informational for these rows and
no consumer branches on it. A row whose `state` is anything but `ok` paints as unknown/stale exactly
like a telemetry row, and never paints `last_ok` as live.

The earlier §4.1 said durable artifacts carry "not a freshness envelope". That was wider than the
argument it was making, and it collided with `GW-FRESHVALUE-PUBLISH`: `pnp.capability` is a durable row
the GUI paints (§8.1, §9.4), so the old wording would have left the most truth-bearing surface in the
track as the one unwrapped thing on screen — the same class of hole as the missing `pnp.capability`
kind (§4.2). The distinction PnP actually argues, and the one that survives, is **no wall-clock TTL and
no envelope-derived identity**, not "no envelope".

`contracts/mfg_pnp.md` therefore states the narrowed rule in both directions: a later contributor may
not give an authored artifact a wall-clock `ttl_ms`, may not let one read `stale` by clock, and may not
treat the envelope as its persistence identity — and equally may not paint one unwrapped.

Internal simulation is tick-domain (`TICK_NS = 1_000_000`, 1 kHz integer ticks). The publisher stamps the
external envelope at the boundary; ticks never leak into `t_mono_ns`. A sequence gap forces `unknown`.

### 4.2 Durable `seq`

Gateway owns durable `seq` and assigns it inside the journal (`GW-SEQ-1`). **`mfg_event_append`
is the only way PnP obtains a `seq`**: PnP supplies content, the journal assigns the number. Minting,
guessing, deriving, or carrying a `seq` that did not come back from an append is a contract-test
failure, not a style preference. `services/manufacturing/pnp/evidence.py` is the single site that calls
it, through the gateway-owned helper `services/manufacturing/rpc/journal_client.py` — which PnP
**uses and never copies** (`GW-CLIENT-5`).

`mfg_event_append{domain_id:"pnp", kind, subject, result_kind, body, raw?, admission?} -> {seq, epoch, ref}`.
`src`/`admission` are computed by the gateway and never accepted from the caller (`GW-SRC-3`), so the
honest classification of PnP evidence is enforced by the daemon rather than by the domain's own good behaviour.

**Durable result kinds (`GW-KIND-2`, schema per `GW-KIND-COLUMNS`).** A `result_kind` must already exist in
the durable-result-kind table, else the append is refused `mfg_unregistered_result_kind` — a domain
cannot create an unreconciled durable row. Every kind MUST declare **both** columns; declaring only
one is a schema rejection. They are different axes: a kind can be `epoch_invariant` and still be
falsified mid-epoch by a source-hash change.

**Epoch policy for the whole PnP set is `epoch_invariant`, and that is a consequence rather than a
preference:** PnP holds no probed transport (§7.1.4, `GW-SRC-3`), so none of its durable state
is transport-bound and all of it survives a gateway restart intact. A gateway restart may not
re-establish, downgrade, or re-assert any PnP row — there is nothing for it to reconcile against.

Per `GW-KIND-REASON` every `supersede_with_parent` choice on a derived artifact row is the owning
domain's call **provided the kind table carries a one-line reason**, so each is justified below rather
than left to inference. Two choices are not free: `GW-PF-FOLLOWS-PARENT` makes `supersede_with_parent:true` mandatory
for any kind whose id ends in `procedural_fact`, and `GW-CAP-NO-INHERIT` forbids it on any
`*capability` kind.

| `result_kind` | epoch policy | runtime invalidation triggers | `derived_from` | `supersede_with_parent` (reason) |
|---|---|---|---|---|
| `pnp.board_revision` | `epoch_invariant` | superseded on re-import at a new revision; **falsified** on source-file hash change | (source files) | — (root; nothing to follow) |
| `pnp.panel_definition` | `epoch_invariant` | superseded on array/skip/rail edit | `board_revision` | **yes** — a panel is a pure derivation of its board; an old panel over a new board is never the current derivation |
| `pnp.machine_profile` | `epoch_invariant` | superseded on revision bump | — | — (root; authored independently) |
| `pnp.feeder_assignment` | `epoch_invariant` | **falsified** when the part no longer fits the assigned tip | `board_revision`, `machine_profile` | **no** — human-authored intent; a profile bump must not silently retire an assignment a person made, and genuine incompatibility falsifies instead |
| `pnp.fiducial_fit` | `epoch_invariant` | **falsified** on a rejected re-fit (residual over tolerance) | `board_revision` \| `panel_definition` | **no** — a fit is a measurement-like claim, not a derivation; it is either still true or falsified |
| `pnp.job_artifact` | `epoch_invariant` | superseded on any bound-input change | `board_revision` \| `panel_definition`, `machine_profile`, `feeder_assignment` | **yes** — the job is defined by its inputs; a changed input makes it stale, not wrong |
| `pnp.sim_result` | `epoch_invariant` | superseded with its job artifact | `job_artifact` | **yes** — the result describes one job artifact and means nothing detached from it |
| `pnp.gate_row` | `epoch_invariant` | **falsified** with its subject or its cited procedural fact | `job_artifact`, cited fact | **yes** — a gate row about a superseded artifact is no longer the current verdict |
| `pnp.export_artifact` | `epoch_invariant` | superseded with its job artifact | `job_artifact` | **yes** — the file is a rendering of one job; a new job makes it the previous rendering, not a false one |
| `pnp.q1_procedural_fact` | `epoch_invariant` | **falsified** on a failing re-run | `export_artifact` | **yes, mandatory** (`GW-PF-FOLLOWS-PARENT`) — it is what could flip a level; leaving it standing under a superseded bundle would keep a claim verified after the check it cites was re-run |
| `pnp.capability` | `epoch_invariant` | re-derived and re-appended whenever any cited evidence row is falsified or superseded | the evidence rows it cites | **no, forbidden** (`GW-CAP-NO-INHERIT`) — capability re-derives from its own evidence and never inherits a parent's supersession |

`pnp.capability` was missing from the earlier submission and is a real gap, not a formality: the GUI
paints the capability table (§9.4), and DoD #10 requires a gateway-stamped durable row for **every**
durable result a UI may paint, not only telemetry. Without it the most truth-bearing surface in the
track would have been the one unjournaled thing on screen.

**Subject namespaces (`GW-SUBJECT-NS`, `GW-GUARD-ACCEPTS`).** PnP declares exactly `art:` · `job:` · `res:` at registration.
It declares **no** `dev:` and **no** `src:`, so an append naming a device or monitor subject is refused
`mfg_subject_namespace_not_permitted` by the daemon rather than by domain discipline. Every PnP Stage-2
guard declares `accepts{subject_ns:["art:","job:","res:"], admission:["procedural_fact"]}`, so
`gates::evaluate` refuses out-of-set evidence by name — including a fresh `probed` `src:` observation,
which is exactly the cross-lane case PnP raised and which is now a mechanism instead of prose.

PnP declares no `retain_as_last_ok` row: that flag exists for transport-bound observations whose value
clears while `FreshValue.last_ok` stays display-only, and PnP publishes no such observation.

Offline domains still get a real `seq`: `chudware-mfgd --journal-only` (`GW-JOURNAL-ONLY-6`) is auto-started by the
bridge, opens no transport, registers no device, reports the last four levels `unsupported` with a
`why`, and adds no gate SKIP because it is hardware-free. PnP adds **no** C ABI symbol and no native
build surface for any of this — these are three more `mfg_*` methods over the bridge PnP already calls
(`GW-NO-ABI-7`).

### 4.3 Invalidation — producer-pushed, seq-scoped

Envelope: `{class:"falsified"|"superseded", reason, why, affected:[ids], at_seq, superseded_by?}`.
Emitted through the gateway-owned `mfg_result_invalidate{domain_id:"pnp", class, reason, why,
affected[], superseded_by?} -> {at_seq}` (`GW-CLOSURE`), which returns the authoritative `at_seq`; the
domain event `mfg.pnp.invalidated` is the local mirror of that call, never an independent source of
truth. Producers push; nothing waits for polling or staleness.

**PnP does not compute `affected[]`.** Per `GW-CLOSURE`, `mfg_event_append` carries `derived_from[]`
(the edges declared in §4.2) and `mfg_result_invalidate` computes the transitive closure, so `affected`
is the gateway's closure rather than the domain's best guess — which removes the most likely correctness bug in
the whole track, since a hand-maintained affected-set silently under-invalidates the moment someone
adds a derived row. `falsified` propagates along `derived_from`; `superseded` propagates only into rows
declaring `supersede_with_parent`, which is why the job/sim/gate/export/Q1 chain sets it and the
resource rows do not. The closure column below is therefore **illustrative of the expected outcome**,
not an input PnP supplies; `test_invalidation.py` asserts the gateway's closure matches it.

| Trigger | Class | Expected closure (gateway-computed) | Note |
|---|---|---|---|
| Board re-imported at a new revision | `superseded` | placements, jobs, sim results, exports bound to the old revision | retains, points forward via `superseded_by`, no alarm |
| Panel array edited | `superseded` | panel instances, jobs referencing the panel | |
| Machine profile revision bump | `superseded` | jobs, sim results | |
| Feeder assignment contradicted (part no longer fits the assigned tip after a package edit) | `falsified` | that assignment, any job containing it, its validation rows | clears |
| Fiducial fit re-run and rejected (residual over tolerance) | `falsified` | the prior accepted fit and everything that consumed it | clears |
| Source file hash changes under a bound board | `falsified` | the board revision and its whole downstream cone | the artifact no longer describes the file |
| Q1 harness re-run and load fails against a previously passing bundle | `falsified` | the Q1 evidence record and any capability row that cited it | capability drops back to `declared` |

Every result carries the durable `seq` that established it. A notice applies **only** to rows with
`seq <= at_seq`; a row established after the notice is untouched. Enforced in `invalidation.py` and
asserted in `test_invalidation.py`.

---

## 5. Deterministic virtual machine (Lane A emulator)

### 5.1 `VirtualPnpMachine`

Fixed 1 kHz integer ticks; no wall-clock, no RNG without an explicit seed recorded in the result, no
dict-iteration-order dependence. State: per-axis position under a trapezoid point-to-point profile from
`PnpMachineProfile.kinematics`; per-nozzle vacuum boolean; per-nozzle mounted tip; per-feeder remaining
count; tip-changer slot occupancy; camera exposure gate. Executes `PnpJob.steps` in order and emits
tick-stamped `PnpSimEvent`s, streamed as `mfg.pnp.sim_tick` / `mfg.pnp.sim_event`.

Snapshot/restore: `snapshot() -> bytes` / `restore(bytes)` on the whole state tuple, so a run can be
replayed from any tick — matching the twin decision in the decisions brief. Replay from a snapshot MUST
reproduce the same `deterministic_digest` tail.

### 5.2 Fault injection (declared, not physical)

`faults.py` catalogue, each injected by rule at a named step or tick — never sampled from a physics model:
`feeder_empty`, `feeder_pitch_error`, `pick_miss`, `vacuum_leak`, `part_dropped_in_transit`,
`tip_mismatch`, `tip_change_failed`, `part_height_exceeds_nozzle`, `fiducial_not_found`,
`fiducial_residual_exceeded`, `up_vision_no_part`, `up_vision_offset_exceeds_limit`,
`envelope_violation`, `duplicate_refdes_at_runtime`.

### 5.3 `PnpFidelityDeclaration` — mandatory, rendered in the GUI

Breadth-audit abstraction 10 made concrete. Every `PnpSimResult` embeds it and the panel prints it:

```json
{
  "parser": true,
  "resource_state_machine": true,
  "kinematics": "trapezoid_point_to_point_nominal",
  "collision": "axis_aligned_envelope_only",
  "dynamics": false,
  "process_physics": false,
  "vision": "geometric_only_no_image_formation",
  "sensor_faults": "injected_by_rule_only",
  "timing": "nominal_profile_not_controller_accurate",
  "models_missed_picks": false,
  "models_vacuum_physics": false,
  "models_feeder_jams": false,
  "models_nozzle_runout": false,
  "models_tombstoning_or_reflow": false,
  "yield_claim": "none"
}
```

Consequence encoded in `gates.py`: a clean simulation is **context only**. It can satisfy a *procedural*
Stage-1 fact ("check X ran against artifact hash H at seq N", DoD #14) and nothing else. It can never
satisfy an execute-side gate — and PnP has none, because it advertises no execute capability.

---

## 6. Interchange

### 6.1 Import (Lane A, always available)

`centroid.py` detects dialect by header signature, then parses:

| Dialect | Detection | Notes |
|---|---|---|
| `kicad_pos` | `### Module positions` / `# Ref Val Package PosX PosY Rot Side` header | units from the `## Unit = mm/inch` line — no default |
| `altium_pickplace` | `Designator,Comment,Layer,Footprint,Center-X,Center-Y,Rotation` column set | `Layer` → `side`; unit suffix per cell |
| `csv_centroid` | column-name fuzzy map over a fixed synonym table | ambiguous → refusal, never a guess |

Refusals (structured error envelope of `doc_ops.md` §5, as all new worker methods use):
`units_unresolved`, `rotation_convention_unresolved`, `side_column_missing`, `duplicate_refdes`,
`nonnumeric_coordinate`, `nonnumeric_rotation`, `empty_after_filter`. A partial import is never
committed: the reader either produces a whole `BoardRevision` or refuses, with `rejected_rows` populated
in the error detail so the user can fix the file.

BOM join (`bom.py`) is a separate, explicit step: refdes → part → package. Unjoined refdes are reported,
not invented.

### 6.2 Export formats

| `format` | Availability | Content |
|---|---|---|
| `canonical_json` | always | `chudware.pnp.job/1` — the whole model, self-describing, our own schema |
| `csv_centroid` | always | round-trippable centroid CSV; `test_centroid_import.py` asserts export→import is identity |
| `openpnp` | **only when a verified schema profile exists** (§6.3) | a complete OpenPnP config bundle directory |

### 6.3 The OpenPnP writer, without fabricating a schema

The design does not invent OpenPnP XML element names. `export_openpnp.py` is **schema-profile driven**:

- `fixtures/mfg/pnp/openpnp_schema/<version>.profile.json` is a hand-authored, human-verified mapping
  from our model fields to that OpenPnP version's element/attribute names, carrying
  `{openpnp_version, authored_by, authored_at, verified_against:"<install path + sha256>", files:[...]}`.
- `openpnp_schema.py` loads and validates the profile. **If no profile is present for any version,
  `export_pnp_job(format="openpnp")` is a Tier-0 construction refusal** at the action entry point:
  `"no verified OpenPnP schema profile is installed; canonical_json and csv_centroid are available"`.
- The writer emits `machine.xml` (NullDriver + simulated up/down cameras only — the emitter has no code
  path that can write a `GcodeDriver`), `packages.xml`, `parts.xml`, the board file, and the job file.
- The bundle is stamped `openpnp_load_verified: false` until a Q1 run (§7) proves an actual OpenPnP
  binary loaded it. Export never claims verification it does not have.

This is the honest construction: the emitter is real and complete, and the one fact that cannot be obtained from
repo context — the exact element names of a specific OpenPnP release — is a named, human-supplied,
verifiable input rather than a guess baked into code.

---

## 7. Qualification stages

DoD #18 requires hardware-free stages in the normal gate and forbids any capability level exceeding the
highest stage actually passed. DoD #1 permits SKIPs only for an enumerated prerequisite list that does
**not** include an OpenPnP binary. Both are satisfied by keeping every gate-resident stage
dependency-free and moving the third-party stage out of the gate entirely under a distinct name:

| Stage | Name | In `gate.sh`? | Content |
|---|---|---|---|
| E0 | schema conformance | yes | every type round-trips; contract ↔ code shape parity; fixture corpus parses |
| E1 | deterministic virtual run | yes | tick-exact golden event stream; digest stable across runs and process restarts |
| E2 | fault + invalidation exercise | yes | every catalogue fault fires and only when injected; every invalidation trigger produces the right class and seq scope |
| E3 | canonical run/evidence + gate integration | yes | every evidence record emitted with a gateway seq; two-stage gate evaluation matches the expected table; guard liveness proven |
| **Q1** | external OpenPnP conformance | **no — `scripts/qualify_pnp_q1.sh` only** | a user-supplied OpenPnP binary loads a bundle we wrote |

No new SKIP class is introduced, so DoD #1 is untouched — and this matches the final project ruling that
optional OpenPnP/ROS sidecars are opt-in qualification only and add **no** normal-gate SKIP. Per
Gateway's corrected tally the whole manufacturing batch now requests **zero** new SKIPs and the
documented list stays the DoD #1 five (`GW-ZERO-NEW-SKIP`). Absence of the optional binary is a
Tier-0 refusal naming the prerequisite (§8.2), never a SKIP and never a pass. PnP requests none;
`CHUDWARE_OPENPNP_BIN` follows the existing `CHUDWARE_SLICER_BIN` / `CHUDWARE_NGSPICE` /
`OPENMVS_BIN` discovery idiom but is read exclusively by `scripts/qualify_pnp_q1.sh`, and
`test_gates.py` asserts `gate.sh` never invokes it. Q1 is deliberately *not* numbered as an emulator
stage so it cannot be mistaken for something the gate owes.

### 7.1 Q1 harness — probe, never assume

`openpnp_harness.py`:

1. **Discover** — `CHUDWARE_OPENPNP_BIN` (executable, or a `java -jar …` argv), optional
   `CHUDWARE_OPENPNP_HOME`. Absent → `mfg_pnp_harness_run` returns a Tier-0 refusal naming the exact
   missing prerequisite; capability stays where it was.
2. **Probe** — record binary path + sha256 + any version string, then classify its entry mode by parsing
   `--help` output against fixture-backed matchers: `headless_script` | `gui_only` | `unknown`.
   We do not assume any CLI flag exists.
3. **Run** —
   - `headless_script`: generate the bundle into a temp `--config-dir`, invoke the binary's scripted
     entry to load the config and report object counts, compare to expected. Recorded as a
     **machine-checked procedural fact** (§8.3): `{check:"openpnp_bundle_load", bin_sha256, version_text,
     bundle_artifact_hash, expected_counts, observed_counts, exit_code, at_seq}`.
   - `gui_only` / `unknown`: emit a written operator procedure plus the bundle, and accept only an
     `operator_attested` artifact `{attested_by, at, openpnp_version, observed_counts, notes}` —
     a **human-asserted** procedural fact carrying the same fields plus the attestation.
4. **Consequence — corrected against `GW-SRC-3`.** `src` is derived from typed transport
   provenance, not self-asserted: `serial|tcp|websocket|http_adapter -> probed`, `emulator -> simulated`,
   `profile/config -> declared`, `camera/model -> advisory`. A subprocess that loads a config file is
   **none of the probed transports**, so **Q1 can never yield `probed` evidence** — not in the
   `headless_script` path either. The earlier framing of headless Q1 as `probed` was wrong and is
   withdrawn. Both Q1 paths produce `declared`-class procedural facts; the difference between them is
   *auditability* (a binary's own exit code and parsed counts vs. a human's attestation), never truth
   level. `capability.py` therefore caps the `openpnp_interop` sub-capability at `declared` in **both**
   paths, and records which path produced it. This is a structural guarantee rather than a policy: no
   PnP code path constructs a probed-class transport, so PnP evidence could not pass an execute-side
   Stage-1 admission even if someone later added one.
5. **Sandbox** — temp config dir under the app cache, deleted after; bounded wall-clock timeout; output
   captured to the evidence record; a non-zero exit is a recorded failure, never an exception that
   escapes to the caller.

---

## 8. Capability truth, Tier-0 refusals, and gates

### 8.1 Capability claims (`mfg_pnp_capabilities` / `describe_pnp_capabilities`)

Composes Gateway's `CapabilityTruth` from `core/device/capability.h` (`GW-TYPES-OWNED`) — PnP
**never re-declares the level list**. Per-level shape:
`{level, truth: verified|declared|assumed|unsupported|not_applicable, evidence_ref: "res:kind:ulid"
(seq-stamped journal rows only), emulator_stage: E0..E6|none, missing_level (required when assumed),
why (required unless verified)}`. The domain report is Gateway's `CapabilityReport`
`{domain_id:"pnp", levels{}, max_advertised, emulator_stage_passed}`.

| Level | `truth` | `emulator_stage` | `why` (required unless verified) |
|---|---|---|---|
| `plan` | `verified` | E1 | — |
| `generate` | `verified` | E1 | — |
| `post` | `verified` | E0 | scoped `interchange_bundle_not_machine_program`; the `openpnp` sub-format additionally requires a §6.3 schema profile |
| `parse` | `verified` | E0 | — |
| `simulate` | `verified` at fidelity §5.3 | E3 | sub-capability `openpnp_interop` is `declared`, capped there in both Q1 paths (§7.1.4) |
| `monitor` | `unsupported` | none | "pnp has no device path in this wave; no observation source exists" |
| `transport` | `unsupported` | none | "pnp has no device path in this wave; no transport is implemented" |
| `execute` | `unsupported` | none | "pnp has no device path in this wave; no execute code exists" |
| `safety_integration` | `unsupported` | none | "never unlocked by any emulator stage; pnp performs no protective function" |

Per `GW-TYPES-OWNED` these four are `unsupported` **with a `why`**, deliberately *not* `not_applicable`.

**Ceiling check (`GW-EMULATOR-CEILING`).** `emulator_stage_passed = E3` → ceiling `monitor` (E0–E3 → monitor; E4–E5 →
transport; E6 → execute; `safety_integration` is never unlocked by any stage). PnP's
`max_advertised = simulate`, a deliberate stop one rung below the ceiling, so the report is conformant
with headroom. Q1 is **not** an emulator stage and never advances `emulator_stage_passed`; it attaches
only as an `evidence_ref` on `simulate`. `test_capability_truth.py` asserts the ceiling arithmetic
directly rather than trusting the table.

`assumed` disables actions exactly like absent and names the missing level (DoD #2): `capability.py`
returns `truth:"assumed"` with `missing_level` populated and `disables:[actions]`, and the MCP and GUI
surfaces both refuse rather than degrade.

**Command owner:** `{"device_command_owner": null, "why": "pnp has no device path in this wave"}` is an
explicit field, not an omission.

**The report is itself journaled (`GW-CAP-KIND-REQUIRED`, `GW-CAP-DERIVED-FROM-EVIDENCE`).** PnP advertises five levels above `unsupported`,
so it MUST have a registered `pnp.capability` durable kind or registration is refused
`mfg_capability_requires_durable_kind`. It does (§4.2, eleventh kind). The operative consequence is
not the row's existence but its `derived_from`: the report cites its evidence rows, so when any cited
row is falsified or superseded PnP re-derives and appends a fresh report and the closure makes the old
one non-current. **A level cannot outlive the evidence under it** — there is no path where `simulate`
stays `verified` on a falsified E3 result, which is the whole point of the rule.

### 8.2 Tier-0 construction refusals (before a proposal or preflight exists)

Rows conform to Gateway's register row from `core/manufacturing/tier0.h` (`GW-TYPES-OWNED`):
`{action, prerequisite, class:"PREREQ", artifact_dependent, applies_when:{artifact_kind[],
machine_class[]}, why, missing_level?, remedy[]}`. `class` is **always** `PREREQ` — a whole action
prerequisite can never be `not_applicable`, missing evidence fails, and the refusal renders at the
action entry point before any proposal exists. PnP ships its rows as a **declarative Python table** in
`services/manufacturing/pnp/tier0.py` and registers them at domain init through the gateway-owned
`journal_client.register_tier0`, which calls
`mfg_tier0_register{domain_id:"pnp", schema_version, rows[]} -> {registered, rejected[{row_index,why}],
totality{covered, missing[{action,artifact_kind,machine_class}]}, at_seq}` (`GW-TIER0-REGISTER`, closing PnP's
earlier open item — Gateway took both proposed options and combined them). Registration is **atomic
and idempotent** per `(domain_id, action, prerequisite, hash(applies_when))`: a partially invalid batch
registers nothing and reports per-row reasons, and any `class` other than `PREREQ` is rejected outright.
Gateway owns the type, runs the totality check over `action × artifact_kind × machine_class`, returns
the uncovered tuples, and owns the rendering contract. A later guard catching the same condition is
defense-in-depth, asserted separately, never the substitute.

| `action` | `prerequisite` | `artifact_dependent` | `applies_when.artifact_kind` | `remedy[0]` |
|---|---|---|---|---|
| any board-dependent action | `board_revision_bound` | yes | all | "import a centroid file" |
| any job action | `machine_profile_selected` | no | all | "select a virtual machine profile" |
| `mfg_pnp_job_build` | `every_enabled_placement_has_part_and_package` | yes | `board`, `panel` | "map the N unresolved placements" |
| `mfg_pnp_job_build` | `units_and_rotation_resolved_non_assumed` | yes | `board`, `panel` | "re-import with a unit header, or set units explicitly" |
| `mfg_pnp_export` | `openpnp_schema_profile_verified` | yes | `job` when `format=="openpnp"` | "author a schema profile (§6.3), or export canonical_json/csv_centroid" |
| `mfg_pnp_harness_run` | `openpnp_binary_present` | no | `job` | "set CHUDWARE_OPENPNP_BIN to your own OpenPnP install" |
| `mfg_pnp_fiducial_fit` | `two_or_more_distinct_fiducials` | yes | `board`, `panel` | "add a second non-coincident fiducial" |
| any device verb (`jog/home/pick/place/feed/vacuum/enable/park_physical`) | `device_path_exists` | no | all | **no remedy in this wave** — hard refusal, no model allow path |

Missing evidence **fails**; it is never `not_applicable`. The last row has an intentionally empty
remedy set: there is no action the user can take to obtain a device path here, and offering one would
be a lie.

### 8.3 Two-stage gates

Only one action in this track is gate-worthy: publishing an interchange artifact
(`mfg_pnp_export` / `export_pnp_job`).

**Stage 1 (necessary, never sufficient).** Corrected against `GW-SRC-3`: because `src` derives
from typed transport and PnP has no probed-class transport, **PnP produces no `probed` evidence and
therefore has no execute-side gate.** The export action is a pure authoring action, so its Stage-1 set
is built entirely on **procedural facts** — the one thing DoD #14 explicitly permits Gateway to gate on:
that a named check ran against a specific artifact/config/snapshot/seq. Each row is a
`ProceduralFact{check_name, artifact_hash, config_epoch, snapshot_id?, at_seq, outcome}` carrying
`admission:"procedural_fact"`, so it can never be read as, promoted to, or substituted for probed
device evidence. Of the two admission classes (`GW-ADMISSION-CLASSES`) PnP produces exactly one:
it emits no `probed_evidence` at all, and `procedural_fact` is admissible here only because every PnP
guard declares it in `accepts.admission` (`GW-GUARD-ACCEPTS`) over an `art:`/`job:`/`res:` subject.
No PnP subject is execute-side, which is the other half of that rule and the reason PnP never reaches
the boundary it forbids.

`pnp.board_revision_bound` · `pnp.source_files_hash_match` · `pnp.units_resolved` ·
`pnp.rotation_convention_resolved` · `pnp.no_duplicate_refdes` ·
`pnp.every_enabled_placement_has_part_and_package` · `pnp.every_part_has_feeder_or_named_waiver` ·
`pnp.every_feeder_part_tip_compatible` · `pnp.fiducials_present_for_scope` ·
`pnp.envelope_contains_all_pick_and_place_points` ·
`pnp.simulate_e1_ran_clean_for_this_artifact_hash`.

**Scope limit on procedural facts (`GW-PF-SCOPE-4`).** A `procedural_fact` row may flip only
`plan | generate | post | parse | simulate` to `verified`. `monitor | transport | execute |
safety_integration` require fresh probed evidence plus the emulator-stage ceiling and can **never** be
flipped by a procedural fact, nor by simulated, declared, advisory, or assumed evidence. PnP is
comfortably inside that boundary: the only levels it advertises above `unsupported` are the five the
rule permits, and its Q1 procedural facts move nothing higher than the `openpnp_interop`
sub-capability's `declared`. `test_capability_truth.py` asserts the negative directly — feeding a
passing procedural fact for each of the four device-side levels must leave every one of them
`unsupported`.

Every row is stale-scoped to the artifact hash and config epoch it names: any edit to a bound input
falsifies the fact (§4.3) rather than ageing it. Unknown / stale / refused fail closed. A simulated,
advisory, or assumed value is never admitted to any row. `unsupported` may become `not_applicable` only
when independently corroborated for the same subject/config epoch by fresh evidence.

**Stage 2** — `pass | fail | not_applicable`, no unknown. `not_applicable` is only ever a missing
*corroborating sub-condition* (e.g. `panel_fiducial_fit_within_tolerance` when the fit used the optional
local-fiducial refinement that this board does not carry), never a whole action prerequisite, and never
counts as pass. Machine-class irrelevant checks are **omitted from the required set** entirely:
`bottom_side_placements_reachable` is omitted for a single-sided board; `panel_*` rows are omitted for a
single-board job.

**Waivers** are durable `{by, reason, at_seq}` and the aggregate waiver rate per gate is exposed on
`mfg_pnp_validate` so the GUI can show it.

**Guard liveness (DoD #8)** — `test_gates.py` proves, for every guard, one fixture that makes it pass and
one that makes it fail, and that every gate reaches `pass` from at least one fixture set. Deliberately
absent guards (there is no execute-side gate) are documented with the reason.

---

## 9. Surfaces

### 9.1 Worker RPC — `mfg_pnp_*`

Served by `services/manufacturing/pnp/rpc.py` exporting `WORKER_METHODS`, registered into the doc worker
exactly like `drawing.WORKER_METHODS` (`contracts/worker_rpc.md` §Drawings). Newline-delimited JSON over
stdio, protocol 2, **structured error envelope** (all new methods). Read-only methods never mutate.

| Method | Params → Result |
|---|---|
| `mfg_pnp_capabilities` | `{}` → `{profile_id?, levels:{…nine…}, device_command_owner:null, why, fidelity, stages:{E0..E3,Q1}}` |
| `mfg_pnp_board_import` | `{paths:[str], bom_path?, board_name?, revision?}` → `{board:BoardRevision, rejected_rows, seq}` |
| `mfg_pnp_board_get` | `{board_id}` → `{board:BoardRevision}` |
| `mfg_pnp_panel_define` | `{board_id, array, skips?, panel_fiducials?, rail_mm?}` → `{panel:PanelDefinition, seq}` |
| `mfg_pnp_package_upsert` | `{packages:[Package]}` → `{library:PackageLibrary, seq}` |
| `mfg_pnp_placement_list` | `{board_id, side?, enabled_only?}` → `{placements:[Placement], counts}` |
| `mfg_pnp_placement_set` | `{board_id, updates:[{id, …patch}]}` → `{placements, invalidations:[Invalidation], seq}` |
| `mfg_pnp_resource_list` | `{profile_id}` → `{feeders, nozzles, tips, cameras}` |
| `mfg_pnp_resource_set` | `{profile_id, feeders?, nozzles?, tips?, cameras?}` → `{profile, invalidations, seq}` |
| `mfg_pnp_resolve_feeders` | `{board_id\|panel_id, profile_id, policy?}` → `{assignments:[{part_id, feeder_id, tip_id}], unassigned:[part_id], conflicts:[…]}` |
| `mfg_pnp_fiducial_fit` | `{subject_id, observations:[{fiducial_id, measured_mm:[x,y], source:Provenance}], kind?, tolerance_mm?}` → `{fit:FiducialFitResult, seq}` |
| `mfg_pnp_validate` | `{board_id\|panel_id, profile_id}` → `{tier0:[…], stage1:[GateRow], stage2:[GateRow], waivers, waiver_rate, verdict?}` |
| `mfg_pnp_job_build` | `{board_id\|panel_id, profile_id, sequence_policy?}` → `{job:PnpJob, seq}` |
| `mfg_pnp_simulate` | `{job_id, faults?:[str], seed?, from_snapshot?}` → streams `mfg.pnp.sim_tick`/`sim_event` → `{result:PnpSimResult, seq}` |
| `mfg_pnp_export` | `{job_id, path, format:"canonical_json"\|"csv_centroid"\|"openpnp"}` → `{path, sha256, format, openpnp_load_verified:bool, gate_report, seq}` |
| `mfg_pnp_harness_describe` | `{}` → `{present:bool, bin_path?, sha256?, version_text?, entry_mode:"headless_script"\|"gui_only"\|"unknown", missing_prereq?}` |
| `mfg_pnp_harness_run` | `{job_id, timeout_s?}` → `{outcome, evidence:PnpEvidenceRecord, stdout_tail, exit_code}` |
| `mfg_pnp_harness_status` | `{}` → `FreshValue<HarnessState>` |
| `mfg_pnp_evidence_get` | `{run_id?, subject_id?, kind?}` → `{records:[PnpEvidenceRecord]}` |

`verdict` on `mfg_pnp_validate` appears **only** when the complete check set ran (DoD #3); it is computed,
revision/artifact bound, and flips to unknown on any edit to a bound input.

Result fields the GUI paints cross this seam wrapped in `FreshValue<T>` (`GW-FRESHVALUE-PUBLISH`), the
durable authored ones with `state:"ok"` and `ttl_ms:null` per §4.1; the `seq` in each envelope is the
journal-stamped one returned by the append, never a locally chosen number.

### 9.2 MCP tools — shared family `"manufacture"`

Per the final project ruling, the entire manufacturing overhaul registers under **one** shared family id
`"manufacture"` — the product term, not the `mfg` wire prefix — and that family is **OFF by default in
`agent.tool_families`** until commissioning, so `enable_tools` cannot silently self-add manufacturing
surface. Default-off is **not** achieved by omission: an unlisted family resolves *enabled*, so the
mechanism is the `DEFAULT_TOOL_FAMILIES` + `DEFAULT_OFF_TOOL_FAMILIES` pair in
`services/agent/config.py`. That file and `test_manufacture_family_default_off.py` are **Gateway-owned
in W1.1** and are a hard predecessor of this registration (`GW-FAMILY-MANUFACTURE`); PnP edits neither
and asserts the resulting property from the outside (§11, `test_mcp_registration.py`). Per-tool toggles retain granularity via the existing `get_tool_options` path, so nothing is
lost by collapsing the family. The earlier per-domain family `"pnp"` is withdrawn. Registered via
`register_pnp_tools(_reg.view(mcp, "manufacture"))`. Risk classes are chosen so
`tool_registry.classify_risk` yields the intended value from the name alone, with three explicit
overrides where it would not:

| Tool | Risk | How |
|---|---|---|
| `list_pnp_boards`, `list_pnp_feeders`, `list_pnp_nozzles` | READ | `_READ_PREFIXES` `list_` |
| `describe_pnp_board`, `describe_pnp_capabilities` | READ | `describe_` |
| `check_pnp_job` | READ | `check_` |
| `get_pnp_placement_plan`, `get_pnp_harness_status` | READ | `get_` |
| `pnp_define_panel`, `pnp_set_feeder_assignment` | WRITE | classifier default (fail-safe) |
| `pnp_simulate_job` | WRITE | classifier default; it writes a durable evidence record |
| `import_pnp_centroid` | EXTERNAL | `_EXTERNAL_PREFIXES` `import_` |
| `export_pnp_job` | EXTERNAL | `_EXTERNAL_PREFIXES` `export_` |
| `run_pnp_harness` | EXTERNAL | **override required** — spawns a third-party process |

Three `_RISK_OVERRIDES` rows are supplied as **data to Gateway**, the single owner of
`services/mcp/tool_registry.py` for the batch (`GW-RISK-TABLE-OWNER`); PnP appends nothing to that
file itself: `"run_pnp_harness": RISK_EXTERNAL`, `"pnp_simulate_job": RISK_WRITE`,
`"pnp_define_panel": RISK_WRITE` (the latter two pinned explicitly so a future classifier change
cannot silently reclassify them).

There is **no** MCP tool for any device verb. Motion/safety risk is explicit-membership and never
lexical: PnP contributes **no** `RISK_MOTION` member, no `request_mfg_*` tool, and no safety-class tool
at all — absence, not denial. `test_no_device_path.py` asserts PnP exposes none of them.

### 9.3 Events — `mfg.pnp.*`

`import_progress` · `validation` · `sim_tick` · `sim_event` · `harness_state` · `invalidated` ·
`evidence`. Each carries `{seq, subject_id, subject_revision}`; `invalidated` carries the DoD #9 envelope
verbatim.

### 9.4 GUI — `host/src/ui/manufacture/pnp_panel.{h,cpp}`

Read-only. Consumes JSON already crossing the worker-RPC seam; adds no C ABI symbol and no new canvas.
Mirrors the `host/src/ui/cam/cam_panel.h` shape (integrated left-rail bodies that draw inside their
caller's surface, plus a native viewport overlay accessor):

```cpp
namespace ui {
struct PnpOverlay;                                   // board outline, placements, feeder banks
void pnpLoadBoard(AppState& st, const std::string& boardId);
void pnpHandleCommand(AppState& st, const std::string& cmd);   // read-only commands only
void drawPnpTreeBody(AppState& st);                  // board / panel / placements / resources
void drawPnpOutputBody(AppState& st);                // validation, gates, evidence, invalidations
void drawPnpCapabilityTable(AppState& st);           // the nine truth levels + fidelity + boundary
const PnpOverlay* pnpActiveOverlay();
void pnpAdoptSimResult(const nlohmann::json& result);
void pnpDrawViewportHud(AppState& st, const ImVec2& vpMin, const ImVec2& vpSize);
void pnpDebugSetPlayhead(float frac);                // shot-only, matching camDebugSetPlayhead
}  // namespace ui
```

Mandatory rendering rules: the fidelity declaration and the "OpenPnP not included / no device path"
statement are always visible on the panel, not behind a disclosure; `FreshValue` states render honestly
(`unknown`/`stale` never paint `last_ok` as live); `assumed` capability rows render disabled with the
missing level named; superseded rows are retained and greyed with a forward pointer, falsified rows are
cleared.

**Every painted row arrives wrapped (`GW-FRESHVALUE-PUBLISH`).** The panel has no unwrapped read path,
including for the capability table: `pnp.capability` and every other durable authored row paints from a
`FreshValue<T>` with `state:"ok"`, `ttl_ms:null` and the journal-stamped `seq`, so the panel cannot age
an authored artifact out by clock — a durable row leaves the screen only when the gateway's closure
falsifies it (cleared) or supersedes it (retained, greyed, forward-pointed). The row's identity on the
wire and in the journal remains `seq` + `revision` + `artifact_hash`; the envelope carries no identity
and the panel keys nothing on `age_ms`.

---

## 10. Test fixtures — `fixtures/mfg/pnp/`

```
kicad_pos/simple_top.pos                  kicad_pos/simple_bottom.pos
kicad_pos/unit_mm_header.pos              kicad_pos/unit_inch_header.pos
kicad_pos/no_unit_header.pos              # must REFUSE, never default
altium/pick_place_all.csv                 altium/pick_place_top_only.csv
csv/generic_designator_x_y_rot_side.csv   csv/comma_decimal_locale.csv
csv/missing_side_column.csv               csv/duplicate_refdes.csv
csv/nonnumeric_rotation.csv               csv/ambiguous_columns.csv
bom/simple_bom.csv                        bom/unjoined_refdes.csv
boards/two_fiducial_board.json            boards/three_fiducial_board.json
boards/collinear_fiducials.json           boards/no_fiducial_board.json
boards/double_sided_board.json
panels/2x3_step_repeat.json               panels/2x3_with_skips.json
machine/virtual_null_profile.json         machine/undersized_envelope_profile.json
machine/no_tip_for_bga_profile.json       machine/single_nozzle_profile.json
packages/base_library.json                packages/bga_requires_large_tip.json
jobs/golden_job_canonical.json            jobs/golden_job_canonical.sha256
sim/expected_ticks_golden_job.jsonl       sim/expected_digest_golden_job.txt
sim/faults/<one file per catalogue fault>.json
harness/help_headless_script.txt          harness/help_gui_only.txt
harness/help_unparseable.txt              harness/attestation_example.json
openpnp_schema/README.md                  # how to author + verify a profile; no profile checked in
expected/canonical_json/golden_job.json   expected/csv_centroid/golden_job.csv
evidence/expected_evidence_records.json
gates/expected_stage1_pass.json           gates/expected_stage1_fail_matrix.json
gates/expected_stage2_matrix.json
invalidation/expected_notices.json
```

`openpnp_schema/` intentionally ships **no** profile: the gate must prove that `format="openpnp"` is
Tier-0 refused in a clean checkout, and that the refusal names the missing prerequisite.

The harness fixtures make the entire Q1 probe/classification logic testable **with no OpenPnP installed**,
which is why Q1's decision code is gate-covered even though Q1 itself is not.

---

## 11. Tests

All under `services/manufacturing/pnp/`, pure Python, no native build, no network, no third-party binary.

| File | Contract it defends |
|---|---|
| `test_model_roundtrip.py` | every type round-trips JSON; unknown keys tolerated, never silently dropped from a re-serialize |
| `test_centroid_import.py` | every dialect fixture parses to the expected board; export→import is identity for `csv_centroid` |
| `test_units_provenance.py` | `no_unit_header.pos` refuses; units are never defaulted; `assumed` provenance blocks job build |
| `test_import_refusals.py` | duplicate refdes / missing side / nonnumeric / ambiguous → structured error with `rejected_rows`; no partial commit |
| `test_bom_join.py` | unjoined refdes reported, never invented |
| `test_panel.py` | step-and-repeat instance transforms exact; skips honoured; instance count matches |
| `test_fiducial_fit.py` | exact recovery of a synthesized rigid and similarity transform to 1e-9; collinear input rejected with `reject_reason`; residual over tolerance → not accepted; `source:"simulated"` observations stay `simulated` and are never admitted to a Stage-1 row |
| `test_resources.py` | package↔tip↔feeder compatibility; unassigned parts enumerated; waiver `{by,reason,at_seq}` durable; waiver rate computed |
| `test_job_build.py` | step ordering per policy; `artifact_hash` stable and input-sensitive; a changed input changes the hash |
| `test_virtual_machine.py` | tick-exact match to the golden stream; identical `deterministic_digest` across two in-process runs **and** two separate processes; snapshot/restore reproduces the digest tail; no wall-clock or unseeded RNG dependence |
| `test_fault_injection.py` | each catalogue fault fires exactly when injected and never otherwise; a clean run emits none |
| `test_fidelity_declaration.py` | the declaration is present on every result and asserts the negatives (no physics, no yield claim) |
| `test_export_canonical.py` / `test_export_csv.py` | byte-golden output; sha256 recorded in the result matches the file |
| `test_export_openpnp_refusal.py` | with no schema profile: Tier-0 refusal naming the prerequisite; the other two formats still succeed |
| `test_export_openpnp_writer.py` | with a synthetic test profile: byte-golden bundle; the writer has no code path that emits a non-NullDriver driver |
| `test_harness_probe.py` | the three help fixtures classify correctly; absent binary → Tier-0 refusal naming `CHUDWARE_OPENPNP_BIN`; **neither** Q1 path ever emits `probed`; both cap `openpnp_interop` at `declared`; the path that produced it is recorded |
| `test_capability_truth.py` | the report composes Gateway's `CapabilityTruth`/`CapabilityReport` and re-declares no level list; monitor/transport/execute/safety_integration are `unsupported` **with a `why`**, never `not_applicable`; `why` present unless verified and `missing_level` present when assumed; `max_advertised` ≤ the ceiling implied by `emulator_stage_passed` (asserted arithmetically); Q1 never advances `emulator_stage_passed`; feeding a passing procedural fact for each device-side level leaves all four `unsupported`; `device_command_owner` is explicitly null with a reason. **`GW-CAP-KIND-REQUIRED` / `GW-CAP-DERIVED-FROM-EVIDENCE`:** the painted table is backed by a journaled `pnp.capability` row (no unjournaled UI truth surface); that kind is `epoch_invariant` and does **not** set `supersede_with_parent`; advertising a level above `unsupported` with the kind unregistered is refused `mfg_capability_requires_durable_kind`; and — the sharp one — **falsifying any single cited evidence row produces a new capability row AND drops the affected level**, asserted per level, so no level can stay `verified` on falsified evidence |
| `test_tier0.py` | every row validates against Gateway's row schema with `class=="PREREQ"`; `mfg_tier0_register` returns `registered` with an empty `rejected[]` and an empty `totality.missing[]`; a deliberately malformed batch registers **nothing** and reports per-row reasons (atomicity); re-registering the identical table is idempotent; a non-`PREREQ` class is rejected; every row refuses at the action entry point before any proposal exists; missing evidence fails and is never `not_applicable`; the device-verb row offers no remedy; the downstream guard for the same condition is asserted separately as defense-in-depth |
| `test_gates.py` | every Stage-1 row carries `admission:"procedural_fact"` and none claims `probed`; every guard declares `accepts{subject_ns,admission}` and `gates::evaluate` refuses out-of-set evidence **by name** — including a fresh `probed` `src:` observation fed into every PnP guard, and every namespace/admission class a guard did not declare; no execute-side gate exists and none can be constructed from PnP evidence; simulated/advisory/assumed/stale/unknown are never admitted; an edit to a bound input falsifies the fact rather than ageing it; Stage 2 has no `unknown`; `not_applicable` never counts as pass and never covers a whole prerequisite; irrelevant checks omitted, not passed; guard liveness (each guard passes on one fixture and fails on another; each gate reaches pass from ≥1 fixture set); `gate.sh` never invokes `scripts/qualify_pnp_q1.sh` |
| `test_invalidation.py` | each trigger yields the right class; every notice is emitted through `mfg_result_invalidate` and uses the `at_seq` it returns, never a locally chosen one; `mfg.pnp.invalidated` mirrors the call rather than sourcing it; falsified clears, superseded retains and points forward without alarm; notices apply only to `seq <= at_seq`; producers push without polling |
| `test_freshvalue.py` | the envelope shape is exact; `v` null unless `state=="ok"`; sequence gap forces unknown; **every value a GUI surface paints is wrapped** — asserted by enumerating the panel's read set, including `pnp.capability`, and failing on any unwrapped field; a durable authored row publishes with `ttl_ms:null` and `state:"ok"` and its wrapped `seq` equals the `seq` returned by `mfg_event_append`; **no** durable authored row carries a wall-clock `ttl_ms` or is reachable by a clock-driven `stale` transition; persistence identity is `seq`+`revision`+`artifact_hash` and no code path keys, joins, supersedes or invalidates on `age_ms`, `t_mono_ns` or `ttl_ms`; only the three decaying rows in §4.1 carry a non-null `ttl_ms` |
| `test_evidence.py` | every durable producer obtains its `seq` from `mfg_event_append` via the gateway-owned `journal_client`; no module mints, derives, or synthesizes a `seq`, and none is copied from another record; every emitted `result_kind` appears in the durable-result-kind table declaring **both** columns (epoch policy **and** runtime triggers), and a kind declaring one column is a schema rejection; an unregistered kind is refused `mfg_unregistered_result_kind`; every append supplies its declared `derived_from[]` and PnP never populates `affected[]` itself; PnP never sends `src`/`admission` as caller input; `journal_client.py` is imported, never vendored or re-implemented under `services/manufacturing/pnp/` |
| `test_no_device_path.py` | no module constructs a serial/socket connection; the only `subprocess` call site is the Q1 harness and it is env-gated; every device verb is refused with no model allow path; PnP registers no `RISK_MOTION`, no `request_mfg_*`, and no safety-class tool |
| `test_licensing_boundary.py` | no OpenPnP source/jar/class in the repo; no JVM binding import; no `org.openpnp` reference outside documentation and fixture help text |
| `test_rpc_contract.py` | every `mfg_pnp_*` method in `contracts/mfg_pnp.md` exists and every registered method is documented; param/result shapes match |
| `test_mcp_registration.py` | family is `manufacture` (not a per-domain family) and resolves **disabled** with an empty config, with `role_tools` set, and with `tool_families` absent — the property Gateway's `services/agent/config.py` edit establishes (`GW-FAMILY-MANUFACTURE`), asserted here from the outside; per-tool toggles still resolve; each tool's risk class equals the table in §9.2 and `classify_risk` is unchanged for every pre-existing tool; `run_pnp_harness` is EXTERNAL |

### Smokes (run the thing, not a test file)

- `scripts/smoke_pnp_offline.sh` — gate-resident, dependency-free end-to-end:
  import `kicad_pos/simple_top.pos` + `bom/simple_bom.csv` → `panel_define(2x3 with skips)` →
  `resolve_feeders` → `fiducial_fit` → `validate` → `job_build` → `simulate` →
  `export(canonical_json)` and `export(csv_centroid)`; asserts the golden artifact hashes, the expected
  evidence records, and that `export(openpnp)` is refused with the prerequisite named.
- `scripts/qualify_pnp_q1.sh` — opt-in; requires `CHUDWARE_OPENPNP_BIN`; runs the harness against the
  golden job and writes the Q1 evidence artifact. **Never invoked by `scripts/gate.sh`.**

---

## 12. Dependencies on peer tracks

| Peer | What PnP needs | What PnP gives |
|---|---|---|
| `GatewayDesignRecovery` | **ACCEPTED, closed — no open items.** Owns `CapabilityTruth`/`CapabilityReport` (`core/device/capability.h`), the Tier-0 register row (`core/manufacturing/tier0.h`), the journal (`mfg_event_append`), Tier-0 registration (`mfg_tier0_register`), invalidation (`mfg_result_invalidate`), the shared `journal_client.py`, the `FreshValue<T>` publisher, and the run/evidence registry | a producer that pushes invalidations, never polls, mints no `seq`, sends no `src`/`admission`, and constructs no probed-class transport |
| `TwinDesignRecovery` | the shared integer-tick convention and snapshot/restore semantics if published; otherwise `VirtualPnpMachine` is self-contained at 1 kHz | a second conforming tick-domain producer |
| `MonitoringArchitecture` | nothing at runtime | if a PnP machine is ever surfaced through MTConnect/OPC UA it is their `ObservationRecord` over their `IReadTransport`, and post-refinement it is genuinely `src=probed` — not "context only". It still cannot lift a PnP capability level or enter a PnP gate, but by **subject mismatch, not evidential weakness**: it measures a physical-machine subject, whereas every PnP Stage-1 row is `admission:"procedural_fact"` bound to an artifact hash, and PnP's `monitor` level is `unsupported` because *PnP* owns no observation source. PnP never widens such an observation past its own measured subject |
| `TurningArchitecture`, `CuttingProcessArchitecture` | nothing; PnP touches no `core/cam/**` file and no `cam_tools.py` seam | shared `CapabilityTruth` + Tier-0 row consumption, so the four tracks render identically |
| `RobotArchitecture` | nothing | the same "planning/emulation only, no device path" posture, so the two `Next`-tier tracks stay consistent |

### 12.1 Cross-track types and the journal seam — resolved, no open items

`GatewayDesignRecovery` accepted ownership of the shared types (`GW-TYPES-OWNED`) and then closed PnP's
one remaining blocker (`GW-TIER0-REGISTER`, `GW-CLIENT-5`, `GW-SEQ-1`). PnP **composes, never re-declares**: `CapabilityTruth` /
`CapabilityReport` from `core/device/capability.h`, and the Tier-0 register row from
`core/manufacturing/tier0.h`. Four tracks (pnp, twin, cut, turn) now compose rather than duplicate,
which is what makes the DoD truthfulness rules mechanically enforceable instead of per-track prose.

**Blocker closed.** PnP refused to reimplement `registerRows` or the `seq` stamp on the Python side,
because that would recreate the duplication the ownership decision eliminated and a locally minted
`seq` would violate `GW-SEQ-1` outright. Gateway resolved it by combining both proposed options:
a declarative Python table registered through the service seam PnP already calls. PnP now
ships `services/manufacturing/pnp/tier0.py` as data and calls the gateway-owned
`services/manufacturing/rpc/journal_client.py` (`append` / `invalidate` / `register_tier0`) at domain
init. That helper is gateway-owned and is imported, never copied per domain — `test_evidence.py`
asserts no vendored copy exists under the PnP tree.

**The part worth keeping visible:** `GW-SRC-3` makes `src`/`admission` gateway-computed and
caller-unsettable, with `probed` reachable only from transport code inside the daemon holding an open
`ITransport`. That means a pure-Python domain **structurally cannot** produce probed evidence. The
withdrawn Q1-as-probed claim is now impossible to reintroduce by accident rather than merely
documented as wrong — which is a strictly better outcome than a correction made by hand, and the
reason it was worth reporting the error rather than quietly fixing the prose.

---

## 13. Non-goals — explicit and durable

1. **No physical actuation of any pick-and-place machine.** No motion, homing, jogging, machine enable,
   feeder actuation, vacuum or GPIO control, nozzle/tip change on hardware. No serial, USB, or network
   transport to a placement controller. `Feeder.actuation` and `PnpMachineProfile.driver` are
   single-value literals so this cannot be defeated by configuration.
2. **No OpenPnP embedding.** No source, jar, class, JNI, JVM-in-process, plugin built against it, or
   redistribution. Lane B is a user-supplied out-of-process binary only. Revisiting this requires
   counsel sign-off on the exact boundary plus a source-offer process.
3. **No `monitor`, `transport`, `execute`, or `safety_integration` capability** at any level, and no
   device command owner — stated as an explicit null with a reason, not an omission.
4. **No real-camera vision.** Simulated cameras are geometric only; there is no image formation, no
   bottom-vision image processing, no template matching against pixels, and no closed-loop placement
   correction from a physical camera. Camera-derived results remain advisory and can never gate.
5. **No feeder inventory authority.** `remaining_count` carries `remaining_source` and is `declared` or
   `unknown` unless counted by a source this wave does not have.
6. **No yield, first-pass-rate, or placement-accuracy claims.** The emulator models no missed picks,
   vacuum leaks (except as declared injections), feeder jams, nozzle runout, tombstoning, paste, or
   reflow, and says so in every result.
7. **No SMT line integration** — no stencil printer, SPI, AOI, reflow profile, conveyor handoff, or
   line-level scheduling.
8. **No safety function.** Nothing in this track is guarding, interlock, E-stop, or a protective
   function, and no simulation result may be presented as evidence that a physical placement operation
   is safe.
9. **No `scripts/gate.sh` change and no new SKIP class.** Q1 stays outside the gate specifically so
   DoD #1's enumerated prerequisite list is untouched.
10. **No pilot stage.** There is no E6 for PnP in this wave and no path by which `execute` could be
    advertised, because no execute code exists to advertise.

---

## 14. Review closure — architecture plan review

Every row below is a change **already applied in this document**, not a plan.

| finding | applies to PnP? | how it is closed here | where |
|---|---|---|---|
| **C8** — capability rows `FreshValue`-wrapped by three domains, never-wrapped by PnP | **yes — PnP is the named divergence** | §4.1 rewritten to the ruling: a durable authored row is **journaled first, then published wrapped**, `state:"ok"` + `ttl_ms:null` for rows that do not decay; the old "not a freshness envelope" prose narrows to **"no wall-clock TTL and no envelope-derived identity"**; persistence identity stays `seq` + `revision` + `artifact_hash` and nothing keys on `age_ms`/`t_mono_ns`/`ttl_ms`. `contracts/mfg_pnp.md` states both directions (never wall-clock-TTL'd, never painted unwrapped). `test_freshvalue.py` inverts from "durable artifacts are not wrapped" to enumerating the panel read set and failing on any unwrapped field | §4.1, §9.1, §9.4, §11 |
| **C8 consequence** — `pnp.capability` is a durable row the GUI paints | yes | it is the eleventh registered kind (§4.2) **and** a wrapped published row (§9.4); it can therefore be neither an unjournaled UI truth surface nor an unwrapped one | §4.2, §8.1, §9.4 |
| **B10 / kind drift** — the durable-result-kind table must be total over every appending domain | already compliant, restated as a hard prerequisite | eleven `pnp.*` kinds, each declaring **both** columns (`GW-KIND-COLUMNS`), `derived_from[]`, and a one-line declared `supersede_with_parent` reason (`GW-KIND-REASON`); `pnp.q1_procedural_fact` sets it `true` (`GW-PF-FOLLOWS-PARENT`) and `pnp.capability` must not (`GW-CAP-NO-INHERIT`); an unregistered kind is refused `mfg_unregistered_result_kind` (`GW-KIND-2`) | §4.2, §2 item 6 |
| **B11** — the `manufacture`-family-off-by-default mechanism has no owner | yes, as a **consumer** | §9.2 now states that default-off is not achievable by omission, that the `DEFAULT_TOOL_FAMILIES` + `DEFAULT_OFF_TOOL_FAMILIES` pair in `services/agent/config.py` is **Gateway-owned in W1.1** and a hard predecessor of PnP's MCP registration, and that PnP edits neither that file nor its test — it asserts the resulting property from outside | §9.2, §11 |
| **B12 / family drift** — one shared MCP family | already compliant | `register_pnp_tools(_reg.view(mcp, "manufacture"))`; the earlier per-domain `"pnp"` family stays withdrawn; granularity via `get_tool_options` per-tool toggles | §9.2 |
| **B14** — five claimants on `services/mcp/tool_registry.py` | yes, as one of the five | PnP's three `_RISK_OVERRIDES` rows are **data handed to Gateway**, the single owner of that file for the batch (`GW-RISK-TABLE-OWNER`); PnP appends nothing to it, and the shared acceptance check is `classify_risk` unchanged for every pre-existing tool | §2 item 3, §9.2, §11 |
| **B1** — Stage-1 admission classes | yes, as a consumer | §8.3 names the two classes explicitly: PnP emits **no** `probed_evidence`, its whole Stage-1 set is `procedural_fact` rows admissible only because every guard lists the class in `accepts.admission`, and **no PnP subject is execute-side** | §8.3 |
| **B6/B7/B8/B9** — probed-for-computed-report, re-declared `CapabilityTruth`, self-minted `seq`, self-computed closure | not PnP defects; compliance restated with rule ids | `src`/`admission` gateway-computed (`GW-SRC-3`); `CapabilityTruth`/`CapabilityReport` composed verbatim, `probed` never used as a truth value (`GW-TYPES-OWNED`); `mfg_event_append` is the only source of a PnP `seq` (`GW-SEQ-1`); PnP supplies `derived_from[]` and never populates `affected[]` (`GW-CLOSURE`) | §4.2, §4.3, §8.1 |
| **Zero new normal-gate SKIP** | already compliant, now id-bound | Q1 runs only from `scripts/qualify_pnp_q1.sh`, never from `gate.sh`; absence of `CHUDWARE_OPENPNP_BIN` is a Tier-0 refusal naming the prerequisite, never a SKIP and never a pass (`GW-ZERO-NEW-SKIP`) | §7, §8.2, §13.9 |
| **C9 — fixtures root** | already compliant | `fixtures/mfg/pnp/**` | §10 |
| **C10 — cross-reference drift on the load-bearing gateway rules** | yes | **every** gateway citation in this document now names a stable rule id bound to the rule's content and its refusal code; no `§24.x` or "ruling N" number is cited anywhere | §Citation convention, throughout |
| **No-execute boundary (DoD #12, #15)** | unchanged by every edit above | no device path at any layer; no `RISK_MOTION` member, no `request_mfg_*` tool, no safety-class tool; `monitor`/`transport`/`execute`/`safety_integration` stay `unsupported` **with a `why`**; `device_command_owner` is an explicit `null` with a reason; wrapping a capability row changes what a panel *reads*, never what PnP may *do* | §0, §8.1, §9.2, §13 |
| **Gateway `seq` authority** | unchanged by every edit above | the envelope's `seq` is the journal's stamped `seq`; publishing wrapped adds no second ordering namespace and no domain-side stamping (`GW-SEQ-1`) | §4.1, §4.2 |
