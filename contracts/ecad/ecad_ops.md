# Contract: ECAD Document Ops — v1

> The closed vocabulary of ECAD document mutations. The GUI submits these; **the AI ECAD tools emit
> exactly these**. Mirrors `contracts/doc_ops.md` (envelope, transactionality, id placeholders).
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

## 8. v1.1 schematic ops (additive)

v1 could CREATE a power port, a no-connect, a sheet, a sheet symbol and a sheet pin but could never
edit or delete any of them, and the schema's `buses`/`graphics` arrays had no mutation at all — so
a bus, a note or a drawn box could exist in a document (e.g. from a KiCad import) yet be
uncreatable and undeletable through the ops vocabulary. These close that hole.

| op | fields | notes |
|---|---|---|
| `sch_add_bus` | `name`, `pts`, `sheet?` | id prefix `b`. `name` may be a range (`D[0..7]`) or a group (`{SDA,SCL}`); the netlist expands members |
| `sch_edit_bus` | `id`, patch {`name`,`pts`} | |
| `sch_delete_bus` | `id` | cascades its bus entries |
| `sch_add_bus_entry` | `pos`, `size?`, `bus?`, `member?`, `sheet?` | id prefix `be`; `size` defaults `[2.54, 2.54]`. `member` omitted → resolved from the attached wire's label |
| `sch_delete_bus_entry` | `id` | |
| `sch_add_text` | `pos`, `text`, `size?`, `angle?`, `sheet?` | id prefix `tx` — schematic notes |
| `sch_edit_text` / `sch_delete_text` | `id` (+ patch) | |
| `sch_add_graphic` | `kind`:"line\|rect\|circle\|arc\|polyline", `pts`, `radius?`, `a0?`, `a1?`, `width?`, `filled?`, `sheet?` | id prefix `g` |
| `sch_edit_graphic` / `sch_delete_graphic` | `id` (+ patch) | |
| `sch_edit_power` / `sch_delete_power` | `id` (+ patch {`pos`,`net`,`symbol`}) | |
| `sch_delete_no_connect` | `id` | |
| `sch_edit_junction` | `id`, `pos` | |
| `sch_edit_sheet` / `sch_delete_sheet` | `id` (+ patch {`name`,`size`}) | deleting the last sheet is `invalid_op`; delete cascades the sheet's entities and any sheet symbol instantiating it |
| `sch_edit_sheet_symbol` / `sch_delete_sheet_symbol` | `id` (+ patch {`pos`,`child`,`size`}) | |
| `sch_edit_sheet_pin` / `sch_delete_sheet_pin` | `id`, `pin` (+ patch {`name`,`pos`,`shape`}) | |
| `sch_set_title_block` | patch of {`title`,`company`,`rev`,`date`,`comment1..4`,`paper`} | feeds the plotted sheet frame |

### 8.1 Existing ops whose accepted keys widened

- `sch_add_symbol` / `sch_edit_symbol` additionally accept `in_bom`, `on_board`, `unit`,
  `datasheet`. v1 hard-coded `in_bom`/`on_board` to `true` at add and parsed neither at edit, so
  a DNP-or-not-fitted part could not be expressed. `sch_edit_symbol` also accepts `sheet` (move a
  symbol between sheets) and `libid` (symbol swap; warns `pin_count_changed`).
- `sch_add_wire` / `sch_edit_wire` accept `bus: bool`; v1 hard-coded `false`.
- `sch_import_kicad {path, mode?}` is **implemented** (`mode`: `"merge"` default | `"replace"`).
  In v1 it was registered, dispatched, and did nothing but append a warning.

### 8.2 Hierarchy ops now documented

`sch_add_sheet {parent?, name?, size?}`, `sch_add_sheet_symbol {child, pos, pins?, sheet?}` and
`sch_add_sheet_pin {id, name, pos, shape?}` shipped in v1's registry but were never listed here.
They are part of the frozen vocabulary.
