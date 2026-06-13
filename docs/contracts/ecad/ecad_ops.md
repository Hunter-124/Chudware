# Contract: ECAD Document Ops — v1

> The closed vocabulary of ECAD document mutations. The GUI submits these; **the AI ECAD tools emit
> exactly these**. Mirrors `docs/contracts/doc_ops.md` (envelope, transactionality, id placeholders).
> Design once, freeze here; changes require a version bump.

## 1. Envelope

Request (`ecad_apply_ops`):
```json
{"doc_id":"…","base_revision":17,"ops":[Op, …]}
```
Response (also from `ecad_undo`/`ecad_redo`/`ecad_rebuild`):
```json
{
  "revision": 18,
  "id_map": {"$0":"s9","$1":"w12"},
  "kind": "sch|pcb",
  "tree": { …full document JSON… },
  "compile": {
    "netlist": {…} | null,                  // sch docs: built netlist (see netlist.md)
    "erc": [Violation, …],                   // sch docs
    "drc": [Violation, …],                   // pcb docs
    "ratsnest": [{"net":"GND","p1":[x,y],"p2":[x,y]}, …],  // pcb docs: unrouted airwires
    "stats": {"nets": 7, "unrouted": 3, "ms": 4.1}
  },
  "render": {"layers": {...} | null}         // optional 2D render hints for the canvas
}
```

## 2. Transactionality (normative — identical discipline to doc_ops §2)

1. **Validation atomic**: every op schema- and referentially-validated first; any failure rejects
   the whole batch `invalid_op {index, reason}`, no state change.
2. **Application atomic**: all ops apply; revision +1 per batch.
3. **Compile (netlist/ERC/DRC) failures do NOT reject ops** — they surface in `compile`. A board
   with DRC violations is still a valid edited document.
4. Undo/redo operate on whole batches (bounded snapshot stack in `session.py`).

## 3. Id placeholders

Ops creating objects may be referenced later **in the same batch** as `"$<op-index>"` (e.g. add a
symbol in op 0, wire to it with `"from":{"sym":"$0","pin":"1"}` in op 1). Same semantics as doc_ops §3.

## 4. Schematic ops (`kind == sch`)

| op | fields | notes |
|---|---|---|
| `sch_add_symbol` | `libid`, `pos`, `angle?`, `mirror?`, `ref?`, `value?`, `footprint?`, `sheet?` | placeholder-eligible (→ symbol id). Auto-assigns ref if omitted (next free `<prefix><n>`) |
| `sch_edit_symbol` | `id`, patch of {`pos`,`angle`,`mirror`,`ref`,`value`,`footprint`,`fields`,`dnp`,`sim_params`} | present keys replace; `null` clears optional |
| `sch_delete_symbol` | `id` | cascades pins; wires left dangling (re-netlisted) |
| `sch_add_wire` | `pts`, `sheet?` | orthogonalized; merges colinear/abutting segments |
| `sch_edit_wire` | `id`, `pts` | |
| `sch_delete_wire` | `id` | |
| `sch_add_junction` | `pos`, `sheet?` | |
| `sch_delete_junction` | `id` | |
| `sch_add_label` | `pos`, `text`, `scope?`, `angle?`, `sheet?` | |
| `sch_edit_label` | `id`, patch {`pos`,`text`,`scope`,`angle`} | |
| `sch_delete_label` | `id` | |
| `sch_add_power` | `pos`, `net`, `symbol?`, `sheet?` | |
| `sch_add_no_connect` | `pos`, `sheet?` | |
| `sch_connect` | `from`:{`sym`,`pin`}, `to`:{`sym`,`pin`} | **convenience**: auto-routes an orthogonal wire pin→pin (uses pin world coords); placeholder-eligible refs |
| `sch_annotate` | `scope`:"all|sheet", `mode`:"keep|reset" | assign ref-des to all `?` symbols deterministically |
| `sch_set_grid` | `grid` | mm |
| `sch_import_kicad` | `path` | replace/merge a `.kicad_sch` (see formats); returns id_map of created objects |

## 5. PCB ops (`kind == pcb`)

| op | fields | notes |
|---|---|---|
| `pcb_set_stackup` | `copper_layers`, `thickness`, `layers?` | |
| `pcb_set_outline` | `shapes`, `closed?` | validates closed loop |
| `pcb_add_footprint` | `fpid`, `pos`, `angle?`, `side?`, `ref?`, `value?`, `linked_symbol?` | placeholder-eligible; pads inherit nets from linked symbol via netlist |
| `pcb_edit_footprint` | `id`, patch {`pos`,`angle`,`side`,`ref`,`value`} | |
| `pcb_delete_footprint` | `id` | |
| `pcb_add_track` | `net?`, `layer`, `width?`, `kind?`, `p1`, `p2`, (`center`,`a0`,`a1` for arc) | width defaults to net-class |
| `pcb_add_tracks` | `net?`, `layer`, `width?`, `pts:[[x,y],…]` | polyline → segments (route convenience) |
| `pcb_delete_track` | `id` | |
| `pcb_add_via` | `net?`, `at`, `drill?`, `diameter?`, `layers?`, `type?` | |
| `pcb_delete_via` | `id` | |
| `pcb_add_zone` | `net`, `layer`, `polygon`, `clearance?`, `thermal?` | fill computed by compile |
| `pcb_edit_zone` | `id`, patch | re-fills |
| `pcb_delete_zone` | `id` | |
| `pcb_set_rules` | patch of DesignRules / `net_classes` | |
| `pcb_assign_net` | `pad`:{`fp`,`pad`}, `net` | manual override |
| `pcb_update_from_sch` | `sch_doc_id` | **the back-annotation op**: pull components/nets/footprints from a linked schematic's netlist; add missing footprints, update refs/values, rebind pad nets, report removed parts. Idempotent. As a single-doc op it only records `linked_sch` (it cannot reach the schematic doc); the real netlist-driven reconciliation is the cross-document RPC `ecad_update_from_sch` `{board_doc_id, sch_doc_id, prune?, place_origin?}` → board ops-response + `report{added,updated,removed,unresolved,nets_bound}` (engine.ecad.sync.update_board_from_sch). |
| `pcb_autoplace` | `area?`, `strategy?` | best-effort initial placement of unplaced footprints (grid/connectivity heuristic) |

## 6. Shared ops (both kinds)

| op | fields |
|---|---|
| `rename_doc` | `name` |
| `set_field` | `target`:{...}, `key`, `value` (arbitrary user field) |
| `nop` | (used for transactional grouping/tests) |

## 7. Error codes

`invalid_op` (with `{index, reason}`), `unknown_doc`, `unknown_object`, `bad_ref`,
`outline_not_closed`, `layer_not_in_stackup`, `net_not_found`, `stale_revision` (base_revision
mismatch → caller resyncs), `duplicate_ref`. Mirrors doc_ops error envelope shape.
