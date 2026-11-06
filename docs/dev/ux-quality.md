# UX quality: audited behavior and acceptance map

This is a **finite, source-linked inventory**, not a product certification or a second
architecture/contract authority. IDs preserve the numeric/form, command and fillet audit
cases; R1–R19, A1–A5 and G1–G4 retain additional form, availability and geometry cases.
Each row records a failure mode and the required behavioral result. Source links identify
ownership. See
[architecture and route diagrams](../../ARCHITECTURE.md),
[GUI subsystem notes](gui-architecture.md) and the append-only
[contracts](../../contracts/README.md) for the corresponding boundaries.

Record verification per ID using the [evidence protocol](#evidence-protocol), and leave
unexercised cases unverified.

## Shared invariants

- **Inputs and edits:** parse the actual current text, not its previous numeric value;
  reject invalid or non-finite canonical results. Unitless, angular, and length values
  follow their respective semantics in both manual and AI-assisted input. Preserve a
  still-edited or invalid buffer through unit switches. Validate all relevant visible
  fields, but do not use generic positive-number rules for signed datums or invent
  kernel feasibility limits from widget ranges.
- **Authoritative mutation result:** transport success is not feature acceptance. A
  part `doc_apply_ops` rebuild rejection returns the candidate diagnostics while the
  engine restores the old tree, revision, mesh and history. Never ingest rejected
  statuses as the live statuses or treat a rejected add as a created feature. In
  contrast, assembly failures have their own semantics; do not assume they are atomic.
  The mirror uses explicit `rejected_tree` to identify part rejection, not failed rows
  that may describe committed assembly/history or read-only results. Async completion's
  boolean means **current**, not successful: a current error/rejection still needs
  diagnostics, while IDs and mesh require current **and** `ok`. Candidate diagnostics
  remain separate from live statuses. A known unchanged same-base refusal differs from
  transport ambiguity, stale revision, another writer or a superseded request.
- **Document-origin admission:** canonical accepted ingestion invokes the accepted-ops
  observer once before notification/completion, for current async and successful direct
  results. A usable tessellation has a nonempty path, or an explicit empty path with
  explicitly empty bodies to clear the origin's mesh; missing tessellation does not clear it. A superseded
  accepted changed-revision or uncertain mutation sends only an origin/revision hint,
  never its stale payload; a proven same-base rollback or read-only completion does not.
  Coalesced catch-up uses an authoritative warm-cache `doc_activate` read for an existing
  tab/session without a forced solve, revision downgrade, auto-open or selection change.
  Dirty state reflects a genuinely newer fetched revision. Reconcile a matching accepted
  response after tree-only resync without overwriting newer state.
- **Form lifecycle:** live previews are serialized, generation- and document-bound.
  OK flushes the editor, waits for the latest successful applicable result and remains
  open on error; Cancel uses a revision-guarded checkpoint and does not overwrite uncertain
  or foreign changes. An older reply may not modify a later form session or newer text.
  Inactive/cleared PM-owned edit keys are explicitly deleted where required by patch
  semantics; unrelated authored data survives. Local hidden field values can be retained
  for mode switching without serializing them.
  Escape first dismisses a focused nonmodal child popup without canceling the parent
  form or changing draft state; a subsequent Escape may cancel the form.
- **Command availability:** one context-dependent policy supplies the ribbon, split
  faces/flyouts, overflow, S-bar, palette and direct-panel entry points. Hide options
  inapplicable to context; visibly disable in-context actions that cannot work, with a
  useful reason; recheck fresh context at dispatch. Include active tab kind/ID rather
  than a possibly stale mechanical mirror, 2D/3D sketch and immediate selection facts,
  study/physics and known solver capability, ECAD kind/pair, sheet-metal state and CAM
  job/setup/busy/source facts. Do not require a selection for a form or tool that is
  designed to collect its pick later.
- **Performance:** preserve the existing 250 ms preview debounce, one in-flight preview,
  disposable-preview timeout, patient final apply, incremental rebuild and roughly
  20 Hz viewport drag submission ceiling. Assemble command context once per render or
  activation, invalidating document-derived facts on document/revision/tree changes;
  no per-button worker RPC, geometry/selector probe, B-rep property computation or
  feature-tree scan in a frame. Preserve the requested-radius successful fillet fast
  path; perform bounded feasibility work only after failure, never per keystroke on the
  GUI thread. These are design/acceptance constraints, not measurements in this file.
- **Fillet authority:** a successful all-selected-edge result must be sound and cover
  every unique selected edge. Status reports requested and effective canonical-mm radii,
  correction direction, selection/coverage, tolerance and probes. Literal corrections
  belong to the accepted transaction; authored expressions survive with separately
  derived effective values. Tree, status, actual geometry, undo and saved/reopened cache
  must agree. A bounded verified candidate is not proof of a global optimum. Non-finite
  inputs, invalid selectors, unsupported topology, crashes and timeouts are explicit
  failures, not evidence of a radius bound. Chamfer semantics remain separate.

### Source-owner key

| Key | Primary responsibility / source |
|---|---|
| PM | [PropertyManager state, preview and commit](../../host/src/ui/property_manager.cpp), [field definitions](../../host/src/ui/pm_forms.cpp), [layout](../../host/src/ui/layout.cpp) |
| Mirror | [DocMirror response and async ingestion](../../host/src/core/doc_mirror.cpp), [response types](../../host/src/core/doc_mirror.h) |
| Session | [document operations, transaction and persistence](../../core/doc/session.cpp); [op contract](../../contracts/doc_ops.md) |
| Policy | [command context and router](../../host/src/app_commands.cpp), [command metadata and policy](../../host/src/ui/ribbon_def.cpp) |
| Surfaces | [ribbon](../../host/src/ui/widgets/ribbon.cpp), [palette](../../host/src/ui/command_search.cpp), [shortcut bar](../../host/src/ui/shortcut_bar.cpp), [feature tree](../../host/src/ui/feature_tree.cpp), [dispatch](../../host/src/app.cpp) |
| Context | [active-tab state](../../host/src/core/app_state.h), [sketch modes](../../host/src/ui/sketch_mode.cpp), [3D sketch](../../host/src/ui/sketch3d_mode.cpp), [viewport](../../host/src/ui/viewport_panel.cpp) |
| Study/CAM | [simulation manager](../../host/src/ui/sim_manager.cpp), [study picker](../../host/src/ui/sim/sim_manager_tree.cpp), [simulation Run](../../host/src/ui/sim/sim_manager_run.cpp), [CAM panel](../../host/src/ui/cam/cam_panel.cpp) |
| ECAD | [ECAD panel](../../host/src/ui/ecad/ecad_panel.cpp) and [application ECAD dispatch](../../host/src/app.cpp) |
| Compiler | [feature compilation and status](../../core/doc/document.cpp), [selector resolution](../../core/doc/picks.cpp) |
| Validation | [native feature/schema validation](../../core/doc/validate.cpp) |
| Kernel | [fillet geometry](../../core/geom/dress.cpp), [geometry API](../../core/geom/dress.h), [thread geometry](../../core/geom/thread.cpp), [process isolation](../../core/util/process_isolation.cpp) |
| Cache | [session cache/open/save](../../core/doc/session.cpp), [container and cache key](../../core/doc/stepplus.cpp), [container contract](../../contracts/stepplus.md) |
| Consumers | [C ABI result](../../core/api/libchudware.cpp), [agent document tools](../../services/mcp/cad_doc_tools.py), PM and Mirror |

## Numeric and form audit

The finite N IDs correspond to the original 15 source findings plus the independent
unit-change state-loss case. Relevant engine constraints are authoritative, not implied
by this table's examples.

| ID | Audited defect | Owner | Observable acceptance |
|---|---|---|---|
| N1 | Chrome OK can precede parsing the focused editor. | PM | Type a valid new value and click OK without Enter: that value is retained; malformed current text leaves the form open with an error. |
| N2 | OK closes on a queued/in-flight preview or Thread final rebuild. | PM, Mirror | Delayed success, rejection and transport failure close only after the latest successful accepted result; Thread waits for the full-quality result. |
| N3 | In-band compile rejection is ingested as an applied preview, including rejected adds or downstream failures. | Mirror, PM, Session | Rejected add, edit and downstream failure show actual candidate diagnostics, create no ghost target/success state, and preserve live tree, statuses, revision and undo. |
| N4 | A known no-mutation rejection makes safe Cancel unavailable. | Mirror, PM | Valid preview → same-base atomic refusal → Cancel restores the checkpoint; transport uncertainty or a foreign revision never reverts another writer. |
| N5 | The preview claims it is current while pending or failed. | PM | Pending, invalid, failed and latest-successful generations display distinct truthful states; only a current successful generation is up to date. |
| N6 | An old preview callback can change a newly opened form. | PM, Mirror | Hold an old response, close/reopen another form, release it: no target, field, checkpoint or in-flight state of the newer session changes. |
| N7 | Readiness returns on a well or first positive dimension without validating the remaining visible fields. | PM | A malformed first or later dimension blocks acceptance for both primitive and selector forms; correction or hiding the irrelevant field restores readiness. |
| N8 | Generic positivity blocks valid zero/signed datum and no-dimension forms. | PM, Compiler | Zero and negative offset Plane, midplane/three-point Plane, coordinate system and default full-length Thread can preview with required references; missing references remain actionable. |
| N9 | Bare `=` silently retains the old value; entry paths disagree on documented bounds. | PM, Compiler | Reject bare `=` and malformed equations visibly; allow valid equations and signed offsets; K-factor limits behave consistently or surface the actual engine refusal. |
| N10 | Explicit `mm` in IPS is treated as inches; finiteness is checked before conversion. | PM parser and consumers | In IPS, `1mm` = 1 mm, `(1+1)mm` = 2 mm and `1/2in` = 12.7 mm; post-conversion overflow is rejected without changing the previous value. Preserve bare IPS and angle behavior. |
| N11 | AI unitless suggestions are IPS-scaled like lengths. | PM | Equivalent manual and AI patches to a K-factor, angle and length have the same canonical meaning in both unit systems. |
| N12 | Hidden/cleared keys are omitted from an `edit_feature` patch and remain persisted. | PM, Session | Custom Hole → standard, standard Thread → custom, and clearing optional text/wells delete only inactive/cleared PM-owned keys; reopen and geometry agree, unrelated params remain. |
| N13 | Thread's proxy-only preview flag survives the final patch. | PM, Session, Compiler | The final accepted Thread has no proxy flag and actual full-quality geometry, not merely a completed preview. |
| N14 | Nested visibility and blend readiness use hidden retained control/pick state. | PM | Extrude draft visibility follows its visible parent transitively; chamfer edge/vertex modes require the currently visible well, not an old hidden pick. |
| N15 | Pattern “Instances to skip” control has no compiler effect. | PM, Compiler | Unsupported skip controls are not offered unless real geometry semantics are implemented; ordinary patterns work and existing unrelated feature data is not erased. |
| N16 | Unit switching reformats away invalid or uncommitted dimension text. | PM | Switch units with an invalid buffer and with an in-progress valid buffer: the edit is preserved or explicitly resolved, never silently replaced by the prior value. |

## Command availability audit

For each row, check presentation **and activation** on every applicable surface, including
palette click/Enter, ribbon face/flyout/overflow and direct-panel paths. Disabled means
visibly unavailable, activation refused and a reason accessible, including narrow layouts.

| ID | Audited defect | Owner | Observable acceptance |
|---|---|---|---|
| C1 | Inactive 2D/3D sketch tools and exits remain callable. | Policy, Context, Surfaces | Mode-specific tools/exit are hidden or disabled outside their mode; New Sketch remains usable on an active part and collects a plane later. |
| C2 | New Sketch is enabled during a 2D sketch although entry refuses it. | Policy, Surfaces | New Sketch is disabled with a finish/cancel reason while the current sketch is active; Finish and Cancel remain usable. |
| C3 | S-bar presents modeling shortcuts during a 3D sketch. | Context, Surfaces | Explicit none/2D/3D context gives appropriate 3D actions or suppresses the inapplicable model bar; exit paths still work. |
| C4 | Immediate fix/construction/delete actions are enabled without applicable sketch selections. | Context, Policy | Gate only the immediate 2D/3D selection actions on cached relevant picks; tools that arm for a later pick stay usable. |
| C5 | Drawing actions run without an active drawing and scale actions without a view. | Policy, Context | Drawing commands require an active drawing ID; scale requires a selected view; valid drawing annotation tools can arm later picks. |
| C6 | Drawing advertises a Sketch tab that cannot remain selected. | Surfaces | Unsupported Sketch tab is absent in Drawing, while View Layout and Annotation remain usable. |
| C7 | Assembly operations appear enabled on a part or no document. | Policy, Context | Insert, Move, Mate, Pattern, Explode, Interference, BOM and Motion require the active assembly; Parts Library and later-pick forms remain available in their intended context. |
| C8 | Simulation Reference actions are enabled but central lockout rejects them. | Policy, Surfaces | Reference plane/axis/point/coordinate system are disabled consistently with the leave-Simulation reason. |
| C9 | Typed study setup commands lack the required study. | Study/CAM, Policy | No matching type yields the appropriate reason; an existing matching study is usable even when not currently selected if the handler can switch to it. |
| C10 | Structural force/pressure are enabled on nonstructural topology physics. | Study/CAM, Policy | Thermal/flow topology offers the appropriate Heat/Flow boundaries, not active structural Force/Pressure actions. |
| C11 | Study picker and split creation rows disagree about active assembly, solid domain and solver capability. | Study/CAM, Policy, Surfaces | Motion requires the active assembly, topology the supported active solid; known missing Contact/Vibration capabilities agree across choices. Editable EM setup remains usable even when EM Run is unavailable. |
| C12 | ECAD actions are enabled for the wrong schematic/PCB kind or missing pair. | ECAD, Policy, Context | Exact schematic- and board-only actions follow the active kind; Update Board requires the needed pair; New, Open and AI Design remain available. |
| C13 | CAM commands ignore cached no-job/no-setup/busy/source conditions. | Study/CAM, Policy | Generate/Simulate/Post require a job, setup-dependent operations a setup, and busy/source restrictions are consistent; creation actions remain available and Simulate/Post can generate when designed to. |
| C14 | Sheet-metal Flatten and Export Flat are enabled without a valid source state. | Policy, Context, Compiler | Flatten requires sheet metal with bends; Export requires a valid flat; base flange and forms that collect later picks remain usable. |
| C15 | Ribbon-only disabled states are bypassed by palette, S-bar and panel dispatch. | Policy, Surfaces, ECAD, Study/CAM | All relevant entry points share the same availability and recheck at activation; no disabled action executes via click, Enter or a direct panel. |
| C16 | Overflow omits the disabled reason. | Surfaces | Disabled normal, flyout and overflow rows expose the same actionable reason by their available hover/focus affordance. |
| C17 | Extrude offers end conditions the compiler does not implement. | PM, Compiler | Direction one cannot offer unsupported `up_to_next`/`up_to_body`; direction two cannot offer `up_to_next`, `up_to_surface`, `offset_from_surface`, `up_to_body`, `up_to_vertex`, `up_to_reference` or `offset_from_reference` unless actually implemented. Hide/disable with reason; no silent remapping. |
| C18 | Surface sweep/loft expose guides or centerline that their compiler ignores. | PM, Compiler | Unimplemented `surf_sweep.guides`, `surf_loft.guides` and `surf_loft.centerline` cannot be misleadingly applied; supported solid sweep single-guide behavior remains. |

## Fillet audit

These IDs are the native geometry audit; overlapping PM/transport conditions are linked to
N IDs instead of treating a UI fix as geometry proof. Constant and variable endpoint modes
are in scope, including implicit direction selection and shared-edge contact joins.

The structured successful status is `data.fillet = {requested, effective,
adjustment, selected_edges, covered_edges, tolerance_mm, probes}`, with both `radius` and
optional `radius2` in canonical mm. The accepted feature may use `data.fillet_effective`
for a derived expression result; it is refreshed or removed on rebuild, never treated as
authored input. The typed result and persisted tree—not a warning string—are authoritative.
These are acceptance semantics, not evidence that every consumer has been exercised.

| ID | Audited defect | Owner | Observable acceptance |
|---|---|---|---|
| F1 | Tiny positive radius is committed as unchanged geometry. | Compiler, Kernel | A finite tiny request produces a verified usable lower correction on every selected edge or an explicit failure; no successful no-op. |
| F2 | Failed all-edge build falls back to a partial subset. | Kernel, Compiler | Mixed easy/tight edges, adjacent corners and all-edge selection either produce one sound result covering every requested edge or fail atomically; no partial success. |
| F3 | Seams, degenerates or contact remapping silently lose selected edges. | Kernel, Compiler | Every unique explicit pick is covered/accounted for; unsupported, filtered or unmappable picks fail explicitly, including mixed contact/ordinary selections. |
| F4 | Dangling and resolved selections can be committed partially. | Compiler, Session | One invalid selector among valid picks rejects the whole edit with actionable diagnostics; tree, revision, geometry and undo remain unchanged. |
| F5 | Variable endpoint zero/negative or close unequal radii silently take the constant path. | Compiler, Kernel | Preserve both endpoint intents along the OCCT contour without asserting linear interpolation. Each effective endpoint must strictly exceed its applicable source/generated local tolerance bound; lower correction retains a resolvable opposite endpoint when feasible. Positive unequal requests within OCCT precision fail immediately only when both already exceed their source bounds; otherwise correct or fail without collapsing the unequal law. Variable contact joins and incompatible shared-vertex demands fail explicitly. The guard is not independent physical radius metrology; `tolerance_mm` is search resolution. See [endpoint resolution v3](../../contracts/feature_params.md#fillet-endpoint-resolution-v3-append-only). |
| F6 | There is no geometry-aware verified lower/upper correction. | Kernel, Compiler, [process isolation](../../core/util/process_isolation.cpp) | Requested radius is tried first; bounded work after failure yields an actual all-edge feasible candidate with reported tested tolerance, or an honest explicit failure. Crash/timeout are typed failures, not bounds; preserve platform and large-shape support, and do not claim a global mathematical optimum. |
| F7 | Correcting only geometry leaves persisted params/expressions and later rebuilds inconsistent. | Compiler, Session, Cache | Literal correction is in the accepted undoable transaction; expressions retain authored request and separately derived effective state. Upstream edits/rebuild, undo/redo and save/open agree with status and geometry. |
| F8 | Invalid fillet text can accept the previous preview. | PM | N1, N7 and N10 hold with a populated fillet well, in MMGS/IPS and for expressions; no stale value is accepted. |
| F9 | PM closes before a later fillet preview result. | PM, Mirror | N2 and N6 hold for corrected, rejected, superseded and delayed fillet previews; returned effective data never overwrites newer typing. |
| F10 | Post-conversion non-finite values can reach the native fillet. | PM, Compiler, Kernel | Finite checks run after unit conversion and at the native boundary; NaN/infinity/overflow fail explicitly, with prior state intact. |
| F11 | Query/selector overlap is not deduplicated (`IndexedMap::Add` returns an index). | Compiler | Overlapping query and selector lists identify unique selected edges once, with matching selection and coverage counts. |
| F12 | Structured status is dropped by non-PM consumers. | Consumers, Compiler | Successful and adjusted result data is returned to the C ABI, agent document tool and GUI without parsing warning prose; unadjusted success carries authoritative data too. |
| F13 | Separate legacy `run_model` build123d fillet retries arbitrary exceptions by halving with a fixed floor. | [Legacy worker](../../services/kernel_exec.py), [legacy regression owner](../../services/test_kernel_exec_aliases.py) | For 3D solids retain an already-valid requested radius, otherwise use only a bounded, detached all-selected-edge search that reports actual effective radius/tolerance and applies the accepted result once. Empty/foreign/seam/contact selections, non-finite input and unclassifiable errors fail explicitly. Preserve aliases, native 1D/2D behavior and chamfer; no global-optimum claim. Native fillet changes alone do not validate this consumer. |
| F14 | Agent fillet guidance incorrectly excludes a direction query for variable mode; shared operations guidance also describes an unsupported linear taper. | [Fillet guidance](../../services/agent/prompts/shared/cheatsheets/fillet.md), [operations guidance](../../services/agent/prompts/shared/ops_cheatsheet.md), Compiler | Source guidance reflects the supported query path, OCCT contour endpoint semantics and authoritative correction/failure result. Regenerate dependent manuals rather than hand-editing them; frozen historical contract text remains superseded by the append-only amendment. |
| F15 | A blend-well click made while any engine call is queued (even the open form's 1 s `edit_hold` renewal) is dropped with an "engine is busy" toast. | Surfaces, PM | The pick stays pending and routes once the queue is idle; a revision change in between rejects it with "select it again" instead of resolving stale topology ids. |
| F16 | A pick the well does not hold (toggled off, wrong kind, unresolvable) stays highlighted as selected beside an empty well. | Surfaces, PM | The active well owns its picks' highlight; consumed picks leave the global selection, so an empty well shows no selected edge and OK's "select the required references" message matches the view. |
| F17 | In an edge-only well a corner vertex within the snap radius steals a click on its edge, which the well then rejects. | Context, [ID pass](../../host/src/render/id_pass.cpp) | Wells that reject vertices pick the nearest edge; a click near a short edge's end adds that edge. |

Fillet acceptance also needs sound closed B-rep and full existing geometry validation,
operation/owner provenance, constant and variable endpoint coverage, supported contact
joins, unsupported topology, injected crash/timeout and a usable later operation. Verify
that cache epoch changes invalidate obsolete geometry; after an appropriate successful
build/save, reopening should reuse valid state according to the container contract. Cover
unverified provenance/background validation separately rather than calling all cache hits
trusted. A single oversized constant-radius smoke is not evidence for all those cases.

## Subsequent review findings

Owners use the source-owner key above. R13 is an assertion gap, not a reported runtime failure.

| ID | Finding | Owner | Behavioral acceptance |
|---|---|---|---|
| R1 | Failed statuses are classified as rejection even when an assembly committed or a read-only copy succeeded. | Mirror, Session | Distinguish an explicit part candidate rejection from committed assembly and read-only results; preserve returned IDs, copy bundle and revision. |
| R2 | Hidden-field deletion can erase an existing inward extrude draft. | PM, Compiler | Edit depth on an extrusion with nonzero draft and `draft_outward:false`; its inward taper and authored parameters remain unless explicitly changed. |
| R3 | Legacy fillets can hydrate incorrectly and lose the second radius or its expression. | PM, Compiler | Infer variable mode for an absent subtype with a nonzero unequal second radius. Preserve an authored second-endpoint expression across unrelated edits even when its current evaluated value is equal and the legacy path is constant. |
| R4 | A literal preserved across a unit toggle is relabelled without conversion after blur. | PM | Enter bare `2` mm, switch to IPS while editing, blur and reopen: the displayed value still represents 2 mm and cannot be reinterpreted as 2 inches. |
| R5 | Tree-only stale resync can overtake a committed async result and leave an older mesh. | Mirror | Reconcile or reload the mesh for a proven matching committed revision without accepting a stale result over newer work. |
| R6 | A fresh part loses its identity, so a known unchanged refusal disables Cancel. | Mirror, PM | On a new empty part, rejected expression → valid Box preview → Cancel restores the empty checkpoint; uncertain/foreign changes remain protected. |
| R7 | Sketch cancel ownership is lost after an atomic rejected edit. | Context, Mirror | In both 2D and 3D, a known same-base rejection preserves the ability to cancel earlier session edits; stale or foreign changes cannot be reverted. |
| R8 | Feature-tree, equation and Flatten callbacks drop current rejection diagnostics before reporting them. | Mirror, PM, Surfaces | Surface the failed candidate's actual diagnostics with the live tree/mesh intact; discard only stale or superseded callbacks. |
| R9 | Through-all surface extrude hides depth although the native schema requires it. | PM, Compiler, Validation | Creating and editing through-all surface extrudes uses a schema-valid supported operation; verify the actual geometry and retained data. |
| R10 | Escape from a temporary scalar editor can parse and commit canceled text. | PM | Real Ctrl-click → type → Escape restores the pre-edit value and buffer without submitting the canceled value. |
| R11 | Thread auto-sizing changes the selected size without clearing an unsupported-combo flag. | PM | Open an unsupported M14 entry, pick a face auto-sized to supported M12, and confirm the valid new size is displayed and accepted. |
| R12 | Reopened equation-bound fillets hydrate the request but not derived effective radii. | PM, Session | Reopen a corrected expression-bound fillet: display the authoritative effective value while preserving the expression; unbinding retains the effective value. |
| R13 | Final-thread regression can accept an unthreaded fallback as success. | [GUI scenarios](../../host/src/ui/scenarios.cpp), Compiler | Compare against the unthreaded baseline and require real material removal plus no fallback warning, not only a difference from the preview proxy. |
| R14 | An accepted part mutation can refresh the active viewport while a different schematic is selected and leave the originating part's parked cache stale. | Mirror, Context, Session | When A's revision 2 arrives with B active, keep B selected and its viewport untouched; ingest A's accepted state into A's parked session. Catch-up must remain origin-safe and warm, mark dirty only for a genuinely newer revision, and never force a solve or open a tab. |
| R15 | Escape from a PropertyManager combo cancels its parent feature editor; ribbon-owned popups can also ignore Escape when global keyboard navigation is disabled. | PM, Surfaces | The first real Escape closes the focused nonmodal combo, split or overflow popup, keeps the underlying form and draft intact, and does not dismiss unrelated or modal popups; explicit Cancel or a later unclaimed Escape restores the owned checkpoint. |
| R16 | A running camera tween can overwrite manual 3D-sketch wheel zoom. | Context | Manual wheel input cancels the tween; subsequent frames retain the user's zoom and real world-point picking remains consistent. |
| R17 | The viewport's hit window intercepts presses on the visible sketch shortcut bar. | Surfaces | The real Model, 2D and 3D shortcut buttons receive their own press and release, obey the common availability policy and perform the intended transition without viewport click-through. |
| R18 | Latched OK displays “Applying” after its request drains even though newer text is invalid. | PM | A real queued acceptance followed by malformed live text stays open, preserves the invalid draft and displays the editing/error state rather than an in-flight or successful result. Correcting the latest text can complete acceptance. |
| R19 | A long selection-well caption overlaps its active viewport-pick hint. | PM | Shared feature and assembly selection wells wrap long captions and move the hint to a separate row when necessary; short captions keep the compact inline layout and the actual well remains clickable. |
| A1 | Async study-list adoption preserves an index and can select another study. | Study/CAM | Remap the current selected study by stable ID when a list changes; selecting B in `[A,B,C]` then deleting A must keep B selected. |
| A2 | Unknown/stale study facts are treated as confirmed absence, especially with the manager pane hidden. | Study/CAM, Policy | Track freshness and refresh asynchronously on activation/invalidation independently of pane visibility; do not assert a missing study from unknown facts. |
| A3 | Deferred ECAD Simulate can act on a newly active schematic instead of its origin. | ECAD, Surfaces | Bind or revalidate the originating document through checked dispatch so a tab switch cannot simulate a different schematic. |
| A4 | Motion consumers use raw tab kind after the picker uses schema-aware assembly identity. | Study/CAM, Policy | Motion creation, setup and Run agree for an active assembly even if its legacy tab kind is `Part`; an actual part remains ineligible. |
| A5 | Study creation remains enabled while the study-list request makes interaction busy. | Study/CAM, Policy | Disable with an appropriate pending reason or queue creation; never silently refuse or open an existing study instead. |
| G1 | A document validity-budget timeout can be reduced to ordinary invalid geometry and searched as a radius bound. | Compiler, Kernel | Propagate the typed timeout through the fillet validator and stop searching without diagnosing it as an oversized radius. A previously validated all-edge candidate remains usable; without one the timeout is a failure. |
| G2 | Lower endpoint search can alternate across a precision bound while changing the valid opposite endpoint. | Kernel | Bracket and probe the same endpoint interval with the resolvable opposite endpoint fixed; preserve unequal intent and the bounded-search budget without claiming monotonicity. |
| G3 | A failed helical Boolean returns the unthreaded body as success, or a valid partial cut omits requested thread sections. | Compiler, Kernel | Final cut and raised threads produce valid geometry across the requested run, handedness and starts, or refuse atomically. Explicit cosmetic mode and provisional preview geometry remain distinct. Verify actual material change and section-boundary classifications, not merely a successful transport or a difference from the proxy. |
| G4 | Forking a multithreaded host can strand fillet children on inherited OCCT/allocator locks, and legacy fork retirement can disable later strict containment. | Kernel | Concurrent real fillets use a fresh verified executable with the original selections and validation policy. Crashes fail and timeouts stop the search; a timeout after a verified candidate retains that candidate. Missing or invalid helpers never cause an in-process fallback, and a later healthy operation still works. See the [worker contract](../../contracts/shape_build_worker.md). |

## Evidence protocol

For **each ID and applicable surface**, attach a reproducible record rather than changing
this inventory to a blanket PASS:

1. **Revision and environment:** commit or checkout identifier (including relevant
   uncommitted changes), build/runtime configuration, platform, unit system, document
   kind and command surface. State whether evidence is static inspection, automated
   regression, isolated runtime interaction or measured performance.
2. **Input and procedure:** exact command/test target or manual sequence, test data and
   private workspace/daemon configuration. Identify whether the request was accepted,
   atomically rejected, stale, superseded or transport-uncertain.
3. **Observed result and oracle:** exit/result status, user-visible state, authoritative
   tree/revision/feature status, actual geometry or solver/result data when relevant,
   undo/save/reopen outcome and artifact/report location. Distinguish candidate diagnostics
   from restored live state; capture both requested and effective fillet values.
4. **Coverage and limits:** exercised IDs and cases, skipped cases, observed timing/probe
   counts when measured, backend availability and failures. Say **unverified** when not
   exercised; report a failure rather than treating its missing result as a pass.

The minimum record fields are:

| Field | Meaning |
|---|---|
| `ids`, `surface`, `scenario` | Exact inventory IDs and exercised UI/RPC/context combination |
| `status` | `unverified`, `passed`, `failed` or `skipped`; include the reason for a skip |
| `revision`, `environment` | Checkout/build, platform, relevant configuration and isolated workspace/daemon |
| `procedure`, `inputs` | Invocation or manual steps and reproducible test data |
| `observed`, `oracle` | Actual outcome and how correctness was established, including authoritative state and geometry when relevant |
| `artifacts`, `limits` | Result/report locations, coverage exclusions, unavailable backends and any measured timings |

Use real consumer-visible and boundary/state regression assertions; do not substitute
source-text checks, mock echoes or screenshot existence for behavior. Isolate HOME,
APPDATA, XDG paths, workspace, session store and daemon from user sessions. Ordinary
native form/fillet investigation may use `CHUDWARE_ENGINE_DAEMON=0`; cross-client
Cancel/foreign-writer and daemon fanout require a **private isolated daemon**, not an
active user's daemon. `CHUDWARE_SHOT=pmform` establishes layout/visibility only; it
cannot establish rebuild, final acceptance or rollback. For representative parent-owned
runtime paths, exercise immediate OK valid/invalid Box, signed/three-point Plane,
Thread final geometry, Hole custom→standard, delayed rejection/Cancel, all command
surfaces across sketch/drawing/assembly/study/ECAD/CAM contexts, and literal/expression
fillet save/open with all-edge coverage.

## Product-readiness limits

This map records a bounded audit of the named numeric, command and native-fillet paths;
it is not exhaustive over every form, geometry type, simulation, operating system,
transport, browser, optional solver or printer. Source findings and the required behavior
are not measurements of frame time, interactive latency, geometric optimum, solver
accuracy or release readiness. The browser deployment's documented worker/Copilot and
parity limits remain in the [browser guide](../../web/README.md); simulation accreditation
and optimizer claims remain evidence-gated by their own
[programs](../simulation/ACCREDITATION_PROGRAM.md) and
[optimizer program](../simulation/OPTIMIZER_PROGRAM.md). No result in this file asserts
that those programs or the product as a whole have passed.
